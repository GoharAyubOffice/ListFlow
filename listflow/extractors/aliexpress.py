"""AliExpress extractor: Playwright persistent profile + embedded state JSON (spec §5.1).

Implemented in Phase 6, fixture-first (tests/fixtures/aliexpress_1005010171981745*).

State discovery (pinned 2026-07-18 from the captured fixture): the product data lives
in `window._d_c_` → `lifeCycleEventList[0].data`, a module map with PRODUCT_TITLE,
SKU, PRICE, QUANTITY_PC, HEADER_IMAGE_PC, PRODUCT_PROP_PC, DESC, SHOP_CARD_PC.
Fallback chain per spec: pinned key → runtime window scan → JSON-LD → DOM scrape.
The description is lazy-loaded from the tokenised URL in the DESC module.

Politeness: one page per call, randomised 2-5s dwell, no retries on blocks — a
login-wall/captcha surfaces a clear error telling the user to run --headed.
"""

import contextlib
import json
import logging
import random
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

import httpx
from selectolax.parser import HTMLParser

from listflow.config import listflow_home
from listflow.extractors.base import ExtractionError, Extractor, save_debug_snapshot
from listflow.models import RawProduct, RawVariant, SourcePlatform

logger = logging.getLogger(__name__)

STATE_KEY = "_d_c_"  # pinned at build time; runtime scan is the fallback
CANDIDATE_KEYS = (STATE_KEY, "runParams", "__INIT_DATA__")

_ITEM_ID_RE = re.compile(r"/(?:item|i)/(\d+)")
_JSONLD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
_SIZE_SUFFIX_RE = re.compile(r"_\d+x\d+[a-z0-9]*(?=\.\w+(?:$|\?))")
# html markers only — "login.aliexpress" must NOT be here: every normal page has a
# sign-in nav link. A login redirect shows up in the final URL instead.
_BLOCK_MARKERS = ("x5secdata", "punish?", "slide to verify")


def _looks_blocked(html: str, final_url: str) -> bool:
    lowered_url = final_url.lower()
    if "login." in lowered_url or "/login" in lowered_url or "punish" in lowered_url:
        return True
    lowered = html.lower()
    return any(marker in lowered for marker in _BLOCK_MARKERS)

# Evaluated in the page: pinned keys first, then a window scan for any object that
# looks like product state. Returns {key, json} or null.
STATE_DUMP_JS = """
() => {
    const tryKey = (k) => {
        try {
            const v = window[k];
            if (v && typeof v === 'object') {
                const t = JSON.stringify(v);
                if (t && t.length > 5000) return {key: k, json: t};
            }
        } catch (e) {}
        return null;
    };
    for (const k of ['_d_c_', 'runParams', '__INIT_DATA__']) {
        const hit = tryKey(k);
        if (hit) return hit;
    }
    let best = null;
    for (const k of Object.keys(window)) {
        try {
            const v = window[k];
            if (!v || typeof v !== 'object') continue;
            const t = JSON.stringify(v);
            if (t && t.length > 5000 &&
                (t.includes('skuPaths') || t.includes('skuModule') ||
                 t.includes('PRODUCT_TITLE'))) {
                if (!best || t.length > best.json.length) best = {key: k, json: t};
            }
        } catch (e) {}
    }
    return best;
}
"""


def item_id_from_url(url: str) -> str | None:
    match = _ITEM_ID_RE.search(url)
    return match.group(1) if match else None


def full_size_image_url(url: str) -> str:
    """Strip thumbnail size suffixes like _220x220 / _960x960q75 from image URLs."""
    return _SIZE_SUFFIX_RE.sub("", url)


def _money_from_string(text: str) -> Decimal | None:
    digits = re.sub(r"[^0-9.,]", "", text).replace(",", "")
    if not digits:
        return None
    try:
        return Decimal(digits)
    except InvalidOperation:
        return None


def _state_modules(state: dict) -> dict | None:
    """Locate the module map inside the state blob (tolerant to wrapper drift)."""
    if "PRODUCT_TITLE" in state:
        return state
    for event in state.get("lifeCycleEventList", []) or []:
        data = event.get("data") if isinstance(event, dict) else None
        if isinstance(data, dict) and "PRODUCT_TITLE" in data:
            return data
    return None


def desc_url_from_state(state: dict) -> str | None:
    modules = _state_modules(state)
    if not modules:
        return None
    desc = modules.get("DESC") or {}
    return desc.get("msiteDescUrl") or desc.get("nativeDescUrl") or None


class AliExpressExtractor(Extractor):
    platform = SourcePlatform.ALIEXPRESS

    def __init__(
        self,
        headed: bool = False,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._headed = headed
        self._http = http or httpx.Client(timeout=30, follow_redirects=True)
        self._sleep = sleep

    # ------------------------------------------------------------- live path

    def extract(self, url: str) -> RawProduct:
        html, state_text, state_key, final_url = self._fetch(url)
        state = json.loads(state_text, parse_float=Decimal) if state_text else None
        if state_key and state_key != STATE_KEY:
            logger.warning(
                "AliExpress state found under window.%s (pinned key %s missing) — "
                "runtime scan fallback used; consider re-pinning",
                state_key,
                STATE_KEY,
            )
        try:
            raw = self.parse(html, final_url or url, state=state)
        except ExtractionError as err:
            if err.page_snapshot_path is None:
                item = item_id_from_url(url) or "unknown"
                err.page_snapshot_path = save_debug_snapshot(html, f"aliexpress_{item}")
            raise
        if not raw.description_html and state:
            desc_url = desc_url_from_state(state)
            if desc_url:
                raw.description_html = self.fetch_description(desc_url)
        return raw

    def _fetch(self, url: str) -> tuple[str, str | None, str | None, str | None]:
        from playwright.sync_api import sync_playwright  # heavy import, live path only

        profile_dir = listflow_home() / "chrome-profile"
        profile_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                str(profile_dir),
                headless=not self._headed,
                locale="en-GB",
                viewport={"width": 1366, "height": 900},
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                self._sleep(random.uniform(2, 5))  # politeness dwell (spec hard rule)
                with contextlib.suppress(Exception):
                    # busy pages are fine; the dwell already happened
                    page.wait_for_load_state("networkidle", timeout=15000)
                html = page.content()
                final_url = page.url
                hit = page.evaluate(STATE_DUMP_JS)
            finally:
                context.close()

        if _looks_blocked(html, final_url):
            raise ExtractionError(
                "AliExpress presented a login wall or captcha. Re-run with --headed, "
                "solve it manually in the visible browser window, then try again.",
                page_snapshot_path=save_debug_snapshot(html, "aliexpress_blocked"),
            )
        if hit:
            return html, hit["json"], hit["key"], final_url
        return html, None, None, final_url

    def fetch_description(self, desc_url: str) -> str:
        """Fetch the lazy-loaded description endpoint (one polite request)."""
        try:
            response = self._http.get(desc_url)
        except httpx.HTTPError as exc:
            logger.warning("description fetch failed: %s", exc)
            return ""
        if response.status_code != 200:
            logger.warning("description fetch HTTP %d", response.status_code)
            return ""
        return response.text

    # ------------------------------------------------------------ parse paths

    def parse(self, html: str, url: str, state: dict | None = None) -> RawProduct:
        """Fallback chain: state JSON → JSON-LD → DOM scrape (spec §5.1)."""
        if state is not None:
            modules = _state_modules(state)
            if modules:
                return self.parse_state(state, url)
            logger.warning("AliExpress state blob present but unrecognised — falling back")
        raw = self._parse_jsonld(html, url)
        if raw is not None:
            logger.warning(
                "AliExpress: using JSON-LD fallback — partial data only "
                "(no SKU matrix, no per-variant stock)"
            )
            return raw
        logger.warning("AliExpress: JSON-LD missing too — DOM scrape (schema drift!)")
        return self._parse_dom(html, url)

    def parse_state(self, state: dict, url: str) -> RawProduct:
        modules = _state_modules(state)
        if modules is None:
            raise ExtractionError(
                "AliExpress state blob does not contain the product module map",
                field_missing="state",
            )

        title = (modules.get("PRODUCT_TITLE") or {}).get("text", "").strip()
        if not title:
            raise ExtractionError(
                "no product title in AliExpress state — item may be unavailable",
                field_missing="title",
            )

        item_id = item_id_from_url(url)
        if not item_id:
            match = re.search(r'"itemId":\s*(\d+)', json.dumps(modules, default=str))
            item_id = match.group(1) if match else None
        if not item_id:
            raise ExtractionError(
                "could not determine AliExpress productId", field_missing="productId"
            )

        variants, currency = self._variants_from_state(modules)
        salable = [v.price for v in variants if v.price is not None and (v.stock or 0) > 0]
        all_prices = [v.price for v in variants if v.price is not None]
        price = min(salable or all_prices) if (salable or all_prices) else None
        if price is None:
            raise ExtractionError("no price found in AliExpress state", field_missing="price")

        images: list[str] = []
        header = modules.get("HEADER_IMAGE_PC") or {}
        for image_url in header.get("imagePathList") or header.get("mainImages") or []:
            full = full_size_image_url(image_url)
            if full not in images:
                images.append(full)

        attributes: dict[str, str] = {}
        for prop in (modules.get("PRODUCT_PROP_PC") or {}).get("showedProps") or []:
            name = (prop.get("attrName") or "").strip()
            value = (prop.get("attrValue") or "").strip()
            if name and value:
                attributes.setdefault(name, value)

        seller = (modules.get("SHOP_CARD_PC") or {}).get("sellerInfo") or {}
        store_name = seller.get("storeName") or None

        return RawProduct(
            source_platform=SourcePlatform.ALIEXPRESS,
            source_url=url,
            source_id=item_id,
            title=title,
            price=price,
            currency=currency or "GBP",
            description_html="",  # lazy-loaded; extract() fills it from the DESC url
            image_urls=images,
            variants=variants,
            attributes=attributes,
            store_name=store_name,
            extracted_at=datetime.now(UTC),
        )

    def _variants_from_state(self, modules: dict) -> tuple[list[RawVariant], str | None]:
        sku_module = modules.get("SKU") or {}
        price_map = (modules.get("PRICE") or {}).get("skuPriceInfoMap") or {}
        quantity_map = (modules.get("QUANTITY_PC") or {}).get("allSkuQuantityView") or {}

        # property id -> name, (property id, value id) -> display name / image
        prop_names: dict[str, str] = {}
        value_names: dict[tuple[str, str], str] = {}
        value_images: dict[tuple[str, str], str] = {}
        for prop in sku_module.get("skuProperties") or []:
            pid = str(prop.get("skuPropertyId"))
            prop_names[pid] = prop.get("skuPropertyName") or f"Option {pid}"
            for value in prop.get("skuPropertyValues") or []:
                vid = str(value.get("propertyValueIdLong"))
                display = (
                    value.get("propertyValueDisplayName")
                    or value.get("propertyValueDefinitionName")
                    or value.get("propertyValueName")
                    or vid
                )
                value_names[(pid, vid)] = display
                if value.get("skuPropertyImagePath"):
                    value_images[(pid, vid)] = full_size_image_url(
                        value["skuPropertyImagePath"]
                    )

        variants: list[RawVariant] = []
        currency: str | None = None
        for path_entry in sku_module.get("skuPaths") or []:
            sku_id = str(path_entry.get("skuIdStr") or path_entry.get("skuId") or "")
            attributes: dict[str, str] = {}
            image_url: str | None = None
            for pair in (path_entry.get("path") or "").split(";"):
                if ":" not in pair:
                    continue
                pid, vid = pair.split(":", 1)
                attributes[prop_names.get(pid, pid)] = value_names.get((pid, vid), vid)
                image_url = image_url or value_images.get((pid, vid))

            price_info = price_map.get(sku_id) or {}
            price: Decimal | None = None
            sale_text = price_info.get("salePriceString")
            if sale_text:
                price = _money_from_string(sale_text)
            original = price_info.get("originalPrice") or {}
            if price is None:
                value = original.get("value")
                if isinstance(value, Decimal):
                    price = value
                elif isinstance(value, int | str):
                    price = Decimal(str(value))
            currency = currency or original.get("currency")

            stock = None
            quantity_info = quantity_map.get(sku_id) or {}
            if "maxBuyCount" in quantity_info:
                stock = int(quantity_info["maxBuyCount"])
            elif "skuStock" in path_entry:
                stock = int(path_entry["skuStock"])
            elif "salable" in path_entry:
                stock = 1 if path_entry["salable"] else 0

            variants.append(
                RawVariant(
                    attributes=attributes,
                    price=price,
                    stock=stock,
                    image_url=image_url,
                )
            )
        return variants, currency

    def _parse_jsonld(self, html: str, url: str) -> RawProduct | None:
        for block in _JSONLD_RE.findall(html):
            try:
                parsed = json.loads(block, parse_float=Decimal)
            except ValueError:
                continue
            entries = parsed if isinstance(parsed, list) else [parsed]
            for entry in entries:
                if not (isinstance(entry, dict) and entry.get("@type") == "Product"):
                    continue
                offers = entry.get("offers") or {}
                price_value = offers.get("price")
                if price_value is None:
                    continue
                price = (
                    price_value
                    if isinstance(price_value, Decimal)
                    else Decimal(str(price_value))
                )
                images: list[str] = []
                image_field = entry.get("image")
                image_list = image_field if isinstance(image_field, list) else [image_field]
                for image_url in image_list:
                    if image_url:
                        full = full_size_image_url(str(image_url))
                        if full not in images:
                            images.append(full)
                attributes: dict[str, str] = {}
                brand = entry.get("brand")
                if isinstance(brand, dict) and brand.get("name"):
                    attributes["Brand Name"] = brand["name"]
                item_id = item_id_from_url(url) or item_id_from_url(
                    str(offers.get("url", ""))
                )
                if not item_id:
                    continue
                return RawProduct(
                    source_platform=SourcePlatform.ALIEXPRESS,
                    source_url=url,
                    source_id=item_id,
                    title=str(entry.get("name") or "").strip(),
                    price=price,
                    currency=str(offers.get("priceCurrency") or "GBP"),
                    description_html=str(entry.get("description") or ""),
                    image_urls=images,
                    attributes=attributes,
                    extracted_at=datetime.now(UTC),
                )
        return None

    def _parse_dom(self, html: str, url: str) -> RawProduct:
        tree = HTMLParser(html)
        title = ""
        for node in tree.css("h1"):
            text = node.text(strip=True)
            if text and text.lower() != "aliexpress":
                title = text
                break
        if not title:
            raise ExtractionError(
                "AliExpress page has no recognisable product data (state, JSON-LD and "
                "DOM all empty) — the item may be removed, or the page did not render; "
                "try again with --headed",
                field_missing="title",
            )
        raise ExtractionError(
            f"AliExpress DOM scrape found a title ({title[:40]!r}) but no structured "
            "price — schema drift; capture a new fixture and repair the extractor",
            field_missing="price",
        )
