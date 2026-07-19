"""Image download, size validation (>=500px longest side), eBay Media API re-hosting
(spec §6.5). Implemented in Phase 4.

Downloads are sequential (politeness cap: one live request at a time). Dimensions are
read straight from the file headers (PNG/GIF/JPEG/WebP) — no imaging dependency.
Listings only ever reference the re-hosted eBay URLs, never supplier CDNs.
"""

import logging
import struct

import httpx

from listflow.ebay.client import EbayApiError, EbayClient
from listflow.models import ImageAsset, Product

logger = logging.getLogger(__name__)

MIN_LONGEST_SIDE = 500  # eBay requirement
MEDIA_PATH = "/commerce/media/v1_beta/image"


class ImageError(RuntimeError):
    """No usable images, or the Media API upload did not yield an eBay URL."""


def image_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) parsed from the file header, or None if unrecognised."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
        return width, height
    if data[:6] in (b"GIF87a", b"GIF89a"):
        width, height = struct.unpack("<HH", data[6:10])
        return width, height
    if data[:2] == b"\xff\xd8":  # JPEG: walk segment markers to a SOFn frame header
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:  # no length field
                i += 2
                continue
            length = struct.unpack(">H", data[i + 2 : i + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                height, width = struct.unpack(">HH", data[i + 5 : i + 9])
                return width, height
            i += 2 + length
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        # WebP has three chunk layouts; AliExpress serves VP8 (lossy) as .jpg URLs.
        fourcc = data[12:16]
        if fourcc == b"VP8X":  # extended: 24-bit canvas width-1 / height-1
            width = int.from_bytes(data[24:27], "little") + 1
            height = int.from_bytes(data[27:30], "little") + 1
            return width, height
        if fourcc == b"VP8 ":  # lossy: 14-bit dims after the 0x9d012a start code
            if data[23:26] == b"\x9d\x01\x2a":
                width = (data[26] | (data[27] << 8)) & 0x3FFF
                height = (data[28] | (data[29] << 8)) & 0x3FFF
                return width, height
        elif fourcc == b"VP8L":  # lossless: 14+14 bits packed after the 0x2f signature
            bits = int.from_bytes(data[21:25], "little")
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
            return width, height
    return None


def fetch_images(
    product: Product, http: httpx.Client | None = None
) -> list[tuple[ImageAsset, bytes]]:
    """Download source images one at a time; keep only >=500px; error if none usable."""
    client = http or httpx.Client(timeout=30, follow_redirects=True)
    usable: list[tuple[ImageAsset, bytes]] = []
    for asset in product.images:
        url = str(asset.source_url)
        try:
            response = client.get(url)
        except httpx.HTTPError as exc:
            logger.warning("image download failed (%s): %s", url, exc)
            continue
        if response.status_code != 200:
            logger.warning("image download HTTP %d: %s", response.status_code, url)
            continue
        size = image_size(response.content)
        if size is None:
            logger.warning("unrecognised image format, skipping: %s", url)
            continue
        width, height = size
        if max(width, height) < MIN_LONGEST_SIDE:
            logger.warning("image below %dpx (%dx%d), skipping: %s",
                           MIN_LONGEST_SIDE, width, height, url)
            continue
        asset.width, asset.height = width, height
        usable.append((asset, response.content))
    if not usable:
        raise ImageError(
            f"no usable images — eBay needs at least one image >={MIN_LONGEST_SIDE}px "
            "on its longest side"
        )
    return usable


def upload_images(client: EbayClient, assets: list[tuple[ImageAsset, bytes]]) -> None:
    """Best-effort re-host via the eBay Media API, filling asset.ebay_url.

    The Media image endpoint is not available in eBay's sandbox (404), and can be
    flaky in production. On any failure we leave ebay_url unset and fall back to the
    source URL — eBay downloads and re-hosts imageUrls to its own EPS servers when the
    offer is published, so the live listing still never hotlinks the supplier CDN.
    """
    for asset, data in assets:
        try:
            response = client.post(
                client.media_base_url + MEDIA_PATH,
                files={"image": ("image", data, "application/octet-stream")},
            )
        except EbayApiError as exc:
            logger.warning(
                "eBay Media API unavailable (%s) — passing the source image URL instead; "
                "eBay re-hosts imageUrls to its own servers at publish",
                exc,
            )
            continue
        ebay_url = _extract_image_url(client, response)
        if ebay_url:
            asset.ebay_url = ebay_url
            logger.info("image re-hosted on eBay: %s", ebay_url)
        else:
            logger.warning("Media API returned no imageUrl — using the source URL for this image")


def listing_image_urls(assets: list[tuple[ImageAsset, bytes]]) -> list[str]:
    """URLs to put in the listing: the eBay-hosted URL when we got one, else the
    validated source URL (which eBay re-hosts at publish)."""
    return [str(asset.ebay_url or asset.source_url) for asset, _ in assets]


def _extract_image_url(client: EbayClient, response: httpx.Response) -> str | None:
    try:
        body = response.json()
        if isinstance(body, dict) and body.get("imageUrl"):
            return body["imageUrl"]
    except ValueError:
        pass
    location = response.headers.get("Location", "")
    if location:
        if not location.startswith("http"):
            location = client.media_base_url + location
        follow_up = client.get(location)
        try:
            return follow_up.json().get("imageUrl")
        except ValueError:
            return None
    return None
