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
from listflow.models import PricingResult, Product

logger = logging.getLogger(__name__)

LOCATION_KEY = "MAIN"
STEPS = ("location", "images", "category", "inventory_item", "offer", "publish")


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
        logger.info("creating inventory location %r", LOCATION_KEY)
        self._client.post(
            f"/sell/inventory/v1/location/{LOCATION_KEY}",
            json={
                "name": "Listflow main location",
                "location": {"address": {"country": "GB"}},
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
        done("images")

        title = product.title_ebay or product.title_raw
        result.category_id = category_id or suggest_category(self._client, title)
        done("category")

        self._client.put(
            f"/sell/inventory/v1/inventory_item/{sku}",
            json=self._inventory_payload(product, title),
        )
        done("inventory_item")

        offer_payload = self._offer_payload(product, pricing, sku, result.category_id)
        if existing_offer_id:
            # retry path: the offer was already created on a previous run — update it
            # in place rather than POSTing a duplicate (eBay rejects two offers per SKU).
            self._client.put(f"/sell/inventory/v1/offer/{existing_offer_id}", json=offer_payload)
            result.offer_id = existing_offer_id
        else:
            offer_response = self._client.post("/sell/inventory/v1/offer", json=offer_payload)
            result.offer_id = offer_response.json()["offerId"]
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

    def _inventory_payload(self, product: Product, title: str) -> dict:
        image_urls = [str(a.ebay_url) for a in product.images if a.ebay_url]
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
            "fulfillmentPolicyId": self._settings.fulfillment_policy_id,
        }
        policies = {k: v for k, v in policies.items() if v}
        if policies:
            payload["listingPolicies"] = policies
        return payload
