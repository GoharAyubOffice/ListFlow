"""SQLite tracker tests: schema, start/step/finish lifecycle, list filtering,
CSV export, Decimal round-trip via TEXT. Phase 7."""

import csv
from decimal import Decimal

import pytest

from listflow.storage import Tracker


@pytest.fixture
def tracker(tmp_path):
    with Tracker.open(tmp_path / "listflow.db") as t:
        yield t


def start_row(tracker, sku="LF-B00-abcd", **overrides):
    kwargs = {
        "sku": sku,
        "platform": "amazon",
        "source_url": "https://www.amazon.co.uk/dp/B00BAGTNAQ",
        "source_id": "B00BAGTNAQ",
        "title_ebay": "Pet Hair Remover Brush",
        "cost": Decimal("10.00"),
        "sell_price": Decimal("15.99"),
        "margin_actual": Decimal("0.2276"),
    }
    kwargs.update(overrides)
    tracker.start(**kwargs)
    return sku


def test_open_creates_schema(tmp_path):
    with Tracker.open(tmp_path / "db.sqlite") as t:
        cols = {r[1] for r in t.conn.execute("PRAGMA table_info(imports)")}
    expected = {
        "id", "created_at", "updated_at", "source_platform", "source_url", "source_id",
        "title_ebay", "cost", "sell_price", "margin_actual", "ebay_sku",
        "ebay_offer_id", "ebay_listing_id", "status", "last_step", "notes",
    }
    assert expected <= cols


def test_start_creates_failed_row(tracker):
    sku = start_row(tracker)
    row = tracker.get(sku)
    assert row["status"] == "failed"  # pessimistic until a step/finish flips it
    assert row["ebay_sku"] == sku
    assert row["cost"] == "10.00"  # Decimal stored exactly as text
    assert row["sell_price"] == "15.99"
    assert row["last_step"] is None
    assert row["ebay_offer_id"] is None


def test_mark_step_updates_last_step(tracker):
    sku = start_row(tracker)
    tracker.mark_step(sku, "location")
    tracker.mark_step(sku, "images")
    assert tracker.get(sku)["last_step"] == "images"


def test_finish_draft(tracker):
    sku = start_row(tracker)
    tracker.finish(sku, status="draft", offer_id="OFF-1")
    row = tracker.get(sku)
    assert row["status"] == "draft"
    assert row["ebay_offer_id"] == "OFF-1"
    assert row["ebay_listing_id"] is None


def test_finish_published(tracker):
    sku = start_row(tracker)
    tracker.finish(sku, status="published", offer_id="OFF-1", listing_id="LIST-9")
    row = tracker.get(sku)
    assert row["status"] == "published"
    assert row["ebay_listing_id"] == "LIST-9"


def test_finish_failed_records_notes(tracker):
    sku = start_row(tracker)
    tracker.finish(sku, status="failed", notes="offer step: errorId 25002")
    row = tracker.get(sku)
    assert row["status"] == "failed"
    assert "25002" in row["notes"]


def test_start_twice_same_sku_upserts_not_duplicates(tracker):
    start_row(tracker)
    start_row(tracker, title_ebay="Updated Title")
    rows = tracker.all()
    assert len(rows) == 1
    assert rows[0]["title_ebay"] == "Updated Title"


def test_get_missing_returns_none(tracker):
    assert tracker.get("nope") is None


def test_all_and_status_filter(tracker):
    start_row(tracker, sku="LF-1")
    start_row(tracker, sku="LF-2")
    tracker.finish("LF-1", status="draft", offer_id="O1")
    tracker.finish("LF-2", status="published", offer_id="O2", listing_id="L2")
    assert len(tracker.all()) == 2
    drafts = tracker.all(status="draft")
    assert len(drafts) == 1
    assert drafts[0]["ebay_sku"] == "LF-1"


def test_all_orders_newest_first(tracker):
    start_row(tracker, sku="LF-1")
    start_row(tracker, sku="LF-2")
    assert [r["ebay_sku"] for r in tracker.all()] == ["LF-2", "LF-1"]


def test_set_status_kill(tracker):
    sku = start_row(tracker)
    tracker.finish(sku, status="draft", offer_id="O1")
    tracker.set_status(sku, "killed", notes="zero watchers after 14 days")
    assert tracker.get(sku)["status"] == "killed"


def test_export_csv(tracker, tmp_path):
    start_row(tracker, sku="LF-1")
    tracker.finish("LF-1", status="published", offer_id="O1", listing_id="L1")
    out = tmp_path / "out.csv"
    count = tracker.export_csv(out)
    assert count == 1
    with out.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["ebay_sku"] == "LF-1"
    assert rows[0]["status"] == "published"
    assert rows[0]["sell_price"] == "15.99"


def test_default_path_uses_listflow_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LISTFLOW_HOME", str(tmp_path / "home"))
    with Tracker.open() as t:
        start_row(t)
    assert (tmp_path / "home" / "listflow.db").exists()
