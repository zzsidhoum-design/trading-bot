"""Print a Data Quality Report over the persisted price universe.

Reads the live database (no network) and runs the same checks as the
``data_quality_cycle`` worker job: structural integrity, coverage, daily-gap
and daily/intraday consistency, plus freshness when the market is open.

Usage::

    .venv\\Scripts\\python.exe scripts\\data_quality_report.py
    .venv\\Scripts\\python.exe scripts\\data_quality_report.py --symbols AAPL,MSFT,TSLA

Exit code is 0 on PASS and 1 on FAIL (useful for CI gates).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from qtrader.application.services.data_quality import DataQualityAuditor
from qtrader.config.container import Container
from qtrader.config.settings import Settings


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--symbols",
        default=None,
        help="comma-separated symbols; defaults to the configured watchlist",
    )
    args = parser.parse_args()

    container = Container()
    try:
        settings = container.resolve(Settings)
        auditor = container.resolve(DataQualityAuditor)
        symbols = (
            [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
            if args.symbols
            else settings.watchlist_symbols
        )
        report = await auditor.audit(symbols)
    finally:
        await container.aclose()

    print(f"Data Quality Report  generated_at={report.generated_at.isoformat()}")
    print(f"scope={', '.join(report.scope) or 'none'}  "
          f"verdict={report.verdict}  score={report.score:.2f}")
    print("-" * 64)
    for check in report.checks:
        marker = "ok  " if check.passed else "FAIL"
        print(f"{marker} {check.name:<22} {check.detail}")
    print("-" * 64)
    sys.exit(0 if report.verdict == "PASS" else 1)


if __name__ == "__main__":
    asyncio.run(main())
