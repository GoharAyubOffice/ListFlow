"""Orchestration shared by the `import` and `retry` CLI commands.

`prepare()` runs the pure, offline half of the pipeline (detect → extract →
normalize → clean → describe → validate → price) and returns everything needed to
either print a dry-run or drive the publisher. Kept separate from cli.py so the logic
is unit-testable without Typer, and `prepare_from_raw()` is fully network-free.
"""

import logging
from dataclasses import dataclass
from decimal import Decimal

from listflow.config import Settings
from listflow.content import (
    build_description,
    clean_title,
    strip_forbidden_content,
    validate_forbidden,
)
from listflow.detector import detect
from listflow.models import Product, RawProduct, SourcePlatform, Variant
from listflow.normalize import normalize
from listflow.pricing import PricingResult, price

logger = logging.getLogger(__name__)


class VariantError(ValueError):
    """The --variant selector matched no variant (or the product has none)."""


@dataclass
class Prepared:
    platform: SourcePlatform
    raw: RawProduct
    product: Product
    pricing: PricingResult
    store_name: str | None


def _parse_variant_selector(selector: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for chunk in selector.split(","):
        if "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        key, value = key.strip(), value.strip()
        if key and value:
            pairs[key.lower()] = value.lower()
    return pairs


def select_variant(product: Product, selector: str) -> Variant:
    """Find the variant whose attributes match every key=value in the selector."""
    if not product.variants:
        raise VariantError(
            f"cannot select --variant {selector!r}: this product has no variants"
        )
    wanted = _parse_variant_selector(selector)
    if not wanted:
        raise VariantError(f"could not parse --variant {selector!r} (use Colour=Red,Size=XL)")
    for variant in product.variants:
        lowered = {k.lower(): v.lower() for k, v in variant.attributes.items()}
        if all(lowered.get(k) == v for k, v in wanted.items()):
            return variant
    available = "; ".join(
        ", ".join(f"{k}={v}" for k, v in variant.attributes.items())
        for variant in product.variants
    )
    raise VariantError(
        f"no variant matches {selector!r}. Available: {available}"
    )


def prepare_from_raw(
    raw: RawProduct,
    *,
    settings: Settings,
    margin: Decimal | None = None,
    variant: str | None = None,
    primary_keyword: str | None = None,
) -> Prepared:
    """Pure half of the pipeline: RawProduct → validated, priced Product."""
    product = normalize(raw)

    if variant:
        chosen = select_variant(product, variant)
        product.base_cost = chosen.source_price
        product.item_specifics.update(chosen.attributes)

    product.title_ebay = clean_title(product.title_raw, primary_keyword=primary_keyword)

    # Strip supplier noise (platform tokens like "visit our Amazon store") from bullets
    # and specifics before building the description. The brand — which Amazon exposes as
    # a "store name" — is legitimate in a listing, so it is NOT treated as forbidden;
    # only the platform/dropship tokens are. build_description drops cross-sell sentences.
    for dropped in strip_forbidden_content(product):
        logger.info("dropped forbidden content: %s", dropped)

    product.description_html = build_description(product, boilerplate=settings.boilerplate)
    validate_forbidden(product)

    pricing = price(
        product.base_cost,
        margin=margin if margin is not None else settings.margin,
        fvf_rate=settings.fvf_rate,
        fixed_fee=settings.fixed_fee,
    )
    return Prepared(
        platform=raw.source_platform,
        raw=raw,
        product=product,
        pricing=pricing,
        store_name=raw.store_name,
    )


def get_extractor(platform: SourcePlatform, *, headed: bool = False):
    """Construct the right extractor (imported lazily to keep startup fast)."""
    if platform is SourcePlatform.AMAZON:
        from listflow.extractors.amazon import AmazonExtractor

        return AmazonExtractor()
    from listflow.extractors.aliexpress import AliExpressExtractor

    return AliExpressExtractor(headed=headed)


def prepare(
    url: str,
    *,
    settings: Settings,
    margin: Decimal | None = None,
    variant: str | None = None,
    headed: bool = False,
    primary_keyword: str | None = None,
) -> Prepared:
    """Full pipeline including the live extraction (network)."""
    platform = detect(url)
    logger.info("detected platform: %s", platform.value)
    extractor = get_extractor(platform, headed=headed)
    raw = extractor.extract(url)
    return prepare_from_raw(
        raw,
        settings=settings,
        margin=margin,
        variant=variant,
        primary_keyword=primary_keyword,
    )
