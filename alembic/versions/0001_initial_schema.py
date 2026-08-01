"""Initial schema: universe, market data, signals, trading, ops.

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-01
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stocks",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("exchange", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("sector", sa.String(length=64), nullable=True),
        sa.Column("industry", sa.String(length=64), nullable=True),
        sa.Column("market_cap", sa.Numeric(20, 2), nullable=True),
        sa.Column("shares_outstanding", sa.Numeric(20, 2), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_stocks"),
        sa.UniqueConstraint("symbol", "exchange", name="uq_stocks_symbol_exchange"),
    )
    op.create_index("ix_stocks_sector", "stocks", ["sector"])

    op.create_table(
        "portfolios",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("initial_capital", sa.Numeric(20, 2), nullable=False),
        sa.Column("current_cash", sa.Numeric(20, 2), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_portfolios"),
    )

    op.create_table(
        "prices",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("stock_id", sa.BigInteger(), nullable=False),
        sa.Column("interval", sa.String(length=8), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(18, 6), nullable=False),
        sa.Column("high", sa.Numeric(18, 6), nullable=False),
        sa.Column("low", sa.Numeric(18, 6), nullable=False),
        sa.Column("close", sa.Numeric(18, 6), nullable=False),
        sa.Column("volume", sa.Numeric(20, 0), nullable=False),
        sa.Column("vwap", sa.Numeric(18, 6), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(["stock_id"], ["stocks.id"], name="fk_prices_stock_id_stocks"),
        sa.PrimaryKeyConstraint("id", name="pk_prices"),
        sa.UniqueConstraint("stock_id", "interval", "ts", name="uq_prices_stock_interval_ts"),
    )
    op.create_index("ix_prices_stock_interval_ts", "prices", ["stock_id", "interval", "ts"])

    op.create_table(
        "indicators",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("stock_id", sa.BigInteger(), nullable=False),
        sa.Column("interval", sa.String(length=8), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rsi", sa.Numeric(12, 6), nullable=True),
        sa.Column("ema_9", sa.Numeric(12, 6), nullable=True),
        sa.Column("ema_21", sa.Numeric(12, 6), nullable=True),
        sa.Column("sma_50", sa.Numeric(12, 6), nullable=True),
        sa.Column("sma_200", sa.Numeric(12, 6), nullable=True),
        sa.Column("macd", sa.Numeric(12, 6), nullable=True),
        sa.Column("macd_signal", sa.Numeric(12, 6), nullable=True),
        sa.Column("macd_hist", sa.Numeric(12, 6), nullable=True),
        sa.Column("atr", sa.Numeric(12, 6), nullable=True),
        sa.Column("vwap", sa.Numeric(18, 6), nullable=True),
        sa.Column("boll_upper", sa.Numeric(12, 6), nullable=True),
        sa.Column("boll_middle", sa.Numeric(12, 6), nullable=True),
        sa.Column("boll_lower", sa.Numeric(12, 6), nullable=True),
        sa.Column("adx", sa.Numeric(12, 6), nullable=True),
        sa.Column("stoch_k", sa.Numeric(12, 6), nullable=True),
        sa.Column("stoch_d", sa.Numeric(12, 6), nullable=True),
        sa.Column("ichimoku_tenkan", sa.Numeric(12, 6), nullable=True),
        sa.Column("ichimoku_kijun", sa.Numeric(12, 6), nullable=True),
        sa.Column("ichimoku_senkou_a", sa.Numeric(12, 6), nullable=True),
        sa.Column("ichimoku_senkou_b", sa.Numeric(12, 6), nullable=True),
        sa.Column("ichimoku_chikou", sa.Numeric(12, 6), nullable=True),
        sa.Column("volume_profile", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("extras", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(["stock_id"], ["stocks.id"], name="fk_indicators_stock_id_stocks"),
        sa.PrimaryKeyConstraint("id", name="pk_indicators"),
        sa.UniqueConstraint("stock_id", "interval", "ts", name="uq_indicators_stock_interval_ts"),
    )
    op.create_index("ix_indicators_stock_interval_ts", "indicators", ["stock_id", "interval", "ts"])

    op.create_table(
        "fundamentals",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("stock_id", sa.BigInteger(), nullable=False),
        sa.Column("period", sa.String(length=16), nullable=False),
        sa.Column("report_date", sa.Date(), nullable=True),
        sa.Column("revenue", sa.Numeric(20, 2), nullable=True),
        sa.Column("eps", sa.Numeric(14, 6), nullable=True),
        sa.Column("pe_ratio", sa.Numeric(14, 6), nullable=True),
        sa.Column("debt_total", sa.Numeric(20, 2), nullable=True),
        sa.Column("cash_flow", sa.Numeric(20, 2), nullable=True),
        sa.Column("roe", sa.Numeric(10, 6), nullable=True),
        sa.Column("roa", sa.Numeric(10, 6), nullable=True),
        sa.Column("gross_margin", sa.Numeric(10, 6), nullable=True),
        sa.Column("operating_margin", sa.Numeric(10, 6), nullable=True),
        sa.Column("net_margin", sa.Numeric(10, 6), nullable=True),
        sa.Column("revenue_growth", sa.Numeric(10, 6), nullable=True),
        sa.Column("earnings_growth", sa.Numeric(10, 6), nullable=True),
        sa.Column("price_to_book", sa.Numeric(14, 6), nullable=True),
        sa.ForeignKeyConstraint(["stock_id"], ["stocks.id"], name="fk_fundamentals_stock_id_stocks"),
        sa.PrimaryKeyConstraint("id", name="pk_fundamentals"),
        sa.UniqueConstraint("stock_id", "period", name="uq_fundamentals_stock_period"),
    )

    op.create_table(
        "earnings",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("stock_id", sa.BigInteger(), nullable=False),
        sa.Column("fiscal_period", sa.String(length=16), nullable=False),
        sa.Column("report_date", sa.Date(), nullable=True),
        sa.Column("eps_actual", sa.Numeric(14, 6), nullable=True),
        sa.Column("eps_estimate", sa.Numeric(14, 6), nullable=True),
        sa.Column("revenue_actual", sa.Numeric(20, 2), nullable=True),
        sa.Column("revenue_estimate", sa.Numeric(20, 2), nullable=True),
        sa.Column("surprise_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("is_upcoming", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["stock_id"], ["stocks.id"], name="fk_earnings_stock_id_stocks"),
        sa.PrimaryKeyConstraint("id", name="pk_earnings"),
        sa.UniqueConstraint("stock_id", "fiscal_period", name="uq_earnings_stock_fiscal_period"),
    )
    op.create_index("ix_earnings_report_date", "earnings", ["report_date"])

    op.create_table(
        "news",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("stock_id", sa.BigInteger(), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("url", sa.String(length=1024), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("categories", postgresql.ARRAY(sa.String(length=64)), nullable=True),
        sa.Column("sentiment_score", sa.Numeric(6, 4), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("expected_market_impact", sa.String(length=8), nullable=True),
        sa.Column("impact_direction", sa.Numeric(1), nullable=True),
        sa.Column("analysis_confidence", sa.Numeric(6, 4), nullable=True),
        sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["stock_id"], ["stocks.id"], name="fk_news_stock_id_stocks"),
        sa.PrimaryKeyConstraint("id", name="pk_news"),
        sa.UniqueConstraint("url", name="uq_news_url"),
    )
    op.create_index("ix_news_published", "news", ["published_at"])
    op.create_index("ix_news_stock_published", "news", ["stock_id", "published_at"])

    op.create_table(
        "signals",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("stock_id", sa.BigInteger(), nullable=False),
        sa.Column("agent", sa.String(length=32), nullable=False),
        sa.Column("interval", sa.String(length=8), nullable=True),
        sa.Column("signal_type", sa.String(length=16), nullable=False),
        sa.Column("score", sa.Numeric(8, 4), nullable=False),
        sa.Column("strength", sa.Numeric(8, 4), nullable=True),
        sa.Column("horizon", sa.String(length=16), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["stock_id"], ["stocks.id"], name="fk_signals_stock_id_stocks"),
        sa.PrimaryKeyConstraint("id", name="pk_signals"),
    )
    op.create_index("ix_signals_created", "signals", ["created_at"])
    op.create_index("ix_signals_stock_agent_created", "signals", ["stock_id", "agent", "created_at"])

    op.create_table(
        "predictions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("stock_id", sa.BigInteger(), nullable=False),
        sa.Column("model_name", sa.String(length=64), nullable=False),
        sa.Column("model_version", sa.Numeric(10), nullable=False),
        sa.Column("horizon", sa.String(length=16), nullable=False),
        sa.Column("prob_up", sa.Numeric(8, 4), nullable=True),
        sa.Column("prob_down", sa.Numeric(8, 4), nullable=True),
        sa.Column("prob_trend", sa.Numeric(8, 4), nullable=True),
        sa.Column("confidence", sa.Numeric(8, 4), nullable=True),
        sa.Column("expected_return", sa.Numeric(12, 6), nullable=True),
        sa.Column("expected_volatility", sa.Numeric(12, 6), nullable=True),
        sa.Column("features_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["stock_id"], ["stocks.id"], name="fk_predictions_stock_id_stocks"),
        sa.PrimaryKeyConstraint("id", name="pk_predictions"),
    )
    op.create_index("ix_predictions_stock_created", "predictions", ["stock_id", "created_at"])

    op.create_table(
        "decision_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("decision_uuid", sa.String(length=36), nullable=False),
        sa.Column("stock_id", sa.BigInteger(), nullable=False),
        sa.Column("decision", sa.String(length=8), nullable=False),
        sa.Column("confidence", sa.Numeric(8, 4), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("agent_scores", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["stock_id"], ["stocks.id"], name="fk_decision_log_stock_id_stocks"),
        sa.PrimaryKeyConstraint("id", name="pk_decision_log"),
        sa.UniqueConstraint("decision_uuid", name="uq_decision_log_decision_uuid"),
    )

    op.create_table(
        "positions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("portfolio_id", sa.BigInteger(), nullable=False),
        sa.Column("stock_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 4), nullable=False),
        sa.Column("avg_entry_price", sa.Numeric(18, 6), nullable=False),
        sa.Column("current_price", sa.Numeric(18, 6), nullable=True),
        sa.Column("stop_loss", sa.Numeric(18, 6), nullable=True),
        sa.Column("take_profit", sa.Numeric(18, 6), nullable=True),
        sa.Column("realized_pnl", sa.Numeric(20, 6), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["portfolio_id"], ["portfolios.id"], name="fk_positions_portfolio_id_portfolios"
        ),
        sa.ForeignKeyConstraint(["stock_id"], ["stocks.id"], name="fk_positions_stock_id_stocks"),
        sa.PrimaryKeyConstraint("id", name="pk_positions"),
    )
    op.create_index("ix_positions_portfolio_status", "positions", ["portfolio_id", "status"])

    op.create_table(
        "orders",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("idempotency_key", sa.String(length=36), nullable=False),
        sa.Column("portfolio_id", sa.BigInteger(), nullable=False),
        sa.Column("stock_id", sa.BigInteger(), nullable=False),
        sa.Column("side", sa.String(length=4), nullable=False),
        sa.Column("order_type", sa.String(length=8), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 4), nullable=False),
        sa.Column("limit_price", sa.Numeric(18, 6), nullable=True),
        sa.Column("stop_price", sa.Numeric(18, 6), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("broker_order_id", sa.String(length=64), nullable=True),
        sa.Column("filled_qty", sa.Numeric(20, 4), nullable=False),
        sa.Column("avg_fill_price", sa.Numeric(18, 6), nullable=True),
        sa.Column("commission", sa.Numeric(14, 6), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("decision_ref", sa.String(length=36), nullable=True),
        sa.Column("reason", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"], name="fk_orders_portfolio_id_portfolios"),
        sa.ForeignKeyConstraint(["stock_id"], ["stocks.id"], name="fk_orders_stock_id_stocks"),
        sa.PrimaryKeyConstraint("id", name="pk_orders"),
        sa.UniqueConstraint("idempotency_key", name="uq_orders_idempotency_key"),
    )
    op.create_index("ix_orders_created", "orders", ["created_at"])
    op.create_index("ix_orders_portfolio_status", "orders", ["portfolio_id", "status"])

    op.create_table(
        "trades",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("position_id", sa.BigInteger(), nullable=True),
        sa.Column("portfolio_id", sa.BigInteger(), nullable=False),
        sa.Column("stock_id", sa.BigInteger(), nullable=False),
        sa.Column("strategy", sa.String(length=64), nullable=False),
        sa.Column("side", sa.String(length=4), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 4), nullable=False),
        sa.Column("entry_price", sa.Numeric(18, 6), nullable=False),
        sa.Column("exit_price", sa.Numeric(18, 6), nullable=False),
        sa.Column("pnl", sa.Numeric(20, 6), nullable=True),
        sa.Column("pnl_pct", sa.Numeric(12, 6), nullable=True),
        sa.Column("fees", sa.Numeric(14, 6), nullable=False),
        sa.Column("entry_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exit_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision_reason", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=True),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"], name="fk_trades_portfolio_id_portfolios"),
        sa.ForeignKeyConstraint(["position_id"], ["positions.id"], name="fk_trades_position_id_positions"),
        sa.ForeignKeyConstraint(["stock_id"], ["stocks.id"], name="fk_trades_stock_id_stocks"),
        sa.PrimaryKeyConstraint("id", name="pk_trades"),
    )
    op.create_index("ix_trades_portfolio_exit", "trades", ["portfolio_id", "exit_time"])
    op.create_index("ix_trades_strategy", "trades", ["strategy"])

    op.create_table(
        "risk_history",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("decision_uuid", sa.String(length=36), nullable=True),
        sa.Column("portfolio_id", sa.BigInteger(), nullable=True),
        sa.Column("stock_id", sa.BigInteger(), nullable=True),
        sa.Column("approved", sa.Boolean(), nullable=False),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("position_size", sa.Numeric(20, 4), nullable=True),
        sa.Column("stop_loss", sa.Numeric(18, 6), nullable=True),
        sa.Column("take_profit", sa.Numeric(18, 6), nullable=True),
        sa.Column("risk_per_trade_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("exposure_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("max_daily_loss_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("daily_pnl_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["portfolio_id"], ["portfolios.id"], name="fk_risk_history_portfolio_id_portfolios"
        ),
        sa.ForeignKeyConstraint(["stock_id"], ["stocks.id"], name="fk_risk_history_stock_id_stocks"),
        sa.PrimaryKeyConstraint("id", name="pk_risk_history"),
    )

    op.create_table(
        "agent_metrics",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("agent_name", sa.String(length=32), nullable=False),
        sa.Column("metric_name", sa.String(length=32), nullable=False),
        sa.Column("value", sa.Numeric(12, 6), nullable=False),
        sa.Column("window", sa.String(length=16), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_agent_metrics"),
    )
    op.create_index(
        "ix_agent_metrics_name_metric_time", "agent_metrics", ["agent_name", "metric_name", "computed_at"]
    )

    op.create_table(
        "strategy_performance",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("strategy", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("total_return", sa.Numeric(12, 6), nullable=True),
        sa.Column("sharpe", sa.Numeric(12, 6), nullable=True),
        sa.Column("sortino", sa.Numeric(12, 6), nullable=True),
        sa.Column("max_drawdown", sa.Numeric(12, 6), nullable=True),
        sa.Column("win_rate", sa.Numeric(10, 4), nullable=True),
        sa.Column("profit_factor", sa.Numeric(12, 6), nullable=True),
        sa.Column("trades_count", sa.BigInteger(), nullable=True),
        sa.Column("final_equity", sa.Numeric(20, 2), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_strategy_performance"),
        sa.UniqueConstraint(
            "strategy", "mode", "period_start", "period_end", name="uq_strategy_perf_period"
        ),
    )

    op.create_table(
        "model_registry",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Numeric(10), nullable=False),
        sa.Column("artifact_path", sa.String(length=512), nullable=True),
        sa.Column("hyperparams", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("offline_metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("trained_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("training_window", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_model_registry"),
    )

    op.create_table(
        "backtest_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("universe", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("start", sa.Date(), nullable=False),
        sa.Column("end", sa.Date(), nullable=False),
        sa.Column("initial_capital", sa.Numeric(20, 2), nullable=False),
        sa.Column("final_capital", sa.Numeric(20, 2), nullable=True),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_backtest_runs"),
    )

    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_uuid", sa.String(length=36), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_events"),
        sa.UniqueConstraint("event_uuid", name="uq_events_event_uuid"),
    )
    op.create_index("ix_events_processed", "events", ["processed_at"])
    op.create_index("ix_events_type_occurred", "events", ["type", "occurred_at"])

    op.create_table(
        "system_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("level", sa.String(length=8), nullable=False),
        sa.Column("component", sa.String(length=64), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_system_logs"),
    )


def downgrade() -> None:
    op.drop_table("system_logs")
    op.drop_table("events")
    op.drop_table("backtest_runs")
    op.drop_table("model_registry")
    op.drop_table("strategy_performance")
    op.drop_table("agent_metrics")
    op.drop_table("risk_history")
    op.drop_table("trades")
    op.drop_table("orders")
    op.drop_table("positions")
    op.drop_table("decision_log")
    op.drop_table("predictions")
    op.drop_table("signals")
    op.drop_table("news")
    op.drop_table("earnings")
    op.drop_table("fundamentals")
    op.drop_table("indicators")
    op.drop_table("prices")
    op.drop_table("portfolios")
    op.drop_table("stocks")
