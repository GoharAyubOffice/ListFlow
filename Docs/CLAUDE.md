# CLAUDE.md — Instructions for Claude Code

## What this project is

**Listflow** — a personal CLI tool that imports a product from an AliExpress or Amazon product URL and creates a draft (or published) eBay listing via eBay's official Sell Inventory API. Single user, human-triggered, low volume (≤30 imports/day). It is NOT a SaaS product — do not add multi-tenancy, user accounts, queues, or web frameworks.

**Read `TECHNICAL_SPEC.md` in full before writing any code.** It is the source of truth for architecture, module layout, data models, extractor strategy, eBay pipeline, content rules, and test plan. If this file and the spec ever conflict, the spec wins; flag the conflict.

## Build order (follow this sequence)

1. **Scaffold** — `pyproject.toml`, package layout exactly as spec §2.1, ruff config, pytest config, `.env.example`, `.gitignore` (must ignore `.env`, `~/.listflow`, `credentials.json`, `tests/fixtures/*.html` stays committed).
2. **Models + pricing + content** (pure logic, no I/O) — `models.py`, `pricing.py`, `content.py` with their unit tests first. These are fully specified in spec §4 and §6; get them green before touching any network code.
3. **Detector** — `detector.py` + tests (spec §9.1 URL shapes).
4. **eBay auth + client** — `ebay/auth.py` (authorization-code grant, local callback listener on port 8912, refresh-token cache chmod 600), `ebay/client.py` (retry/backoff, error surfacing). Build against **Sandbox** (`EBAY_ENV=sandbox`). Never hardcode API hosts; read from env.
5. **Publisher pipeline** — `ebay/taxonomy.py`, `ebay/publisher.py` per spec §7.2, respx-mocked tests per §9.1.
6. **Amazon extractor** (easier, do first) — `extractors/amazon.py`. Before writing parse code, fetch one real product page, save it to `tests/fixtures/amazon_<asin>.html`, and develop against the fixture.
7. **AliExpress extractor** — `extractors/aliexpress.py` with Playwright persistent profile. Same fixture-first rule: capture the page + embedded JSON blob into `tests/fixtures/` first. The exact window key for the state object must be discovered from the captured page (candidates: `__INIT_DATA__`, `runParams`, script tags containing `skuModule`) — implement the runtime key-scan fallback described in spec §5.1.
8. **CLI + storage** — `cli.py` (Typer, commands per spec §8), `storage.py` (SQLite schema per spec §4.3), wire the full pipeline.
9. **Integration gates E1–E4** from spec §9.2, in order. Do not run E3 (production) without the human explicitly saying go.

## Hard rules

- **Sandbox first.** No production eBay calls until the human approves after a successful sandbox end-to-end publish.
- **Fixture-first extractor development.** Never iterate extractor parsing logic against live sites in a loop. Capture page → commit fixture → develop offline. Live hits are for capturing fixtures and final E1 verification only.
- **Politeness caps are load-bearing.** One live request at a time, randomised 2–5s dwell for Playwright, no parallel extraction, no retry storms (max 3, exponential backoff). Do not add proxy support, CAPTCHA solvers, fingerprint spoofing, or anything designed to defeat bot detection — if a site blocks a request, surface a clear error telling the user to run `--headed` and proceed manually. This tool stays at human scale by design.
- **Forbidden tokens** (`aliexpress`, `amazon`, `alibaba`, store names, `dropshipping`) must never survive into any listing field — this is a hard validation failure in `content.py`, covered by tests.
- **Secrets:** never print tokens; never commit `.env` or `credentials.json`; credentials file written with mode 600.
- **Money is `Decimal`,** never float. Prices rounded per spec §4.2.
- **Every eBay API failure** must print eBay's own `errors[].message` and `errorId` verbatim plus which pipeline step failed, and persist resumable state so `listflow retry <sku>` works.
- Keep dependencies minimal: typer, pydantic, httpx, selectolax, playwright, respx (dev), pytest (dev), ruff (dev). Justify anything beyond this list before adding it.

## When an extractor breaks (maintenance loop)

This will happen every few months. The repair procedure:
1. Reproduce with `listflow import <url> --dry-run` — the failure auto-saves raw HTML/JSON to `~/.listflow/debug/`.
2. Copy that snapshot into `tests/fixtures/` as a new dated fixture.
3. Fix the extractor until **both** old and new fixtures pass (or retire the old fixture with a dated comment explaining the site change).
4. Run the full offline suite, then one live `--dry-run` to confirm.

## Testing expectations

- `pytest` must pass **offline** in <30s — no test may hit a live site or the real eBay API (respx-mock everything).
- Table-driven tests for pricing and content rules (spec §9.1 lists the required cases — implement all of them).
- Every bug fixed gets a regression test.

## How to run (end state)

```bash
# one-time
cp .env.example .env            # fill eBay keys
playwright install chromium
listflow auth                   # OAuth consent → refresh token stored

# daily use
listflow import <url> --dry-run # inspect what would be listed
listflow import <url>           # create draft offer on eBay
listflow import <url> --publish # go live
listflow list                   # tracker table
listflow export --csv out.csv   # for the Excel workbook
```

## Style

Python 3.11+, full type hints, ruff-clean, small functions, no clever metaprogramming. Log with `logging` (INFO default, DEBUG via `--verbose`), human-readable CLI output via rich tables is fine (rich comes with typer). Comments explain *why*, not *what*.
