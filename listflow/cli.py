"""Typer CLI entry point: auth, import, retry, list, export commands (spec §8).

Wires the whole pipeline together. Extraction/pricing/content is delegated to
pipeline.py; eBay publishing to ebay/publisher.py; persistence to storage.py.
"""

import contextlib
import logging
import sys
from decimal import Decimal
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(help="Import AliExpress/Amazon products as eBay draft listings.")
console = Console()
logger = logging.getLogger(__name__)


@app.callback()
def main(verbose: bool = typer.Option(False, "--verbose", help="Enable DEBUG logging.")) -> None:
    """Listflow — AliExpress / Amazon -> eBay product import tool."""
    # Windows terminals default to cp1252, which can't encode £/✓/— in our output.
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )


def _load_settings():
    from listflow.config import MissingConfigError, load_settings

    try:
        return load_settings()
    except MissingConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


@app.command()
def auth() -> None:
    """One-time eBay OAuth consent flow; stores the refresh token locally."""
    from listflow.ebay.auth import AuthError, EbayAuth

    settings = _load_settings()
    try:
        typer.echo(f"Authorising against eBay {settings.ebay_env}…")
        EbayAuth(settings).run_consent_flow()
    except AuthError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.secho("Done — eBay authorisation stored.", fg=typer.colors.GREEN)


def _print_prepared(prepared, *, category_id: str | None) -> None:
    """Render the cleaned title, price breakdown, images, specifics (dry-run view)."""
    product, pricing = prepared.product, prepared.pricing

    console.print(f"\n[bold]{product.title_ebay}[/bold]  [dim]({len(product.title_ebay)}/80)[/dim]")

    price_table = Table(title="Price breakdown", show_header=False, title_justify="left")
    price_table.add_row("Source cost", f"{product.base_cost} {product.currency}")
    price_table.add_row("eBay fees (est)", f"£{pricing.ebay_fees_est}")
    price_table.add_row("Target margin", f"{pricing.target_margin}")
    price_table.add_row("[bold]Sell price[/bold]", f"[bold]£{pricing.sell_price}[/bold]")
    price_table.add_row("Net profit (est)", f"£{pricing.net_profit_est}")
    floor = ("[green]yes[/green]" if pricing.passes_floor else "[red]NO — below 20%[/red]")
    price_table.add_row("Margin actual", f"{pricing.margin_actual}  (passes floor: {floor})")
    console.print(price_table)

    known = [f"{a.width}x{a.height}" for a in product.images if a.width]
    if known:
        console.print(f"[bold]Images:[/bold] {len(product.images)}  [dim]{', '.join(known)}[/dim]")
    else:
        console.print(
            f"[bold]Images:[/bold] {len(product.images)}  "
            "[dim](sizes validated at publish; <500px rejected)[/dim]"
        )
    if category_id:
        console.print(f"[bold]Category:[/bold] {category_id}")
    else:
        console.print(
            "[bold]Category:[/bold] [dim]auto — resolved at publish via Taxonomy API[/dim]"
        )

    if product.item_specifics:
        spec_table = Table(title="Item specifics", show_header=False, title_justify="left")
        for key, value in product.item_specifics.items():
            spec_table.add_row(key, value)
        console.print(spec_table)

    if product.variants:
        console.print(
            f"[dim]{len(product.variants)} variants found; "
            "listing a single SKU. Use --variant to choose another.[/dim]"
        )


@app.command(name="import")
def import_(
    url: str = typer.Argument(..., help="AliExpress or Amazon product URL"),
    publish: bool = typer.Option(False, "--publish", help="Publish live instead of draft."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Extract + price + print; no eBay."),
    margin: float | None = typer.Option(None, "--margin", help="Override target margin."),
    headed: bool = typer.Option(False, "--headed", help="Visible browser (AliExpress)."),
    variant: str | None = typer.Option(None, "--variant", help='e.g. "Colour=Red,Size=XL".'),
    category: str | None = typer.Option(None, "--category", help="eBay category id override."),
    force: bool = typer.Option(False, "--force", help="Publish even if below margin floor."),
) -> None:
    """Import a product URL into an eBay draft offer (or --publish live)."""
    from listflow.content import ForbiddenTokenError
    from listflow.extractors.base import ExtractionError
    from listflow.pipeline import VariantError, prepare

    settings = _load_settings()
    margin_dec = Decimal(str(margin)) if margin is not None else None
    try:
        prepared = prepare(
            url, settings=settings, margin=margin_dec, variant=variant, headed=headed
        )
    except (ExtractionError, VariantError) as exc:
        typer.secho(f"Extraction failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    except ForbiddenTokenError as exc:
        typer.secho(
            f"Blocked: {exc}\nA forbidden supplier token is in the title or description "
            "body and cannot be auto-stripped — edit the source or skip this product.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1) from exc

    _print_prepared(prepared, category_id=category)

    if not prepared.pricing.passes_floor:
        if not force:
            typer.secho(
                "\nMargin is below the 20% floor — refusing. Re-run with --force to override.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)
        typer.secho("Margin below floor — proceeding because --force was given.",
                    fg=typer.colors.YELLOW)

    if dry_run:
        typer.secho("\nDry run — no eBay calls made, nothing stored.", fg=typer.colors.CYAN)
        return

    _run_publish(settings, prepared, publish=publish, category_id=category)


def _run_publish(settings, prepared, *, publish: bool, category_id: str | None,
                 existing_offer_id: str | None = None) -> None:
    from listflow.ebay.auth import EbayAuth
    from listflow.ebay.client import EbayApiError, EbayClient
    from listflow.ebay.publisher import Publisher, make_sku
    from listflow.images import ImageError
    from listflow.storage import Tracker

    product, pricing = prepared.product, prepared.pricing
    sku = make_sku(product.source_id)

    with Tracker.open() as tracker:
        tracker.start(
            sku=sku,
            platform=prepared.platform.value,
            source_url=str(product.source_url),
            source_id=product.source_id,
            title_ebay=product.title_ebay,
            cost=product.base_cost,
            sell_price=pricing.sell_price,
            margin_actual=pricing.margin_actual,
        )
        client = EbayClient(settings, EbayAuth(settings))
        publisher = Publisher(client, settings, on_step=tracker.mark_step)
        try:
            result = publisher.publish(
                product, pricing, publish=publish, category_id=category_id,
                existing_offer_id=existing_offer_id,
            )
        except (EbayApiError, ImageError) as exc:
            tracker.finish(sku, status="failed", notes=str(exc))
            typer.secho(f"\nPublish failed at step after "
                        f"'{tracker.get(sku)['last_step']}':", fg=typer.colors.RED, err=True)
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            typer.secho(f"State saved — resume with:  listflow retry {sku}", fg=typer.colors.YELLOW)
            raise typer.Exit(code=1) from exc

        status = "published" if result.listing_id else "draft"
        tracker.finish(sku, status=status, offer_id=result.offer_id,
                       listing_id=result.listing_id)

    _print_publish_result(settings, sku, result, status)


def _print_publish_result(settings, sku: str, result, status: str) -> None:
    base = "sandbox.ebay.com" if settings.ebay_env == "sandbox" else "ebay.com"
    console.print(f"\n[bold green]✓ {status.upper()}[/bold green]  SKU [bold]{sku}[/bold]")
    console.print(f"  Offer:   {result.offer_id}")
    if result.listing_id:
        console.print(f"  Listing: {result.listing_id}")
        console.print(f"  URL:     https://www.{base}/itm/{result.listing_id}")
    else:
        console.print(
            "  [dim]Draft offer created — publish from Seller Hub or run --publish.[/dim]"
        )


@app.command()
def retry(
    sku: str = typer.Argument(..., help="SKU of a failed import to resume"),
    publish: bool = typer.Option(False, "--publish", help="Publish live, not just draft."),
) -> None:
    """Resume a failed publish, re-extracting the source and reusing the SKU."""
    from listflow.content import ForbiddenTokenError
    from listflow.extractors.base import ExtractionError
    from listflow.pipeline import prepare
    from listflow.storage import Tracker

    settings = _load_settings()
    with Tracker.open() as tracker:
        row = tracker.get(sku)
    if row is None:
        typer.secho(f"No import found for SKU {sku!r}.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    if row["status"] not in ("failed", "draft"):
        typer.secho(f"SKU {sku} has status {row['status']!r} — nothing to retry.",
                    fg=typer.colors.YELLOW)
        raise typer.Exit(code=0)

    typer.echo(f"Retrying {sku} from {row['source_url']} (last step: {row['last_step']})…")
    try:
        prepared = prepare(row["source_url"], settings=settings)
    except (ExtractionError, ForbiddenTokenError) as exc:
        typer.secho(f"Re-extraction failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    # if a previous run already created the offer, update it instead of duplicating
    go_live = publish or row["status"] == "published" or bool(row["ebay_listing_id"])
    _run_publish(settings, prepared, publish=go_live, category_id=None,
                 existing_offer_id=row["ebay_offer_id"])


@app.command(name="list")
def list_(
    status: str | None = typer.Option(None, "--status", help="draft|published|failed|killed"),
) -> None:
    """Show the import tracker table."""
    from listflow.storage import Tracker

    with Tracker.open() as tracker:
        rows = tracker.all(status=status)
    if not rows:
        msg = "No imports recorded yet." if not status else f"No imports with status {status!r}."
        typer.echo(msg)
        return

    table = Table(title="Listflow imports")
    for col in ("created", "platform", "sku", "title", "cost", "sell", "margin", "status"):
        table.add_column(col)
    for row in rows:
        colour = {"published": "green", "draft": "cyan", "failed": "red", "killed": "dim"}.get(
            row["status"], "white"
        )
        table.add_row(
            row["created_at"][:16].replace("T", " "),
            row["source_platform"],
            row["ebay_sku"],
            (row["title_ebay"][:40] + "…") if len(row["title_ebay"]) > 40 else row["title_ebay"],
            f"£{row['cost']}",
            f"£{row['sell_price']}",
            row["margin_actual"],
            f"[{colour}]{row['status']}[/{colour}]",
        )
    console.print(table)


@app.command()
def export(csv_path: Path = typer.Option(..., "--csv", help="Output CSV path")) -> None:
    """Dump the tracker to CSV for the Excel workbook."""
    from listflow.storage import Tracker

    with Tracker.open() as tracker:
        count = tracker.export_csv(csv_path)
    typer.secho(f"Exported {count} row(s) to {csv_path}", fg=typer.colors.GREEN)


@app.command()
def gui() -> None:
    """Open the local Streamlit GUI (install with: pip install -e .[gui])."""
    import subprocess
    import sys

    try:
        import streamlit  # noqa: F401
    except ImportError as exc:
        typer.secho(
            'Streamlit is not installed — run:  pip install -e ".[gui]"',
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1) from exc

    app_path = Path(__file__).parent / "gui_app.py"
    typer.echo("Starting the Listflow GUI (Ctrl+C to stop)…")
    subprocess.run(
        [
            sys.executable, "-m", "streamlit", "run", str(app_path),
            "--browser.gatherUsageStats", "false",
            "--server.address", "127.0.0.1",
        ],
        check=False,
    )
