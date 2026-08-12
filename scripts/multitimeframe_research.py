"""Phase 3: run the multi-timeframe research engine and print the report.

Run:  python scripts/multitimeframe_research.py [--symbols AAPL,MSFT] [--days 730]

Determines which timeframes and timeframe combinations are most useful for the
Strategy Research Engine. Explicitly research-only: no strategies are built or
traded here. Outputs the per-timeframe study, combination results, walk-forward
OOS robustness, parameter sensitivity and the final ranked recommendations.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from qtrader.application.services.multitimeframe import MultitimeframeResearchEngine  # noqa: E402
from qtrader.config.container import Container  # noqa: E402


def _fmt(v: float | int | None, nd: int = 3) -> str:
    return f"{v:.{nd}f}" if v is not None else "n/a"


def _print_report(report: object) -> None:
    from qtrader.application.services.multitimeframe import ResearchReport

    r = report
    if not isinstance(r, ResearchReport):
        return
    print("\n=== Multi-Timeframe Research ===")
    print(f"as_of: {r.as_of}  window: {r.start} .. {r.end}")
    print(f"symbols: {len(r.symbols)}  limitations: {', '.join(r.limitations) or 'none'}")
    print(f"best roles: context={r.best_context.value} setup={r.best_setup.value} "
          f"entry={r.best_entry.value}")

    print("\n--- per-timeframe study ---")
    print(
        f"{'interval':<6} {'cov%':>5} {'gap':>5} {'noise':>7} {'vol':>6} "
        f"{'sig/d':>6} {'trades':>6} {'ret%':>7} {'sharpe':>6} {'stability':>8}"
    )
    for s in r.timeframe_studies:
        print(
            f"{s.interval.value:<6} {s.quality.coverage_pct * 100:>5.0f} "
            f"{s.quality.max_gap_bars:>5.1f} {s.noise_mean_abs_ret:>7.4f} "
            f"{s.volatility_annualized:>6.2f} {s.signals_per_day:>6.2f} "
            f"{s.n_trades:>6} {s.total_return * 100:>7.2f} {s.sharpe:>6.2f} "
            f"{s.signal_stability:>8.2f}"
        )

    print("\n--- cost sensitivity (per timeframe, total-return %) ---")
    for s in r.timeframe_studies:
        points = " ".join(
            f"{p.commission_bps:.0f}bps={p.total_return * 100:.1f}%"
            for p in s.cost_sensitivity
        )
        print(f"{s.interval.value:<6} {points}")

    print("\n--- combination results (sorted by rank) ---")
    for i, c in enumerate(r.combinations, 1):
        wf = c.walk_forward
        ps = c.param_sensitivity
        m = c.metrics
        print(
            f"[{i}] {c.combo.key}: ret={m.total_return * 100:.2f}% "
            f"sharpe={m.sharpe:.2f} dd={m.max_drawdown:.2f} pf={m.profit_factor:.2f} "
            f"trades={m.n_trades} | OOS sharpe={wf.oos_sharpe_mean:.2f} "
            f"OOS ret={wf.oos_return_mean * 100:.2f}% folds={wf.n_folds} "
            f"param-stability={ps.sharpe_positive_ratio:.2f}"
        )

    print("\n--- recommendations ---")
    for i, rec in enumerate(r.recommendations, 1):
        print(
            f"[{i}] {rec.combo.key}: robustness={rec.robustness} score={rec.score:.3f} "
            f"EV={rec.expected_value:.3f}% sharpe={rec.sharpe:.2f} "
            f"OOS sharpe={rec.oos_sharpe:.2f} regime-consistency={rec.regime_consistency:.2f}"
        )


def _write_json(path: str, report: object) -> None:
    from qtrader.application.services.multitimeframe import _jsonable

    with open(path, "w", encoding="utf-8") as fh:
        json.dump(_jsonable(report), fh, indent=2)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default=None, help="comma-separated override list")
    parser.add_argument("--days", type=int, default=None, help="lookback in days")
    parser.add_argument("--json", default=None, help="write report JSON to this path")
    args = parser.parse_args()

    container = Container()
    try:
        engine = container.resolve(MultitimeframeResearchEngine)
        symbols = None
        if args.symbols:
            symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        end = date.today()
        start = None
        if args.days:
            from datetime import timedelta

            start = end - timedelta(days=args.days)
        report = await engine.run(symbols=symbols, start=start, end=end)
        _print_report(report)
        if args.json:
            _write_json(args.json, report)
            print(f"\nreport written to {args.json}")
    finally:
        await container.aclose()


if __name__ == "__main__":
    asyncio.run(main())
