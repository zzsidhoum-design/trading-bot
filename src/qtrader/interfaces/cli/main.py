"""CLI entry point. Exposes system introspection and agent control.

Usage::

    qtrader status
    qtrader run-agent data --symbol AAPL --interval 5m --days 30
    qtrader run-agent scanner
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import typer

from qtrader.application.agents.base import AgentContext
from qtrader.application.agents.registry import default_registry
from qtrader.config.container import Container
from qtrader.config.settings import Settings
from qtrader.domain.value_objects import Interval

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
    typer.echo(f"watchlist      : {', '.join(settings.watchlist_symbols)}")


@app.command()
def version() -> None:
    """Show version."""
    typer.echo("qtrader 0.1.0")


@app.command()
def run_agent(
    agent: str = typer.Argument(help="agent name (data, scanner, ...)"),
    symbol: str = typer.Option("AAPL", "--symbol", "-s"),
    interval: str = typer.Option("5m", "--interval", "-i"),
    days: int = typer.Option(30, "--days", "-d", help="backfill window (data agent)"),
) -> None:
    """Run a single agent standalone with a fresh composition root."""
    registry = default_registry()
    cls = registry.get(agent)
    if cls is None:
        typer.echo(f"unknown agent {agent!r}; available: {', '.join(registry.names)}", err=True)
        raise typer.Exit(code=2)

    settings = Settings()
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    ctx = AgentContext(
        symbol=symbol,
        interval=Interval(interval),
        start=start,
        end=end,
    )

    async def _main() -> None:
        container = Container(settings)
        try:
            instance = container.resolve(cls)
            await instance.run(ctx)
        finally:
            await container.aclose()

    asyncio.run(_main())


if __name__ == "__main__":
    app()
