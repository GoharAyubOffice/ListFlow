"""Extractor tests — run against tests/fixtures/ only, NEVER live sites (spec §5.3).

Phases 5–6.
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest
import respx
from selectolax.parser import HTMLParser

from listflow.extractors.aliexpress import (
    AliExpressExtractor,
    _looks_blocked,
    desc_url_from_state,
    full_size_image_url,
    item_id_from_url,
)
from listflow.extractors.amazon import AmazonExtractor, _asin_from_url, _parse_money
from listflow.extractors.base import ExtractionError
from listflow.models import SourcePlatform

FIXTURES = Path(__file__).parent / "fixtures"
AMAZON_FIXTURE = FIXTURES / "amazon_B00BAGTNAQ.html"
AMAZON_URL = "https://www.amazon.co.uk/ChomChom-Roller-Dog-Hair-Remover/dp/B00BAGTNAQ"
AE_STATE_FIXTURE = FIXTURES / "aliexpress_1005010171981745_state.json"
AE_HTML_FIXTURE = FIXTURES / "aliexpress_1005010171981745.html"
AE_DESC_FIXTURE = FIXTURES / "aliexpress_1005010171981745_desc.html"
AE_URL = "https://www.aliexpress.com/item/1005010171981745.html"

ROBOT_PAGE = (
    "<html><body>To discuss automated access to Amazon data please contact "
    "api-services-support@amazon.com.</body></html>"
)
BROKEN_PAGE = "<html><body><p>Sorry, this page has nothing on it.</p></body></html>"


@pytest.fixture(scope="module")
def amazon_html() -> str:
    return AMAZON_FIXTURE.read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Debug snapshots must land in a temp dir, never the real ~/.listflow."""
    monkeypatch.setenv("LISTFLOW_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


# ------------------------------------------------------------ fixture parse

def test_parse_fixture_populates_every_canonical_field(amazon_html):
    raw = AmazonExtractor().parse(amazon_html, AMAZON_URL)
    assert raw.source_platform is SourcePlatform.AMAZON
    assert raw.source_id == "B00BAGTNAQ"
    assert raw.source_url == AMAZON_URL
    assert raw.title.startswith("ChomChom Pet Hair Remover")
    assert raw.price == Decimal("13.99")
    assert isinstance(raw.price, Decimal)
    assert raw.currency == "GBP"
    assert len(raw.bullet_points) >= 4
    assert all(b.strip() for b in raw.bullet_points)
    assert len(raw.image_urls) >= 4
    assert all(u.startswith("https://") for u in raw.image_urls)
    assert len(raw.image_urls) == len(set(raw.image_urls))  # deduped
    assert raw.attributes.get("Brand") == "ChomChom Roller"
    assert raw.attributes.get("Colour") == "White"
    assert raw.attributes.get("Material") == "Plastic"
    assert raw.store_name is not None
    assert "ChomChom" in raw.store_name
    assert raw.description_html  # A+ content fallback on this page
    assert raw.extracted_at is not None


def test_parse_fixture_attribute_noise_filtered(amazon_html):
    raw = AmazonExtractor().parse(amazon_html, AMAZON_URL)
    lowered = {k.lower() for k in raw.attributes}
    # regression (2026-07-18): css("td,th") returned selector order, so #prodDetails
    # rows were read reversed and the noise filter checked the wrong cell.
    assert "customer reviews" not in lowered
    assert "best sellers rank" not in lowered
    assert "asin" not in lowered
    assert "chomchom roller" not in lowered  # a VALUE must never end up as a key
    # keys map to real attribute names, values to their contents (not reversed)
    assert raw.attributes["Brand"] == "ChomChom Roller"
    assert raw.attributes["Colour"] == "White"
    # no scraped-JS blobs survived
    assert all(len(v) <= 100 for v in raw.attributes.values())


def test_attributes_use_th_as_key_not_css_order():
    # <th>label</th><td>value</td>: label is the key regardless of selector order
    html = (
        "<div id='prodDetails'><table>"
        "<tr><th class='prodDetSectionEntry'>Material Type</th><td>Silicone</td></tr>"
        "<tr><th>ASIN</th><td>B000000000</td></tr>"
        "</table></div>"
    )
    attrs = AmazonExtractor()._attributes(HTMLParser(html))
    assert attrs == {"Material Type": "Silicone"}  # ASIN row blocklisted, order correct


# ------------------------------------------------------------ helper units

@pytest.mark.parametrize(
    ("text", "amount", "currency"),
    [
        ("£13.99", "13.99", "GBP"),
        ("£1,299.00", "1299.00", "GBP"),
        ("$9.99", "9.99", "USD"),
        ("€24.50", "24.50", "EUR"),
        ("  £5  ", "5", "GBP"),
    ],
)
def test_parse_money(text, amount, currency):
    assert _parse_money(text) == (Decimal(amount), currency)


def test_parse_money_garbage_is_none():
    assert _parse_money("Currently unavailable") is None


@pytest.mark.parametrize(
    "url",
    [
        "https://www.amazon.co.uk/dp/B00BAGTNAQ",
        "https://www.amazon.co.uk/Name-Here/dp/B00BAGTNAQ/ref=sr_1_1?keywords=x",
        "https://www.amazon.com/gp/product/B00BAGTNAQ",
        "https://www.amazon.co.uk/gp/aw/d/B00BAGTNAQ",
    ],
)
def test_asin_from_url_shapes(url):
    assert _asin_from_url(url) == "B00BAGTNAQ"


def test_asin_from_url_absent():
    assert _asin_from_url("https://www.amazon.co.uk/s?k=pet+brush") is None


# ------------------------------------------------- failure + robot handling

@respx.mock
def test_broken_page_raises_with_debug_snapshot(isolated_home):
    respx.get(AMAZON_URL).respond(200, text=BROKEN_PAGE)
    with pytest.raises(ExtractionError) as excinfo:
        AmazonExtractor(sleep=lambda _s: None).extract(AMAZON_URL)
    err = excinfo.value
    assert err.field_missing == "title"
    assert err.page_snapshot_path is not None
    assert err.page_snapshot_path.exists()
    assert err.page_snapshot_path.is_relative_to(isolated_home / "debug")
    assert "snapshot" in str(err)


@respx.mock
def test_robot_check_retries_once_after_30s(amazon_html):
    import httpx

    route = respx.get(AMAZON_URL)
    route.side_effect = [
        httpx.Response(200, text=ROBOT_PAGE),
        httpx.Response(200, text=amazon_html),
    ]
    sleeps: list[float] = []
    raw = AmazonExtractor(sleep=sleeps.append).extract(AMAZON_URL)
    assert raw.source_id == "B00BAGTNAQ"
    assert sleeps == [30]
    assert route.call_count == 2


@respx.mock
def test_robot_check_twice_fails_with_snapshot(isolated_home):
    respx.get(AMAZON_URL).respond(200, text=ROBOT_PAGE)
    sleeps: list[float] = []
    with pytest.raises(ExtractionError, match="robot"):
        AmazonExtractor(sleep=sleeps.append).extract(AMAZON_URL)
    assert sleeps == [30]  # exactly one retry, no storm
    assert list((isolated_home / "debug").glob("amazon_robotcheck-*.html"))


@respx.mock
def test_http_error_raises_with_snapshot(isolated_home):
    respx.get(AMAZON_URL).respond(503, text="<html>Service Unavailable</html>")
    with pytest.raises(ExtractionError, match="503"):
        AmazonExtractor(sleep=lambda _s: None).extract(AMAZON_URL)
    assert list((isolated_home / "debug").glob("amazon_error-*.html"))


# ================================================================ AliExpress

@pytest.fixture(scope="module")
def ae_state() -> dict:
    # parse_float=Decimal mirrors the live path: no float ever touches money
    doc = json.loads(AE_STATE_FIXTURE.read_text(encoding="utf-8"), parse_float=Decimal)
    return doc["state"]


@pytest.fixture(scope="module")
def ae_html() -> str:
    return AE_HTML_FIXTURE.read_text(encoding="utf-8")


def test_ae_state_fixture_records_pinned_window_key():
    doc = json.loads(AE_STATE_FIXTURE.read_text(encoding="utf-8"))
    assert doc["window_key"] == "_d_c_"  # matches aliexpress.STATE_KEY


def test_ae_parse_state_populates_every_canonical_field(ae_state):
    raw = AliExpressExtractor().parse_state(ae_state, AE_URL)
    assert raw.source_platform is SourcePlatform.ALIEXPRESS
    assert raw.source_id == "1005010171981745"
    assert raw.title.startswith("Pet Hair Remover Laundry Ball")
    assert raw.price == Decimal("0.85")  # cheapest salable SKU
    assert isinstance(raw.price, Decimal)
    assert raw.currency == "GBP"
    assert len(raw.image_urls) >= 3
    assert len(raw.image_urls) == len(set(raw.image_urls))  # deduped
    assert all(u.startswith("https://") for u in raw.image_urls)
    assert raw.attributes.get("Brand Name") == "XMSJ"
    assert raw.extracted_at is not None


def test_ae_parse_state_sku_matrix(ae_state):
    raw = AliExpressExtractor().parse_state(ae_state, AE_URL)
    assert len(raw.variants) >= 4
    by_value = {next(iter(v.attributes.values()), None): v for v in raw.variants}
    assert "1 PC" in by_value
    assert "16 PCS" in by_value
    assert next(iter(by_value["1 PC"].attributes.keys())) == "Color"
    assert by_value["1 PC"].price == Decimal("0.85")
    assert by_value["1 PC"].stock == 979
    assert by_value["16 PCS"].stock == 0  # sold out
    for variant in raw.variants:
        if variant.price is not None:
            assert isinstance(variant.price, Decimal)


def test_ae_desc_url_from_state(ae_state):
    url = desc_url_from_state(ae_state)
    assert url is not None
    assert url.startswith("https://")
    assert "desc" in url


def test_ae_parse_state_missing_modules_raises():
    with pytest.raises(ExtractionError, match="module map"):
        AliExpressExtractor().parse_state({"unrelated": True}, AE_URL)


def test_ae_jsonld_fallback_from_html(ae_html, caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        raw = AliExpressExtractor().parse(ae_html, AE_URL, state=None)
    assert raw.source_id == "1005010171981745"
    assert raw.title.startswith("Pet Hair Remover Laundry Ball")
    assert raw.price == Decimal("0.85")
    assert raw.currency == "GBP"
    assert len(raw.image_urls) >= 1
    assert raw.attributes.get("Brand Name") == "XMSJ"
    assert "JSON-LD fallback" in caplog.text


def test_ae_empty_page_raises_actionable_error():
    with pytest.raises(ExtractionError, match="headed"):
        AliExpressExtractor().parse("<html><body></body></html>", AE_URL, state=None)


@respx.mock
def test_ae_fetch_description_uses_fixture():
    desc_url = "https://pdp.aliexpress-media.com/product/description/msite/v2/desc.htm?x=1"
    respx.get(desc_url).respond(200, text=AE_DESC_FIXTURE.read_text(encoding="utf-8"))
    html = AliExpressExtractor().fetch_description(desc_url)
    assert "detail-desc-decorate-richtext" in html


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.aliexpress.com/item/1005010171981745.html", "1005010171981745"),
        ("https://m.aliexpress.com/i/32970396872.html", "32970396872"),
        ("https://www.aliexpress.com/w/wholesale-x.html", None),
    ],
)
def test_ae_item_id_from_url(url, expected):
    assert item_id_from_url(url) == expected


def test_ae_normal_page_not_mistaken_for_login_wall(ae_html):
    # regression: every AliExpress page has a login.aliexpress nav link — that alone
    # must never count as a block (false positive found in live smoke, 2026-07-18)
    assert "login.aliexpress" in ae_html.lower()
    assert _looks_blocked(ae_html, AE_URL) is False


def test_ae_real_blocks_detected():
    assert _looks_blocked("<html>x5secdata=abc</html>", AE_URL) is True
    assert _looks_blocked("<html>ok</html>", "https://login.aliexpress.com/?return=x") is True
    assert _looks_blocked("<html>ok</html>", "https://www.aliexpress.com/p/punish/x.html") is True


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://ae01.alicdn.com/kf/abc_220x220.jpg", "https://ae01.alicdn.com/kf/abc.jpg"),
        (
            "https://ae01.alicdn.com/kf/abc_960x960q75.jpg?ver=1",
            "https://ae01.alicdn.com/kf/abc.jpg?ver=1",
        ),
        ("https://ae01.alicdn.com/kf/abc.jpg", "https://ae01.alicdn.com/kf/abc.jpg"),
    ],
)
def test_ae_full_size_image_url(url, expected):
    assert full_size_image_url(url) == expected
