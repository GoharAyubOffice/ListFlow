"""RawProduct -> Product normalisation tests. Phase 5."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from listflow.models import RawProduct, RawVariant, SourcePlatform
from listflow.normalize import MAX_LISTING_IMAGES, normalize

D = Decimal


def make_raw(**overrides) -> RawProduct:
    base = {
        "source_platform": SourcePlatform.AMAZON,
        "source_url": "https://www.amazon.co.uk/dp/B00BAGTNAQ",
        "source_id": "B00BAGTNAQ",
        "title": "ChomChom Pet Hair Remover",
        "price": D("13.99"),
        "currency": "GBP",
        "description_html": "<p>Removes pet hair.</p>",
        "bullet_points": ["Reusable", "No batteries"],
        "image_urls": ["https://m.media-amazon.com/images/I/1.jpg"],
        "attributes": {"colour": "White", "material": "Plastic"},
        "store_name": "ChomChom Roller Store",
        "extracted_at": datetime.now(UTC),
    }
    base.update(overrides)
    return RawProduct(**base)


def test_single_sku_product():
    product = normalize(make_raw())
    assert product.source_platform == "amazon"
    assert product.source_id == "B00BAGTNAQ"
    assert product.title_raw == "ChomChom Pet Hair Remover"
    assert product.title_ebay == ""  # content.py fills this later
    assert product.variants == []
    assert product.base_cost == D("13.99")
    assert product.currency == "GBP"
    assert str(product.images[0].source_url) == "https://m.media-amazon.com/images/I/1.jpg"


def test_item_specifics_aliased_with_brand_default():
    product = normalize(make_raw(attributes={"colour": "White"}))
    assert product.item_specifics == {"Colour": "White", "Brand": "Unbranded"}


def test_store_name_never_reaches_listing_fields():
    product = normalize(make_raw())
    dumped = product.model_dump_json()
    assert "ChomChom Roller Store" not in dumped


def test_variants_cheapest_in_stock_becomes_base_cost():
    variants = [
        RawVariant(attributes={"Colour": "Red"}, price=D("5.99"), stock=0),  # out of stock
        RawVariant(attributes={"Colour": "Blue"}, price=D("6.99"), stock=3),
        RawVariant(attributes={"Colour": "Green"}, price=D("9.99")),  # stock unknown
    ]
    product = normalize(make_raw(variants=variants))
    assert len(product.variants) == 3
    assert product.base_cost == D("6.99")


def test_variants_all_out_of_stock_falls_back_to_raw_price():
    variants = [RawVariant(attributes={"Colour": "Red"}, price=D("5.99"), stock=0)]
    product = normalize(make_raw(variants=variants))
    assert product.base_cost == D("13.99")


def test_variant_without_price_is_dropped():
    variants = [RawVariant(attributes={"Colour": "Red"})]
    product = normalize(make_raw(variants=variants))
    assert product.variants == []


def test_sku_suffix_generated_from_attributes():
    variants = [RawVariant(attributes={"Colour": "Red", "Size": "XL"}, price=D("5.00"), stock=1)]
    product = normalize(make_raw(variants=variants))
    assert product.variants[0].sku_suffix == "RED-XL"


def test_sku_suffix_fallback_for_empty_attributes():
    variants = [RawVariant(attributes={}, price=D("5.00"), stock=1)]
    product = normalize(make_raw(variants=variants))
    assert product.variants[0].sku_suffix == "V1"


def test_images_capped_at_listing_target():
    urls = [f"https://m.media-amazon.com/images/I/{i}.jpg" for i in range(20)]
    product = normalize(make_raw(image_urls=urls))
    assert len(product.images) == MAX_LISTING_IMAGES


def test_no_images_fails_loudly():
    with pytest.raises(ValidationError):
        normalize(make_raw(image_urls=[]))


def test_missing_extracted_at_gets_now():
    product = normalize(make_raw(extracted_at=None))
    assert product.extracted_at is not None
