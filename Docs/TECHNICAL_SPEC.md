# LISTFLOW — Technical Specification v1.0

**Product:** AliExpress / Amazon → eBay Product Import Tool (personal CLI tool)
**Owner:** TOP AI LABS LTD
**Status:** Pre-development
**Target:** Working v1 in 1–2 focused build days
**Environment:** Local machine or Hetzner VPS (Ubuntu), Python 3.11+

---

## 1. Product Definition

### 1.1 Problem Statement
Manually creating an eBay listing from an AliExpress or Amazon source product takes 15–25 minutes per product (copying title, downloading/re-uploading images, rewriting description, entering item specifics, setting price). At 10–20 new test listings per week under a test-and-kill dropshipping framework, this is 3–8 hours/week of pure copy-paste labour.

### 1.2 Solution
A single-command CLI tool:

```
listflow import <product-url> [--publish] [--margin 0.25] [--dry-run]
```

which:
1. Detects the source platform (AliExpress or Amazon) from the URL.
2. Extracts full structured product data (title, price, images, description, variants, specifics).
3. Normalises it into one internal schema.
4. Applies the pricing formula (cost → eBay price with margin, fees, shipping baked in).
5. Cleans/rewrites listing content (strip supplier branding, enforce eBay title rules).
6. Pushes to eBay via the official **Sell Inventory API** as a **draft offer** (default) or published listing (`--publish`).
7. Logs the import into a local SQLite tracker (mirrors the existing Excel test-and-kill workbook fields).

### 1.3 Explicit Non-Goals (v1)
- No automated order placement on AliExpress/Amazon (manual fulfilment).
- No continuous price/stock re-sync (v2 candidate).
- No multi-user SaaS features, no web UI (CLI only; optional local web preview page is a stretch goal).
- No proxy rotation / anti-bot arms race — tool runs at human volume (max ~30 imports/day) from a residential connection or on-demand from VPS.

---

## 2. Architecture

```
┌────────────┐    ┌──────────────────┐    ┌──────────────┐    ┌───────────────┐
│  CLI (Typer)│──▶│ Source Detector   │──▶│  Extractor    │──▶│  Normalizer    │
└────────────┘    │ (URL pattern)     │    │  (per-site)   │    │ (ProductModel) │
                  └──────────────────┘    └──────────────┘    └──────┬────────┘
                                                                      ▼
┌────────────┐    ┌──────────────────┐    ┌──────────────┐    ┌───────────────┐
│ SQLite log  │◀──│  eBay Publisher   │◀──│ Content Clean │◀──│ Pricing Engine │
│ (tracker)   │    │ (Inventory API)   │    │ + Image Proc  │    │ (margin rules) │
└────────────┘    └──────────────────┘    └──────────────┘    └───────────────┘
```

### 2.1 Module Layout

```
listflow/
├── pyproject.toml
├── .env.example              # EBAY_CLIENT_ID, EBAY_CLIENT_SECRET, EBAY_REFRESH_TOKEN, EBAY_ENV
├── CLAUDE.md                 # instructions for Claude Code (separate file)
├── listflow/
│   ├── __init__.py
│   ├── cli.py                # Typer app: import, auth, list, retry commands
│   ├── detector.py           # URL → SourcePlatform enum
│   ├── models.py             # Pydantic: Product, Variant, ImageAsset, PricingResult
│   ├── extractors/
│   │   ├── base.py           # ABC: extract(url) -> RawProduct
│   │   ├── aliexpress.py     # Playwright → window JSON blob → RawProduct
│   │   └── amazon.py         # httpx + selectolax HTML parse → RawProduct
│   ├── normalize.py          # RawProduct -> Product (single canonical schema)
│   ├── pricing.py            # cost -> sell price (margin, eBay fees, shipping)
│   ├── content.py            # title truncation (80 chars), brand-strip, desc HTML build
│   ├── images.py             # download, validate (≥500px), re-host via eBay Media API
│   ├── ebay/
│   │   ├── auth.py           # OAuth2 refresh-token flow, token cache
│   │   ├── client.py         # thin REST wrapper (retry, error surface)
│   │   ├── taxonomy.py       # category suggestion via Taxonomy API getCategorySuggestions
│   │   └── publisher.py      # inventory_item -> offer -> publish pipeline
│   ├── storage.py            # SQLite: imports table (test-and-kill tracker fields)
│   └── config.py             # env + config.toml loading
└── tests/
    ├── fixtures/             # saved HTML/JSON pages for offline extractor tests
    ├── test_detector.py
    ├── test_extractors.py    # run against fixtures, NOT live sites
    ├── test_normalize.py
    ├── test_pricing.py
    ├── test_content.py
    └── test_publisher.py     # mocked eBay API (respx)
```

---

## 3. Technology Choices (with rationale)

| Concern | Choice | Rationale |
|---|---|---|
| Language | Python 3.11+ | Owner fluency; rich scraping/API ecosystem |
| CLI | Typer | Typed, autocompletes, minimal boilerplate |
| Data models | Pydantic v2 | Validation at every pipeline boundary; catches extraction drift loudly |
| AliExpress extraction | Playwright (Chromium, headed-optional) | Site is 100% client-rendered; data lives in a page-embedded JSON blob that the site's own frontend consumes — most durable target |
| Amazon extraction | httpx + selectolax | Product pages are largely server-rendered; no browser needed; selectolax is fast and lenient |
| eBay integration | Sell **Inventory API** (REST) + **Media API** (image hosting) + **Taxonomy API** (category suggestion) | Official, sanctioned, stable; Inventory API is eBay's recommended listing path |
| HTTP | httpx | Async-capable, HTTP/2, clean retry hooks |
| Storage | SQLite (stdlib sqlite3 or SQLModel) | Zero-ops, single file, sufficient at ≤10k rows |
| Config/secrets | .env + config.toml | Simple; secrets never committed |
| Tests | pytest + respx (HTTP mocking) + saved page fixtures | Extractors testable offline; API layer testable without live calls |
| Lint/format | ruff | One tool, fast |

---

## 4. Data Contracts

### 4.1 Canonical Product Model (Pydantic)

```python
class ImageAsset(BaseModel):
    source_url: HttpUrl
    ebay_url: HttpUrl | None = None      # after Media API upload
    width: int | None
    height: int | None

class Variant(BaseModel):
    sku_suffix: str                      # e.g. "RED-XL"
    attributes: dict[str, str]           # {"Colour": "Red", "Size": "XL"}
    source_price: Decimal
    stock: int
    image: ImageAsset | None

class Product(BaseModel):
    source_platform: Literal["aliexpress", "amazon"]
    source_url: HttpUrl
    source_id: str                       # AliExpress productId / Amazon ASIN
    title_raw: str
    title_ebay: str = ""                 # ≤80 chars, cleaned (content.py fills)
    description_html: str
    bullet_points: list[str]
    images: list[ImageAsset]             # main first; min 1, target ≥4
    variants: list[Variant]              # empty ⇒ single-SKU
    base_cost: Decimal                   # cheapest variant or single price, GBP
    currency: str
    weight_grams: int | None
    item_specifics: dict[str, str]       # brand, material, etc. (best-effort)
    extracted_at: datetime
```

### 4.2 Pricing Engine Contract

```python
class PricingResult(BaseModel):
    cost: Decimal              # supplier price incl. supplier shipping to buyer
    ebay_fees_est: Decimal     # final value fee ~12.8% + £0.30 (configurable)
    target_margin: Decimal     # default 0.20 (20% net) — from config
    sell_price: Decimal        # rounded to psychological .99
    net_profit_est: Decimal
    margin_actual: Decimal
    passes_floor: bool         # False ⇒ CLI warns loudly / refuses without --force
```

Formula (config-driven, defaults):
```
sell_price = (cost + fixed_fee) / (1 - fvf_rate - target_margin)
then round up to nearest x.99
passes_floor = margin_actual >= 0.20   # test-and-kill floor
```

### 4.3 SQLite `imports` table

```
id, created_at, source_platform, source_url, source_id,
title_ebay, cost, sell_price, margin_actual,
ebay_sku, ebay_offer_id, ebay_listing_id, status
  -- status: draft | published | failed | killed
notes
```
This mirrors the Excel Calculator + Tracker Log so results can be exported (`listflow export --csv`).

---

## 5. Extractor Specifications

### 5.1 AliExpress (`extractors/aliexpress.py`)

**Strategy:** launch Playwright Chromium with a **persistent user profile** (`user_data_dir=~/.listflow/chrome-profile`) so cookies/session persist and the browser looks like a normal returning visitor. Navigate to product URL, wait for network idle, then evaluate JS in page context to read the embedded state object.

**Extraction order of preference (fallback chain):**
1. Page-embedded init/state JSON object (the blob the site's own frontend renders from — check `window.__INIT_DATA__`, `window.runParams`, and script tags containing `"skuModule"` / structured price data; **discover the exact key at build time via DevTools and pin it, with a runtime scan fallback** that searches `window` for an object containing both a product-id field and a SKU/price structure).
2. JSON-LD `<script type="application/ld+json">` if present (partial data: title, image, price).
3. DOM selector scrape (last resort; log a warning that schema drift occurred).

**Fields to pull:** productId, subject/title, description (fetch the separate description iframe/endpoint if description is lazy-loaded), image list (upgrade thumbnail URLs to full-size by stripping size suffixes like `_220x220`), SKU matrix (attribute names/values, per-SKU price + stock + image), shipping-to-UK estimate if exposed, store name (for logging only — never into listing).

**Anti-blocking posture (deliberately minimal):**
- Human-triggered, one product at a time, headed-mode toggle (`--headed`) for the rare CAPTCHA — user solves it manually in the visible window, extraction resumes.
- Randomised 2–5s dwell before evaluate; real Chrome profile; **no proxies** at this volume.
- If HTTP 403 / login-wall detected → clear error message instructing `--headed` run, never silent retry loops.

**Failure contract:** raises `ExtractionError(field_missing=..., page_snapshot_path=...)` — always saves the raw HTML + JSON blob to `~/.listflow/debug/` so the extractor can be repaired against the actual page (this is the file you hand to Claude Code when a site update breaks parsing).

### 5.2 Amazon (`extractors/amazon.py`)

**Strategy:** plain `httpx` GET with realistic browser headers (UA, Accept-Language: en-GB); parse with selectolax.

**Selectors (pin at build time, keep in one `SELECTORS` dict for easy patching):**
- Title: `#productTitle`
- Price: `.a-price .a-offscreen` (first occurrence in buybox context)
- Bullets: `#feature-bullets li span`
- Images: parse the `"colorImages"` / `'imageGalleryData'` JSON inside inline scripts (gives full-res URLs, better than `<img>` tags)
- Description: `#productDescription`, plus A+ content best-effort
- ASIN: from URL or `#ASIN` input
- Variants: `dimensionValuesDisplayData` inline JSON when present (v1: warn + import selected variant only; full Amazon variant matrix is a v2 item)

**Fallback:** if response looks like a robot-check page (detect "captcha" markers), retry once after 30s, then fail with an instruction to use `--playwright` flag (shared Playwright fallback path for Amazon).

### 5.3 Fixture policy (critical for maintainability)
Every extractor change must save at least one **fixture**: full raw HTML/JSON of a real product page committed to `tests/fixtures/`. Extractor unit tests run only against fixtures — never live sites — so the test suite is fast, deterministic, and CI-safe. When a live site changes and breaks extraction, capture the new page as a fixture first, then fix the extractor until old+new fixtures both pass (or retire the old fixture with a dated note).

---

## 6. Content Rules (`content.py`)

1. **Title:** ≤80 chars (eBay hard limit). Strip supplier noise tokens (`"Hot Sale"`, `"Free Shipping"`, `"2026 New"`, `"Dropshipping"`, emoji, ALL-CAPS runs). Front-load primary keyword (consistent with the established ASO methodology). If >80 chars after cleaning, truncate at last full word.
2. **Forbidden strings anywhere in listing:** `aliexpress`, `amazon`, `alibaba`, supplier/store names, `choice`, watermark-suggesting text. Hard validation failure if found post-clean.
3. **Description:** rebuild as clean minimal HTML (eBay-safe subset: p, ul, li, b, br, img). Template: opening line → bullet benefits (from bullets/specs) → spec table → shipping/returns boilerplate block from config. **No active content/JS** (eBay bans it).
4. **Item specifics:** map extracted attributes to eBay aspect names via a small alias table (`colour→Colour`, `color→Colour`, `material→Material`...). Fill `Brand: Unbranded` when unknown.
5. **Images:** min 1, target 4–8; reject <500px on longest side (eBay requirement); first image must be main/white-ish product shot if identifiable; upload all through eBay Media API (`createImageFromFile`) so listing never hotlinks supplier CDNs (they expire/watermark-swap).

---

## 7. eBay Integration Spec

### 7.1 One-time setup (manual, ~1 hour)
1. Register at developer.ebay.com → create keyset (Production + Sandbox).
2. Required OAuth scopes: `sell.inventory`, `sell.account.readonly`, `sell.marketing.readonly` (inventory is the essential one; commerce media scope for image upload).
3. Run `listflow auth` → opens consent URL (authorization-code grant), captures code via local redirect listener on `http://localhost:8912/callback`, exchanges for refresh token, stores encrypted-at-rest in `~/.listflow/credentials.json` (chmod 600).
4. Prereq on the eBay account itself: at least one **business policy** each for payment, return, and postage (created once in Seller Hub); tool reads their IDs via Account API `getFulfillmentPolicies` etc. and caches them in config.

### 7.2 Publish pipeline (`publisher.py`)
```
1. ensure_location()            # createInventoryLocation once (key: "MAIN")
2. upload_images()              # Media API → ebay image URLs
3. suggest_category(title)      # Taxonomy API getCategorySuggestions → top hit,
                                #   overridable with --category <id>
4. createOrReplaceInventoryItem(sku)     # sku = "LF-{source_id[:12]}-{hash4}"
5. createOffer(sku)             # marketplace EBAY_GB, GBP, policies from config,
                                #   listed qty default 3 (config: max_qty)
6. if --publish: publishOffer(offerId)   # else stop at draft offer
7. write SQLite row; print summary table + listing/offer URL
```

**Multi-variant products:** v1 publishes the **cheapest in-stock variant** as a single-SKU listing and prints the variant matrix so the user can choose otherwise via `--variant "Colour=Red,Size=XL"`. Full `inventory_item_group` variation listings are **v2**.

### 7.3 Error handling
- Every eBay error response is printed with its raw `errors[].message` + `errorId` (eBay's errors are actually descriptive).
- Categorise: **retryable** (429, 5xx → exponential backoff, max 3) vs **structural** (missing aspect, bad category → actionable message, no retry).
- Partial-failure recovery: if inventory item created but offer failed, `listflow retry <sku>` resumes from the failed step using SQLite state.

### 7.4 Sandbox-first rule
All integration development happens against **Sandbox** (`EBAY_ENV=sandbox`, `api.sandbox.ebay.com`) until the full pipeline passes an end-to-end sandbox publish. Only then flip `EBAY_ENV=production`. The client reads the base URL from env — no hardcoded hosts.

---

## 8. CLI Surface

```
listflow auth                          # one-time OAuth dance
listflow import <url>                  # extract → price → draft offer (default)
listflow import <url> --publish        # go live immediately
listflow import <url> --dry-run        # extract + price + print, no eBay calls
listflow import <url> --margin 0.25    # override margin floor for this run
listflow import <url> --headed         # visible browser (CAPTCHA/manual assist)
listflow import <url> --variant "Colour=Red"
listflow retry <sku>                   # resume a partially failed publish
listflow list [--status draft]         # show tracker table
listflow export --csv out.csv          # dump tracker for the Excel workbook
```

`--dry-run` output must show: cleaned title, price breakdown (cost/fees/margin/sell), image count + sizes, chosen category, all item specifics — i.e. everything that would be sent, so a human can approve before any real import.

---

## 9. Testing Plan

### 9.1 Unit (offline, fast, run on every change)
- `test_detector.py`: 10+ URL shapes per platform incl. mobile URLs, tracking params, share links.
- `test_extractors.py`: fixtures → assert every canonical field populated + typed; include one deliberately broken fixture asserting `ExtractionError` carries a debug snapshot path.
- `test_pricing.py`: table-driven: margins at floor, below floor (expect `passes_floor=False`), rounding to .99, fee edge cases, zero/negative guard.
- `test_content.py`: 80-char boundary, forbidden-token stripping, emoji removal, HTML sanitisation (script tags stripped).
- `test_publisher.py`: respx-mocked happy path; 429 retry behaviour; partial-failure state written correctly.

### 9.2 Integration (manual gates, in order)
1. **E1 — Live extract:** run `--dry-run` against 5 real AliExpress + 5 real Amazon products across categories (pet, kitchen, car — matching the current candidate list). Pass = all fields populated, prices correct to the penny vs the page.
2. **E2 — Sandbox publish:** full `import --publish` on eBay Sandbox for 3 products incl. one multi-image, one variant product. Pass = listing visible in sandbox with images and specifics.
3. **E3 — Production draft:** `import` (draft only) of 1 real product on the live account; human-review the draft in Seller Hub; then `--publish` manually.
4. **E4 — Timing:** measure wall-clock per import. Target: **≤60s AliExpress, ≤15s Amazon** (vs 15–25 min manual).

### 9.3 Acceptance criteria (v1 "done")
- One command turns a source URL into a correct, publishable eBay draft ≥90% of the time on the current product-candidate list.
- No listing ever contains a forbidden supplier token or hotlinked supplier image.
- Any failure leaves an actionable error + debug snapshot + resumable state.
- Full pytest suite green, offline, <30s.

---

## 10. Risks & Mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| AliExpress JSON key/schema changes | Medium (every few months) | Runtime key-scan fallback; debug snapshots; fixture-first repair loop with Claude Code |
| Amazon robot-check on VPS IP | Medium | Prefer running extraction locally; `--playwright` fallback; keep volume human-scale |
| eBay flags dropship listings (VeRO/duplicates) | Low–Med | Content rewrite step; unique titles; images re-hosted; qty capped low |
| Supplier ToS (scraping) | Accepted risk | Personal-scale, own-account, no redistribution of data; equivalent to manual copying in substance |
| OAuth token leakage | Low | chmod 600, .env git-ignored, no tokens in logs |
| Category suggestion picks wrong leaf | Medium | Always shown in dry-run; `--category` override; store last-used category per keyword |

## 11. v2 Backlog (explicitly deferred)
Variation listings (inventory_item_group) · scheduled price/stock re-check with delta alerts · bulk import from a CSV of URLs · Amazon full variant matrix · simple local web dashboard · kill-trigger automation (auto-end listings at zero views/watchers after 14 days, matching the test-and-kill rule).
