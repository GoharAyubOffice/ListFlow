"""SQLite tracker: imports table mirroring the test-and-kill workbook (spec §4.3).

Implemented in Phase 7. Money is stored as TEXT (the Decimal's own string form) so
values round-trip exactly — never as REAL/float. One row per SKU (UNIQUE), so a
re-import or `listflow retry` updates in place rather than duplicating.
"""

import csv
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from listflow.config import listflow_home

STATUSES = ("draft", "published", "failed", "killed")

# Column order used for CSV export and the `list` table.
EXPORT_COLUMNS = (
    "id", "created_at", "source_platform", "source_url", "source_id",
    "title_ebay", "cost", "sell_price", "margin_actual",
    "ebay_sku", "ebay_offer_id", "ebay_listing_id", "status", "last_step", "notes",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS imports (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    source_platform TEXT NOT NULL,
    source_url     TEXT NOT NULL,
    source_id      TEXT NOT NULL,
    title_ebay     TEXT NOT NULL,
    cost           TEXT NOT NULL,
    sell_price     TEXT NOT NULL,
    margin_actual  TEXT NOT NULL,
    ebay_sku       TEXT NOT NULL UNIQUE,
    ebay_offer_id  TEXT,
    ebay_listing_id TEXT,
    status         TEXT NOT NULL,
    last_step      TEXT,
    notes          TEXT
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Tracker:
    """Thin wrapper over the SQLite imports table."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(_SCHEMA)
        self.conn.commit()

    @classmethod
    def open(cls, db_path: str | Path | None = None) -> "Tracker":
        path = Path(db_path) if db_path else listflow_home() / "listflow.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        return cls(sqlite3.connect(str(path)))

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Tracker":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------- lifecycle

    def start(
        self,
        *,
        sku: str,
        platform: str,
        source_url: str,
        source_id: str,
        title_ebay: str,
        cost: Decimal,
        sell_price: Decimal,
        margin_actual: Decimal,
    ) -> None:
        """Insert (or reset) a row before publishing. Starts as 'failed' — a
        successful finish() flips it, so a crash mid-pipeline leaves it retryable."""
        now = _now()
        self.conn.execute(
            """
            INSERT INTO imports (
                created_at, updated_at, source_platform, source_url, source_id,
                title_ebay, cost, sell_price, margin_actual, ebay_sku, status, last_step
            ) VALUES (?,?,?,?,?,?,?,?,?,?, 'failed', NULL)
            ON CONFLICT(ebay_sku) DO UPDATE SET
                updated_at=excluded.updated_at,
                source_url=excluded.source_url,
                title_ebay=excluded.title_ebay,
                cost=excluded.cost,
                sell_price=excluded.sell_price,
                margin_actual=excluded.margin_actual,
                status='failed',
                last_step=NULL
            """,
            (
                now, now, platform, source_url, source_id,
                title_ebay, str(cost), str(sell_price), str(margin_actual), sku,
            ),
        )
        self.conn.commit()

    def mark_step(self, sku: str, step: str) -> None:
        self.conn.execute(
            "UPDATE imports SET last_step=?, updated_at=? WHERE ebay_sku=?",
            (step, _now(), sku),
        )
        self.conn.commit()

    def finish(
        self,
        sku: str,
        *,
        status: str,
        offer_id: str | None = None,
        listing_id: str | None = None,
        notes: str | None = None,
    ) -> None:
        if status not in STATUSES:
            raise ValueError(f"invalid status {status!r}; expected one of {STATUSES}")
        self.conn.execute(
            """
            UPDATE imports SET
                status=?,
                ebay_offer_id=COALESCE(?, ebay_offer_id),
                ebay_listing_id=COALESCE(?, ebay_listing_id),
                notes=?,
                updated_at=?
            WHERE ebay_sku=?
            """,
            (status, offer_id, listing_id, notes, _now(), sku),
        )
        self.conn.commit()

    def set_status(self, sku: str, status: str, notes: str | None = None) -> None:
        self.finish(sku, status=status, notes=notes)

    # ------------------------------------------------------------- reads

    def get(self, sku: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM imports WHERE ebay_sku=?", (sku,)
        ).fetchone()
        return dict(row) if row else None

    def all(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            cursor = self.conn.execute(
                "SELECT * FROM imports WHERE status=? ORDER BY id DESC", (status,)
            )
        else:
            cursor = self.conn.execute("SELECT * FROM imports ORDER BY id DESC")
        return [dict(row) for row in cursor.fetchall()]

    def export_csv(self, path: str | Path) -> int:
        rows = self.all()
        out = Path(path)
        with out.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=EXPORT_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        return len(rows)
