"""Image pipeline tests: header-only size parsing (no Pillow), sequential download
with >=500px validation, Media API upload filling ebay_url. All respx-mocked.
Phase 4 — written before images.py logic.
"""

import struct
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import respx

from listflow.config import Settings
from listflow.ebay.client import EbayClient
from listflow.images import (
    MIN_LONGEST_SIDE,
    ImageError,
    fetch_images,
    image_size,
    listing_image_urls,
    upload_images,
)
from listflow.models import Product

MEDIA = "https://apim.sandbox.ebay.com/commerce/media/v1_beta/image"


def png_bytes(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + bytes(5)
    )


def gif_bytes(width: int, height: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + bytes(10)


def jpeg_bytes(width: int, height: int) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + bytes(9)
    sof0 = b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", height, width)
    return b"\xff\xd8" + app0 + sof0 + b"\x03" + bytes(9) + b"\xff\xd9"


def _webp(body: bytes) -> bytes:
    return b"RIFF" + struct.pack("<I", len(body) + 4) + b"WEBP" + body


def webp_vp8_bytes(width: int, height: int) -> bytes:  # lossy — what AliExpress serves
    return _webp(
        b"VP8 " + struct.pack("<I", 10) + b"\x00\x00\x00" + b"\x9d\x01\x2a"
        + struct.pack("<H", width & 0x3FFF) + struct.pack("<H", height & 0x3FFF)
    )


def webp_vp8l_bytes(width: int, height: int) -> bytes:  # lossless
    packed = ((width - 1) & 0x3FFF) | (((height - 1) & 0x3FFF) << 14)
    return _webp(b"VP8L" + struct.pack("<I", 5) + b"\x2f" + struct.pack("<I", packed))


def webp_vp8x_bytes(width: int, height: int) -> bytes:  # extended
    return _webp(
        b"VP8X" + struct.pack("<I", 10) + b"\x00\x00\x00\x00"
        + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    )


class FakeAuth:
    def get_access_token(self, force_refresh: bool = False) -> str:
        return "tok"


def make_client() -> EbayClient:
    settings = Settings(ebay_client_id="a", ebay_client_secret="b")
    return EbayClient(settings, FakeAuth(), sleep=lambda _s: None)


def make_product(image_urls: list[str]) -> Product:
    return Product(
        source_platform="amazon",
        source_url="https://www.amazon.co.uk/dp/B000000000",
        source_id="B000000000",
        title_raw="Pet Hair Remover Brush",
        description_html="<p>x</p>",
        images=[{"source_url": url} for url in image_urls],
        base_cost=Decimal("3.50"),
        currency="GBP",
        extracted_at=datetime.now(UTC),
    )


# ------------------------------------------------------------- size parsing

def test_image_size_png():
    assert image_size(png_bytes(640, 480)) == (640, 480)


def test_image_size_gif():
    assert image_size(gif_bytes(700, 500)) == (700, 500)


def test_image_size_jpeg():
    assert image_size(jpeg_bytes(800, 600)) == (800, 600)


def test_image_size_webp_vp8_lossy():
    # regression (2026-07-19): AliExpress serves lossy VP8 WebP as .jpg URLs;
    # image_size() only handled VP8X, so every AliExpress image was rejected
    assert image_size(webp_vp8_bytes(800, 800)) == (800, 800)
    assert image_size(webp_vp8_bytes(1200, 900)) == (1200, 900)


def test_image_size_webp_vp8l_lossless():
    assert image_size(webp_vp8l_bytes(640, 480)) == (640, 480)


def test_image_size_webp_vp8x_extended():
    assert image_size(webp_vp8x_bytes(1024, 768)) == (1024, 768)


def test_image_size_unknown_format():
    assert image_size(b"definitely not an image") is None


def test_min_longest_side_is_ebay_rule():
    assert MIN_LONGEST_SIDE == 500


# ---------------------------------------------------------------- download

@respx.mock
def test_fetch_images_keeps_big_drops_small():
    respx.get("https://cdn.example.com/big.png").respond(200, content=png_bytes(800, 800))
    respx.get("https://cdn.example.com/small.gif").respond(200, content=gif_bytes(300, 200))
    product = make_product(
        ["https://cdn.example.com/big.png", "https://cdn.example.com/small.gif"]
    )
    usable = fetch_images(product)
    assert len(usable) == 1
    asset, data = usable[0]
    assert str(asset.source_url).endswith("big.png")
    assert (asset.width, asset.height) == (800, 800)
    assert data == png_bytes(800, 800)


@respx.mock
def test_fetch_images_all_unusable_raises():
    respx.get("https://cdn.example.com/small.gif").respond(200, content=gif_bytes(300, 200))
    respx.get("https://cdn.example.com/broken.png").respond(404)
    product = make_product(
        ["https://cdn.example.com/small.gif", "https://cdn.example.com/broken.png"]
    )
    with pytest.raises(ImageError, match="500"):
        fetch_images(product)


# ------------------------------------------------------------------ upload

@respx.mock
def test_upload_images_fills_ebay_url_from_body():
    route = respx.post(MEDIA).respond(
        201, json={"imageUrl": "https://i.ebayimg.com/00/s/big.jpg"}
    )
    product = make_product(["https://cdn.example.com/big.png"])
    asset = product.images[0]
    upload_images(make_client(), [(asset, png_bytes(800, 800))])
    assert str(asset.ebay_url) == "https://i.ebayimg.com/00/s/big.jpg"
    request = route.calls.last.request
    assert request.headers["Content-Type"].startswith("multipart/form-data")
    assert request.headers["Authorization"] == "Bearer tok"


@respx.mock
def test_upload_images_follows_location_header():
    respx.post(MEDIA).respond(201, headers={"Location": f"{MEDIA}/IMG-123"}, text="")
    respx.get(f"{MEDIA}/IMG-123").respond(
        200, json={"imageUrl": "https://i.ebayimg.com/00/s/located.jpg"}
    )
    product = make_product(["https://cdn.example.com/big.png"])
    asset = product.images[0]
    upload_images(make_client(), [(asset, png_bytes(800, 800))])
    assert str(asset.ebay_url) == "https://i.ebayimg.com/00/s/located.jpg"


@respx.mock
def test_upload_images_no_url_falls_back_to_source():
    respx.post(MEDIA).respond(201, text="")  # 201 but no imageUrl in body
    product = make_product(["https://cdn.example.com/big.png"])
    asset = product.images[0]
    fetched = [(asset, png_bytes(800, 800))]
    upload_images(make_client(), fetched)
    assert asset.ebay_url is None  # no hard failure
    assert listing_image_urls(fetched) == ["https://cdn.example.com/big.png"]


@respx.mock
def test_upload_images_media_404_falls_back_to_source():
    # sandbox behaviour: Media API endpoint returns 404 → keep source URL
    respx.post(MEDIA).respond(404, json={"errors": [{"errorId": 2002, "message": "Not found"}]})
    product = make_product(["https://cdn.example.com/big.png"])
    asset = product.images[0]
    fetched = [(asset, png_bytes(800, 800))]
    upload_images(make_client(), fetched)
    assert asset.ebay_url is None
    assert listing_image_urls(fetched) == ["https://cdn.example.com/big.png"]


def test_listing_image_urls_prefers_ebay_url():
    from listflow.models import ImageAsset

    a = ImageAsset(source_url="https://cdn.example.com/1.png",
                   ebay_url="https://i.ebayimg.com/1.jpg")
    b = ImageAsset(source_url="https://cdn.example.com/2.png")
    assert listing_image_urls([(a, b""), (b, b"")]) == [
        "https://i.ebayimg.com/1.jpg",
        "https://cdn.example.com/2.png",
    ]
