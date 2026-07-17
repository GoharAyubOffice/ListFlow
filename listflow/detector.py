"""URL -> SourcePlatform detection (spec §2.1). Implemented in Phase 2. Pure logic."""

import re
from urllib.parse import urlparse

from listflow.models import SourcePlatform


class UnknownPlatformError(ValueError):
    """The URL does not belong to a supported source platform."""

    def __init__(self, url: str):
        self.url = url
        super().__init__(
            f"cannot detect source platform from {url!r} — "
            "supported: AliExpress and Amazon product URLs"
        )


# Decision is made on the hostname only, so a platform name appearing in a path or
# query string ("google.com/search?q=amazon") never counts. The registrable domain
# must be aliexpress.* / amazon.* itself — "amazon.evil.example" or "amazonia.com"
# do not match because every label after the brand must be a 2-3 letter TLD part.
_ALIEXPRESS_HOST_RE = re.compile(r"(?:^|\.)aliexpress(?:\.[a-z]{2,3})+$")
_AMAZON_HOST_RE = re.compile(r"(?:^|\.)amazon(?:\.[a-z]{2,3})+$")
_AMAZON_SHORT_RE = re.compile(r"^(?:www\.)?amzn\.(?:to|eu|asia|in|com)$")
_AMAZON_SHORT_HOSTS = frozenset({"a.co"})


def detect(url: str) -> SourcePlatform:
    """Map a product URL onto its source platform; UnknownPlatformError otherwise."""
    candidate = url.strip()
    if not candidate:
        raise UnknownPlatformError(url)
    if "://" not in candidate:
        candidate = f"https://{candidate}"  # tolerate pasted URLs without a scheme
    host = (urlparse(candidate).hostname or "").lower()

    if _ALIEXPRESS_HOST_RE.search(host):
        return SourcePlatform.ALIEXPRESS
    if _AMAZON_HOST_RE.search(host) or _AMAZON_SHORT_RE.match(host) or host in _AMAZON_SHORT_HOSTS:
        return SourcePlatform.AMAZON
    raise UnknownPlatformError(url)
