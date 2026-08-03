"""API response schemas (Pydantic). Thin DTOs — never ORM models or entities."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class HealthCheck(BaseModel):
    status: str = "ok"
    database: str
    cache: str
    broker: str = "not_configured"
    worker: str = "unknown"
    mode: str


class SystemStatus(BaseModel):
    mode: str
    live_enabled: bool
    api_version: str = "v1"
    agents: list[str] = Field(default_factory=list)


class CircuitBreakerSnapshot(BaseModel):
    name: str
    state: str
    consecutive_failures: int
    reset_timeout_seconds: float


class SystemMetrics(BaseModel):
    uptime_seconds: float
    mode: str
    live_enabled: bool
    database: str
    cache: str
    worker: str = "unknown"
    events_by_type: dict[str, int] = Field(default_factory=dict)
    circuit_breakers: list[CircuitBreakerSnapshot] = Field(default_factory=list)


class SystemLogOut(BaseModel):
    log_id: int
    level: str
    component: str | None = None
    message: str
    context: dict = Field(default_factory=dict)
    created_at: datetime


class ModeToggle(BaseModel):
    mode: str


class StockOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    symbol: str
    exchange: str
    name: str | None = None
    currency: str = "USD"
    sector: str | None = None
    industry: str | None = None
    is_active: bool = True


class StockCreate(BaseModel):
    symbol: str
    exchange: str = "XNAS"
    name: str | None = None
    sector: str | None = None
    industry: str | None = None


class PriceBarOut(BaseModel):
    symbol: str
    interval: str
    ts: datetime
    open: str
    high: str
    low: str
    close: str
    volume: str


class IndicatorOut(BaseModel):
    symbol: str
    interval: str
    ts: datetime
    values: dict = Field(default_factory=dict)


class NewsOut(BaseModel):
    title: str
    source: str | None = None
    url: str | None = None
    published_at: datetime | None = None
    sentiment_score: str | None = None
    summary: str | None = None
    expected_market_impact: str | None = None
    impact_direction: int | None = None


class SignalOut(BaseModel):
    agent: str
    signal_type: str
    score: str
    interval: str | None = None
    created_at: datetime
    metadata: dict = Field(default_factory=dict)


class PortfolioSummary(BaseModel):
    name: str
    currency: str
    mode: str
    status: str
    initial_capital: str
    current_cash: str


class OrderOut(BaseModel):
    order_id: int | None = None
    idempotency_key: str
    side: str
    order_type: str
    quantity: str
    filled_qty: int = 0
    avg_fill_price: str | None = None
    status: str
    mode: str
    symbol: str | None = None
    stop_loss: str | None = None
    take_profit: str | None = None
    created_at: datetime
    decision_ref: str | None = None


class OrderCreate(BaseModel):
    symbol: str
    side: str
    quantity: int = Field(gt=0)
    order_type: str = "MARKET"
    stop_loss: str | None = None
    take_profit: str | None = None


class DashboardSummary(BaseModel):
    cash: str
    equity: str
    open_positions: int
    unrealized_pnl: str
    exposure_pct: float
    total_trades: int


class EquityPoint(BaseModel):
    ts: datetime
    equity: str


class PositionOut(BaseModel):
    position_id: int | None = None
    symbol: str | None = None
    quantity: int
    avg_entry_price: str
    current_price: str | None = None
    unrealized_pnl: str | None = None
    status: str
    stop_loss: str | None = None
    take_profit: str | None = None
    realized_pnl: str | None = None
    opened_at: datetime
    closed_at: datetime | None = None


class AllocationOut(BaseModel):
    symbol: str
    sector: str | None = None
    market_value: str
    weight_pct: float


class TopStockOut(BaseModel):
    symbol: str
    score: float


class TradeOut(BaseModel):
    trade_id: int | None = None
    symbol: str | None = None
    strategy: str
    side: str
    quantity: str
    entry_price: str
    exit_price: str
    pnl: str | None = None
    pnl_pct: str | None = None
    fees: str
    entry_time: datetime
    exit_time: datetime
    outcome: str | None = None
    mode: str


class AgentMetricOut(BaseModel):
    agent_name: str
    metric_name: str
    value: str
    window: str
    computed_at: datetime


class RiskAssessmentOut(BaseModel):
    symbol: str
    approved: bool
    rejection_reasons: list[str]
    position_size: str | None = None
    stop_loss: str | None = None
    take_profit: str | None = None
    exposure_pct: str | None = None
    created_at: datetime


class LogOut(BaseModel):
    level: str
    message: str
    component: str | None = None
    context: dict = Field(default_factory=dict)
    created_at: datetime


class PerformanceSummaryOut(BaseModel):
    strategy: str
    mode: str
    period_start: date
    period_end: date
    total_return: str | None = None
    sharpe: str | None = None
    sortino: str | None = None
    max_drawdown: str | None = None
    win_rate: str | None = None
    profit_factor: str | None = None
    trades_count: int | None = None
    final_equity: str | None = None


class RegisteredModelOut(BaseModel):
    model_id: int | None = None
    name: str
    version: int
    hyperparams: dict = Field(default_factory=dict)
    offline_metrics: dict = Field(default_factory=dict)
    is_active: bool = False
    status: str = "registered"
    trained_at: datetime | None = None
    training_window: str | None = None


class BacktestSubmit(BaseModel):
    name: str = "manual"
    symbols: list[str] = Field(min_length=1)
    start: date
    end: date
    initial_capital: str = "100000"
    interval: str = "1d"
    strategy: str = "ensemble"
    commission_bps: float = 1.0
    slippage_bps: float = 0.0
    warmup_bars: int = 30


class BacktestCompare(BaseModel):
    other_run_id: int


class BacktestRunOut(BaseModel):
    run_id: int | None = None
    name: str
    universe: list[str] = Field(default_factory=list)
    start: date
    end: date
    initial_capital: str
    interval: str
    strategy: str
    commission_bps: str
    slippage_bps: str
    final_capital: str | None = None
    status: str
    created_at: datetime
    metrics: PerformanceSummaryOut | None = None


class AgentRunRequest(BaseModel):
    symbol: str = "AAPL"
    interval: str = "5m"
    days: int = 30


class AgentRunResult(BaseModel):
    agent: str
    status: str = "ok"
    detail: str | None = None
