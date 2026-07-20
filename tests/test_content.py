"""Content-rule tests: 80-char boundary, forbidden tokens, emoji removal,
HTML sanitisation (spec §9.1). Phase 1 — written before content.py logic.
"""

import re
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from listflow.content import (
    EBAY_TITLE_LIMIT,
    ForbiddenTokenError,
    build_description,
    clean_title,
    map_item_specifics,
    strip_forbidden_content,
    validate_forbidden,
)
from listflow.models import Product, Variant


def make_product(**overrides) -> Product:
    base = {
        "source_platform": "aliexpress",
        "source_url": "https://www.aliexpress.com/item/100500.html",
        "source_id": "100500",
        "title_raw": "Pet Hair Remover Brush",
        "title_ebay": "Pet Hair Remover Brush",
        "description_html": "<p>Removes pet hair fast.</p>",
        "bullet_points": ["Easy to clean", "Works on sofas"],
        "images": [{"source_url": "https://img.example.com/1.jpg", "width": 800, "height": 800}],
        "variants": [],
        "base_cost": Decimal("3.50"),
        "currency": "GBP",
        "weight_grams": 120,
        "item_specifics": {"Colour": "Blue"},
        "extracted_at": datetime.now(UTC),
    }
    base.update(overrides)
    return Product(**base)


# ---------------------------------------------------------------- clean_title

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Hot Sale Pet Hair Remover Free Shipping", "Pet Hair Remover"),
        ("2026 New Dog Grooming Brush Dropshipping", "Dog Grooming Brush"),
        ("Pet Brush - Free Shipping - Hot Sale", "Pet Brush"),
        ("AliExpress Pet Brush Amazon Choice", "Pet Brush"),
        ("\U0001f525\U0001f525 Pet Hair Remover \U0001f43e", "Pet Hair Remover"),
        ("Dog Bowl ⭐ Premium", "Dog Bowl Premium"),
        ("AMAZING QUALITY Dog Brush USB LED", "Amazing Quality Dog Brush USB LED"),
        ("  Pet   Hair    Remover  ", "Pet Hair Remover"),
        ("", ""),
    ],
    ids=[
        "noise-tokens",
        "year-new-and-dropshipping",
        "stranded-separators",
        "platform-tokens-stripped",
        "emoji-stripped",
        "star-symbol-stripped",
        "allcaps-runs-titlecased-acronyms-kept",
        "whitespace-collapsed",
        "empty-input",
    ],
)
def test_clean_title_cases(raw, expected):
    assert clean_title(raw) == expected


def test_clean_title_truncates_at_last_full_word():
    raw = (
        "Professional Double Sided Stainless Steel Pet Grooming "
        "Brush for Long Haired Dogs and Cats"
    )
    assert len(raw) > EBAY_TITLE_LIMIT
    result = clean_title(raw)
    assert len(result) <= EBAY_TITLE_LIMIT
    assert raw.startswith(result)
    assert raw[len(result)] == " "  # cut exactly at a word boundary


def test_clean_title_exactly_80_chars_untouched():
    raw = "b" * EBAY_TITLE_LIMIT
    assert clean_title(raw) == raw


def test_clean_title_single_long_word_hard_truncated():
    assert len(clean_title("x" * 100)) == EBAY_TITLE_LIMIT


def test_clean_title_mid_word_cut_falls_back_to_previous_word():
    raw = "a" * 70 + " " + "b" * 20
    assert clean_title(raw) == "a" * 70


def test_clean_title_front_loads_primary_keyword():
    result = clean_title("Soft Grooming Brush for Dogs", primary_keyword="Pet Hair Remover")
    assert result == "Pet Hair Remover Soft Grooming Brush for Dogs"


def test_clean_title_front_load_dedupes_existing_keyword():
    result = clean_title("Soft pet hair remover Brush", primary_keyword="Pet Hair Remover")
    assert result.startswith("Pet Hair Remover")
    assert len(re.findall("pet hair remover", result, flags=re.IGNORECASE)) == 1


# --------------------------------------------------------- validate_forbidden

def test_validate_forbidden_passes_on_clean_product():
    validate_forbidden(make_product())  # must not raise


@pytest.mark.parametrize(
    ("overrides", "token", "field_hint"),
    [
        ({"title_ebay": "Amazon Basics Dog Bowl"}, "amazon", "title_ebay"),
        ({"description_html": "<p>Shipped via AliExpress</p>"}, "aliexpress", "description_html"),
        ({"description_html": "<p>ALIEXPRESS bargain</p>"}, "aliexpress", "description_html"),
        ({"bullet_points": ["Durable", "As seen on Alibaba"]}, "alibaba", "bullet_points[1]"),
        ({"item_specifics": {"Brand": "Dropshipping Co"}}, "dropship", "item_specifics"),
        ({"title_ebay": "AmazonBasics Bowl"}, "amazon", "title_ebay"),
    ],
    ids=["amazon", "aliexpress", "case-insensitive", "alibaba", "dropshipping", "brand-prefix"],
)
def test_validate_forbidden_raises(overrides, token, field_hint):
    with pytest.raises(ForbiddenTokenError) as excinfo:
        validate_forbidden(make_product(**overrides))
    assert token in str(excinfo.value).lower()
    assert field_hint in str(excinfo.value)


def test_validate_forbidden_choice_is_word_bounded():
    with pytest.raises(ForbiddenTokenError):
        validate_forbidden(make_product(title_ebay="Choice Pet Brush"))
    # "choices" as an ordinary English word is fine
    validate_forbidden(make_product(bullet_points=["Many choices available"]))


def test_validate_forbidden_checks_variant_attributes():
    variant = Variant(
        sku_suffix="RED",
        attributes={"Colour": "AliExpress Red"},
        source_price=Decimal("2.10"),
        stock=5,
        image=None,
    )
    with pytest.raises(ForbiddenTokenError) as excinfo:
        validate_forbidden(make_product(variants=[variant]))
    assert "variants[0]" in str(excinfo.value)


def test_strip_forbidden_content_drops_bullets_and_specifics():
    product = make_product(
        bullet_points=["Soft and durable", "Visit our Amazon store for more"],
        item_specifics={"Colour": "Blue", "Seller": "AliExpress Direct"},
    )
    dropped = strip_forbidden_content(product)
    assert product.bullet_points == ["Soft and durable"]
    assert product.item_specifics == {"Colour": "Blue"}
    assert len(dropped) == 2
    # after stripping, the product passes validation
    product.description_html = "<p>Clean copy</p>"
    validate_forbidden(product)


def test_strip_forbidden_content_leaves_clean_product_untouched():
    product = make_product()
    before_bullets = list(product.bullet_points)
    dropped = strip_forbidden_content(product)
    assert dropped == []
    assert product.bullet_points == before_bullets


def test_strip_forbidden_content_uses_extra_tokens():
    product = make_product(bullet_points=["From SuperPetStore warehouse"])
    assert strip_forbidden_content(product, extra_forbidden=["SuperPetStore"])
    assert product.bullet_points == []


def test_validate_forbidden_extra_tokens_catch_store_names():
    product = make_product(description_html="<p>Direct from SuperPetStore warehouse</p>")
    validate_forbidden(product)  # store name unknown -> passes
    with pytest.raises(ForbiddenTokenError):
        validate_forbidden(product, extra_forbidden=["SuperPetStore"])


# ----------------------------------------------------------- build_description

def test_build_description_structure():
    out = build_description(make_product(), boilerplate="<p>Fast dispatch from the UK.</p>")
    assert "<p><b>Pet Hair Remover Brush</b></p>" in out
    assert "<p>Removes pet hair fast.</p>" in out
    assert "<li>Easy to clean</li>" in out
    assert "<li>Works on sofas</li>" in out
    assert "<li><b>Colour:</b> Blue</li>" in out
    assert out.rstrip().endswith("<p>Fast dispatch from the UK.</p>")


def test_build_description_only_allowed_tags():
    product = make_product(
        description_html=(
            "<div><table><tr><td>Spec one</td></tr></table>"
            "<script>evil()</script><iframe src='http://x'></iframe></div>"
        )
    )
    out = build_description(product, boilerplate="<p>Ships <i>fast</i> <em>today</em></p>")
    tags = set(re.findall(r"</?\s*([a-zA-Z0-9]+)", out))
    assert tags <= {"p", "ul", "li", "b", "br"}


def test_build_description_drops_forbidden_sentences():
    product = make_product(
        description_html=(
            "<p>Ultra soft microfibre.</p>"
            "<p>Search 'BrandX' on Amazon for more.</p>"
            "<p>Machine washable.</p>"
        )
    )
    out = build_description(product)
    assert "amazon" not in out.lower()
    assert "Ultra soft microfibre" in out
    assert "Machine washable" in out


def test_build_description_script_content_never_survives():
    product = make_product(
        description_html='<p>Good product</p><script>alert("steal")</script>'
    )
    out = build_description(product)
    assert "script" not in out.lower()
    assert "alert" not in out
    assert "Good product" in out


def test_build_description_attributes_stripped():
    product = make_product(description_html='<p onclick="steal()">Nice bowl</p>')
    out = build_description(product)
    assert "onclick" not in out
    assert "Nice bowl" in out


def test_build_description_escapes_text():
    product = make_product(description_html="<p>Tom & Jerry's bowl</p>")
    out = build_description(product)
    assert "&amp;" in out
    assert "&#x27;" in out


def test_build_description_drops_supplier_image_hotlinks():
    product = make_product(
        description_html='<p>See photo <img src="https://ae01.alicdn.com/kf/x.jpg"></p>'
    )
    out = build_description(product)
    assert "alicdn" not in out
    assert "<img" not in out


# --------------------------------------------------------- map_item_specifics

def test_map_item_specifics_aliases_and_brand_default():
    assert map_item_specifics({"color": "Red"}) == {"Colour": "Red", "Brand": "Unbranded"}
    result = map_item_specifics({"colour": "Blue", "material": "Steel"})
    assert result == {"Colour": "Blue", "Material": "Steel", "Brand": "Unbranded"}


def test_map_item_specifics_keeps_existing_brand():
    assert map_item_specifics({"brand": "Acme"}) == {"Brand": "Acme"}
    assert map_item_specifics({"Brand Name": "Acme"}) == {"Brand": "Acme"}


def test_map_item_specifics_unknown_keys_titlecased():
    result = map_item_specifics({"handle type": "Ergonomic"})
    assert result["Handle Type"] == "Ergonomic"


def test_map_item_specifics_trims_and_drops_empty():
    result = map_item_specifics({" size ": " XL ", "material": "   "})
    assert result == {"Size": "XL", "Brand": "Unbranded"}


def test_map_item_specifics_drops_noise_aspects():
    raw = {
        "Colour": "Red",
        "Material": "Cotton",
        "Origin": "Mainland China",
        "Cn": "Hebei",
        "High-Concerned Chemical": "None",
        "Set Type": "Yes",
        "Disposable": "No",
        "Whether Terry Fabric": "No",
        "Product Application Scenarios": "Toilet",
    }
    result = map_item_specifics(raw)
    assert result == {"Colour": "Red", "Material": "Cotton", "Brand": "Unbranded"}


def test_map_item_specifics_drops_none_values_anywhere():
    result = map_item_specifics({"Pattern": "None", "Style": "Modern"})
    assert "Pattern" not in result
    assert result["Style"] == "Modern"


def test_map_item_specifics_keeps_useful_towel_aspects():
    raw = {"Technics": "Woven", "Features": "Machine Washable", "Type": "Bath Towel"}
    result = map_item_specifics(raw)
    assert result["Technics"] == "Woven"
    assert result["Features"] == "Machine Washable"
    assert result["Type"] == "Bath Towel"
