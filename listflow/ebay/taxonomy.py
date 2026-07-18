"""Category suggestion via Taxonomy API getCategorySuggestions (spec §7.2 step 3).

Implemented in Phase 4. Overridable with --category <id>.
"""

import logging

from listflow.ebay.client import EbayClient

logger = logging.getLogger(__name__)


class CategorySuggestionError(RuntimeError):
    """eBay returned no category for the title — the user must pass --category."""


def default_category_tree_id(client: EbayClient) -> str:
    response = client.get(
        "/commerce/taxonomy/v1/get_default_category_tree_id",
        params={"marketplace_id": client.marketplace_id},
    )
    return response.json()["categoryTreeId"]


def suggest_category(client: EbayClient, title: str, tree_id: str | None = None) -> str:
    """Return the categoryId of eBay's top suggestion for this title."""
    tree = tree_id or default_category_tree_id(client)
    response = client.get(
        f"/commerce/taxonomy/v1/category_tree/{tree}/get_category_suggestions",
        params={"q": title},
    )
    suggestions = response.json().get("categorySuggestions") or []
    if not suggestions:
        raise CategorySuggestionError(
            f"eBay returned no category suggestions for {title!r} — "
            "pass --category <id> to choose one explicitly"
        )
    top = suggestions[0]["category"]
    logger.info("Category suggestion: %s (%s)", top.get("categoryName"), top["categoryId"])
    return top["categoryId"]
