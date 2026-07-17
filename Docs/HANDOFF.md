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

## ⏳ Phase 3 — eBay auth + client (Sandbox) — NOT STARTED ← **RESUME HERE**

Offline build can proceed without keysets; only the final live `listflow auth` sandbox
verification is blocked on the user's `.env`. Next action: per `IMPLEMENTATION_PLAN.md`
Phase 3 — `config.py` (.env + optional config.toml, fail-fast on missing keys),
`ebay/auth.py` (authorization-code grant, localhost:8912/callback listener,
refresh-token store at `~/.listflow/credentials.json` chmod 600, access-token cache,
never print tokens), `ebay/client.py` (base URL from `EBAY_ENV`, bearer injection,
429/5xx retry max 3 exponential, `EbayApiError` carrying eBay's raw `errors[].message`
+ `errorId` + failing call), respx-mocked tests for token refresh / retry-on-429 /
error surfacing.

## ⬜ Phase 4 — Publisher pipeline + images + taxonomy — NOT STARTED
## ⬜ Phase 5 — Amazon extractor + normalize — NOT STARTED
## ⬜ Phase 6 — AliExpress extractor — NOT STARTED (needs `playwright install chromium`)
## ⬜ Phase 7 — CLI + storage — NOT STARTED
## ⬜ Phase 8 — Integration gates E1–E4 — NOT STARTED (E3 requires explicit human go)

---

# Pending user (human) tasks

| Task | Needed by | Status |
|---|---|---|
| Register at developer.ebay.com, create Sandbox + Production keysets | Phase 3 | ⏳ in progress |
| Create eBay business policies (payment/return/postage) in Seller Hub | Phase 4 | not started |
| Copy `.env.example` → `.env` and fill keys | Phase 3 | not started |
| Approve production use after sandbox E2E passes (gate E3) | Phase 8 | not started |

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
