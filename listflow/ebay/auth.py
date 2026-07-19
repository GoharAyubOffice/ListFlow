"""OAuth2 authorization-code grant + refresh-token cache (chmod 600, spec §7.1).

Implemented in Phase 3. Callback listener on http://localhost:8912/callback.
Tokens are never printed or logged.
"""

import base64
import json
import logging
import os
import time
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urlparse

import httpx

from listflow.config import Settings, listflow_home

logger = logging.getLogger(__name__)

AUTH_HOSTS = {
    "sandbox": "https://auth.sandbox.ebay.com",
    "production": "https://auth.ebay.com",
}
API_HOSTS = {
    "sandbox": "https://api.sandbox.ebay.com",
    "production": "https://api.ebay.com",
}
CALLBACK_PORT = 8912
SCOPES = (
    "https://api.ebay.com/oauth/api_scope",
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    "https://api.ebay.com/oauth/api_scope/sell.account",  # write: opt-in + create policies
    "https://api.ebay.com/oauth/api_scope/sell.account.readonly",
    "https://api.ebay.com/oauth/api_scope/sell.marketing.readonly",
)

_TOKEN_SAFETY_MARGIN = 60  # refresh this many seconds before actual expiry

_CALLBACK_PAGE = (
    b"<html><body><p>Listflow: eBay authorisation received."
    b" You can close this window and return to the terminal.</p></body></html>"
)


class AuthError(RuntimeError):
    """OAuth flow failed (consent denied, token endpoint error, timeout)."""


class NotAuthenticatedError(AuthError):
    """No stored credentials — the user must run `listflow auth` first."""


def _credentials_path() -> Path:
    return listflow_home() / "credentials.json"


def _save_credentials(data: dict) -> None:
    home = listflow_home()
    home.mkdir(parents=True, exist_ok=True)
    path = _credentials_path()
    # owner-only from the moment of creation; chmod again in case the file pre-existed
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.chmod(path, 0o600)


def _wait_for_callback_code(port: int = CALLBACK_PORT, timeout: float = 300.0) -> str:
    """Serve http://localhost:<port>/callback once and return the ?code= value."""
    result: dict[str, str] = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib API name
            query = parse_qs(urlparse(self.path).query)
            if "code" in query:
                result["code"] = query["code"][0]
            elif "error" in query:
                result["error"] = query.get("error_description", query["error"])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_CALLBACK_PAGE)

        def log_message(self, *args: object) -> None:
            # default logger prints the request line, which contains the auth code
            return

    server = HTTPServer(("127.0.0.1", port), _Handler)
    server.timeout = 0.2
    deadline = time.monotonic() + timeout
    try:
        while not result and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()

    if "code" in result:
        return result["code"]
    if "error" in result:
        raise AuthError(f"eBay consent was not granted: {result['error']}")
    raise AuthError("timed out waiting for the eBay consent callback")


def _code_from_user_paste(pasted: str) -> str:
    """Extract the authorization code from whatever the user pasted.

    Accepts the full address-bar URL of eBay's success page, just its query string,
    or the bare (possibly still URL-encoded) code.
    """
    text = pasted.strip().strip('"').strip("'")
    if "code=" in text:
        query = urlparse(text).query or text.partition("?")[2] or text
        params = parse_qs(query)
        if "code" in params:
            return params["code"][0]
    return unquote(text)


class EbayAuth:
    """Owns the consent dance, the refresh token on disk and the access-token cache."""

    def __init__(self, settings: Settings, http: httpx.Client | None = None):
        self._settings = settings
        self._http = http or httpx.Client(timeout=30)
        self._access_token: str | None = None
        self._expires_at: float = 0.0  # time.monotonic deadline

    @property
    def _token_url(self) -> str:
        return API_HOSTS[self._settings.ebay_env] + "/identity/v1/oauth2/token"

    def consent_url(self) -> str:
        if not self._settings.ebay_ru_name:
            raise AuthError(
                "EBAY_RU_NAME is not set — add it to .env. It is the RuName of the "
                "redirect URL configured in your eBay developer account whose "
                "auth-accepted URL is http://localhost:8912/callback"
            )
        params = {
            "client_id": self._settings.ebay_client_id,
            "response_type": "code",
            "redirect_uri": self._settings.ebay_ru_name,
            "scope": " ".join(SCOPES),
        }
        return AUTH_HOSTS[self._settings.ebay_env] + "/oauth2/authorize?" + urlencode(params)

    def _basic_auth(self) -> str:
        raw = f"{self._settings.ebay_client_id}:{self._settings.ebay_client_secret}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def _token_request(self, form: dict[str, str]) -> dict:
        response = self._http.post(
            self._token_url,
            data=form,
            headers={
                "Authorization": self._basic_auth(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        if response.status_code != 200:
            # eBay's error body here is safe to show (no tokens in it)
            raise AuthError(
                f"eBay token endpoint returned HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )
        return response.json()

    def _store_access(self, payload: dict) -> None:
        self._access_token = payload["access_token"]
        expires_in = int(payload.get("expires_in", 7200))
        self._expires_at = time.monotonic() + expires_in - _TOKEN_SAFETY_MARGIN

    def exchange_code(self, code: str) -> None:
        """Trade an authorization code for tokens; persist the refresh token."""
        payload = self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._settings.ebay_ru_name or "",
            }
        )
        self._store_access(payload)
        _save_credentials(
            {
                "refresh_token": payload["refresh_token"],
                "ebay_env": self._settings.ebay_env,
                "obtained_at": int(time.time()),
            }
        )
        logger.info("eBay refresh token stored in %s", _credentials_path())

    def _stored_refresh_token(self) -> str:
        path = _credentials_path()
        if not path.exists():
            raise NotAuthenticatedError(
                "no eBay credentials found — run `listflow auth` first"
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        token = data.get("refresh_token")
        if not token:
            raise NotAuthenticatedError(
                "stored eBay credentials are incomplete — run `listflow auth` again"
            )
        return token

    def get_access_token(self, force_refresh: bool = False) -> str:
        """Return a valid access token, refreshing via the stored refresh token."""
        if (
            not force_refresh
            and self._access_token
            and time.monotonic() < self._expires_at
        ):
            return self._access_token
        payload = self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": self._stored_refresh_token(),
                "scope": " ".join(SCOPES),
            }
        )
        self._store_access(payload)
        assert self._access_token is not None
        return self._access_token

    def run_consent_flow(
        self,
        open_browser: Callable[[str], object] = webbrowser.open,
        get_code: Callable[[], str] | None = None,
    ) -> None:
        """Full one-time dance: browser consent -> code paste -> token exchange.

        eBay's developer portal only accepts https:// redirect URLs, so a plain
        local-http listener cannot receive the redirect. Default flow: leave the
        redirect URL blank in the portal (eBay shows its default success page) and
        paste the address-bar URL — which carries ?code= — into the terminal.
        (`_wait_for_callback_code` is kept for a future https-capable redirect.)
        """
        url = self.consent_url()
        logger.info(
            "Opening the eBay consent page in your browser (%s environment)…",
            self._settings.ebay_env,
        )
        logger.info("If the browser did not open, visit:\n%s", url)
        open_browser(url)
        if get_code is None:

            def get_code() -> str:
                pasted = input(
                    "\nAfter you approve, the browser lands on an eBay success page.\n"
                    "Copy the FULL URL from the address bar and paste it here: "
                )
                return _code_from_user_paste(pasted)

        code = get_code()
        if not code:
            raise AuthError("no authorization code received — run `listflow auth` again")
        self.exchange_code(code)
        logger.info("eBay authorisation complete — you will not need to log in again.")
