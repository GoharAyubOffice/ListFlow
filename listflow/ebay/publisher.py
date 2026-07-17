"""Publish pipeline: location -> images -> category -> inventory item -> offer ->
[publish] with per-step resumable state for `listflow retry <sku>` (spec §7.2).

Implemented in Phase 4.
"""
