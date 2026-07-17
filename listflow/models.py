"""Pydantic data contracts: Product, Variant, ImageAsset, PricingResult (spec §4),
plus the loose RawProduct intermediate the extractors emit and the SourcePlatform enum.

Implemented in Phase 1. Pure logic — no I/O imports allowed in this module.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, Field, HttpUrl


class SourcePlatform(StrEnum):
    ALIEXPRESS = "aliexpress"
    AMAZON = "amazon"


def _no_float_money(value: object) -> object:
    # Hard rule (CLAUDE.md): money is Decimal, never float — fail loudly at the boundary.
    if isinstance(value, float):
        raise ValueError("money must be Decimal (or str), never float")
    return value


# Decimal that refuses silent float coercion; use for every money/ratio field.
Money = Annotated[Decimal, BeforeValidator(_no_float_money)]


class ImageAsset(BaseModel):
    source_url: HttpUrl
    ebay_url: HttpUrl | None = None  # filled after Media API upload (Phase 4)
    width: int | None = None
    height: int | None = None


class Variant(BaseModel):
    sku_suffix: str  # e.g. "RED-XL"
    attributes: dict[str, str]  # {"Colour": "Red", "Size": "XL"}
    source_price: Money
    stock: int
    image: ImageAsset | None = None


class Product(BaseModel):
    source_platform: Literal["aliexpress", "amazon"]
    source_url: HttpUrl
    source_id: str  # AliExpress productId / Amazon ASIN
    title_raw: str
    title_ebay: str = ""  # <=80 chars, cleaned (content.py fills)
    description_html: str
    bullet_points: list[str] = Field(default_factory=list)
    images: list[ImageAsset] = Field(min_length=1)  # main first; min 1, target >=4
    variants: list[Variant] = Field(default_factory=list)  # empty => single-SKU
    base_cost: Money  # cheapest variant or single price, GBP
    currency: str
    weight_grams: int | None = None
    item_specifics: dict[str, str] = Field(default_factory=dict)  # brand, material, etc.
    extracted_at: datetime


class PricingResult(BaseModel):
    cost: Money  # supplier price incl. supplier shipping to buyer
    ebay_fees_est: Money  # final value fee ~12.8% + £0.30 (configurable)
    target_margin: Money  # default 0.20 (20% net) — from config
    sell_price: Money  # rounded to psychological .99
    net_profit_est: Money
    margin_actual: Money
    passes_floor: bool  # False => CLI warns loudly / refuses without --force


class RawVariant(BaseModel):
    """One SKU row as an extractor found it — attributes/price/stock may be partial."""

    attributes: dict[str, str] = Field(default_factory=dict)
    price: Money | None = None
    stock: int | None = None
    image_url: str | None = None


class RawProduct(BaseModel):
    """Loose intermediate emitted by extractors before normalisation (normalize.py).

    Deliberately forgiving: plain-string URLs, optional fields, best-effort attributes.
    Validation strictness lives in Product, at the normalise boundary.
    """

    source_platform: SourcePlatform
    source_url: str
    source_id: str
    title: str
    price: Money
    currency: str = "GBP"
    description_html: str = ""
    bullet_points: list[str] = Field(default_factory=list)
    image_urls: list[str] = Field(default_factory=list)
    variants: list[RawVariant] = Field(default_factory=list)
    attributes: dict[str, str] = Field(default_factory=dict)
    store_name: str | None = None  # logging only — must never enter a listing field
    weight_grams: int | None = None
    shipping_estimate: Money | None = None
    extracted_at: datetime | None = None
