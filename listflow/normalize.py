"""RawProduct -> Product normalisation into the single canonical schema (spec §2.1).

Implemented in Phase 5 alongside the Amazon extractor. Pure logic — no I/O.
"""

import re
from datetime import UTC, datetime

from listflow.content import map_item_specifics
from listflow.models import ImageAsset, Product, RawProduct, RawVariant, Variant

MAX_LISTING_IMAGES = 8  # spec §6.5: min 1, target 4-8


def _sku_suffix(variant: RawVariant, index: int) -> str:
    joined = "-".join(variant.attributes.values()).replace(" ", "-")
    cleaned = re.sub(r"[^A-Za-z0-9-]+", "", joined).upper().strip("-")
    return cleaned[:20] or f"V{index + 1}"


def normalize(raw: RawProduct) -> Product:
    """Map the loose extractor output onto the strict canonical Product.

    - Variants without a price are dropped (unusable for pricing).
    - Unknown variant stock is treated as in-stock (stock=1).
    - base_cost = cheapest in-stock variant, falling back to the page price.
    - Images are capped at the listing target; store_name never enters the Product.
    """
    variants: list[Variant] = []
    for index, raw_variant in enumerate(raw.variants):
        if raw_variant.price is None:
            continue
        variants.append(
            Variant(
                sku_suffix=_sku_suffix(raw_variant, index),
                attributes=raw_variant.attributes,
                source_price=raw_variant.price,
                stock=raw_variant.stock if raw_variant.stock is not None else 1,
                image=ImageAsset(source_url=raw_variant.image_url)
                if raw_variant.image_url
                else None,
            )
        )

    in_stock_prices = [v.source_price for v in variants if v.stock > 0]
    base_cost = min(in_stock_prices) if in_stock_prices else raw.price

    return Product(
        source_platform=raw.source_platform.value,
        source_url=raw.source_url,
        source_id=raw.source_id,
        title_raw=raw.title,
        description_html=raw.description_html,
        bullet_points=raw.bullet_points,
        images=[ImageAsset(source_url=url) for url in raw.image_urls[:MAX_LISTING_IMAGES]],
        variants=variants,
        base_cost=base_cost,
        currency=raw.currency,
        weight_grams=raw.weight_grams,
        item_specifics=map_item_specifics(raw.attributes),
        extracted_at=raw.extracted_at or datetime.now(UTC),
    )
