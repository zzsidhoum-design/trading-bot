"""Backtest the ValueFactorStrategy end-to-end and persist to strategy_performance.

Run:  python scripts/backtest_value_factor.py [--rebalance N] [--quantile Q]

Mirrors walk_forward.py's wiring: loads full bars per symbol, computes
per-(symbol, date) probabilities via ValueFactorStrategy, feeds them to
BacktestRunner._simulate through the model_outputs contract, and persists the
PerformanceSummary (win_rate / profit_factor / sharpe / drawdown).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd

from qtrader.application.services.backtest import BacktestParams, BacktestRunner
from qtrader.application.services.indicators import IndicatorEngine
from qtrader.application.services.risk_calculator import RiskCalculator, RiskPolicy
from qtrader.application.services.strategies.base import StrategyInputs
from qtrader.application.services.strategies.value_factor import ValueFactorStrategy
from qtrader.config.container import Container
from qtrader.config.settings import Settings
from qtrader.domain.entities import BacktestRun
from qtrader.domain.ports import (
    BacktestRepository,
    PerformanceRepository,
    PriceRepository,
    SystemLogRepository,
)
from qtrader.domain.value_objects import Interval, Money

FUND_PATH = r"C:\Users\User\AppData\Local\Temp\opencode\fundamentals.pkl"
START = datetime(2021, 8, 1, tzinfo=UTC)
END = datetime(2026, 8, 10, 23, 59, 59, tzinfo=UTC)
MIN_HISTORY_BARS = 900


async def _universe() -> list[str]:
    import asyncpg

    from qtrader.config.settings import Settings

    settings = Settings()
    conn = await asyncpg.connect(settings.database_url.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        rows = await conn.fetch(
            """
            SELECT s.symbol
            FROM stocks s
            JOIN (SELECT stock_id, COUNT(*) AS n FROM prices
                  WHERE interval = '1d' GROUP BY stock_id HAVING COUNT(*) >= $1) p
              ON p.stock_id = s.id
            ORDER BY s.symbol
            """,
            MIN_HISTORY_BARS,
        )
    finally:
        await conn.close()
    return [r["symbol"] for r in rows]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rebalance", type=int, default=63)
    parser.add_argument("--quantile", type=float, default=0.15)
    args = parser.parse_args()

    container = Container()
    try:
        prices = container.resolve(PriceRepository)
        backtests = container.resolve(BacktestRepository)
        performance = container.resolve(PerformanceRepository)
        logs = container.resolve(SystemLogRepository)
        settings = container.resolve(Settings)

        symbols = await _universe()
        fund = pd.read_pickle(FUND_PATH)[["symbol", "asof", "book_per_share", "shares"]]
        fund = fund[fund["symbol"].isin(symbols)]
        print(f"universe: {len(symbols)} symbols, fundamentals rows: {len(fund)}", flush=True)

        bars_by_symbol: dict[str, list] = {}
        for i, symbol in enumerate(symbols):
            bars = await prices.history(symbol, Interval.D1, START, END, limit=50_000)
            bars_by_symbol[symbol] = sorted(bars, key=lambda b: b.ts)
            if (i + 1) % 50 == 0:
                print(f"  bars {i + 1}/{len(symbols)}", flush=True)

        strategy = ValueFactorStrategy(
            fundamentals=fund,
            rebalance_bars=args.rebalance,
            quantile=args.quantile,
        )
        inputs = StrategyInputs(
            bars_by_symbol=bars_by_symbol,
            series_by_symbol={},
            oos=bars_by_symbol,
        )
        probs = strategy.generate_probs(inputs)
        n_sig = sum(len(v) for v in probs.values())
        print(f"probs: {len(probs)} symbols, {n_sig} (symbol, ts) signals", flush=True)

        run = await backtests.create(
            BacktestRun(
                name=f"value-factor-{args.rebalance}",
                universe=symbols,
                start=START.date(),
                end=END.date(),
                initial_capital=Money(Decimal(str(settings.portfolio_initial_capital))),
                interval=Interval.D1,
                strategy="value_factor",
                commission_bps=Decimal("1.0"),
                slippage_bps=Decimal("5.0"),
            )
        )

        policy = RiskPolicy(
            risk_per_trade_pct=0.01,
            max_daily_loss_pct=0.10,
            max_portfolio_exposure_pct=1.0,
            max_positions=60,
            per_sector_limit_pct=1.0,
            max_position_pct_adv=0.05,
            min_cooldown_minutes=0.0,
            max_trades_per_day=200,
            atr_stop_mult=3.0,
            take_profit_r_mult=2.0,
            allow_add_to_position=False,
        )
        runner = BacktestRunner(
            prices=prices,
            backtests=backtests,
            performance=performance,
            risk_calculator=RiskCalculator(policy),
            indicator_engine=IndicatorEngine(),
            logs=logs,
        )
        params = BacktestParams(
            interval=Interval.D1,
            strategy="value_factor",
            commission_bps=1.0,
            slippage_bps=5.0,
            warmup_bars=30,
            stop_loss_pct=0.30,
            take_profit_pct=1.00,
            max_hold_bars=0,
        )
        series = {
            symbol: runner._indicator_engine.compute_series(bars, symbol, Interval.D1)
            for symbol, bars in bars_by_symbol.items()
            if bars
        }
        result = runner._simulate(
            run,
            bars_by_symbol,
            settings.portfolio_initial_capital,
            params,
            model_outputs=probs,
            series=series,
        )
        await runner._persist(result)

        s = result.summary
        print("\n=== ValueFactorStrategy backtest ===")
        print(f"strategy: value_factor  rebalance={args.rebalance}d  quantile={args.quantile}")
        print(f"period:   {s.period_start} .. {s.period_end}")
        print(f"trades:   {s.trades_count}")
        print(f"total_return: {s.total_return}   cagr: {s.cagr}")
        print(f"sharpe:   {s.sharpe}   sortino: {s.sortino}")
        print(f"max_drawdown: {s.max_drawdown}")
        print(f"win_rate: {s.win_rate}   profit_factor: {s.profit_factor}")
        print(f"expectancy: {s.expectancy}   avg_win: {s.avg_win}   avg_loss: {s.avg_loss}")
        print(f"turnover: {s.turnover}   total_costs: {s.total_costs}")
    finally:
        await container.aclose()


if __name__ == "__main__":
    asyncio.run(main())
