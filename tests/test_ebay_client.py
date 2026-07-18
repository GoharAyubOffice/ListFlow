"""eBay client tests: env-based host, bearer injection, 429/5xx retry with backoff,
structural 4xx no-retry, 401 re-auth, verbatim error surfacing. All respx-mocked.
Phase 3 — written before ebay/client.py logic.
"""

import httpx
import pytest
import respx

from listflow.config import Settings
from listflow.ebay.client import MAX_RETRIES, EbayApiError, EbayClient

SANDBOX = "https://api.sandbox.ebay.com"


class FakeAuth:
    """Stands in for EbayAuth — no HTTP, counts forced refreshes."""

    def __init__(self):
        self.forced = 0

    def get_access_token(self, force_refresh: bool = False) -> str:
        if force_refresh:
            self.forced += 1
        return f"tok{self.forced}"


def make_client(env="sandbox"):
    settings = Settings(ebay_client_id="a", ebay_client_secret="b", ebay_env=env)
    sleeps: list[float] = []
    client = EbayClient(settings, FakeAuth(), sleep=sleeps.append)
    return client, sleeps


@respx.mock
def test_happy_path_headers_and_host():
    route = respx.get(f"{SANDBOX}/sell/inventory/v1/inventory_item/SKU1").respond(
        200, json={"sku": "SKU1"}
    )
    client, _ = make_client()
    response = client.get("/sell/inventory/v1/inventory_item/SKU1")
    assert response.status_code == 200
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer tok0"
    assert request.headers["Content-Language"] == "en-GB"
    assert request.headers["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_GB"


@respx.mock
def test_production_env_switches_host():
    route = respx.get("https://api.ebay.com/sell/account/v1/fulfillment_policy").respond(
        200, json={}
    )
    client, _ = make_client(env="production")
    client.get("/sell/account/v1/fulfillment_policy")
    assert route.call_count == 1


@respx.mock
def test_429_retries_with_exponential_backoff_then_succeeds():
    route = respx.post(f"{SANDBOX}/sell/inventory/v1/offer")
    route.side_effect = [
        httpx.Response(429, json={"errors": [{"errorId": 1001, "message": "Rate limited"}]}),
        httpx.Response(429, json={"errors": [{"errorId": 1001, "message": "Rate limited"}]}),
        httpx.Response(201, json={"offerId": "OFFER-1"}),
    ]
    client, sleeps = make_client()
    response = client.post("/sell/inventory/v1/offer", json={"sku": "X"})
    assert response.status_code == 201
    assert route.call_count == 3
    assert sleeps == [1, 2]  # exponential backoff


@respx.mock
def test_retries_exhausted_raises_with_verbatim_error():
    route = respx.get(f"{SANDBOX}/sell/inventory/v1/location")
    route.side_effect = [
        httpx.Response(500, json={"errors": [{"errorId": 2003, "message": "Internal error"}]})
    ] * (MAX_RETRIES + 1)
    client, sleeps = make_client()
    with pytest.raises(EbayApiError) as excinfo:
        client.get("/sell/inventory/v1/location")
    assert route.call_count == MAX_RETRIES + 1
    assert len(sleeps) == MAX_RETRIES
    message = str(excinfo.value)
    assert "2003" in message
    assert "Internal error" in message  # eBay's message verbatim
    assert "GET /sell/inventory/v1/location" in message  # which call failed


@respx.mock
def test_structural_4xx_fails_immediately_no_retry():
    route = respx.post(f"{SANDBOX}/sell/inventory/v1/offer").respond(
        400,
        json={"errors": [{"errorId": 25002, "message": "A required aspect is missing"}]},
    )
    client, sleeps = make_client()
    with pytest.raises(EbayApiError) as excinfo:
        client.post("/sell/inventory/v1/offer", json={})
    assert route.call_count == 1
    assert sleeps == []
    assert "25002" in str(excinfo.value)
    assert "A required aspect is missing" in str(excinfo.value)
    assert excinfo.value.status_code == 400


@respx.mock
def test_401_triggers_single_token_refresh_and_retry():
    route = respx.get(f"{SANDBOX}/sell/inventory/v1/inventory_item/SKU1")
    route.side_effect = [
        httpx.Response(401, json={"errors": [{"errorId": 1, "message": "Invalid token"}]}),
        httpx.Response(200, json={"sku": "SKU1"}),
    ]
    client, _ = make_client()
    response = client.get("/sell/inventory/v1/inventory_item/SKU1")
    assert response.status_code == 200
    assert route.call_count == 2
    assert route.calls.last.request.headers["Authorization"] == "Bearer tok1"  # new token


@respx.mock
def test_401_twice_raises():
    route = respx.get(f"{SANDBOX}/x")
    route.side_effect = [
        httpx.Response(401, json={"errors": [{"errorId": 1, "message": "Invalid token"}]}),
        httpx.Response(401, json={"errors": [{"errorId": 1, "message": "Invalid token"}]}),
    ]
    client, _ = make_client()
    with pytest.raises(EbayApiError):
        client.get("/x")
    assert route.call_count == 2


@respx.mock
def test_non_json_error_body_still_surfaces():
    respx.get(f"{SANDBOX}/x").respond(502, text="<html>Bad gateway</html>")
    client, _ = make_client()
    with pytest.raises(EbayApiError) as excinfo:
        client.get("/x")
    assert "502" in str(excinfo.value)
