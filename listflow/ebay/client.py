"""Thin eBay REST wrapper: base URL from EBAY_ENV (never hardcoded), bearer injection,
retry/backoff on 429/5xx (max 3), verbatim error surfacing (spec §7.3).

Implemented in Phase 3.
"""
