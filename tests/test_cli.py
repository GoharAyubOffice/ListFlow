"""CLI tests via Typer's CliRunner — extraction and eBay publishing are patched so
the whole flow runs offline. Covers dry-run, draft import, floor refusal, list,
export, retry. Phase 7."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from typer.testing import CliRunner

from listflow.cli import app
from listflow.ebay.publisher import PublishResult, make_sku
from listflow.models import RawProduct, SourcePlatform

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Point config + storage at temp dirs so tests never touch real state."""
    monkeypatch.setenv("LISTFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("EBAY_CLIENT_ID", "cid")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "secret")
    monkeypatch.setenv("EBAY_ENV", "sandbox")
    monkeypatch.setenv("COLUMNS", "200")  # rich renders full-width instead of truncating
    monkeypatch.chdir(tmp_path)  # no .env / config.toml here → pure defaults
    return tmp_path


def make_raw(**overrides) -> RawProduct:
    base = {
        "source_platform": SourcePlatform.AMAZON,
        "source_url": "https://www.amazon.co.uk/dp/B00BAGTNAQ",
        "source_id": "B00BAGTNAQ",
        "title": "Hot Sale Pet Hair Remover Brush",
        "price": Decimal("10.00"),
        "currency": "GBP",
        "description_html": "<p>Removes pet hair.</p>",
        "bullet_points": ["Reusable"],
        "image_urls": ["https://m.media-amazon.com/images/I/1.jpg"],
        "attributes": {"colour": "White"},
        "extracted_at": datetime.now(UTC),
    }
    base.update(overrides)
    return RawProduct(**base)


class FakeExtractor:
    def __init__(self, raw):
        self._raw = raw

    def extract(self, url):
        return self._raw


def patch_extractor(monkeypatch, raw):
    monkeypatch.setattr(
        "listflow.pipeline.get_extractor", lambda platform, headed=False: FakeExtractor(raw)
    )


AMAZON_URL = "https://www.amazon.co.uk/dp/B00BAGTNAQ"
SKU = make_sku("B00BAGTNAQ")


# ---------------------------------------------------------------- dry-run

def test_dry_run_prints_and_makes_no_calls(monkeypatch):
    patch_extractor(monkeypatch, make_raw())

    def boom(*a, **k):
        raise AssertionError("publisher must not run in dry-run")

    monkeypatch.setattr("listflow.cli._run_publish", boom)
    result = runner.invoke(app, ["import", AMAZON_URL, "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Pet Hair Remover Brush" in result.output
    assert "Sell price" in result.output
    assert "Dry run" in result.output


def test_dry_run_shows_category_override(monkeypatch):
    patch_extractor(monkeypatch, make_raw())
    monkeypatch.setattr("listflow.cli._run_publish", lambda *a, **k: None)
    result = runner.invoke(app, ["import", AMAZON_URL, "--dry-run", "--category", "179011"])
    assert "179011" in result.output


# ------------------------------------------------------------ floor logic

def test_below_floor_refuses_without_force(monkeypatch):
    patch_extractor(monkeypatch, make_raw())
    called = {}
    monkeypatch.setattr("listflow.cli._run_publish",
                        lambda *a, **k: called.setdefault("ran", True))
    result = runner.invoke(app, ["import", AMAZON_URL, "--margin", "0.05"])
    assert result.exit_code == 1
    assert "below the 20% floor" in result.output
    assert "ran" not in called


def test_below_floor_proceeds_with_force(monkeypatch):
    patch_extractor(monkeypatch, make_raw())
    captured = {}
    monkeypatch.setattr("listflow.cli._run_publish",
                        lambda settings, prepared, **k: captured.update(prepared=prepared))
    result = runner.invoke(app, ["import", AMAZON_URL, "--margin", "0.05", "--force"])
    assert result.exit_code == 0
    assert captured["prepared"].pricing.passes_floor is False


# --------------------------------------------------------- draft + publish

def _install_fake_publisher(monkeypatch, result_obj):
    class FakePublisher:
        def __init__(self, client, settings, on_step=None):
            self._on_step = on_step or (lambda s, step: None)

        def publish(self, product, pricing, *, publish=False, category_id=None,
                    existing_offer_id=None):
            for step in ("location", "images", "category", "inventory_item", "offer"):
                self._on_step(make_sku(product.source_id), step)
            if publish:
                self._on_step(make_sku(product.source_id), "publish")
            return result_obj

    monkeypatch.setattr("listflow.ebay.publisher.Publisher", FakePublisher)
    # patch the auth + client so no network/credentials are needed
    monkeypatch.setattr("listflow.ebay.auth.EbayAuth", lambda settings: object())
    monkeypatch.setattr("listflow.ebay.client.EbayClient", lambda settings, auth: object())


def test_draft_import_persists_row(monkeypatch):
    patch_extractor(monkeypatch, make_raw())
    _install_fake_publisher(monkeypatch, PublishResult(sku=SKU, offer_id="OFF-1", category_id="1"))
    result = runner.invoke(app, ["import", AMAZON_URL])
    assert result.exit_code == 0, result.output
    assert "DRAFT" in result.output
    assert "OFF-1" in result.output

    from listflow.storage import Tracker

    with Tracker.open() as tracker:
        row = tracker.get(SKU)
    assert row["status"] == "draft"
    assert row["ebay_offer_id"] == "OFF-1"
    assert row["last_step"] == "offer"


def test_publish_import_marks_published(monkeypatch):
    patch_extractor(monkeypatch, make_raw())
    _install_fake_publisher(
        monkeypatch, PublishResult(sku=SKU, offer_id="OFF-1", listing_id="LIST-9", category_id="1")
    )
    result = runner.invoke(app, ["import", AMAZON_URL, "--publish"])
    assert result.exit_code == 0, result.output
    assert "PUBLISHED" in result.output
    assert "LIST-9" in result.output

    from listflow.storage import Tracker

    with Tracker.open() as tracker:
        assert tracker.get(SKU)["status"] == "published"


def test_import_publish_failure_records_failed_and_hints_retry(monkeypatch):
    patch_extractor(monkeypatch, make_raw())
    from listflow.ebay.client import EbayApiError

    class FailingPublisher:
        def __init__(self, client, settings, on_step=None):
            self._on_step = on_step or (lambda s, step: None)

        def publish(self, product, pricing, **k):
            self._on_step(make_sku(product.source_id), "location")
            raise EbayApiError("POST /sell/inventory/v1/offer", 400,
                               [{"errorId": 25002, "message": "Missing aspect"}])

    monkeypatch.setattr("listflow.ebay.publisher.Publisher", FailingPublisher)
    monkeypatch.setattr("listflow.ebay.auth.EbayAuth", lambda settings: object())
    monkeypatch.setattr("listflow.ebay.client.EbayClient", lambda settings, auth: object())

    result = runner.invoke(app, ["import", AMAZON_URL])
    assert result.exit_code == 1
    assert "25002" in result.output
    assert f"listflow retry {SKU}" in result.output

    from listflow.storage import Tracker

    with Tracker.open() as tracker:
        row = tracker.get(SKU)
    assert row["status"] == "failed"
    assert row["last_step"] == "location"
    assert "25002" in row["notes"]


# ---------------------------------------------------------------- list/export

def test_list_empty(monkeypatch):
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "No imports" in result.output


def test_list_and_export_after_import(monkeypatch, tmp_path):
    patch_extractor(monkeypatch, make_raw())
    _install_fake_publisher(monkeypatch, PublishResult(sku=SKU, offer_id="OFF-1", category_id="1"))
    runner.invoke(app, ["import", AMAZON_URL])

    listed = runner.invoke(app, ["list"])
    assert SKU in listed.output

    filtered = runner.invoke(app, ["list", "--status", "published"])
    assert "No imports with status" in filtered.output

    out_csv = tmp_path / "export.csv"
    exported = runner.invoke(app, ["export", "--csv", str(out_csv)])
    assert "Exported 1 row" in exported.output
    assert out_csv.exists()
    assert SKU in out_csv.read_text(encoding="utf-8")


# ---------------------------------------------------------------- retry

def test_retry_unknown_sku(monkeypatch):
    result = runner.invoke(app, ["retry", "LF-nope-0000"])
    assert result.exit_code == 1
    assert "No import found" in result.output


def test_retry_resumes_failed(monkeypatch):
    from listflow.storage import Tracker

    with Tracker.open() as tracker:
        tracker.start(
            sku=SKU, platform="amazon", source_url=AMAZON_URL, source_id="B00BAGTNAQ",
            title_ebay="Pet Hair Remover Brush", cost=Decimal("10.00"),
            sell_price=Decimal("15.99"), margin_actual=Decimal("0.2276"),
        )
        tracker.finish(SKU, status="failed", offer_id="OFF-1", notes="offer failed")

    patch_extractor(monkeypatch, make_raw())
    captured = {}

    def fake_run_publish(settings, prepared, *, publish, category_id, existing_offer_id=None):
        captured.update(existing_offer_id=existing_offer_id, publish=publish)

    monkeypatch.setattr("listflow.cli._run_publish", fake_run_publish)
    result = runner.invoke(app, ["retry", SKU])
    assert result.exit_code == 0, result.output
    assert captured["existing_offer_id"] == "OFF-1"  # reuse, don't duplicate
