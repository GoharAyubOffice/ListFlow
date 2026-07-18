"""Typer CLI entry point: auth, import, retry, list, export commands (spec §8).

Phase 3 adds `auth`; the remaining commands arrive in Phase 7.
"""

import logging

import typer

app = typer.Typer(help="Import AliExpress/Amazon products as eBay draft listings.")


@app.callback()
def main(verbose: bool = typer.Option(False, "--verbose", help="Enable DEBUG logging.")) -> None:
    """Listflow — AliExpress / Amazon -> eBay product import tool."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )


@app.command()
def auth() -> None:
    """One-time eBay OAuth consent flow; stores the refresh token locally."""
    from listflow.config import MissingConfigError, load_settings
    from listflow.ebay.auth import AuthError, EbayAuth

    try:
        settings = load_settings()
        typer.echo(f"Authorising against eBay {settings.ebay_env}…")
        EbayAuth(settings).run_consent_flow()
    except (MissingConfigError, AuthError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.secho("Done — eBay authorisation stored.", fg=typer.colors.GREEN)
