"""Thin eBay REST wrapper: base URL from EBAY_ENV (never hardcoded), bearer injection,
retry/backoff on 429/5xx (max 3), verbatim error surfacing (spec §7.3).

Implemented in Phase 3.
"""

import logging
import time
from collections.abc import Callable
from typing import Any, Protocol

import httpx

from listflow.config import Settings
from listflow.ebay.auth import API_HOSTS

logger = logging.getLogger(__name__)

MAX_RETRIES = 3

# The Media API (image hosting) lives on its own host, still env-selected.
MEDIA_HOSTS = {
    "sandbox": "https://apim.sandbox.ebay.com",
    "production": "https://apim.ebay.com",
}


class _TokenProvider(Protocol):
    def get_access_token(self, force_refresh: bool = False) -> str: ...


class EbayApiError(RuntimeError):
    """An eBay API call failed. Carries eBay's own errors[] verbatim (spec §7.3)."""

    def __init__(self, call: str, status_code: int, errors: list[dict]):
        self.call = call
        self.status_code = status_code
        self.errors = errors
        details = (
            "; ".join(
                f"errorId={item.get('errorId')}: {item.get('message')}" for item in errors
            )
            or "<no error body>"
        )
        super().__init__(f"eBay API call failed [{call}] HTTP {status_code} — {details}")


def _parse_errors(response: httpx.Response) -> list[dict]:
    try:
        body = response.json()
    except ValueError:
        return [{"errorId": None, "message": response.text[:300]}]
    if isinstance(body, dict) and body.get("errors"):
        return body["errors"]
    return [{"errorId": None, "message": str(body)[:300]}]


class EbayClient:
    """All eBay REST traffic goes through here — one place for auth, retry, errors."""

    def __init__(
        self,
        settings: Settings,
        auth: _TokenProvider,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._settings = settings
        self._auth = auth
        self._http = http or httpx.Client(timeout=30)
        self._sleep = sleep
        self.base_url = API_HOSTS[settings.ebay_env]
        self.media_base_url = MEDIA_HOSTS[settings.ebay_env]
        self.marketplace_id = settings.marketplace_id

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict | None = None,
        headers: dict[str, str] | None = None,
        files: dict | None = None,
    ) -> httpx.Response:
        call = f"{method} {path}"
        url = path if path.startswith("http") else self.base_url + path
        retries = 0
        reauthed = False
        while True:
            merged_headers = {
                "Authorization": f"Bearer {self._auth.get_access_token()}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Content-Language": "en-GB",
                "X-EBAY-C-MARKETPLACE-ID": self._settings.marketplace_id,
            } | (headers or {})
            if files is not None:
                # httpx must set its own multipart boundary Content-Type
                merged_headers.pop("Content-Type", None)
            response = self._http.request(
                method, url, json=json, params=params, headers=merged_headers, files=files
            )
            if response.status_code < 400:
                return response
            if response.status_code == 401 and not reauthed:
                reauthed = True  # token may have just expired — refresh once and retry
                self._auth.get_access_token(force_refresh=True)
                continue
            retryable = response.status_code == 429 or response.status_code >= 500
            if retryable and retries < MAX_RETRIES:
                delay = 2**retries  # 1s, 2s, 4s
                retries += 1
                logger.warning(
                    "eBay %s returned HTTP %d — retry %d/%d in %ds",
                    call,
                    response.status_code,
                    retries,
                    MAX_RETRIES,
                    delay,
                )
                self._sleep(delay)
                continue
            raise EbayApiError(
                call=call,
                status_code=response.status_code,
                errors=_parse_errors(response),
            )

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("DELETE", path, **kwargs)
