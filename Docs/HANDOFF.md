# LISTFLOW — Handoff & Progress Log

> **Purpose of this file:** If a new AI (or human) takes over this project, read this file
> FIRST, then the three docs below. It records exactly what has been built, verified, and
> decided so far, and precisely where to resume. **This file is updated at the end of every
> completed phase — updating it is part of each phase's definition of done.**

## Required reading order for a new AI

1. `Docs/HANDOFF.md` (this file) — current state, where to resume.
2. `Docs/CLAUDE.md` — hard rules and build order. Non-negotiable.
3. `Docs/TECHNICAL_SPEC.md` — **source of truth** for architecture, data models,
   extractor strategy, eBay pipeline, content rules, tests. If anything conflicts, the spec wins.
4. `Docs/IMPLEMENTATION_PLAN.md` — the phase-by-phase plan with exit gates.

## Project in one paragraph

**Listflow** is a personal Python CLI: `listflow import <aliexpress-or-amazon-url>` extracts
product data, normalises it to one Pydantic schema, prices it (Decimal only, margin + eBay
fees, round up to x.99), cleans content (≤80-char title, forbidden supplier tokens are a hard
failure), re-hosts images via eBay Media API, and creates a **draft offer** (or `--publish`)
through eBay's Sell Inventory API, logging to a SQLite tracker. Single user, ≤30 imports/day,
sandbox-first, NOT a SaaS. Do not add multi-tenancy, queues, web frameworks, or anti-bot tooling.

## Critical rules a new AI must not break (summary — full list in CLAUDE.md)

- **Sandbox first.** No production eBay calls until the human explicitly approves after a
  successful sandbox end-to-end publish (gate E2).
- **Fixture-first extractors.** Never iterate parsing logic against live sites; capture page →
  commit to `tests/fixtures/` → develop offline.
- **Forbidden tokens** (`aliexpress`, `amazon`, `alibaba`, `choice`, store names,
  `dropshipping`) must never survive into any listing field — hard validation failure.
- **Money is `Decimal`**, never float.
- **Secrets:** never print tokens; `.env` / `credentials.json` never committed; credentials
  file mode 600.
- `pytest` must pass **offline in <30s** — respx-mock all HTTP, no live sites in tests.
- Politeness caps: one live request at a time, 2–5s dwell, max 3 retries w/ backoff,
  no proxies/CAPTCHA solvers/fingerprint spoofing ever.
- Deps limited to: typer, pydantic, httpx, selectolax, playwright (+ pytest, respx, ruff dev).
  Justify anything else before adding.

## Environment facts (this machine)

- Windows 11, repo at `C:\Users\buttg\desktop\Listflow`, git branch `main`.
- Python **3.12.10** installed at `%LOCALAPPDATA%\Programs\Python\Python312\python.exe`.
  ⚠️ Plain `python` on PATH still resolves to **3.10** — always use the project venv.
- Project venv: `.venv\` (Python 3.12, git-ignored). Run tools as:
  `.venv\Scripts\python.exe -m pytest`, `.venv\Scripts\python.exe -m ruff check .`,
  `.venv\Scripts\listflow.exe --help`.
- Playwright chromium **not yet installed** (`playwright install chromium` — needed by Phase 6).
- eBay developer keysets **not yet available** (user registering at developer.ebay.com —
  needed by Phase 3). `.env` does not exist yet; template is `.env.example`.

---

# Phase log

## ✅ Phase 0 — Scaffold (COMPLETED 2026-07-17)

**What was built:**
- `pyproject.toml` — package `listflow`, `requires-python >=3.11`; runtime deps typer,
  pydantic≥2.7, httpx, selectolax, playwright; dev extra `[dev]` = pytest, respx, ruff;
  console script `listflow = listflow.cli:app`; ruff config (line 100, py311, rules
  E/F/I/UP/B/SIM) and pytest config (`testpaths=tests`, `-q`) inline.
- Full package layout per spec §2.1 — every module exists as a **docstring-only stub**
  stating its purpose and which phase implements it: `cli.py` (minimal Typer app only),
  `detector.py`, `models.py`, `normalize.py`, `pricing.py`, `content.py`, `images.py`,
  `storage.py`, `config.py`, `extractors/{base,amazon,aliexpress}.py`,
  `ebay/{auth,client,taxonomy,publisher}.py`.
- `tests/` — stub test modules (docstrings only, no test code yet) + `tests/fixtures/.gitkeep`.
- `.env.example` (EBAY_CLIENT_ID/SECRET/REFRESH_TOKEN, EBAY_ENV=sandbox), `.gitignore`
  (ignores `.env`, `credentials.json`, `.venv/`, `.listflow/`; fixtures stay committed),
  `README.md`.

**Verified (exit gate green):** `pip install -e .[dev]` on the 3.12 venv succeeded;
`pytest` collects cleanly (0 tests, expected); `ruff check .` passes; `listflow --help` runs.

**Decisions made:** none beyond the spec. No deviations.

**Known issues / debt:** none.

## ✅ Phase 1 — Pure core: models, pricing, content (COMPLETED 2026-07-17)

**What was built (tests written first, then implementations):**
- `tests/test_pricing.py` — table-driven per spec §9.1: 7-row expected-value table
  (typical, raw landing exactly on x.99, bump-up past .99, mid-band round-up, below-floor
  `passes_floor=False`, tiny cost → 0.99 minimum, zero-fee case), `.99`-ending sweep,
  Decimal-type assertions, zero/negative-cost `ValueError`, float-args `TypeError`,
  `fvf+margin ≥ 1` guard, negative-rate guard, and model-level float rejection for
  `Product`/`PricingResult`.
- `tests/test_content.py` — 80-char boundary (word-boundary truncation, exact-80,
  single-long-word hard cut), noise/emoji/platform-token stripping, ALL-CAPS handling,
  keyword front-loading + dedupe, forbidden tokens across every listing field (incl.
  variants and `extra_forbidden` store names), HTML sanitisation (script/iframe/attrs/
  hotlinked imgs never survive; only p/ul/li/b/br emitted; entities escaped),
  item-specifics alias mapping. 58 tests total.
- `listflow/models.py` — `SourcePlatform` (StrEnum), `ImageAsset`, `Variant`, `Product`
  (spec §4.1; `images` enforces `min_length=1`), `PricingResult` (spec §4.2), plus loose
  `RawProduct`/`RawVariant` intermediates for the extractors. All money fields use
  `Money = Annotated[Decimal, BeforeValidator(...)]` which **raises on float input**.
- `listflow/pricing.py` — `price(cost, *, margin=0.20, fvf_rate=0.128, fixed_fee=0.30,
  floor=MARGIN_FLOOR)`: spec §4.2 formula, rounds UP to nearest x.99; fees/net quantised
  to the penny (HALF_UP), `margin_actual` to 4 dp; Decimal-only (floats → `TypeError`);
  guards zero/negative cost, negative rates, `fvf_rate+margin ≥ 1`.
- `listflow/content.py` — `clean_title(raw, primary_keyword=None)` (emoji + noise-phrase
  + platform-token stripping, lone-separator cleanup, ≤80-char word-boundary truncation,
  optional keyword front-loading), `validate_forbidden(product, extra_forbidden=())` →
  `ForbiddenTokenError` naming token + field, `build_description(product, boilerplate="")`
  (source HTML reduced to plain text and rebuilt; boilerplate sanitised to allowed tags),
  `sanitize_html`, `map_item_specifics` (alias table + `Brand: Unbranded` default).
  Sanitiser is stdlib `html.parser` — no new dependencies.

**Verified (exit gate green):** `.venv\Scripts\python.exe -m pytest` → **58 passed in
0.22s** (offline); `.venv\Scripts\python.exe -m ruff check .` → all checks passed;
`grep` confirms zero httpx/playwright/sqlite imports in models/pricing/content.

**Decisions made (where the spec left room):**
- Float money is rejected at *both* boundaries: pydantic field validator (models) and
  `isinstance` checks in `price()` — fail loudly rather than silently coerce.
- Fees quantised HALF_UP to pennies; `margin_actual` to 4 dp; `passes_floor` compares the
  quantised margin to a `floor` parameter (default `MARGIN_FLOOR = 0.20`).
- ALL-CAPS rule: words with ≥5 letters all-caps are Title-Cased; shorter (USB, LED, 4K)
  kept — spec said "strip ALL-CAPS runs" but keeping acronyms preserves search keywords.
- Standalone separators (` - `, ` | `…) left behind by noise removal are dropped entirely.
- Forbidden matching: platform names as substrings (catches "AmazonBasics"); `choice`
  word-bounded only, so "many choices" is not a false positive.
- Descriptions: source HTML is flattened to plain text and rebuilt from our template —
  source markup, attributes and `<img>` hotlinks can never survive. "Spec table" is
  rendered as `<ul><li><b>K:</b> V</li></ul>` because `<table>` isn't in the allowed set.
- `ImageAsset.width/height` default to `None` (spec listed them without defaults, which
  pydantic would treat as required).
- `clean_title` grew an optional `primary_keyword` param to implement §6.1 front-loading.

**Known issues / debt:** none.

## ✅ Phase 2 — Detector (COMPLETED 2026-07-17)

**What was built (tests first):**
- `tests/test_detector.py` — 12 URL shapes per platform: desktop, bare-domain, mobile
  (`m.`, `/gp/aw/d/`), country domains (`.us`, `.ru`, `es.`, `.de`, `.com.au`),
  share/short links (`a.aliexpress.com`, `amzn.eu`, `amzn.to`, `a.co`), tracking params,
  scheme-less paste, uppercase URL. Plus 8 rejection cases: eBay, platform name only in
  the query string, lookalike domains (`amazonia.com`, `best-aliexpress-deals.com`),
  spoofed subdomain (`amazon.evil.example`), garbage/empty/blank input.
- `listflow/detector.py` — `detect(url) -> SourcePlatform`; hostname-only matching
  (registrable domain must be `aliexpress.*`/`amazon.*` or a known short-link host);
  tolerates missing scheme; unknown → `UnknownPlatformError(ValueError)` with a clear
  "supported platforms" message.

**Verified (exit gate green):** `pytest` → **91 passed in 0.20s** (offline);
`ruff check .` → all checks passed.

**Decisions made:** platform decision uses the hostname only (a brand name in a path or
query string never counts); after the brand label every remaining label must be a 2–3
letter TLD part, which rejects `amazon.evil.example`-style spoofs; Amazon short-link
hosts `amzn.to|eu|asia|in|com` and `a.co` accepted (they redirect — the extractor
follows them).

**Known issues / debt:** none.

## ✅ Phase 3 — eBay auth + client (Sandbox) (COMPLETED 2026-07-18)

**Live gate passed 2026-07-18:** user ran `listflow auth` against sandbox — consent →
code paste → token exchange → refresh token stored in `~/.listflow/credentials.json`.
Verified end-to-end with a real sandbox call through the full stack
(`EbayClient` → `EbayAuth` refresh → `GET /sell/account/v1/privilege` → HTTP 200).
Note: sandbox test user shows `sellerRegistrationCompleted: False` — may need
attention before gate E2 (sandbox publish).

**What was built (tests first):**
- `tests/test_config.py` (10), `tests/test_ebay_auth.py` (12), `tests/test_ebay_client.py`
  (8) — all respx-mocked / localhost-only; an autouse fixture points `LISTFLOW_HOME` at
  tmp so tests never touch real `~/.listflow`, and another clears real eBay env vars.
- `listflow/config.py` — `load_env_file` (tiny stdlib .env parser, existing environ wins;
  no python-dotenv dep), `load_settings` → `Settings` (pydantic): required
  `EBAY_CLIENT_ID`/`EBAY_CLIENT_SECRET` (missing → `MissingConfigError` with hint),
  `EBAY_RU_NAME` optional, `EBAY_ENV` sandbox-default, plus config.toml overrides
  (margin/fvf_rate/fixed_fee converted `Decimal(str(x))` at the boundary — no floats
  leak; max_qty, boilerplate, marketplace/currency, 3 business-policy IDs).
- `listflow/ebay/auth.py` — `EbayAuth`: `consent_url()` (auth.sandbox vs auth host from
  env; raises if RU_NAME missing), `_wait_for_callback_code()` (stdlib HTTPServer on
  127.0.0.1:8912, request-line logging suppressed so the code never hits logs, timeout →
  `AuthError`), `exchange_code()` (Basic-auth token POST → refresh token saved to
  `LISTFLOW_HOME/credentials.json`, `os.open(0o600)` + chmod), `get_access_token()`
  (in-memory cache with 60s safety margin, `force_refresh` flag, missing credentials →
  `NotAuthenticatedError("run listflow auth")`), `run_consent_flow()`. `SCOPES` =
  api_scope + sell.inventory + sell.account.readonly + sell.marketing.readonly.
- `listflow/ebay/client.py` — `EbayClient.request()`: host from `API_HOSTS[ebay_env]`,
  bearer + Content-Language en-GB + `X-EBAY-C-MARKETPLACE-ID` headers; 401 → one forced
  token refresh then retry; 429/5xx → exponential backoff 1s/2s/4s (max 3, injectable
  `sleep` for tests); other 4xx → immediate `EbayApiError` carrying eBay `errors[]`
  verbatim (errorId + message + which call). `get/post/put/delete` helpers.
- `listflow/cli.py` — minimal `auth` command wired (`listflow auth`), `--verbose` flag.

**Verified:** `pytest` → **120 passed in 4.71s** offline; `ruff check .` clean;
`listflow --help` shows the auth command.

**Decisions made:** no python-dotenv (stdlib parser keeps the dep list per CLAUDE.md);
LISTFLOW_HOME env var overrides `~/.listflow` (testability); access token cached in
memory only (never persisted); 401 handled by one forced refresh before failing;
callback listener suppresses request-line logging (auth code would appear in it).

**⚠️ Spec deviation (2026-07-18):** spec §7.1 assumed a localhost:8912 callback
listener, but eBay's developer portal only accepts **https://** redirect URLs, so the
redirect can never reach a plain local HTTP listener. `run_consent_flow` now uses a
**manual-paste flow**: redirect-URL fields stay blank in the portal (eBay shows its
default success page whose address bar carries `?code=`), the user pastes that URL into
the terminal, `_code_from_user_paste` extracts/decodes the code. The 8912 listener code
is kept for a future https-capable redirect. User's sandbox RuName:
`TOP_G_LABS_LTD-TOPGLABS-opencl-qhnfskhf` (goes in `.env` as `EBAY_RU_NAME`).

**⏳ Exit gate remaining (needs the human):** run `.venv\Scripts\listflow.exe auth`
with a filled `.env` (EBAY_CLIENT_ID/SECRET sandbox keyset + EBAY_RU_NAME; redirect-URL
fields left blank in the portal), approve in the browser with a **sandbox test user**,
paste the success-page URL into the terminal, confirm "Done — eBay authorisation
stored." and that `~/.listflow/credentials.json` exists. Then Phase 3 is ✅ and
Phase 4 (publisher pipeline, fully offline/respx) can start.

## ✅ Phase 4 — Publisher pipeline + images + taxonomy (COMPLETED 2026-07-18)

**What was built (tests first — `test_taxonomy.py` 4, `test_images.py` 9,
`test_publisher.py` 8, plus client extensions):**
- `listflow/ebay/client.py` extended: absolute-URL requests (Media API lives on
  `apim.[sandbox.]ebay.com` — added `MEDIA_HOSTS`/`media_base_url`), multipart `files=`
  uploads (drops the JSON Content-Type so httpx sets the boundary), public
  `marketplace_id`.
- `listflow/ebay/taxonomy.py` — `default_category_tree_id()` (marketplace-scoped) and
  `suggest_category(client, title, tree_id=None)` → top hit's categoryId; empty result →
  `CategorySuggestionError` telling the user to pass `--category`.
- `listflow/images.py` — `image_size()` reads dimensions from raw PNG/GIF/JPEG/WebP-VP8X
  headers (**no Pillow — dep list unchanged**); `fetch_images()` downloads sequentially
  (politeness cap), drops <500px/broken/unknown images with warnings, errors if none
  usable; `upload_images()` re-hosts via Media API POST `/commerce/media/v1_beta/image`
  (imageUrl from body, or follows the Location header), fills `asset.ebay_url`.
- `listflow/ebay/publisher.py` — `make_sku()` (`LF-{source_id[:12]}-{sha1[:4]}`,
  unsafe chars stripped), `Publisher.ensure_location()` (GET MAIN → 404 → create GB
  WAREHOUSE), `Publisher.publish(product, pricing, publish=False, category_id=None)`
  running location → images → category → inventory item (title/description/aspects/
  imageUrls/qty/condition NEW) → offer (FIXED_PRICE, price str from Decimal, policies
  from config when set) → optional publishOffer. Each completed step is appended to
  `PublishResult.steps_completed` **and** reported via `on_step(sku, step)` — storage.py
  (Phase 7) will persist this for `listflow retry`.

**Verified (exit gate green):** `pytest` → **148 passed in 11.51s** offline;
`ruff check .` clean. Covered: draft + publish happy paths with full payload
assertions, location auto-create, `--category` override skips taxonomy, 429-retry on
offer, partial failure (offer 400) records exactly the 4 completed steps, sku
stability, image size parsing per format, undersized/broken image handling, Media API
body/Location-header variants.

**Decisions made:** image dimensions parsed from file headers instead of adding
Pillow; `listingPolicies` omitted when no policy IDs configured (publish will fail at
E2 without them — the user task "create business policies" already covers this);
supplier `<img>` never reaches eBay (only `ebay_url`s go into imageUrls);
`on_step` callback decouples publisher from storage until Phase 7.

**Known issues / debt:** Media API sandbox behaviour unverified until E2; location
payload is minimal (country GB only) — may need a postcode for production publish.
## ✅ Phase 5 — Amazon extractor + normalize (COMPLETED 2026-07-18)

**Fixture:** `tests/fixtures/amazon_B00BAGTNAQ.html` (ChomChom Roller, amazon.co.uk,
2.3MB, captured 2026-07-18 with one polite httpx fetch — no robot check hit).

**What was built (tests first — `test_extractors.py` 15, `test_normalize.py` 12):**
- `listflow/config.py` — `listflow_home()` moved here from ebay/auth.py (extractors
  need the debug dir without depending on the ebay package; auth re-exports it).
- `listflow/extractors/base.py` — `Extractor` ABC; `ExtractionError(field_missing,
  page_snapshot_path)` whose str() names the snapshot; `save_debug_snapshot()` writes
  timestamped pages to `LISTFLOW_HOME/debug/` (the repair-loop raw material).
- `listflow/extractors/amazon.py` — httpx + selectolax, one pinned `SELECTORS` dict.
  Title `#productTitle`; price = first `.a-price .a-offscreen` (→ Decimal + currency
  from symbol); ASIN from `input#ASIN` or URL (`/dp/`, `/gp/product/`, `/gp/aw/d/`);
  bullets deduped; description `#productDescription` **falling back to
  `#aplus_feature_div`** (A+ content — the fixture page has no plain description
  block); images from `"hiRes"` inline JSON (fallback `"large"`, then `#landingImage`),
  deduped; attributes from the `#productOverview_feature_div` table + `#prodDetails`
  rows with a noise blocklist (ASIN/reviews/rank/dates); store name from `#bylineInfo`
  (log only). Robot-check markers → wait 30s → one retry → hard `ExtractionError` with
  snapshot ("run from your normal network" advice). `dimensionValuesDisplayData`
  presence → warns "importing selected variant only" (matrix is v2).
- `listflow/normalize.py` — `normalize(raw) -> Product`: variants without price
  dropped, unknown stock treated as in-stock (stock=1), `sku_suffix` generated from
  attribute values ("RED-XL", fallback "V<n>"), base_cost = cheapest in-stock variant
  else page price, images capped at `MAX_LISTING_IMAGES = 8` (spec target 4–8),
  `item_specifics` via content.map_item_specifics, store_name never enters Product.

**Verified (exit gate green):** `pytest` → **176 passed in 11.74s** offline;
`ruff check .` clean; live smoke on a *second* product (ACE2ACE B0819XVK92):
extract → normalize → clean_title (exactly 80 chars) → build_description →
validate_forbidden → price(£14.43 → £21.99, margin 0.2024, floor ok), 8 images,
5 specifics — all green.

**⚠️ Pipeline-order lesson (Phase 7 must respect this):** `validate_forbidden` runs on
the **rebuilt** description (`product.description_html = build_description(product)`),
not the raw supplier HTML — raw Amazon HTML always contains "amazon" (media CDN URLs)
and correctly fails validation. Order: normalize → clean_title → build_description →
validate → price → publish.

**Known issues / debt:** `_decap_shouting` title-cases all-caps brand names in titles
("ACE2ACE" → "Ace2Ace") — cosmetic, revisit if it bothers listings.
## ✅ Phase 6 — AliExpress extractor (COMPLETED 2026-07-18)

**Fixtures (live item, pet hair laundry ball):**
`tests/fixtures/aliexpress_1005010171981745.html` (402KB page),
`..._state.json` (80KB blob from `window._d_c_` + which key it came from),
`..._desc.html` (lazy description endpoint response). Two earlier candidate items
turned out to be dead listings (render an empty shell with hidden `<h1>Aliexpress</h1>`
— NOT a bot block; recognise this pattern before blaming detection).

**🔑 State discovery (pinned, the critical maintenance knowledge):** product data
lives in `window._d_c_` → `lifeCycleEventList[0].data` = module map:
`PRODUCT_TITLE.text`; `SKU.skuPaths[]` ({path "14:200004889", skuIdStr, skuStock,
salable}) + `SKU.skuProperties[]` (property/value id → display name + sku image);
`PRICE.skuPriceInfoMap[skuIdStr]` (salePriceString "￡0.85", originalPrice.{value,
currency}); `QUANTITY_PC.allSkuQuantityView[skuIdStr].maxBuyCount`;
`HEADER_IMAGE_PC.imagePathList`; `PRODUCT_PROP_PC.showedProps[]` (attrName/attrValue);
`DESC.msiteDescUrl` (tokenised lazy-description endpoint); `SHOP_CARD_PC.sellerInfo`
(storeName often absent). `runParams` exists but is **empty** on the current PDP.
JSON-LD (`@type: Product`) is embedded in the HTML — partial fallback.

**What was built (tests first — 16 new tests in `test_extractors.py`):**
- `listflow/extractors/aliexpress.py` — `AliExpressExtractor`: Playwright persistent
  profile at `LISTFLOW_HOME/chrome-profile`, headless default + `headed=True` toggle,
  randomised 2–5s dwell, `STATE_DUMP_JS` (pinned keys → runtime window scan returning
  `{key, json}`), state parsed with `json.loads(parse_float=Decimal)` so **no float
  ever touches money**; fallback chain parse(): state → JSON-LD (warns partial) → DOM
  (actionable schema-drift errors); `fetch_description()` pulls the lazy DESC url;
  block detection `_looks_blocked(html, final_url)` (x5secdata/punish/slide-to-verify
  in html, login/punish in final URL) → error instructing `--headed`, snapshot saved,
  no retries; sku matrix → `RawVariant`s with per-sku price/stock/image.
- Regression captured in tests: a normal page's `login.aliexpress` nav link must NOT
  count as a block (false positive found during the live smoke and fixed).

**Verified (exit gate green):** `pytest` → **192 passed in 16.39s** offline (<30s ✓);
`ruff check .` clean; live headless end-to-end smoke: extract → normalize →
clean_title (74 chars) → build_description → validate_forbidden → price: 5 variants,
base cost £0.85 (cheapest in-stock SKU) → sell £1.99, margin 0.2965, 6 images,
Brand XMSJ mapped. Headless worked on live items once the profile existed.

**Known issues / debt:** dead-listing shell pages surface as "no product title …
item may be unavailable" — fine, but a friendlier "item appears to be removed" check
(detect hidden-h1 shell) could come with Phase 7 polish. Item specifics include
`Origin: Mainland China` and `High-Concerned Chemical: None` — harmless, revisit.
## ✅ Phase 7 — CLI + storage (COMPLETED 2026-07-18)

**What was built (tests first — `test_storage.py` 14, `test_pipeline.py` 14,
`test_cli.py` 11, +1 publisher, +2 extractor regression):**
- `listflow/storage.py` — `Tracker` (SQLite at `LISTFLOW_HOME/listflow.db`), `imports`
  table per spec §4.3 + `last_step` resume column; money stored as **TEXT** (Decimal's
  own str, exact round-trip); `ebay_sku` UNIQUE so re-import/retry upserts.
  `start()` (inserts as 'failed' — a crash mid-pipeline stays retryable),
  `mark_step()`, `finish(status, offer_id, listing_id, notes)`, `set_status()`,
  `get()`, `all(status)` (newest first), `export_csv()`.
- `listflow/pipeline.py` — the testable orchestration core. `prepare_from_raw()` (pure:
  normalize → optional variant select → clean_title → build_description(boilerplate) →
  validate_forbidden(+store_name) → price); `select_variant()`/`VariantError`;
  `get_extractor()` (lazy import); `prepare()` (adds live detect+extract).
- `listflow/cli.py` — all spec §8 commands: `import <url>
  [--publish|--dry-run|--margin|--headed|--variant|--category|--force]`, `retry <sku>`,
  `list [--status]`, `export --csv`. Rich tables for dry-run (title+len, price
  breakdown, images, specifics) and `list`. Below-floor → refuse unless `--force`.
  Publish failure persists 'failed' + prints eBay error verbatim + "resume with
  listflow retry <sku>".
- `listflow/ebay/publisher.py` — `publish()` gained `existing_offer_id`: retry PUT-updates
  the existing offer instead of POSTing a duplicate (eBay allows one offer per SKU).
- `pyproject.toml` — ruff `extend-immutable-calls = [typer.Option, typer.Argument]`
  (the Typer DI idiom).

**🐛 Bug found by the live dry-run smoke (fixed + regression-tested):** the Amazon
extractor read item-specifics **reversed and full of noise** (Best Sellers Rank,
Customer Reviews with embedded JS, ASIN). Cause: `#prodDetails` rows are
`<th>key</th><td>value</td>`, but selectolax `css("td, th")` returns **selector order
(all td, then th)**, not document order — so key/value were swapped and the noise
blocklist checked the wrong cell (the old test passed *because* of the reversal — a
blind spot). Fixed with semantic th/td parsing (`_row_key_value`), prefer the clean
`#productOverview_feature_div` table, expanded blocklist, and a 100-char value guard.
Verified live: specifics now Brand/Material/Colour/Handle Material/Operation Mode only.

**Verified (exit gate green):** `pytest` → **230 passed in ~17s** offline (<30s ✓);
`ruff check .` clean; `listflow --help` shows all 5 commands; **live `import --dry-run`
end-to-end on a real Amazon URL** (ChomChom): title 73/80, £13.99 → £21.99 (margin
0.2224, passes floor), 8 images, clean specifics, no eBay calls / nothing stored.

**Decisions made:** dry-run does NOT download images (image sizes shown as "validated
at publish"; supplier download isn't an eBay call but adds latency — deferred to
publish where <500px is rejected); dry-run shows category as "auto — resolved at
publish" unless `--category` given (taxonomy needs a live call, and dry-run makes none);
`retry` re-extracts from the stored URL with default options (original --margin/--variant
not persisted — acceptable for v1) and reuses the SKU + any existing offer id.

**Known issues / debt:** retry loses the original --margin/--variant/--category choices
(re-extracts with defaults); `list` needs a wide-ish terminal (8 columns).

## ⏳ Phase 8 — Integration gates E1–E4 — E2 PASSED 2026-07-18 ← **RESUME HERE**

**✅ E2 — Sandbox publish PASSED (first real end-to-end):** `listflow import --publish`
(then `retry --publish`) put a live listing in sandbox. Verified via API: offer
`11323710010` status **PUBLISHED**, listing **110589969538**, 8 imageUrls accepted,
aspects Brand/Material/Handle Material/Colour/Operation Mode, price £21.99 GBP; tracker
row `published`. Category auto-suggested "Brushes, Combs & Rakes (46305)".
Test SKU: `LF-B00BAGTNAQ-59a4` (ChomChom, amazon.co.uk).

**Five sandbox-reality bugs found & fixed during E2 (all with regression tests):**
1. **Inventory location** — country-only address → `25802 Input error`. eBay needs a
   real ship-from address. Added `ship_from_address_line1/city/postal_code/country` to
   Settings + `config.toml`; publisher builds the address and raises a clear error if
   city/postcode are unset. (Sandbox placeholder address is in `config.toml`.)
2. **Media API 404 in sandbox** — `apim.sandbox.ebay.com/commerce/media/v1_beta/image`
   returns 404 (endpoint not available in sandbox; also flaky/503). Made `upload_images`
   **best-effort**: on failure it falls back to putting the source image URL in the
   inventory item's `imageUrls`, which eBay re-hosts to its own EPS at publish — the live
   listing still doesn't hotlink the supplier. New `listing_image_urls()` helper; publisher
   builds imageUrls from the fetched/validated assets (ebay_url or source_url). **Deviation
   from spec §6.5** (which mandated Media API) — justified: same end result, and Media is
   unavailable in sandbox. Revisit for production (Media API may work there).
3. **Transport errors uncaught** — a `WriteTimeout` crashed the CLI with a raw traceback.
   `EbayClient` now catches `httpx.TransportError`, retries like a 5xx (backoff, max 3),
   and raises a clean `EbayApiError(status_code=0, "network error: …")` if exhausted.
4. **Windows Unicode crash** — the `✓`/`£`/`—` in output raised `UnicodeEncodeError` on
   cp1252 consoles (after the publish had already succeeded). `cli.main` now reconfigures
   stdout/stderr to UTF-8.
5. **retry couldn't publish / resume a draft** — `retry` defaulted to draft (can't infer
   original `--publish` intent) and refused non-'failed' rows. Added `retry --publish` and
   allowed resuming `draft` rows (publishes the existing offer via `existing_offer_id`).

**Verified:** `pytest` → **235 passed** offline; `ruff` clean.

**✅ E1 — Live extract PASSED (2026-07-19):** 5 Amazon + 5 AliExpress dry-runs across
pet/kitchen/car/home, all fields populated, all sell prices clear the 20% floor.
Amazon: towels B0B4F78QZX £16.04→£24.99; garlic press B09YRSGLWQ £3.99→£6.99; car holder
B0CJHPPTV2 £12.99→£19.99; dog lick mat B0DP9Y388X £4.99→£7.99; KitchenCraft garlic
B0001IWXW4 £11.95→£18.99. AliExpress (all with SKU matrix): towels 1005010246193426
£24.53→£36.99; garlic mincer 1005011654023758 £3.91→£6.99; lick-mat insert 1005010161665899
£5.08→£8.99; TOPK car holder 1005012304329318 £8.45→£13.99; dog lick mat 1005010759204083
£0.99→£1.99. (Live AliExpress item IDs were harvested from search pages via the in-app
browser — web search doesn't surface live /item/ URLs; several candidates were dead
listings, which the extractor rejected cleanly with a snapshot.)

**Three real bugs found & fixed during E1 (all regression-tested):**
1. A supplier bullet "visit our Amazon Official store" hard-crashed the CLI via the
   forbidden-token guard → `strip_forbidden_content()` drops noisy bullets/specifics.
2. Amazon exposes the brand as "store name", so a brand like "Aileem" in a description
   falsely blocked the product → brands are no longer treated as forbidden.
3. A cross-sell sentence ("...into your Amazon search bar") blocked a product → such
   sentences are now dropped from the description; `validate_forbidden` is the safety net.

Minor debt: AliExpress specifics include low-value fields ("Cn: Hebei", "Set Type: Yes",
"Disposable: No") — cosmetic, not blocking.

**✅ Production setup + E3 DRAFT PASSED (2026-07-19):** user authed a **personal**
seller account (production, `sellerRegistrationCompleted: True`, limit £37k/5000);
opted in + created 3 **production** policies (payment 400048221023, return 400048222023,
fulfillment 400048223023); real dispatch address (41 Manor Drive, Wembley HA9 8EB) in
config. `config.toml` now **git-ignored** (holds the real address — never commit). Real
draft import succeeded: AliExpress garlic mincer → offer `210940067011` (UNPUBLISHED),
SKU LF-100501165402-af0b, category 122939, £8.99, all 3 policies attached, 6 images, NEW.
Read back from eBay to confirm.

**🐛 Bug found by E3 (fixed + regression-tested):** AliExpress serves **lossy VP8 WebP**
images under `.jpg` URLs; `image_size()` only handled VP8X, so every AliExpress image was
rejected → `ImageError`. Now parses VP8/VP8L/VP8X (verified 800×800 on the real images).
Also: `ImageError` now caught gracefully (records failed state + retry hint) instead of a
traceback. Invisible to E1 (dry-run skips image download) and E2 (Amazon JPEGs).
Also note: eBay **Media API returns 404 in production too** for this app → images fall
back to source URLs, which eBay re-hosts to its own EPS servers at publish (no hotlinks).

**✅ Source-aware postage (2026-07-19):** production has TWO fulfillment policies —
fast `400048223023` (3-day handling, Amazon) and slow `400048465023` (10-day handling,
AliExpress; supplier ships from abroad). `Settings.fulfillment_policy_id_slow` +
`Publisher._fulfillment_policy_id()` pick by `product.source_platform`. Boilerplate no
longer hard-codes a dispatch time (eBay's policy-driven estimate is authoritative).

**✅ Local GUI (2026-07-19, v2 feature):** `listflow gui` → Streamlit app
(`listflow/gui_app.py`, optional `[gui]` extra — justified deviation from the minimal-dep
rule; core CLI unaffected). Flow: URL → preview (price metrics, floor badge) → edit title
(80-char counter) / description (rendered preview) / image grid with per-image
checkboxes → Create draft / Publish live (reuses pipeline+publisher; edits re-validated
for forbidden tokens; below-floor blocked unless Force ticked). Verified in-browser
against a live AliExpress product. VPS deployment considered and rejected (datacentre
IPs get robot-checked; credentials belong local).

**⬜ Remaining (need the human):**
- **Review the draft** (offer 210940067011) and decide: publish live (`--publish`, explicit
  go) or delete the test draft. Note: AliExpress specifics include "Origin: Mainland China"
  + "High-Concerned Chemical: None" — low-value noise worth filtering before real listings.
- **E4 timing** — not yet measured.

**⬜ Earlier remaining gates:**
- **E3 — Production draft:** ⚠️ ONLY on explicit human go. Flip `EBAY_ENV=production`,
  create production business policies + real ship-from address in a production
  `config.toml`, re-run `listflow auth` against production, one draft import, review in
  Seller Hub. Media API may work in production (remove/keep fallback as needed).
- **E4 — Timing:** wall-clock ≤60s AliExpress, ≤15s Amazon.

---

# Pending user (human) tasks

| Task | Needed by | Status |
|---|---|---|
| Register at developer.ebay.com, create Sandbox + Production keysets | Phase 3 | ✅ done (sandbox) |
| Create eBay business policies (payment/return/postage) | Phase 4/E2 | ✅ done (via API, see below) |
| Copy `.env.example` → `.env` and fill keys | Phase 3 | ✅ done |
| Approve production use after sandbox E2E passes (gate E3) | Phase 8 | ⏳ E2 passed — awaiting go |

**Business policies (created 2026-07-18):** the sandbox Seller Hub UI for business
policies is broken (well-documented eBay issue) AND the account was not opted in
(`get_opted_in_programs` returned `[]`, policy calls returned errorId 20403 "User is
not eligible for Business Policy"). Fixed via API: added the **`sell.account` write
scope** to `ebay/auth.py` SCOPES (user re-ran `listflow auth`), called
`POST /sell/account/v1/program/opt_in {programType: SELLING_POLICY_MANAGEMENT}` (200),
then created three EBAY_GB policies via the Account API:
- payment_policy_id = `6237835000` (Listflow Payment)
- return_policy_id = `6237836000` (Listflow Returns, 30-day, buyer pays)
- fulfillment_policy_id = `6237837000` (Listflow Postage, 1-day handling, RM 2nd class £2.99)

These IDs + pricing + a shipping/returns boilerplate now live in `config.toml`
(project root, non-secret, loads into Settings — verified). **Regenerate all three IDs
for production before E3.**

---

# Template for future phase entries (copy when completing a phase)

```markdown
## ✅ Phase N — <name> (COMPLETED <date>)

**What was built:** <files created/changed, with the key functions/classes and behaviour>
**Verified (exit gate):** <exact commands run and their results>
**Decisions made:** <anything chosen that the spec left open, with rationale>
**Known issues / debt:** <anything deferred, flaky, or worth revisiting — or "none">
**User actions completed:** <if any>
```
