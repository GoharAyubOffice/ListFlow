"""eBay OAuth tests: consent URL, code exchange, refresh-token cache, credential
storage (chmod 600), callback listener. All HTTP is respx-mocked; the callback test
uses localhost only. Phase 3 — written before ebay/auth.py logic.
"""

import base64
import json
import os
import threading
import time

import httpx
import pytest
import respx

from listflow.config import Settings
from listflow.ebay.auth import (
    SCOPES,
    AuthError,
    EbayAuth,
    NotAuthenticatedError,
    _code_from_user_paste,
    _wait_for_callback_code,
    listflow_home,
)

SANDBOX_TOKEN_URL = "https://api.sandbox.ebay.com/identity/v1/oauth2/token"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Auth tests must never touch the real ~/.listflow."""
    monkeypatch.setenv("LISTFLOW_HOME", str(tmp_path / "lfhome"))
    return tmp_path / "lfhome"


def make_settings(**overrides) -> Settings:
    base = {
        "ebay_client_id": "client-abc",
        "ebay_client_secret": "secret-xyz",
        "ebay_ru_name": "My_App-RuName",
        "ebay_env": "sandbox",
    }
    base.update(overrides)
    return Settings(**base)


def seed_credentials(refresh_token="stored-refresh-token"):
    home = listflow_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "credentials.json").write_text(
        json.dumps({"refresh_token": refresh_token}), encoding="utf-8"
    )


# ------------------------------------------------------------- consent URL

def test_consent_url_contains_required_params():
    url = EbayAuth(make_settings()).consent_url()
    assert url.startswith("https://auth.sandbox.ebay.com/oauth2/authorize?")
    assert "client_id=client-abc" in url
    assert "response_type=code" in url
    assert "My_App-RuName" in url
    assert "sell.inventory" in url


def test_consent_url_production_host():
    url = EbayAuth(make_settings(ebay_env="production")).consent_url()
    assert url.startswith("https://auth.ebay.com/oauth2/authorize?")


def test_consent_url_requires_ru_name():
    with pytest.raises(AuthError, match="EBAY_RU_NAME"):
        EbayAuth(make_settings(ebay_ru_name=None)).consent_url()


# ------------------------------------------------------------ code exchange

@respx.mock
def test_exchange_code_posts_basic_auth_and_stores_refresh_token(isolated_home):
    route = respx.post(SANDBOX_TOKEN_URL).respond(
        200,
        json={
            "access_token": "AT-1",
            "expires_in": 7200,
            "refresh_token": "RT-1",
            "refresh_token_expires_in": 47304000,
        },
    )
    auth = EbayAuth(make_settings())
    auth.exchange_code("the-auth-code")

    request = route.calls.last.request
    expected_basic = base64.b64encode(b"client-abc:secret-xyz").decode()
    assert request.headers["Authorization"] == f"Basic {expected_basic}"
    body = request.content.decode()
    assert "grant_type=authorization_code" in body
    assert "the-auth-code" in body

    cred_file = isolated_home / "credentials.json"
    assert cred_file.exists()
    stored = json.loads(cred_file.read_text(encoding="utf-8"))
    assert stored["refresh_token"] == "RT-1"
    if os.name == "posix":
        assert oct(cred_file.stat().st_mode & 0o777) == "0o600"


@respx.mock
def test_exchange_code_failure_raises_without_leaking_secret():
    respx.post(SANDBOX_TOKEN_URL).respond(400, json={"error": "invalid_grant"})
    auth = EbayAuth(make_settings())
    with pytest.raises(AuthError) as excinfo:
        auth.exchange_code("bad-code")
    assert "secret-xyz" not in str(excinfo.value)


# ------------------------------------------------------- access-token cache

@respx.mock
def test_get_access_token_refreshes_then_caches():
    seed_credentials()
    route = respx.post(SANDBOX_TOKEN_URL).respond(
        200, json={"access_token": "AT-2", "expires_in": 7200}
    )
    auth = EbayAuth(make_settings())
    assert auth.get_access_token() == "AT-2"
    assert auth.get_access_token() == "AT-2"  # served from cache
    assert route.call_count == 1
    body = route.calls.last.request.content.decode()
    assert "grant_type=refresh_token" in body
    assert "stored-refresh-token" in body


@respx.mock
def test_get_access_token_force_refresh_bypasses_cache():
    seed_credentials()
    route = respx.post(SANDBOX_TOKEN_URL).respond(
        200, json={"access_token": "AT-3", "expires_in": 7200}
    )
    auth = EbayAuth(make_settings())
    auth.get_access_token()
    auth.get_access_token(force_refresh=True)
    assert route.call_count == 2


@respx.mock
def test_get_access_token_expired_cache_refetches():
    seed_credentials()
    route = respx.post(SANDBOX_TOKEN_URL).respond(
        200, json={"access_token": "AT-4", "expires_in": 7200}
    )
    auth = EbayAuth(make_settings())
    auth.get_access_token()
    auth._expires_at = 0.0  # simulate expiry
    auth.get_access_token()
    assert route.call_count == 2


def test_get_access_token_without_credentials_tells_user_to_auth():
    auth = EbayAuth(make_settings())
    with pytest.raises(NotAuthenticatedError, match="listflow auth"):
        auth.get_access_token()


def test_scopes_include_inventory():
    assert "https://api.ebay.com/oauth/api_scope/sell.inventory" in SCOPES


# ------------------------------------------------------ manual consent flow

@pytest.mark.parametrize(
    ("pasted", "expected"),
    [
        (
            "https://signin.sandbox.ebay.com/authsucess"
            "?isAuthSuccessful=true&code=v%5E1.1%23abc&expires_in=299",
            "v^1.1#abc",
        ),
        ("v^1.1#i^1#raw-code", "v^1.1#i^1#raw-code"),  # bare code, already decoded
        ("v%5E1.1%23abc", "v^1.1#abc"),  # bare code, still URL-encoded
        ('  "https://x/cb?code=abc123&expires_in=299"  ', "abc123"),  # quoted paste
    ],
    ids=["full-url", "bare-code", "encoded-code", "quoted-url"],
)
def test_code_from_user_paste(pasted, expected):
    assert _code_from_user_paste(pasted) == expected


@respx.mock
def test_run_consent_flow_manual_paste(isolated_home):
    respx.post(SANDBOX_TOKEN_URL).respond(
        200, json={"access_token": "AT-9", "expires_in": 7200, "refresh_token": "RT-9"}
    )
    opened: list[str] = []
    auth = EbayAuth(make_settings())
    auth.run_consent_flow(open_browser=opened.append, get_code=lambda: "the-code")
    assert opened and "oauth2/authorize" in opened[0]
    stored = json.loads((isolated_home / "credentials.json").read_text(encoding="utf-8"))
    assert stored["refresh_token"] == "RT-9"


def test_run_consent_flow_empty_code_rejected():
    auth = EbayAuth(make_settings())
    with pytest.raises(AuthError, match="no authorization code"):
        auth.run_consent_flow(open_browser=lambda url: None, get_code=lambda: "")


# --------------------------------------------------------- callback listener

def test_callback_listener_captures_code():
    result: dict[str, str] = {}

    def run():
        result["code"] = _wait_for_callback_code(port=8912, timeout=10)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    # wait for the listener to come up, then simulate eBay's redirect
    response = None
    for _ in range(50):
        try:
            response = httpx.get(
                "http://127.0.0.1:8912/callback?code=v%5E1.1%23abc&expires_in=299"
            )
            break
        except httpx.ConnectError:
            time.sleep(0.1)
    assert response is not None and response.status_code == 200
    thread.join(timeout=5)
    assert result["code"] == "v^1.1#abc"  # URL-decoded


def test_callback_listener_times_out():
    with pytest.raises(AuthError, match="timed out"):
        _wait_for_callback_code(port=8912, timeout=0.2)
