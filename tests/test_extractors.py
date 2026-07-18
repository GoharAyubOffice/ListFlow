"""Extractor tests — run against tests/fixtures/ only, NEVER live sites (spec §5.3).

Phases 5–6.
"""

from decimal import Decimal
from pathlib import Path

import pytest
import respx

from listflow.extractors.amazon import AmazonExtractor, _asin_from_url, _parse_money
from listflow.extractors.base import ExtractionError
from listflow.models import SourcePlatform

FIXTURES = Path(__file__).parent / "fixtures"
AMAZON_FIXTURE = FIXTURES / "amazon_B00BAGTNAQ.html"
AMAZON_URL = "https://www.amazon.co.uk/ChomChom-Roller-Dog-Hair-Remover/dp/B00BAGTNAQ"

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
    assert "customer reviews" not in lowered
    assert "best sellers rank" not in lowered


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
