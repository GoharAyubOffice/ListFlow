"""Amazon extractor: httpx + selectolax, SELECTORS dict pinned in one place (spec §5.2).

Implemented in Phase 5, fixture-first (tests/fixtures/amazon_B00BAGTNAQ.html).
Robot-check pages get exactly one retry after 30s — no retry storms, no evasion.
"""

import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

import httpx
from selectolax.parser import HTMLParser

from listflow.extractors.base import ExtractionError, Extractor, save_debug_snapshot
from listflow.models import RawProduct, SourcePlatform

logger = logging.getLogger(__name__)

# One place to patch when Amazon's markup drifts (CLAUDE.md maintenance loop).
SELECTORS = {
    "title": "#productTitle",
    "price": ".a-price .a-offscreen",  # first occurrence = buybox context
    "bullets": "#feature-bullets li span.a-list-item",
    "description": "#productDescription",
    "description_fallback": "#aplus_feature_div",  # A+ content (some pages lack the plain block)
    "asin_input": "input#ASIN",
    "byline": "#bylineInfo",
    "overview_rows": "#productOverview_feature_div tr",  # Brand / Colour / Material table
    "details_rows": "#prodDetails tr",
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

_ROBOT_MARKERS = ("validateCaptcha", "api-services-support@amazon.com", "Robot Check")
_ASIN_URL_RE = re.compile(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})", re.IGNORECASE)
_HIRES_RE = re.compile(r'"hiRes":"(https://[^"]+)"')
_LARGE_RE = re.compile(r'"large":"(https://[^"]+)"')
_CURRENCY_SYMBOLS = {"£": "GBP", "€": "EUR", "$": "USD"}

# Detail-table keys that are order/logistics noise, not listing item specifics.
_ATTR_SKIP = {
    "asin",
    "customer reviews",
    "best sellers rank",
    "date first available",
    "item model number",
    "manufacturer reference",
    "upc",
    "product dimensions",
    "guaranteed software updates until",
}


def _asin_from_url(url: str) -> str | None:
    match = _ASIN_URL_RE.search(url)
    return match.group(1).upper() if match else None


def _parse_money(text: str) -> tuple[Decimal, str] | None:
    currency = next((code for sym, code in _CURRENCY_SYMBOLS.items() if sym in text), "GBP")
    digits = re.sub(r"[^0-9.,]", "", text).replace(",", "")
    if not digits:
        return None
    try:
        return Decimal(digits), currency
    except InvalidOperation:
        return None


def _clean_cell(text: str) -> str:
    # Amazon detail cells are littered with RTL/LTR marks and stray whitespace
    return re.sub(r"[‎‏​]", "", " ".join(text.split())).strip()


def _looks_like_robot_check(html: str) -> bool:
    return any(marker in html for marker in _ROBOT_MARKERS)


class AmazonExtractor(Extractor):
    platform = SourcePlatform.AMAZON

    def __init__(
        self,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._http = http or httpx.Client(timeout=30, follow_redirects=True, headers=_HEADERS)
        self._sleep = sleep

    def extract(self, url: str) -> RawProduct:
        html = self._fetch(url)
        try:
            return self.parse(html, url)
        except ExtractionError as err:
            if err.page_snapshot_path is None:
                asin = _asin_from_url(url) or "unknown"
                err.page_snapshot_path = save_debug_snapshot(html, f"amazon_{asin}")
            raise

    def _fetch(self, url: str) -> str:
        response = self._http.get(url)
        if _looks_like_robot_check(response.text):
            logger.warning("Amazon robot-check page detected — waiting 30s for one retry")
            self._sleep(30)
            response = self._http.get(url)
            if _looks_like_robot_check(response.text):
                raise ExtractionError(
                    "Amazon served a robot-check page twice — extraction blocked. "
                    "Wait a while, or run the import from your normal home network.",
                    page_snapshot_path=save_debug_snapshot(response.text, "amazon_robotcheck"),
                )
        if response.status_code != 200:
            raise ExtractionError(
                f"Amazon returned HTTP {response.status_code} for {url}",
                page_snapshot_path=save_debug_snapshot(response.text, "amazon_error"),
            )
        return response.text

    def parse(self, html: str, url: str) -> RawProduct:
        tree = HTMLParser(html)

        title_node = tree.css_first(SELECTORS["title"])
        if title_node is None:
            raise ExtractionError(
                f"could not find product title ({SELECTORS['title']})", field_missing="title"
            )
        title = title_node.text(strip=True)

        price_node = tree.css_first(SELECTORS["price"])
        money = _parse_money(price_node.text()) if price_node else None
        if money is None:
            raise ExtractionError(
                f"could not find a parseable price ({SELECTORS['price']})",
                field_missing="price",
            )
        price, currency = money

        asin_node = tree.css_first(SELECTORS["asin_input"])
        asin = (asin_node.attributes.get("value") if asin_node else None) or _asin_from_url(url)
        if not asin:
            raise ExtractionError("could not determine ASIN", field_missing="asin")

        if "dimensionValuesDisplayData" in html:
            logger.warning(
                "variant product detected — importing the currently selected variant only "
                "(full Amazon variant matrix is a v2 item)"
            )

        return RawProduct(
            source_platform=SourcePlatform.AMAZON,
            source_url=url,
            source_id=asin,
            title=title,
            price=price,
            currency=currency,
            description_html=self._description(tree),
            bullet_points=self._bullets(tree),
            image_urls=self._images(tree, html),
            attributes=self._attributes(tree),
            store_name=self._store_name(tree),
            extracted_at=datetime.now(UTC),
        )

    def _bullets(self, tree: HTMLParser) -> list[str]:
        seen: list[str] = []
        for node in tree.css(SELECTORS["bullets"]):
            text = " ".join(node.text(strip=True).split())
            if text and text not in seen:
                seen.append(text)
        return seen

    def _description(self, tree: HTMLParser) -> str:
        node = tree.css_first(SELECTORS["description"]) or tree.css_first(
            SELECTORS["description_fallback"]
        )
        return node.html or "" if node else ""

    def _images(self, tree: HTMLParser, html: str) -> list[str]:
        urls = _HIRES_RE.findall(html) or _LARGE_RE.findall(html)
        if not urls:  # last resort: the main gallery image tag
            landing = tree.css_first("#landingImage")
            if landing:
                src = landing.attributes.get("data-old-hires") or landing.attributes.get("src")
                if src:
                    urls = [src]
        deduped: list[str] = []
        for url in urls:
            if url not in deduped:
                deduped.append(url)
        return deduped

    def _attributes(self, tree: HTMLParser) -> dict[str, str]:
        attributes: dict[str, str] = {}
        for selector in (SELECTORS["overview_rows"], SELECTORS["details_rows"]):
            for row in tree.css(selector):
                cells = [_clean_cell(cell.text()) for cell in row.css("td, th")]
                if len(cells) != 2:
                    continue
                key, value = cells
                if not key or not value or key.lower() in _ATTR_SKIP:
                    continue
                attributes.setdefault(key, value)
        return attributes

    def _store_name(self, tree: HTMLParser) -> str | None:
        node = tree.css_first(SELECTORS["byline"])
        if node is None:
            return None
        text = node.text(strip=True)
        text = re.sub(r"^(Visit the|Brand:)\s*", "", text, flags=re.IGNORECASE)
        return text.strip() or None
