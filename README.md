# Listflow

Personal CLI tool: import a product from an AliExpress or Amazon URL and create a
draft (or published) eBay listing via eBay's official Sell Inventory API.

Docs: `Docs/TECHNICAL_SPEC.md` (source of truth), `Docs/CLAUDE.md`, `Docs/IMPLEMENTATION_PLAN.md`.

## Setup (one-time)

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -e .[dev]
playwright install chromium
cp .env.example .env             # fill eBay keys
listflow auth                    # OAuth consent -> refresh token stored
```

## Daily use

```bash
listflow import <url> --dry-run  # inspect what would be listed
listflow import <url>            # create draft offer on eBay
listflow import <url> --publish  # go live
listflow list                    # tracker table
listflow export --csv out.csv    # for the Excel workbook
```

## Development

```bash
pytest        # must pass offline in <30s — no live sites, no real eBay API
ruff check .
```
