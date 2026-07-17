"""URL-shape tests for detector.py — 10+ shapes per platform (spec §9.1). Phase 2."""

import pytest

from listflow.detector import UnknownPlatformError, detect
from listflow.models import SourcePlatform

ALIEXPRESS_URLS = [
    "https://www.aliexpress.com/item/1005006543210987.html",
    "https://aliexpress.com/item/1005006543210987.html",
    "https://m.aliexpress.com/item/1005006543210987.html",
    "https://m.aliexpress.com/i/1005006543210987.html",
    "https://www.aliexpress.us/item/3256801234567890.html",
    "https://es.aliexpress.com/item/1005006543210987.html",
    "https://a.aliexpress.com/_mKjHgF",  # app share short link
    "https://www.aliexpress.com/item/1005006543210987.html"
    "?spm=a2g0o.productlist.main.1&gatewayAdapt=glo2gbr",
    "https://aliexpress.ru/item/1005006543210987.html",
    "https://www.aliexpress.com/i/1005006543210987.html?_t=abc123&aff_fcid=xyz",
    "www.aliexpress.com/item/1005006543210987.html",  # scheme missing
    "HTTPS://WWW.ALIEXPRESS.COM/ITEM/1005006543210987.HTML",  # shouty case
]

AMAZON_URLS = [
    "https://www.amazon.co.uk/dp/B08N5WRWNW",
    "https://www.amazon.com/dp/B08N5WRWNW",
    "https://amazon.de/dp/B08N5WRWNW",
    "https://www.amazon.co.uk/gp/product/B08N5WRWNW",
    "https://www.amazon.co.uk/Stainless-Steel-Grooming-Brush/dp/B08N5WRWNW/ref=sr_1_1"
    "?crid=ABC123&keywords=pet+brush&qid=1700000000&sprefix=pet%2Caps%2C123",
    "https://www.amazon.com.au/dp/B08N5WRWNW",
    "https://smile.amazon.com/dp/B08N5WRWNW",
    "https://www.amazon.co.uk/gp/aw/d/B08N5WRWNW",  # mobile page
    "https://amzn.eu/d/gHtRsWq",  # share short link
    "https://amzn.to/3PqRsTu",  # marketing short link
    "https://a.co/d/aBcDeFg",  # app share short link
    "www.amazon.co.uk/dp/B08N5WRWNW",  # scheme missing
]


@pytest.mark.parametrize("url", ALIEXPRESS_URLS)
def test_detects_aliexpress(url):
    assert detect(url) is SourcePlatform.ALIEXPRESS


@pytest.mark.parametrize("url", AMAZON_URLS)
def test_detects_amazon(url):
    assert detect(url) is SourcePlatform.AMAZON


@pytest.mark.parametrize(
    "url",
    [
        "https://www.ebay.co.uk/itm/123456789",
        "https://www.google.com/search?q=amazon",  # platform name in query only
        "https://www.amazonia.com/dp/B08N5WRWNW",  # lookalike label
        "https://best-aliexpress-deals.com/item/1.html",  # lookalike label
        "https://amazon.evil.example/dp/B08N5WRWNW",  # spoofed subdomain
        "not a url at all",
        "",
        "   ",
    ],
    ids=["ebay", "query-only", "amazonia", "lookalike", "spoof", "garbage", "empty", "blank"],
)
def test_unknown_urls_rejected(url):
    with pytest.raises(UnknownPlatformError) as excinfo:
        detect(url)
    assert "supported" in str(excinfo.value).lower()


def test_unknown_platform_error_is_value_error():
    assert issubclass(UnknownPlatformError, ValueError)
