"""Taxonomy API tests: default tree id, top category suggestion, no-suggestion error.
All respx-mocked. Phase 4 — written before ebay/taxonomy.py logic.
"""

import pytest
import respx

from listflow.config import Settings
from listflow.ebay.client import EbayClient
from listflow.ebay.taxonomy import (
    CategorySuggestionError,
    default_category_tree_id,
    suggest_category,
)

SANDBOX = "https://api.sandbox.ebay.com"


class FakeAuth:
    def get_access_token(self, force_refresh: bool = False) -> str:
        return "tok"


def make_client() -> EbayClient:
    settings = Settings(ebay_client_id="a", ebay_client_secret="b")
    return EbayClient(settings, FakeAuth(), sleep=lambda _s: None)


@respx.mock
def test_default_category_tree_id_uses_marketplace():
    route = respx.get(f"{SANDBOX}/commerce/taxonomy/v1/get_default_category_tree_id").respond(
        200, json={"categoryTreeId": "3", "categoryTreeVersion": "129"}
    )
    assert default_category_tree_id(make_client()) == "3"
    assert route.calls.last.request.url.params["marketplace_id"] == "EBAY_GB"


@respx.mock
def test_suggest_category_returns_top_hit():
    respx.get(f"{SANDBOX}/commerce/taxonomy/v1/get_default_category_tree_id").respond(
        200, json={"categoryTreeId": "3"}
    )
    route = respx.get(
        f"{SANDBOX}/commerce/taxonomy/v1/category_tree/3/get_category_suggestions"
    ).respond(
        200,
        json={
            "categorySuggestions": [
                {"category": {"categoryId": "179011", "categoryName": "Pet Grooming"}},
                {"category": {"categoryId": "1", "categoryName": "Everything Else"}},
            ]
        },
    )
    category_id = suggest_category(make_client(), "Pet Hair Remover Brush")
    assert category_id == "179011"
    assert route.calls.last.request.url.params["q"] == "Pet Hair Remover Brush"


@respx.mock
def test_suggest_category_explicit_tree_skips_lookup():
    route = respx.get(
        f"{SANDBOX}/commerce/taxonomy/v1/category_tree/3/get_category_suggestions"
    ).respond(
        200,
        json={"categorySuggestions": [{"category": {"categoryId": "42", "categoryName": "X"}}]},
    )
    assert suggest_category(make_client(), "anything", tree_id="3") == "42"
    assert route.call_count == 1


@respx.mock
def test_suggest_category_no_results_is_actionable():
    respx.get(f"{SANDBOX}/commerce/taxonomy/v1/get_default_category_tree_id").respond(
        200, json={"categoryTreeId": "3"}
    )
    respx.get(
        f"{SANDBOX}/commerce/taxonomy/v1/category_tree/3/get_category_suggestions"
    ).respond(200, json={"categorySuggestions": []})
    with pytest.raises(CategorySuggestionError, match="--category"):
        suggest_category(make_client(), "zxqjv unmatchable")
