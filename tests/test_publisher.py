"""Publisher tests with respx-mocked eBay API: happy path, 429 retry,
partial-failure resumable state (spec §9.1). Phase 4.
"""

import json as jsonlib
import struct
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
import respx

from listflow.config import Settings
from listflow.ebay.client import EbayApiError, EbayClient
from listflow.ebay.publisher import LOCATION_KEY, Publisher, make_sku
from listflow.models import Product
from listflow.pricing import price

SANDBOX = "https://api.sandbox.ebay.com"
MEDIA = "https://apim.sandbox.ebay.com/commerce/media/v1_beta/image"


def png_bytes(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + bytes(5)
    )


class FakeAuth:
    def get_access_token(self, force_refresh: bool = False) -> str:
        return "tok"


def make_settings(**overrides) -> Settings:
    base = {
        "ebay_client_id": "a",
        "ebay_client_secret": "b",
        "payment_policy_id": "PAY-1",
        "return_policy_id": "RET-1",
        "fulfillment_policy_id": "SHIP-1",
    }
    base.update(overrides)
    return Settings(**base)


def make_product() -> Product:
    return Product(
        source_platform="amazon",
        source_url="https://www.amazon.co.uk/dp/B08N5WRWNW",
        source_id="B08N5WRWNW",
        title_raw="Pet Hair Remover Brush raw",
        title_ebay="Pet Hair Remover Brush",
        description_html="<p><b>Pet Hair Remover Brush</b></p>",
        bullet_points=["Easy to clean"],
        images=[{"source_url": "https://cdn.example.com/main.png"}],
        base_cost=Decimal("10.00"),
        currency="GBP",
        item_specifics={"Colour": "Blue", "Brand": "Unbranded"},
        extracted_at=datetime.now(UTC),
    )


def make_publisher(on_step=None):
    settings = make_settings()
    sleeps: list[float] = []
    client = EbayClient(settings, FakeAuth(), sleep=sleeps.append)
    return Publisher(client, settings, on_step=on_step), sleeps


def mock_happy_pipeline(sku: str, location_exists: bool = True):
    """Mock every eBay endpoint of the pipeline; returns dict of routes."""
    routes = {}
    location_url = f"{SANDBOX}/sell/inventory/v1/location/{LOCATION_KEY}"
    if location_exists:
        routes["loc_get"] = respx.get(location_url).respond(
            200, json={"merchantLocationKey": LOCATION_KEY}
        )
    else:
        routes["loc_get"] = respx.get(location_url).respond(
            404, json={"errors": [{"errorId": 25804, "message": "Not found"}]}
        )
        routes["loc_post"] = respx.post(location_url).respond(204)
    routes["img_src"] = respx.get("https://cdn.example.com/main.png").respond(
        200, content=png_bytes(800, 800)
    )
    routes["media"] = respx.post(MEDIA).respond(
        201, json={"imageUrl": "https://i.ebayimg.com/00/s/main.jpg"}
    )
    routes["tree"] = respx.get(
        f"{SANDBOX}/commerce/taxonomy/v1/get_default_category_tree_id"
    ).respond(200, json={"categoryTreeId": "3"})
    routes["suggest"] = respx.get(
        f"{SANDBOX}/commerce/taxonomy/v1/category_tree/3/get_category_suggestions"
    ).respond(
        200,
        json={
            "categorySuggestions": [
                {"category": {"categoryId": "179011", "categoryName": "Pet"}}
            ]
        },
    )
    routes["inventory"] = respx.put(
        f"{SANDBOX}/sell/inventory/v1/inventory_item/{sku}"
    ).respond(204)
    routes["offer"] = respx.post(f"{SANDBOX}/sell/inventory/v1/offer").respond(
        201, json={"offerId": "OFF-1"}
    )
    routes["publish"] = respx.post(
        f"{SANDBOX}/sell/inventory/v1/offer/OFF-1/publish"
    ).respond(200, json={"listingId": "LIST-1"})
    return routes


def test_make_sku_is_stable_and_traceable():
    sku = make_sku("B08N5WRWNW")
    assert sku.startswith("LF-B08N5WRWNW-")
    assert sku == make_sku("B08N5WRWNW")  # deterministic
    assert make_sku("1005006543210987").startswith("LF-100500654321-")  # 12-char cap


def test_make_sku_strips_unsafe_chars():
    assert "/" not in make_sku("abc/def#123")
    assert "#" not in make_sku("abc/def#123")


@respx.mock
def test_draft_happy_path():
    product = make_product()
    sku = make_sku(product.source_id)
    routes = mock_happy_pipeline(sku)
    publisher, _ = make_publisher()
    result = publisher.publish(product, price(Decimal("10.00")))

    assert result.sku == sku
    assert result.offer_id == "OFF-1"
    assert result.listing_id is None  # draft: stop before publishOffer
    assert result.category_id == "179011"
    assert routes["publish"].call_count == 0
    assert result.steps_completed == [
        "location",
        "images",
        "category",
        "inventory_item",
        "offer",
    ]

    inventory_payload = jsonlib.loads(routes["inventory"].calls.last.request.content)
    assert inventory_payload["product"]["title"] == "Pet Hair Remover Brush"
    assert inventory_payload["product"]["aspects"] == {
        "Colour": ["Blue"],
        "Brand": ["Unbranded"],
    }
    assert inventory_payload["product"]["imageUrls"] == ["https://i.ebayimg.com/00/s/main.jpg"]
    assert inventory_payload["availability"]["shipToLocationAvailability"]["quantity"] == 3
    assert inventory_payload["condition"] == "NEW"

    offer_payload = jsonlib.loads(routes["offer"].calls.last.request.content)
    assert offer_payload["sku"] == sku
    assert offer_payload["marketplaceId"] == "EBAY_GB"
    assert offer_payload["pricingSummary"]["price"] == {"value": "15.99", "currency": "GBP"}
    assert offer_payload["categoryId"] == "179011"
    assert offer_payload["merchantLocationKey"] == LOCATION_KEY
    assert offer_payload["listingPolicies"] == {
        "paymentPolicyId": "PAY-1",
        "returnPolicyId": "RET-1",
        "fulfillmentPolicyId": "SHIP-1",
    }


@respx.mock
def test_publish_happy_path_returns_listing_id():
    product = make_product()
    routes = mock_happy_pipeline(make_sku(product.source_id))
    publisher, _ = make_publisher()
    result = publisher.publish(product, price(Decimal("10.00")), publish=True)
    assert result.listing_id == "LIST-1"
    assert routes["publish"].call_count == 1
    assert result.steps_completed[-1] == "publish"


@respx.mock
def test_missing_location_gets_created():
    product = make_product()
    routes = mock_happy_pipeline(make_sku(product.source_id), location_exists=False)
    publisher, _ = make_publisher()
    publisher.publish(product, price(Decimal("10.00")))
    assert routes["loc_post"].call_count == 1


@respx.mock
def test_category_override_skips_taxonomy():
    product = make_product()
    routes = mock_happy_pipeline(make_sku(product.source_id))
    publisher, _ = make_publisher()
    result = publisher.publish(product, price(Decimal("10.00")), category_id="99999")
    assert result.category_id == "99999"
    assert routes["tree"].call_count == 0
    assert routes["suggest"].call_count == 0
    offer_payload = jsonlib.loads(routes["offer"].calls.last.request.content)
    assert offer_payload["categoryId"] == "99999"


@respx.mock
def test_429_on_offer_is_retried():
    product = make_product()
    sku = make_sku(product.source_id)
    routes = mock_happy_pipeline(sku)
    routes["offer"].side_effect = [
        httpx.Response(429, json={"errors": [{"errorId": 1001, "message": "Rate limited"}]}),
        httpx.Response(201, json={"offerId": "OFF-1"}),
    ]
    publisher, sleeps = make_publisher()
    result = publisher.publish(product, price(Decimal("10.00")))
    assert result.offer_id == "OFF-1"
    assert sleeps == [1]


@respx.mock
def test_partial_failure_records_completed_steps():
    product = make_product()
    sku = make_sku(product.source_id)
    routes = mock_happy_pipeline(sku)
    routes["offer"].side_effect = [
        httpx.Response(
            400, json={"errors": [{"errorId": 25002, "message": "Invalid listing policy"}]}
        )
    ]
    recorded: list[tuple[str, str]] = []
    publisher, _ = make_publisher(on_step=lambda s, step: recorded.append((s, step)))
    with pytest.raises(EbayApiError) as excinfo:
        publisher.publish(product, price(Decimal("10.00")))
    assert "25002" in str(excinfo.value)
    # everything before the offer step is recorded, so `listflow retry` can resume
    assert [step for _, step in recorded] == ["location", "images", "category", "inventory_item"]
    assert all(s == sku for s, _ in recorded)
