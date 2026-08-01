"""CLI entry point. Exposes system introspection and (later) agent control.

Usage: ``qtrader status``
"""

from __future__ import annotations

import typer

from qtrader.config.settings import Settings

app = typer.Typer(name="qtrader", help="Multi-Agent AI Trading System")


@app.command()
def status() -> None:
    """Show system configuration summary."""
    settings = Settings()
    typer.echo(f"mode           : {settings.qtrader_mode}")
    typer.echo(f"live enabled   : {settings.live_enabled}")
    typer.echo(
        f"database       : {settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}"
    )
    typer.echo(f"redis          : {settings.redis_url}")


@app.command()
def version() -> None:
    """Show version."""
    typer.echo("qtrader 0.1.0")


if __name__ == "__main__":
    app()
