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
from listflow.ebay.publisher import (
    LOCATION_KEY,
    Publisher,
    _analyze_variations,
    _variant_sku,
    make_sku,
)
from listflow.models import ImageAsset, Product, Variant
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
        "fulfillment_policy_id": "SHIP-FAST",
        "fulfillment_policy_id_slow": "SHIP-SLOW",
        "ship_from_city": "London",
        "ship_from_postal_code": "EC1A 1BB",
        "ship_from_address_line1": "1 High Street",
    }
    base.update(overrides)
    return Settings(**base)


def test_amazon_uses_fast_fulfillment_policy():
    pub, _ = make_publisher()
    product = make_product()  # source_platform = amazon
    payload = pub._offer_payload(product, price(Decimal("10.00")), "SKU", "123")
    assert payload["listingPolicies"]["fulfillmentPolicyId"] == "SHIP-FAST"


def test_aliexpress_uses_slow_fulfillment_policy():
    pub, _ = make_publisher()
    product = make_product()
    product.source_platform = "aliexpress"
    payload = pub._offer_payload(product, price(Decimal("10.00")), "SKU", "123")
    assert payload["listingPolicies"]["fulfillmentPolicyId"] == "SHIP-SLOW"


def test_aliexpress_falls_back_to_default_when_no_slow_policy():
    settings = make_settings(fulfillment_policy_id_slow=None)
    pub = Publisher(EbayClient(settings, FakeAuth(), sleep=lambda _s: None), settings)
    product = make_product()
    product.source_platform = "aliexpress"
    payload = pub._offer_payload(product, price(Decimal("10.00")), "SKU", "123")
    assert payload["listingPolicies"]["fulfillmentPolicyId"] == "SHIP-FAST"


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
        "fulfillmentPolicyId": "SHIP-FAST",  # amazon product -> fast policy
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
def test_missing_location_gets_created_with_address():
    product = make_product()
    routes = mock_happy_pipeline(make_sku(product.source_id), location_exists=False)
    publisher, _ = make_publisher()
    publisher.publish(product, price(Decimal("10.00")))
    assert routes["loc_post"].call_count == 1
    payload = jsonlib.loads(routes["loc_post"].calls.last.request.content)
    address = payload["location"]["address"]
    assert address["postalCode"] == "EC1A 1BB"  # real ship-from address, not country-only
    assert address["city"] == "London"
    assert address["country"] == "GB"


@respx.mock
def test_missing_ship_from_address_is_actionable():
    product = make_product()
    mock_happy_pipeline(make_sku(product.source_id), location_exists=False)
    settings = make_settings(ship_from_city="", ship_from_postal_code="")
    publisher = Publisher(EbayClient(settings, FakeAuth(), sleep=lambda _s: None), settings)
    with pytest.raises(ValueError, match="ship-from address"):
        publisher.publish(product, price(Decimal("10.00")))


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
def test_duplicate_offer_recovers_by_updating_existing():
    # regression (2026-07-19 GUI test): draft then publish re-ran the pipeline and
    # POSTed a second offer for the same SKU -> eBay 25002 "already exists" crash.
    product = make_product()
    sku = make_sku(product.source_id)
    routes = mock_happy_pipeline(sku)
    routes["offer"].side_effect = [
        httpx.Response(
            400, json={"errors": [{"errorId": 25002, "message": "Offer entity already exists."}]}
        )
    ]
    lookup = respx.get(f"{SANDBOX}/sell/inventory/v1/offer").respond(
        200, json={"offers": [{"offerId": "OFF-OLD"}]}
    )
    update = respx.put(f"{SANDBOX}/sell/inventory/v1/offer/OFF-OLD").respond(204)
    publisher, _ = make_publisher()
    result = publisher.publish(product, price(Decimal("10.00")))
    assert result.offer_id == "OFF-OLD"
    assert lookup.call_count == 1
    assert update.call_count == 1


@respx.mock
def test_retry_with_existing_offer_updates_not_duplicates():
    product = make_product()
    sku = make_sku(product.source_id)
    routes = mock_happy_pipeline(sku)
    update = respx.put(f"{SANDBOX}/sell/inventory/v1/offer/OFF-EXISTING").respond(204)
    publisher, _ = make_publisher()
    result = publisher.publish(
        product, price(Decimal("10.00")), existing_offer_id="OFF-EXISTING"
    )
    assert result.offer_id == "OFF-EXISTING"
    assert update.call_count == 1
    assert routes["offer"].call_count == 0  # no duplicate POST


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


# ============================================================ multi-variation


def make_variant_product() -> Product:
    return Product(
        source_platform="aliexpress",
        source_url="https://www.aliexpress.com/item/1005010246193426.html",
        source_id="1005010246193426",
        title_raw="Oversized Bath Towel Set",
        title_ebay="Oversized Bath Towel Set 90x180cm Soft Absorbent",
        description_html="<p><b>Soft towels</b></p>",
        images=[{"source_url": "https://cdn.example.com/main.png"}],
        variants=[
            Variant(
                sku_suffix="RED-S", attributes={"Colour": "Red", "Size": "S"},
                source_price=Decimal("5.00"), stock=5,
                image=ImageAsset(source_url="https://cdn.example.com/red.png"),
            ),
            Variant(
                sku_suffix="RED-L", attributes={"Colour": "Red", "Size": "L"},
                source_price=Decimal("6.00"), stock=3,
                image=ImageAsset(source_url="https://cdn.example.com/red.png"),
            ),
            Variant(
                sku_suffix="BLUE-L", attributes={"Colour": "Blue", "Size": "L"},
                source_price=Decimal("6.50"), stock=2,
                image=ImageAsset(source_url="https://cdn.example.com/blue.png"),
            ),
        ],
        base_cost=Decimal("5.00"),
        currency="GBP",
        item_specifics={"Brand": "Unbranded", "Material": "Cotton"},
        extracted_at=datetime.now(UTC),
    )


def variant_pricings(product):
    return [(v, price(v.source_price)) for v in product.variants]


def mock_variation_pipeline(group_key: str):
    routes = {}
    routes["loc_get"] = respx.get(
        f"{SANDBOX}/sell/inventory/v1/location/{LOCATION_KEY}"
    ).respond(200, json={"merchantLocationKey": LOCATION_KEY})
    for name in ("main", "red", "blue"):
        respx.get(f"https://cdn.example.com/{name}.png").respond(
            200, content=png_bytes(800, 800)
        )
    routes["media"] = respx.post(MEDIA).respond(
        404, json={"errors": [{"errorId": 2002, "message": "Not found"}]}
    )
    routes["tree"] = respx.get(
        f"{SANDBOX}/commerce/taxonomy/v1/get_default_category_tree_id"
    ).respond(200, json={"categoryTreeId": "3"})
    routes["suggest"] = respx.get(
        f"{SANDBOX}/commerce/taxonomy/v1/category_tree/3/get_category_suggestions"
    ).respond(
        200,
        json={"categorySuggestions": [{"category": {"categoryId": "45591", "categoryName": "T"}}]},
    )
    routes["item"] = respx.put(
        url__regex=rf"{SANDBOX}/sell/inventory/v1/inventory_item/.+"
    ).respond(204)
    routes["group"] = respx.put(
        url__regex=rf"{SANDBOX}/sell/inventory/v1/inventory_item_group/.+"
    ).respond(204)

    counter = {"n": 0}

    def offer_response(request):
        counter["n"] += 1
        return httpx.Response(201, json={"offerId": f"OFF-{counter['n']}"})

    routes["offer"] = respx.post(f"{SANDBOX}/sell/inventory/v1/offer").mock(
        side_effect=offer_response
    )
    routes["publish"] = respx.post(
        f"{SANDBOX}/sell/inventory/v1/offer/publish_by_inventory_item_group"
    ).respond(200, json={"listingId": "LIST-VAR-1"})
    return routes


def test_analyze_variations_splits_varying_from_shared():
    product = make_variant_product()
    varying, shared, deduped = _analyze_variations(variant_pricings(product))
    assert varying == {"Colour": ["Red", "Blue"], "Size": ["S", "L"]}
    assert shared == {}
    assert len(deduped) == 3


def test_analyze_variations_dedupes_and_finds_shared():
    p = make_variant_product()
    pairs = variant_pricings(p)
    pairs.append(pairs[0])  # duplicate combo -> dropped
    varying, shared, deduped = _analyze_variations(pairs)
    assert len(deduped) == 3


def test_variant_sku_unique_and_bounded():
    used: set[str] = set()
    a = _variant_sku("LF-ABC-1234", "RED-S", used)
    b = _variant_sku("LF-ABC-1234", "RED-S", used)  # collision -> suffixed
    assert a != b
    assert a.startswith("LF-ABC-1234-RED-S")
    assert len(a) <= 50


@respx.mock
def test_publish_variations_draft_builds_group_and_offers():
    product = make_variant_product()
    group_key = make_sku(product.source_id)
    routes = mock_variation_pipeline(group_key)
    publisher, _ = make_publisher()
    result = publisher.publish_variations(product, variant_pricings(product))

    assert result.sku == group_key
    assert result.listing_id is None  # draft
    assert routes["publish"].call_count == 0
    assert routes["item"].call_count == 3  # one inventory item per variant
    assert routes["group"].call_count == 1
    assert routes["offer"].call_count == 3
    assert result.steps_completed == [
        "location", "images", "category", "inventory_item", "inventory_group", "offer",
    ]

    group_payload = jsonlib.loads(routes["group"].calls.last.request.content)
    assert len(group_payload["variantSKUs"]) == 3
    assert all(sku.startswith(group_key) for sku in group_payload["variantSKUs"])
    specs = {s["name"]: s["values"] for s in group_payload["variesBy"]["specifications"]}
    assert specs == {"Colour": ["Red", "Blue"], "Size": ["S", "L"]}
    assert group_payload["variesBy"]["aspectsImageVariesBy"] == ["Colour"]
    assert group_payload["aspects"] == {"Brand": ["Unbranded"], "Material": ["Cotton"]}

    # each inventory item carries its own varying aspects + the shared ones
    item_payloads = [jsonlib.loads(c.request.content) for c in routes["item"].calls]
    aspect_sets = [p["product"]["aspects"] for p in item_payloads]
    assert {
        "Colour": ["Red"], "Size": ["S"], "Brand": ["Unbranded"], "Material": ["Cotton"],
    } in aspect_sets

    # each variant offer is priced independently (cost 5.00/6.00/6.50 -> round up to x.99)
    offer_prices = {
        jsonlib.loads(c.request.content)["pricingSummary"]["price"]["value"]
        for c in routes["offer"].calls
    }
    assert offer_prices == {"7.99", "9.99", "10.99"}


@respx.mock
def test_publish_variations_publish_returns_listing_id():
    product = make_variant_product()
    routes = mock_variation_pipeline(make_sku(product.source_id))
    publisher, _ = make_publisher()
    result = publisher.publish_variations(product, variant_pricings(product), publish=True)
    assert result.listing_id == "LIST-VAR-1"
    assert routes["publish"].call_count == 1
    payload = jsonlib.loads(routes["publish"].calls.last.request.content)
    assert payload["inventoryItemGroupKey"] == make_sku(product.source_id)
    assert payload["marketplaceId"] == "EBAY_GB"


@respx.mock
def test_publish_variations_all_below_floor_raises():
    product = make_variant_product()
    # a thin 5% target margin puts every variant below the 20% floor
    pricings = [(v, price(v.source_price, margin=Decimal("0.05"))) for v in product.variants]
    publisher, _ = make_publisher()
    with pytest.raises(ValueError, match="floor"):
        publisher.publish_variations(product, pricings)


@respx.mock
def test_publish_variations_no_varying_aspect_raises():
    # all variants share identical attributes -> not a real variation
    product = make_variant_product()
    for v in product.variants:
        v.attributes = {"Colour": "Red", "Size": "S"}
    publisher, _ = make_publisher()
    with pytest.raises(ValueError, match="single SKU"):
        publisher.publish_variations(product, variant_pricings(product))


# ---------------------------------------------------------------- delete


@respx.mock
def test_delete_single_sku_draft():
    sku = "LF-ABC-1234"
    # not a group
    respx.get(f"{SANDBOX}/sell/inventory/v1/inventory_item_group/{sku}").respond(
        404, json={"errors": [{"errorId": 25710, "message": "not found"}]}
    )
    offers = respx.get(f"{SANDBOX}/sell/inventory/v1/offer").respond(
        200, json={"offers": [{"offerId": "OFF-1", "status": "UNPUBLISHED"}]}
    )
    del_offer = respx.delete(f"{SANDBOX}/sell/inventory/v1/offer/OFF-1").respond(204)
    del_item = respx.delete(f"{SANDBOX}/sell/inventory/v1/inventory_item/{sku}").respond(204)
    publisher, _ = make_publisher()
    summary = publisher.delete_listing(sku)
    assert offers.called and del_offer.called and del_item.called
    assert summary["offers_deleted"] == 1
    assert summary["items_deleted"] == 1
    assert summary["withdrawn"] is False  # draft — nothing to end


@respx.mock
def test_delete_published_single_sku_withdraws_first():
    sku = "LF-ABC-1234"
    respx.get(f"{SANDBOX}/sell/inventory/v1/inventory_item_group/{sku}").respond(404)
    respx.get(f"{SANDBOX}/sell/inventory/v1/offer").respond(
        200, json={"offers": [{"offerId": "OFF-1", "status": "PUBLISHED"}]}
    )
    withdraw = respx.post(f"{SANDBOX}/sell/inventory/v1/offer/OFF-1/withdraw").respond(200)
    respx.delete(f"{SANDBOX}/sell/inventory/v1/offer/OFF-1").respond(204)
    respx.delete(f"{SANDBOX}/sell/inventory/v1/inventory_item/{sku}").respond(204)
    publisher, _ = make_publisher()
    summary = publisher.delete_listing(sku)
    assert withdraw.called
    assert summary["withdrawn"] is True


@respx.mock
def test_delete_variation_group():
    group_key = "LF-GRP-9999"
    respx.get(f"{SANDBOX}/sell/inventory/v1/inventory_item_group/{group_key}").respond(
        200, json={"variantSKUs": [f"{group_key}-RED", f"{group_key}-BLUE"]}
    )
    wd = respx.post(
        f"{SANDBOX}/sell/inventory/v1/offer/withdraw_by_inventory_item_group"
    ).respond(200, json={"listingId": "L1"})
    respx.get(f"{SANDBOX}/sell/inventory/v1/offer").respond(
        200, json={"offers": [{"offerId": "OFF-X", "status": "PUBLISHED"}]}
    )
    respx.delete(url__regex=rf"{SANDBOX}/sell/inventory/v1/offer/.+").respond(204)
    respx.delete(
        url__regex=rf"{SANDBOX}/sell/inventory/v1/inventory_item/{group_key}-.+"
    ).respond(204)
    del_group = respx.delete(
        f"{SANDBOX}/sell/inventory/v1/inventory_item_group/{group_key}"
    ).respond(204)
    publisher, _ = make_publisher()
    summary = publisher.delete_listing(group_key)
    assert wd.called
    assert summary["withdrawn"] is True
    assert summary["items_deleted"] == 2  # two variant inventory items
    assert summary["group"] is True
    assert del_group.called


@respx.mock
def test_delete_already_gone_is_idempotent():
    sku = "LF-ABC-1234"
    respx.get(f"{SANDBOX}/sell/inventory/v1/inventory_item_group/{sku}").respond(404)
    respx.get(f"{SANDBOX}/sell/inventory/v1/offer").respond(404)
    respx.delete(f"{SANDBOX}/sell/inventory/v1/inventory_item/{sku}").respond(404)
    publisher, _ = make_publisher()
    summary = publisher.delete_listing(sku)
    assert summary == {
        "withdrawn": False, "offers_deleted": 0, "items_deleted": 0, "group": False,
    }
