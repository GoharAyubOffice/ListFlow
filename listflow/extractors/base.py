"""Extractor ABC: extract(url) -> RawProduct, and the ExtractionError contract
(always saves a debug snapshot to ~/.listflow/debug/). Implemented in Phase 5.

The snapshot is the repair loop's raw material: when a site changes and breaks
parsing, the saved page is copied into tests/fixtures/ and the extractor is fixed
offline against it (CLAUDE.md maintenance loop).
"""

import logging
import time
from abc import ABC, abstractmethod
from pathlib import Path

from listflow.config import listflow_home
from listflow.models import RawProduct

logger = logging.getLogger(__name__)


class ExtractionError(RuntimeError):
    """Extraction failed; carries which field broke and where the page was saved."""

    def __init__(
        self,
        message: str,
        *,
        field_missing: str | None = None,
        page_snapshot_path: Path | None = None,
    ):
        self.field_missing = field_missing
        self.page_snapshot_path = page_snapshot_path
        super().__init__(message)

    def __str__(self) -> str:
        base = super().__str__()
        if self.page_snapshot_path:
            base += f" (page snapshot saved to {self.page_snapshot_path})"
        return base


def save_debug_snapshot(content: str, name: str) -> Path:
    """Write the raw page to ~/.listflow/debug/ so the extractor can be repaired."""
    debug_dir = listflow_home() / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    path = debug_dir / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}.html"
    path.write_text(content, encoding="utf-8")
    logger.info("debug snapshot saved: %s", path)
    return path


class Extractor(ABC):
    @abstractmethod
    def extract(self, url: str) -> RawProduct:
        """Fetch the product page and return a RawProduct; ExtractionError on failure."""
