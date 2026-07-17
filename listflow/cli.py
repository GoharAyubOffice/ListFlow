"""Typer CLI entry point: auth, import, retry, list, export commands (spec §8)."""

import typer

app = typer.Typer(help="Import AliExpress/Amazon products as eBay draft listings.")


@app.callback()
def main() -> None:
    """Listflow — AliExpress / Amazon -> eBay product import tool."""
