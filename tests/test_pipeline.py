"""Pipeline orchestration tests: prepare_from_raw (normalize → clean → describe →
validate → price) and variant selection. Pure logic, no network. Phase 7."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from listflow.config import Settings
from listflow.models import RawProduct, RawVariant, SourcePlatform
from listflow.pipeline import VariantError, prepare_from_raw, select_variant

D = Decimal


def make_raw(**overrides) -> RawProduct:
    base = {
        "source_platform": SourcePlatform.AMAZON,
        "source_url": "https://www.amazon.co.uk/dp/B00BAGTNAQ",
        "source_id": "B00BAGTNAQ",
        "title": "Hot Sale Pet Hair Remover Brush Free Shipping",
        "price": D("10.00"),
        "currency": "GBP",
        "description_html": "<p>Removes pet hair fast.</p>",
        "bullet_points": ["Reusable", "No batteries"],
        "image_urls": ["https://m.media-amazon.com/images/I/1.jpg"],
        "attributes": {"colour": "White", "material": "Plastic"},
        "store_name": "ChomChom Roller Store",
        "extracted_at": datetime.now(UTC),
    }
    base.update(overrides)
    return RawProduct(**base)


def settings(**overrides) -> Settings:
    base = {"ebay_client_id": "a", "ebay_client_secret": "b", "boilerplate": "<p>Ships fast.</p>"}
    base.update(overrides)
    return Settings(**base)


def test_prepare_from_raw_full_pipeline():
    prepared = prepare_from_raw(make_raw(), settings=settings())
    product = prepared.product
    assert product.title_ebay == "Pet Hair Remover Brush"  # noise stripped
    assert "<b>Pet Hair Remover Brush</b>" in product.description_html
    assert product.description_html.rstrip().endswith("<p>Ships fast.</p>")  # boilerplate
    assert product.item_specifics == {
        "Colour": "White",
        "Material": "Plastic",
        "Brand": "Unbranded",
    }
    assert prepared.pricing.sell_price == D("15.99")
    assert prepared.pricing.passes_floor is True
    assert prepared.platform is SourcePlatform.AMAZON
    assert prepared.store_name == "ChomChom Roller Store"


def test_prepare_drops_platform_token_description_sentence():
    # a supplier cross-sell sentence is dropped from the description, not hard-failed
    raw = make_raw(
        description_html="<p>Super soft towels.</p><p>Shipped via Amazon Prime.</p>"
    )
    prepared = prepare_from_raw(raw, settings=settings())
    assert "amazon" not in prepared.product.description_html.lower()
    assert "Super soft towels" in prepared.product.description_html


def test_prepare_strips_forbidden_bullet_and_succeeds():
    # a supplier-branded bullet is noise — dropped, not a hard failure (real E1 case)
    raw = make_raw(
        bullet_points=["Reusable and soft", "Please visit our Amazon Official store"]
    )
    prepared = prepare_from_raw(raw, settings=settings())
    assert prepared.product.bullet_points == ["Reusable and soft"]
    assert "amazon" not in prepared.product.description_html.lower()


def test_prepare_strips_forbidden_item_specific():
    raw = make_raw(attributes={"colour": "White", "seller": "AliExpress Direct"})
    prepared = prepare_from_raw(raw, settings=settings())
    assert prepared.product.item_specifics.get("Colour") == "White"
    assert all(
        "aliexpress" not in v.lower() for v in prepared.product.item_specifics.values()
    )


def test_prepare_keeps_brand_name_in_content():
    # the brand (Amazon exposes it as store_name) is legitimate — not stripped/failed
    raw = make_raw(
        title="Aileem Pet Brush",
        description_html="<p>Aileem brushes are gentle.</p>",
        store_name="Aileem",
    )
    prepared = prepare_from_raw(raw, settings=settings())
    assert "Aileem" in prepared.product.title_ebay
    assert "Aileem" in prepared.product.description_html


def test_prepare_margin_override():
    prepared = prepare_from_raw(make_raw(), settings=settings(), margin=D("0.30"))
    assert prepared.pricing.target_margin == D("0.30")
    assert prepared.pricing.sell_price > D("15.99")


def test_prepare_below_floor_flag_but_no_raise():
    # heavy cost relative to a low forced margin -> passes_floor False, still returns
    prepared = prepare_from_raw(make_raw(), settings=settings(), margin=D("0.05"))
    assert prepared.pricing.passes_floor is False


def test_prepare_primary_keyword_front_loads():
    prepared = prepare_from_raw(
        make_raw(title="Grooming Brush for Dogs"),
        settings=settings(),
        primary_keyword="Pet Hair Remover",
    )
    assert prepared.product.title_ebay.startswith("Pet Hair Remover")


# ---------------------------------------------------------- variant select

def variant_raw() -> RawProduct:
    return make_raw(
        variants=[
            RawVariant(attributes={"Colour": "Red", "Size": "S"}, price=D("8.00"), stock=5),
            RawVariant(attributes={"Colour": "Blue", "Size": "L"}, price=D("6.00"), stock=3),
            RawVariant(attributes={"Colour": "Blue", "Size": "S"}, price=D("7.00"), stock=0),
        ]
    )


def test_default_base_cost_is_cheapest_in_stock():
    prepared = prepare_from_raw(variant_raw(), settings=settings())
    assert prepared.product.base_cost == D("6.00")  # cheapest in stock


def test_select_variant_matches_and_sets_cost():
    prepared = prepare_from_raw(variant_raw(), settings=settings(), variant="Colour=Red,Size=S")
    assert prepared.product.base_cost == D("8.00")
    assert prepared.product.item_specifics["Colour"] == "Red"
    assert prepared.product.item_specifics["Size"] == "S"


def test_select_variant_case_insensitive():
    prepared = prepare_from_raw(variant_raw(), settings=settings(), variant="colour=blue,size=l")
    assert prepared.product.base_cost == D("6.00")


def test_select_variant_no_match_lists_options():
    with pytest.raises(VariantError) as excinfo:
        prepare_from_raw(variant_raw(), settings=settings(), variant="Colour=Green")
    assert "Colour=Red" in str(excinfo.value) or "Red" in str(excinfo.value)


def test_select_variant_helper_on_product_directly():
    prepared = prepare_from_raw(variant_raw(), settings=settings())
    chosen = select_variant(prepared.product, "Colour=Blue,Size=L")
    assert chosen.source_price == D("6.00")


def test_select_variant_on_single_sku_errors():
    with pytest.raises(VariantError, match="no variants"):
        prepare_from_raw(make_raw(), settings=settings(), variant="Colour=Red")


def test_prepare_prices_in_stock_variants_for_variations():
    prepared = prepare_from_raw(variant_raw(), settings=settings())
    # variant_raw has 3 variants; the Blue/S one is out of stock and excluded
    assert prepared.variant_count == 2
    assert all(o.variant.stock > 0 for o in prepared.variant_offers)
    # each variant is priced independently off its own source price
    by_cost = {o.variant.source_price: o.pricing.sell_price for o in prepared.variant_offers}
    assert set(by_cost) == {D("8.00"), D("6.00")}
    assert all(sell % 1 == D("0.99") for sell in by_cost.values())


def test_single_sku_product_has_no_variant_offers():
    prepared = prepare_from_raw(make_raw(), settings=settings())
    assert prepared.variant_offers == []
    assert prepared.variant_count == 0
