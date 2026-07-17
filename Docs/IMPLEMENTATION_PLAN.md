# LISTFLOW — Implementation Plan v1.0

**Derived from:** `TECHNICAL_SPEC.md` v1.0 + `CLAUDE.md`
**Author role:** Senior engineering plan — architecture, build phases, step-by-step tasks with exit criteria.
**Rule of precedence:** If anything here conflicts with `TECHNICAL_SPEC.md`, the spec wins.

---

## 1. What we are building (one paragraph)

A single-command Python CLI (`listflow import <url>`) that takes an AliExpress or Amazon product URL, extracts structured product data, normalises it into one canonical Pydantic model, prices it (cost → eBay sell price with fees + margin baked in, rounded to x.99), cleans the content (≤80-char title, forbidden supplier tokens stripped, safe HTML description), re-hosts images through eBay's Media API, and creates a **draft offer** (or published listing with `--publish`) via eBay's Sell Inventory API — then logs everything to a local SQLite tracker. Personal tool, ≤30 imports/day, no SaaS features, no anti-bot arms race.

---

## 2. Architecture

### 2.1 Pipeline (data flow)

```
URL ──▶ detector.py ──▶ extractors/{amazon,aliexpress}.py ──▶ RawProduct
                                                                 │
                                                          normalize.py
                                                                 │
                                                             Product ──▶ pricing.py ──▶ PricingResult
                                                                 │
                                                            content.py  (title clean, forbidden-token
                                                                 │       validation, HTML description)
                                                            images.py   (download, ≥500px validate)
                                                                 │
                                              ebay/publisher.py pipeline:
                                              location → media upload → taxonomy
                                              → inventory item → offer → [publish]
                                                                 │
                                                            storage.py (SQLite tracker row)
```

### 2.2 Layering principles

1. **Pure core, I/O shell.** `models.py`, `pricing.py`, `content.py`, `normalize.py`, `detector.py` are pure logic — no network, no filesystem. They are built and unit-tested first and never import httpx/playwright.
2. **Every boundary is a Pydantic model.** Extractors emit `RawProduct`; normaliser emits `Product`; pricing emits `PricingResult`. Extraction drift fails loudly at validation, not silently downstream.
3. **One extractor interface.** `extractors/base.py` defines `extract(url) -> RawProduct` (ABC). Amazon = httpx + selectolax; AliExpress = Playwright persistent profile + embedded-JSON scan. Both share the same failure contract: `ExtractionError` that always writes a debug snapshot to `~/.listflow/debug/`.
4. **eBay isolated behind a thin client.** `ebay/client.py` owns base-URL-from-env (sandbox vs production — never hardcoded), auth header injection, retry/backoff (429/5xx, max 3, exponential), and verbatim surfacing of eBay `errors[].message` + `errorId`.
5. **Resumable pipeline.** Each publisher step records progress in SQLite so `listflow retry <sku>` resumes from the failed step, not from scratch.
6. **Everything money is `Decimal`.** No floats anywhere in pricing paths.

### 2.3 Module dependency graph (build in topological order)

```
config.py ── models.py ── detector.py
                │
     ┌──────────┼─────────────┐
 pricing.py  content.py   normalize.py
     │           │            │
     └───────────┴─────┬──────┘
                  extractors/ (base → amazon → aliexpress)
                       │
        ebay/auth → ebay/client → ebay/taxonomy → images.py → ebay/publisher
                       │
                  storage.py ── cli.py  (wires everything)
```

### 2.4 Key design decisions (locked by spec)

| Decision | Choice | Why |
|---|---|---|
| Listing path | eBay Sell **Inventory API** (inventory item → offer → publish) | Official, recommended, resumable steps |
| Images | Always re-hosted via eBay Media API | Supplier CDN links expire/watermark-swap |
| AliExpress data source | Page-embedded state JSON (key pinned at build time + runtime window-scan fallback) | The blob the site's own frontend renders from is the most durable target |
| Amazon data source | Server-rendered HTML via httpx; `SELECTORS` dict pinned in one place | No browser needed; one place to patch drift |
| Variants v1 | Publish cheapest in-stock variant as single SKU; print matrix; `--variant` override | Full `inventory_item_group` is v2 |
| Secrets | `.env` (git-ignored) + `~/.listflow/credentials.json` chmod 600 | Never printed, never committed |
| SKU scheme | `LF-{source_id[:12]}-{hash4}` | Stable, collision-resistant, traceable |
| Environments | `EBAY_ENV=sandbox` until E2 passes; production only on explicit human go | Hard rule |

---

## 3. Build phases, step by step

Each phase has an **exit gate** — do not start the next phase until the gate is green. This mirrors the build order in `CLAUDE.md` exactly.

### Phase 0 — Scaffold (~30 min)

1. `pyproject.toml`: package `listflow`, Python ≥3.11, deps: `typer`, `pydantic>=2`, `httpx`, `selectolax`, `playwright`; dev deps: `pytest`, `respx`, `ruff`. Console entry point `listflow = listflow.cli:app`.
2. Package layout exactly as spec §2.1 (empty modules with docstrings are fine at this stage).
3. `ruff` config (line length, target py311) and `pytest` config (testpaths, `-q`) in `pyproject.toml`.
4. `.env.example` with `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET`, `EBAY_REFRESH_TOKEN`, `EBAY_ENV=sandbox`.
5. `.gitignore`: `.env`, `~/.listflow` artefacts, `credentials.json`, `__pycache__`, `.venv` — but `tests/fixtures/*.html` **stays committed**.

**Exit gate:** `pip install -e .[dev]` works; `pytest` collects (0 tests ok); `ruff check` clean.

### Phase 1 — Pure core: models, pricing, content (test-first, no I/O)

1. `models.py` — `ImageAsset`, `Variant`, `Product`, `PricingResult` exactly per spec §4.1/§4.2, plus `RawProduct` (loose intermediate: raw strings/Decimals the extractors can realistically fill) and `SourcePlatform` enum.
2. `pricing.py` — `price(cost, *, margin, fvf_rate, fixed_fee) -> PricingResult`:
   - `sell_price = (cost + fixed_fee) / (1 - fvf_rate - target_margin)`, then round **up** to nearest x.99.
   - Compute `ebay_fees_est`, `net_profit_est`, `margin_actual`, `passes_floor = margin_actual >= 0.20`.
   - Guard zero/negative cost; all `Decimal`.
   - Tests first (table-driven, spec §9.1): at-floor, below-floor (`passes_floor=False`), .99 rounding boundaries, fee edge cases, zero/negative.
3. `content.py` —
   - `clean_title(raw) -> str`: strip noise tokens ("Hot Sale", "Free Shipping", "2026 New", "Dropshipping", emoji, ALL-CAPS runs), front-load primary keyword, ≤80 chars truncating at last full word.
   - `validate_forbidden(product) -> None`: raise on `aliexpress`, `amazon`, `alibaba`, `choice`, store names, `dropshipping` anywhere in any listing field — hard failure.
   - `build_description(product, boilerplate) -> str`: eBay-safe HTML subset only (`p, ul, li, b, br, img`), template = opening line → bullet benefits → spec table → shipping/returns boilerplate. Strip any script/active content.
   - `map_item_specifics(attrs) -> dict`: alias table (`colour→Colour`, `color→Colour`, `material→Material`, …), `Brand: Unbranded` default.
   - Tests first: 80-char boundary, forbidden tokens survive-nothing, emoji removal, script-tag sanitisation.

**Exit gate:** `test_pricing.py` + `test_content.py` green; ruff clean; zero network imports in these modules.

### Phase 2 — Detector

1. `detector.py`: `detect(url) -> SourcePlatform` from URL patterns — desktop, mobile (`m.`), share/short links, tracking params, for both platforms. Unknown → clear error.
2. `test_detector.py`: 10+ URL shapes per platform (spec §9.1).

**Exit gate:** detector tests green.

### Phase 3 — eBay auth + client (Sandbox)

1. `config.py`: load `.env` + optional `config.toml` (margin, fvf_rate, fixed_fee, max_qty, boilerplate block, policy IDs). Fail fast with a readable message on missing keys.
2. `ebay/auth.py`:
   - Authorization-code grant: open consent URL, local HTTP listener on `localhost:8912/callback` captures the code, exchange for refresh token.
   - Store in `~/.listflow/credentials.json`, **chmod 600**. Access-token cache with expiry; auto-refresh via refresh token.
   - Never log/print any token.
3. `ebay/client.py`: httpx wrapper — base URL from `EBAY_ENV` (`api.sandbox.ebay.com` / `api.ebay.com`), bearer injection, retry policy (429/5xx exponential backoff max 3; 4xx structural = no retry), and an `EbayApiError` that carries eBay's raw `errors[].message` + `errorId` + which call failed.
4. Tests: respx-mocked token refresh, retry-on-429 behaviour, error surfacing.

**Exit gate:** `listflow auth` completes against Sandbox and stores a working refresh token; client tests green offline.

### Phase 4 — Publisher pipeline (respx-tested)

1. `ebay/taxonomy.py`: `suggest_category(title) -> category_id` via Taxonomy API `getCategorySuggestions` (top hit), overridable by `--category`.
2. `images.py`: download source images (one at a time), validate ≥500px longest side, reject undersized, order main-first; upload via Media API → `ebay_url` filled on each `ImageAsset`.
3. `ebay/publisher.py`, steps exactly per spec §7.2:
   1. `ensure_location()` — createInventoryLocation once, key `"MAIN"`.
   2. `upload_images()`.
   3. `suggest_category(title)`.
   4. `createOrReplaceInventoryItem(sku)` — `sku = "LF-{source_id[:12]}-{hash4}"`.
   5. `createOffer(sku)` — `EBAY_GB`, GBP, business-policy IDs from config, qty from config (default 3).
   6. `--publish` ⇒ `publishOffer(offerId)`; else stop at draft.
   7. Persist row + print summary table with offer/listing URL.
   - After every successful step, update the SQLite row's step marker so `retry` can resume.
4. `test_publisher.py` (respx): happy path; 429-retry; partial failure (inventory item ok, offer fails) leaves correct resumable state.

**Exit gate:** publisher tests green offline, <30s total suite.

### Phase 5 — Amazon extractor (fixture-first)

1. **Capture first:** fetch one real product page once, save to `tests/fixtures/amazon_<asin>.html`. All parse development happens against this file — never a live loop.
2. `extractors/base.py`: ABC `extract(url) -> RawProduct`; `ExtractionError(field_missing=..., page_snapshot_path=...)` that always saves raw HTML/JSON to `~/.listflow/debug/` before raising.
3. `extractors/amazon.py`: httpx GET with realistic headers (UA, `Accept-Language: en-GB`); one `SELECTORS` dict (title `#productTitle`, price `.a-price .a-offscreen`, bullets `#feature-bullets li span`, description `#productDescription` + A+ best-effort, ASIN from URL/`#ASIN`); full-res images from `colorImages`/`imageGalleryData` inline JSON; variants: `dimensionValuesDisplayData` → v1 warns and imports the selected variant only. Robot-check detection → retry once after 30s → fail with `--playwright` instruction.
4. `normalize.py`: `RawProduct -> Product` (shared by both extractors — write it now against the Amazon path).
5. Tests: fixture-driven field assertions + one deliberately broken fixture asserting `ExtractionError` carries a snapshot path.

**Exit gate:** extractor + normalize tests green offline; one live `--dry-run`-style smoke extraction verified manually.

### Phase 6 — AliExpress extractor (fixture-first)

1. **Capture first:** run Playwright once against a real product page, save full HTML **and** the embedded state JSON blob to `tests/fixtures/`. Discover the actual window key from this capture (candidates: `__INIT_DATA__`, `runParams`, script tags containing `skuModule`) and pin it.
2. `extractors/aliexpress.py`:
   - Playwright Chromium, **persistent profile** at `~/.listflow/chrome-profile`; randomised 2–5s dwell before evaluate; `--headed` toggle for manual CAPTCHA solving.
   - Fallback chain: pinned state key → **runtime window-scan** (search `window` for an object with both a product-id field and a SKU/price structure) → JSON-LD → DOM scrape (log schema-drift warning).
   - Pull: productId, title, description (fetch lazy description endpoint if needed), image list (strip `_220x220`-style suffixes for full-size), SKU matrix (attributes, per-SKU price/stock/image), UK shipping estimate if exposed, store name (log only — never into listing).
   - 403/login-wall → clear error instructing `--headed`; **no silent retries, no proxies, no evasion.**
3. Tests against fixtures, same pattern as Amazon.

**Exit gate:** both extractors green against fixtures; suite still offline and <30s.

### Phase 7 — CLI + storage (wire it all)

1. `storage.py`: SQLite at `~/.listflow/listflow.db`, `imports` table per spec §4.3 (id, created_at, source fields, title_ebay, cost, sell_price, margin_actual, ebay_sku/offer_id/listing_id, status `draft|published|failed|killed`, notes) + pipeline-step column for resume.
2. `cli.py` (Typer), commands per spec §8:
   - `auth`, `import <url> [--publish|--dry-run|--margin|--headed|--variant|--category|--force]`, `retry <sku>`, `list [--status]`, `export --csv <path>`.
   - `--dry-run` prints everything that would be sent: cleaned title, full price breakdown, image count + sizes, chosen category, item specifics — rich tables.
   - Below-floor pricing: warn loudly and refuse without `--force`.
   - `--verbose` → DEBUG logging.

**Exit gate:** full offline `pytest` green <30s; `listflow import <url> --dry-run` works end-to-end on a live URL for both platforms.

### Phase 8 — Integration gates E1–E4 (manual, in order)

1. **E1 Live extract:** `--dry-run` on 5 real AliExpress + 5 real Amazon products (pet/kitchen/car). Pass = all fields populated, prices penny-accurate vs page.
2. **E2 Sandbox publish:** full `import --publish` on Sandbox for 3 products incl. one multi-image + one variant product. Pass = listings visible with images and specifics.
3. **E3 Production draft:** ⚠️ **only on explicit human go.** One draft import on the live account, human review in Seller Hub, then manual `--publish`.
4. **E4 Timing:** wall-clock per import ≤60s AliExpress, ≤15s Amazon.

**Done (v1 acceptance, spec §9.3):** ≥90% of candidate URLs → correct publishable draft in one command; no forbidden tokens or hotlinked supplier images ever; every failure leaves actionable error + debug snapshot + resumable state; offline suite green <30s.

---

## 4. Cross-cutting invariants (enforced from Phase 1 onward)

- **Sandbox until human says production.** Base URL always from env.
- **Fixture-first.** Live hits only to capture fixtures and for E1 verification.
- **Politeness caps:** one live request at a time, 2–5s dwell, max 3 retries with backoff, no parallel extraction, no anti-bot tooling.
- **Forbidden tokens = hard validation failure**, covered by tests.
- **Secrets:** never printed, never committed; credentials chmod 600.
- **`Decimal` for all money.**
- **Every eBay failure** prints eBay's own message/errorId verbatim + failing step, and persists resumable state.
- **Every fixed bug gets a regression test.**

## 5. Maintenance loop (when an extractor breaks)

1. Reproduce with `listflow import <url> --dry-run` — failure auto-saves snapshot to `~/.listflow/debug/`.
2. Copy snapshot into `tests/fixtures/` as a new dated fixture.
3. Fix until old + new fixtures both pass (or retire old fixture with a dated note).
4. Full offline suite, then one live `--dry-run` to confirm.

## 6. Estimated effort

| Phase | Estimate |
|---|---|
| 0 Scaffold | 0.5 h |
| 1 Core (models/pricing/content) | 3–4 h |
| 2 Detector | 0.5–1 h |
| 3 eBay auth + client | 2–3 h |
| 4 Publisher + images + taxonomy | 3–4 h |
| 5 Amazon extractor + normalize | 2–3 h |
| 6 AliExpress extractor | 3–4 h |
| 7 CLI + storage | 2 h |
| 8 Gates E1–E4 | 2–3 h (manual) |

Total ≈ 1.5–2 focused build days, matching the spec's target.
