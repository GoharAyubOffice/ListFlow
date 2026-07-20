"""Publish pipeline: location -> images -> category -> inventory item -> offer ->
[publish] with per-step resumable state for `listflow retry <sku>` (spec §7.2).

Implemented in Phase 4. Expects a Product whose content has already been cleaned and
validated (title_ebay filled, forbidden tokens checked) and a PricingResult.
Step completion is reported through the on_step callback; storage.py (Phase 7)
persists it so `listflow retry` can resume from the failed step.
"""

import hashlib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from listflow import images as images_mod
from listflow.config import Settings
from listflow.ebay.client import EbayApiError, EbayClient
from listflow.ebay.taxonomy import suggest_category
from listflow.models import PricingResult, Product, Variant

logger = logging.getLogger(__name__)

LOCATION_KEY = "MAIN"
STEPS = ("location", "images", "category", "inventory_item", "offer", "publish")
# multi-variation publishing inserts an extra "inventory_group" step before "offer"
VARIATION_STEPS = (
    "location", "images", "category", "inventory_item", "inventory_group", "offer", "publish",
)


def _is_duplicate_offer_error(err: EbayApiError) -> bool:
    # eBay reuses errorId 25002 for many user errors — the message is the signal.
    return any(
        "already exists" in str(item.get("message", "")).lower() for item in err.errors
    )


def _analyze_variations(
    pairs: list[tuple[Variant, PricingResult]],
) -> tuple[dict[str, list[str]], dict[str, str], list[tuple[Variant, PricingResult]]]:
    """Split variant attributes into varying vs shared aspects (eBay variesBy).

    Only aspects present on EVERY variant are considered. Variants that duplicate the
    varying-aspect combination are dropped (eBay requires each combo to be unique).
    Returns (varying: aspect -> ordered distinct values, shared: aspect -> value,
    deduped pairs).
    """
    if not pairs:
        return {}, {}, []
    common = set.intersection(*[set(v.attributes) for v, _ in pairs])
    deduped: list[tuple[Variant, PricingResult]] = []
    seen: set[tuple] = set()
    for variant, pricing in pairs:
        combo = tuple(sorted((k, variant.attributes[k]) for k in common))
        if combo in seen:
            continue
        seen.add(combo)
        deduped.append((variant, pricing))
    varying: dict[str, list[str]] = {}
    shared: dict[str, str] = {}
    for key in sorted(common):
        values = list(dict.fromkeys(v.attributes[key] for v, _ in deduped))
        if len(values) > 1:
            varying[key] = values
        else:
            shared[key] = values[0]
    return varying, shared, deduped


def _image_varies_aspect(varying: dict[str, list[str]]) -> str | None:
    """The colour-like varying aspect that per-variant images key off, if any."""
    for key in varying:
        if "colour" in key.lower() or "color" in key.lower():
            return key
    return None


def _variant_sku(group_key: str, suffix: str, used: set[str]) -> str:
    """Unique per-variant SKU derived from the group key + attribute suffix (<=50 chars)."""
    base = re.sub(r"[^A-Za-z0-9-]", "", suffix).upper().strip("-") or "V"
    candidate = f"{group_key}-{base}"[:50]
    n = 1
    while candidate in used:
        candidate = f"{group_key}-{base}-{n}"[:50]
        n += 1
    used.add(candidate)
    return candidate


def make_sku(source_id: str) -> str:
    """Stable, traceable SKU: LF-{source_id[:12]}-{hash4} (spec §7.2 step 4)."""
    safe_id = re.sub(r"[^A-Za-z0-9]", "", source_id)[:12]
    digest = hashlib.sha1(source_id.encode()).hexdigest()[:4]
    return f"LF-{safe_id}-{digest}"


@dataclass
class PublishResult:
    sku: str
    offer_id: str | None = None
    listing_id: str | None = None
    category_id: str | None = None
    steps_completed: list[str] = field(default_factory=list)


class Publisher:
    """Drives the Inventory API pipeline; every eBay call goes through EbayClient."""

    def __init__(
        self,
        client: EbayClient,
        settings: Settings,
        on_step: Callable[[str, str], None] | None = None,
    ):
        self._client = client
        self._settings = settings
        self._on_step = on_step or (lambda _sku, _step: None)

    def ensure_location(self) -> None:
        """Create the MAIN inventory location once; later runs find it and move on."""
        try:
            self._client.get(f"/sell/inventory/v1/location/{LOCATION_KEY}")
            return
        except EbayApiError as err:
            if err.status_code != 404:
                raise
        # eBay rejects a country-only address; a real ship-from postcode + city is required.
        if not (self._settings.ship_from_postal_code and self._settings.ship_from_city):
            raise ValueError(
                "ship-from address is not configured — set ship_from_address_line1, "
                "ship_from_city and ship_from_postal_code in config.toml before publishing"
            )
        address = {
            "city": self._settings.ship_from_city,
            "postalCode": self._settings.ship_from_postal_code,
            "country": self._settings.ship_from_country,
        }
        if self._settings.ship_from_address_line1:
            address["addressLine1"] = self._settings.ship_from_address_line1
        logger.info("creating inventory location %r", LOCATION_KEY)
        self._client.post(
            f"/sell/inventory/v1/location/{LOCATION_KEY}",
            json={
                "name": "Listflow main location",
                "location": {"address": address},
                "merchantLocationStatus": "ENABLED",
                "locationTypes": ["WAREHOUSE"],
            },
        )

    def publish(
        self,
        product: Product,
        pricing: PricingResult,
        *,
        publish: bool = False,
        category_id: str | None = None,
        existing_offer_id: str | None = None,
    ) -> PublishResult:
        sku = make_sku(product.source_id)
        result = PublishResult(sku=sku)

        def done(step: str) -> None:
            result.steps_completed.append(step)
            self._on_step(sku, step)

        self.ensure_location()
        done("location")

        fetched = images_mod.fetch_images(product)
        images_mod.upload_images(self._client, fetched)
        image_urls = images_mod.listing_image_urls(fetched)
        done("images")

        title = product.title_ebay or product.title_raw
        result.category_id = category_id or suggest_category(self._client, title)
        done("category")

        self._client.put(
            f"/sell/inventory/v1/inventory_item/{sku}",
            json=self._inventory_payload(product, title, image_urls),
        )
        done("inventory_item")

        offer_payload = self._offer_payload(product, pricing, sku, result.category_id)
        if existing_offer_id:
            # retry path: the offer was already created on a previous run — update it
            # in place rather than POSTing a duplicate (eBay rejects two offers per SKU).
            self._client.put(f"/sell/inventory/v1/offer/{existing_offer_id}", json=offer_payload)
            result.offer_id = existing_offer_id
        else:
            try:
                offer_response = self._client.post(
                    "/sell/inventory/v1/offer", json=offer_payload
                )
                result.offer_id = offer_response.json()["offerId"]
            except EbayApiError as err:
                if not _is_duplicate_offer_error(err):
                    raise
                # a previous run already created the offer for this SKU — find and update
                result.offer_id = self._find_offer_id(sku)
                logger.info("offer for %s already exists (%s) — updating it", sku, result.offer_id)
                self._client.put(
                    f"/sell/inventory/v1/offer/{result.offer_id}", json=offer_payload
                )
        done("offer")

        if publish:
            publish_response = self._client.post(
                f"/sell/inventory/v1/offer/{result.offer_id}/publish"
            )
            result.listing_id = publish_response.json().get("listingId")
            done("publish")
            logger.info("offer %s published as listing %s", result.offer_id, result.listing_id)
        else:
            logger.info("offer %s left as draft (no --publish)", result.offer_id)
        return result

    def publish_variations(
        self,
        product: Product,
        variant_pricings: list[tuple[Variant, PricingResult]],
        *,
        publish: bool = False,
        category_id: str | None = None,
        force_below_floor: bool = False,
    ) -> PublishResult:
        """Publish all qualifying variants as one multi-variation listing via eBay's
        inventory_item_group API (buyer picks colour/size on a single listing)."""
        group_key = make_sku(product.source_id)
        result = PublishResult(sku=group_key)

        def done(step: str) -> None:
            result.steps_completed.append(step)
            self._on_step(group_key, step)

        usable = [
            (v, p) for v, p in variant_pricings if p.passes_floor or force_below_floor
        ]
        if not usable:
            raise ValueError(
                "no in-stock variant meets the 20% margin floor — nothing to list "
                "(publish a single SKU, or override the floor)"
            )
        varying, shared_attrs, usable = _analyze_variations(usable)
        if not varying:
            raise ValueError(
                "the variants do not differ by any consistent aspect — "
                "list a single SKU instead"
            )

        self.ensure_location()
        done("location")

        fetched = images_mod.fetch_images(product)
        images_mod.upload_images(self._client, fetched)
        gallery_urls = images_mod.listing_image_urls(fetched)
        done("images")

        title = product.title_ebay or product.title_raw
        result.category_id = category_id or suggest_category(self._client, title)
        done("category")

        # shared aspects = constant variant attributes + non-varying item specifics
        shared_aspects = {k: [v] for k, v in shared_attrs.items()}
        for key, value in product.item_specifics.items():
            if key not in varying:
                shared_aspects.setdefault(key, [value])

        used_skus: set[str] = set()
        variant_records: list[tuple[str, Variant, PricingResult]] = []
        have_variant_images = False
        for variant, pricing in usable:
            vsku = _variant_sku(group_key, variant.sku_suffix, used_skus)
            item_images = gallery_urls
            if variant.image is not None:
                got = images_mod.fetch_image_urls([str(variant.image.source_url)])
                if got:
                    images_mod.upload_images(self._client, got)
                    item_images = images_mod.listing_image_urls(got)
                    have_variant_images = True
            aspects = dict(shared_aspects)
            for key in varying:
                aspects[key] = [variant.attributes[key]]
            self._client.put(
                f"/sell/inventory/v1/inventory_item/{vsku}",
                json={
                    "availability": {
                        "shipToLocationAvailability": {"quantity": self._settings.max_qty}
                    },
                    "condition": "NEW",
                    "product": {
                        "title": title,
                        "aspects": aspects,
                        "imageUrls": item_images,
                    },
                },
            )
            variant_records.append((vsku, variant, pricing))
        done("inventory_item")

        varies_by: dict = {
            "specifications": [{"name": k, "values": v} for k, v in varying.items()]
        }
        if have_variant_images and (image_aspect := _image_varies_aspect(varying)):
            varies_by["aspectsImageVariesBy"] = [image_aspect]
        self._client.put(
            f"/sell/inventory/v1/inventory_item_group/{group_key}",
            json={
                "title": title,
                "description": product.description_html,
                "imageUrls": gallery_urls,
                "aspects": shared_aspects,
                "variantSKUs": [rec[0] for rec in variant_records],
                "variesBy": varies_by,
            },
        )
        done("inventory_group")

        offer_ids: list[str] = []
        for vsku, _variant, pricing in variant_records:
            payload = self._offer_payload(product, pricing, vsku, result.category_id)
            try:
                response = self._client.post("/sell/inventory/v1/offer", json=payload)
                offer_ids.append(response.json()["offerId"])
            except EbayApiError as err:
                if not _is_duplicate_offer_error(err):
                    raise
                offer_id = self._find_offer_id(vsku)
                self._client.put(f"/sell/inventory/v1/offer/{offer_id}", json=payload)
                offer_ids.append(offer_id)
        result.offer_id = offer_ids[0] if offer_ids else None
        done("offer")

        if publish:
            response = self._client.post(
                "/sell/inventory/v1/offer/publish_by_inventory_item_group",
                json={
                    "inventoryItemGroupKey": group_key,
                    "marketplaceId": self._settings.marketplace_id,
                },
            )
            result.listing_id = response.json().get("listingId")
            done("publish")
            logger.info(
                "variation group %s published as listing %s (%d variants)",
                group_key, result.listing_id, len(variant_records),
            )
        else:
            logger.info(
                "variation group %s left as draft (%d variants)",
                group_key, len(variant_records),
            )
        return result

    def delete_listing(self, sku: str) -> dict:
        """End + delete a listing or draft — single SKU or variation group.

        Withdraws any live listing first (ending it), then deletes the offer(s),
        inventory item(s), and the group. Idempotent: 404s are treated as
        already-gone. Returns a summary of what was removed.
        """
        summary = {"withdrawn": False, "offers_deleted": 0, "items_deleted": 0, "group": False}

        group = None
        try:
            group = self._client.get(
                f"/sell/inventory/v1/inventory_item_group/{sku}"
            ).json()
        except EbayApiError as err:
            if err.status_code != 404:
                raise

        if group is not None:
            try:
                self._client.post(
                    "/sell/inventory/v1/offer/withdraw_by_inventory_item_group",
                    json={
                        "inventoryItemGroupKey": sku,
                        "marketplaceId": self._settings.marketplace_id,
                    },
                )
                summary["withdrawn"] = True
            except EbayApiError as err:
                logger.info("group %s not published — withdraw skipped (%s)", sku, err)
            skus = list(group.get("variantSKUs", []))
        else:
            skus = [sku]

        for one in skus:
            for offer in self._offers_for_sku(one):
                offer_id = offer["offerId"]
                if group is None and offer.get("status") == "PUBLISHED":
                    try:
                        self._client.post(f"/sell/inventory/v1/offer/{offer_id}/withdraw")
                        summary["withdrawn"] = True
                    except EbayApiError as err:
                        logger.info("offer %s withdraw skipped (%s)", offer_id, err)
                if self._delete_ignoring_404(f"/sell/inventory/v1/offer/{offer_id}"):
                    summary["offers_deleted"] += 1
            if self._delete_ignoring_404(f"/sell/inventory/v1/inventory_item/{one}"):
                summary["items_deleted"] += 1

        if group is not None and self._delete_ignoring_404(
            f"/sell/inventory/v1/inventory_item_group/{sku}"
        ):
            summary["group"] = True
        return summary

    def _offers_for_sku(self, sku: str) -> list[dict]:
        try:
            response = self._client.get(
                "/sell/inventory/v1/offer",
                params={"sku": sku, "marketplace_id": self._settings.marketplace_id},
            )
        except EbayApiError as err:
            if err.status_code == 404:
                return []
            raise
        return response.json().get("offers") or []

    def _delete_ignoring_404(self, path: str) -> bool:
        try:
            self._client.delete(path)
            return True
        except EbayApiError as err:
            if err.status_code == 404:
                return False
            raise

    def _inventory_payload(self, product: Product, title: str, image_urls: list[str]) -> dict:
        return {
            "product": {
                "title": title,
                "description": product.description_html,
                "aspects": {k: [v] for k, v in product.item_specifics.items()},
                "imageUrls": image_urls,
            },
            "condition": "NEW",
            "availability": {
                "shipToLocationAvailability": {"quantity": self._settings.max_qty}
            },
        }

    def _offer_payload(
        self, product: Product, pricing: PricingResult, sku: str, category_id: str | None
    ) -> dict:
        payload = {
            "sku": sku,
            "marketplaceId": self._settings.marketplace_id,
            "format": "FIXED_PRICE",
            "availableQuantity": self._settings.max_qty,
            "categoryId": category_id,
            "listingDescription": product.description_html,
            "merchantLocationKey": LOCATION_KEY,
            "pricingSummary": {
                "price": {
                    "value": str(pricing.sell_price),
                    "currency": self._settings.currency,
                }
            },
        }
        policies = {
            "paymentPolicyId": self._settings.payment_policy_id,
            "returnPolicyId": self._settings.return_policy_id,
            "fulfillmentPolicyId": self._fulfillment_policy_id(product),
        }
        policies = {k: v for k, v in policies.items() if v}
        if policies:
            payload["listingPolicies"] = policies
        return payload

    def _find_offer_id(self, sku: str) -> str:
        response = self._client.get(
            "/sell/inventory/v1/offer",
            params={"sku": sku, "marketplace_id": self._settings.marketplace_id},
        )
        offers = response.json().get("offers") or []
        if not offers:
            raise EbayApiError(
                call=f"GET /sell/inventory/v1/offer?sku={sku}",
                status_code=404,
                errors=[{"errorId": None, "message": f"no existing offer found for {sku}"}],
            )
        return offers[0]["offerId"]

    def _fulfillment_policy_id(self, product: Product) -> str | None:
        """AliExpress ships from abroad — use the slow (long-handling) postage policy
        when configured; Amazon and everything else use the default/fast one."""
        if product.source_platform == "aliexpress" and self._settings.fulfillment_policy_id_slow:
            return self._settings.fulfillment_policy_id_slow
        return self._settings.fulfillment_policy_id
