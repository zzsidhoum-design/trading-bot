"""Phase 6 — AI Strategy Selection & Multi-Agent Integration (research only).

Data models shared by every component of the AI layer. The AI layer:

- never trades: it only produces structured :class:`DecisionProposal` records;
- never bypasses the Phase 5 risk engine (final authority) or the Phase 4
  execution simulator (research only);
- is deterministic — every score is a pure function of its inputs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from qtrader.application.portfolio_mgmt.models import GateVerdict
from qtrader.application.services.market_regime import MarketRegime, VolatilityRegime
from qtrader.domain.value_objects import Interval, TradeSide

# The six agents that may influence a decision through measurable signals.
ALLOWED_AGENTS = (
    "technical",
    "fundamental",
    "news",
    "pattern",
    "prediction",
    "regime",
)


def _now() -> datetime:
    return datetime.now(UTC)


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class ProposalVerdict(StrEnum):
    """What the Decision Engine produced for one decision point."""

    NO_TRADE = "no_trade"
    PROPOSED = "proposed"
    DEGRADED = "degraded"


# --------------------------------------------------------------------------- #
# Market regime
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RegimeAssessment:
    """The Market Regime Agent's assessment (never an order).

    ``volatility`` is ``None`` when history is too short to classify it — the
    agent never fabricates a volatility condition.
    """

    ts: datetime
    regime: MarketRegime
    confidence: float
    volatility: VolatilityRegime | None
    trend: str
    timeframe: Interval
    driver: str = "market_regime_engine"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts.isoformat(),
            "regime": self.regime.value,
            "confidence": self.confidence,
            "volatility": self.volatility.value if self.volatility else None,
            "trend": self.trend,
            "timeframe": self.timeframe.value,
            "driver": self.driver,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RegimeAssessment:
        raw_vol = data.get("volatility")
        return cls(
            ts=datetime.fromisoformat(data["ts"]),
            regime=MarketRegime(data["regime"]),
            confidence=float(data["confidence"]),
            volatility=VolatilityRegime(raw_vol) if raw_vol else None,
            trend=str(data["trend"]),
            timeframe=Interval(data["timeframe"]),
            driver=str(data.get("driver", "market_regime_engine")),
        )


# --------------------------------------------------------------------------- #
# Sentiment (FinBERT / lexicon)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SentimentResult:
    """One document's sentiment assessment by a financial-text model."""

    sentiment: float  # -1 .. 1
    confidence: float  # 0 .. 1
    relevance: float  # 0 .. 1
    model: str
    summary: str | None = None
    error: bool = False
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class NewsAssessment:
    """Aggregated, point-in-time sentiment for one asset."""

    asset: str
    timestamp: datetime
    sentiment: float
    confidence: float
    sources: tuple[str, ...]
    relevance: float
    aggregated_sentiment: float
    items_used: int
    model: str


# --------------------------------------------------------------------------- #
# Agent signals
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AgentSignal:
    """One measurable, auditable signal from one agent."""

    agent: str
    version: str
    score: float  # signed strength in [-1, 1]
    confidence: float  # 0 .. 1
    reason: str
    timestamp: datetime
    features: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentSignalSet:
    """All available agent signals for one asset at one point in time."""

    asset: str
    as_of: datetime
    signals: tuple[AgentSignal, ...] = ()
    regime: RegimeAssessment | None = None
    news: NewsAssessment | None = None

    def by_agent(self) -> dict[str, AgentSignal]:
        return {s.agent: s for s in self.signals}


@dataclass(frozen=True, slots=True)
class AgentWeightsConfig:
    """Versioned, auditable per-agent influence weights."""

    version: str
    weights: dict[str, float]
    enabled: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        unknown = set(self.weights) - set(ALLOWED_AGENTS)
        if unknown:
            raise ValueError(f"unknown agent(s) in weights: {sorted(unknown)}")
        if any(w < 0.0 for w in self.weights.values()):
            raise ValueError("agent weights must be non-negative")
        if not self.enabled:
            object.__setattr__(
                self,
                "enabled",
                tuple(a for a in ALLOWED_AGENTS if self.weights.get(a, 0.0) > 0.0),
            )
        else:
            unknown_enabled = set(self.enabled) - set(ALLOWED_AGENTS)
            if unknown_enabled:
                raise ValueError(
                    f"unknown enabled agent(s): {sorted(unknown_enabled)}"
                )

    def weight(self, agent: str) -> float:
        return self.weights.get(agent, 0.0)

    def effective_agents(self) -> tuple[str, ...]:
        return tuple(a for a in self.enabled if self.weight(a) > 0.0)


# --------------------------------------------------------------------------- #
# Strategy selection
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SelectorConfig:
    """Weights for the multi-factor strategy selector (never returns-only)."""

    oos_sharpe: float = 1.0
    oos_return: float = 0.6
    oos_sortino: float = 0.4
    stability: float = 0.8
    execution: float = 1.0
    recent: float = 0.5
    volatility_match: float = 0.5
    cross_asset: float = 0.4
    risk: float = 0.6
    correlation: float = 0.5
    regime: float = 0.7
    complexity: float = 0.1
    min_positive_fold_fraction: float = 0.5
    max_drawdown_penalty_scale: float = 1.0

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            if name == "min_positive_fold_fraction":
                continue
            if getattr(self, name) < 0.0:
                raise ValueError(f"{name} must be non-negative")


@dataclass(frozen=True, slots=True)
class ExcludedStrategy:
    strategy_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class StrategySelection:
    """One selected strategy plus its multi-factor score and rationale."""

    strategy_id: str
    strategy_version: int
    score: float
    reasons: tuple[str, ...]
    regime_suitability: float


@dataclass(frozen=True, slots=True)
class SelectorReport:
    """The selector's ranked, reproducible output."""

    as_of: datetime
    regime: RegimeAssessment | None
    selections: tuple[StrategySelection, ...] = ()
    excluded: tuple[ExcludedStrategy, ...] = ()


# --------------------------------------------------------------------------- #
# Asset context + decision proposal
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AssetContext:
    """Point-in-time asset state used by the Decision Engine."""

    symbol: str
    price: Decimal
    sector: str | None = None
    atr_pct: float | None = None
    annualized_vol_pct: float | None = None
    reference_ts: datetime | None = None


@dataclass(frozen=True, slots=True)
class DecisionProposal:
    """A structured trade proposal — never an executed trade."""

    strategy_id: str
    strategy_version: int
    symbol: str
    side: TradeSide
    reference_price: Decimal
    requested_quantity: Decimal
    confidence: float
    expected_return: float | None
    expected_risk: float | None
    agents_involved: tuple[str, ...]
    agent_scores: dict[str, float]
    regime: RegimeAssessment | None
    rationale: tuple[str, ...]
    timeframes: tuple[Interval, ...]
    expected_direction: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "symbol": self.symbol,
            "side": self.side.value,
            "reference_price": str(self.reference_price),
            "requested_quantity": str(self.requested_quantity),
            "confidence": self.confidence,
            "expected_return": self.expected_return,
            "expected_risk": self.expected_risk,
            "agents_involved": list(self.agents_involved),
            "agent_scores": dict(self.agent_scores),
            "regime": self.regime.to_dict() if self.regime else None,
            "rationale": list(self.rationale),
            "timeframes": [iv.value for iv in self.timeframes],
            "expected_direction": self.expected_direction,
        }


@dataclass(frozen=True, slots=True)
class RiskGateResult:
    """Outcome of routing a proposal through the Phase 5 risk engine."""

    approved: bool
    verdict: GateVerdict
    approved_quantity: Decimal | None
    reasons: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    evaluated_exposure_pct: float | None = None


# --------------------------------------------------------------------------- #
# Execution (research/testing only)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ExecutionAssumptions:
    scenario: str
    commission_bps: float
    slippage_bps: float
    max_participation_rate: float
    seed: int


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    """Result of simulating one cleared order through Phase 4 execution."""

    filled: bool
    fill_rate: float
    rejected_rate: float
    net_return: float | None
    avg_slippage_bps: float | None
    commission: Decimal
    scenario: str
    assumptions: ExecutionAssumptions

    def to_dict(self) -> dict[str, Any]:
        return {
            "filled": self.filled,
            "fill_rate": self.fill_rate,
            "rejected_rate": self.rejected_rate,
            "net_return": self.net_return,
            "avg_slippage_bps": self.avg_slippage_bps,
            "commission": str(self.commission),
            "scenario": self.scenario,
            "assumptions": {
                "scenario": self.assumptions.scenario,
                "commission_bps": self.assumptions.commission_bps,
                "slippage_bps": self.assumptions.slippage_bps,
                "max_participation_rate": self.assumptions.max_participation_rate,
                "seed": self.assumptions.seed,
            },
        }


# --------------------------------------------------------------------------- #
# AI failure detection
# --------------------------------------------------------------------------- #


class FailureSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class FailureEvent:
    code: str
    severity: FailureSeverity
    message: str
    detail: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=_now)


@dataclass(frozen=True, slots=True)
class FailureReport:
    """AI health snapshot. ``degraded`` means the system must not trade."""

    events: tuple[FailureEvent, ...] = ()
    degraded: bool = False
    reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.degraded

    @property
    def criticals(self) -> tuple[FailureEvent, ...]:
        return tuple(e for e in self.events if e.severity is FailureSeverity.CRITICAL)

    def codes(self) -> tuple[str, ...]:
        return tuple(e.code for e in self.events)


# --------------------------------------------------------------------------- #
# Decision record (reproducibility / audit)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AiDecisionRecord:
    """Every decision, fully reproducible, for the audit trail."""

    decision_id: str
    timestamp: datetime
    asset: str
    strategy: str
    strategy_version: int
    agents_involved: tuple[str, ...]
    agent_signals: dict[str, dict[str, Any]]
    sentiment: dict[str, Any] | None
    market_regime: dict[str, Any] | None
    timeframes: tuple[str, ...]
    confidence: float | None
    expected_return: float | None
    expected_risk: float | None
    proposed_position_size: Decimal | None
    risk_approval: str
    risk_reason: str
    execution_assumptions: dict[str, Any] | None
    execution_result: dict[str, Any] | None
    failure_events: tuple[str, ...]
    proposal: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "timestamp": self.timestamp.isoformat(),
            "asset": self.asset,
            "strategy": self.strategy,
            "strategy_version": self.strategy_version,
            "agents_involved": list(self.agents_involved),
            "agent_signals": dict(self.agent_signals),
            "sentiment": self.sentiment,
            "market_regime": self.market_regime,
            "timeframes": list(self.timeframes),
            "confidence": self.confidence,
            "expected_return": self.expected_return,
            "expected_risk": self.expected_risk,
            "proposed_position_size": (
                str(self.proposed_position_size)
                if self.proposed_position_size is not None
                else None
            ),
            "risk_approval": self.risk_approval,
            "risk_reason": self.risk_reason,
            "execution_assumptions": self.execution_assumptions,
            "execution_result": self.execution_result,
            "failure_events": list(self.failure_events),
            "proposal": self.proposal,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AiDecisionRecord:
        proposed = data.get("proposal")
        return cls(
            decision_id=str(data["decision_id"]),
            timestamp=datetime.fromisoformat(data["timestamp"]),
            asset=str(data["asset"]),
            strategy=str(data["strategy"]),
            strategy_version=int(data["strategy_version"]),
            agents_involved=tuple(str(a) for a in data["agents_involved"]),
            agent_signals=dict(data["agent_signals"]),
            sentiment=data.get("sentiment"),
            market_regime=data.get("market_regime"),
            timeframes=tuple(str(t) for t in data["timeframes"]),
            confidence=data.get("confidence"),
            expected_return=data.get("expected_return"),
            expected_risk=data.get("expected_risk"),
            proposed_position_size=(
                Decimal(data["proposed_position_size"])
                if data.get("proposed_position_size") is not None
                else None
            ),
            risk_approval=str(data["risk_approval"]),
            risk_reason=str(data["risk_reason"]),
            execution_assumptions=data.get("execution_assumptions"),
            execution_result=data.get("execution_result"),
            failure_events=tuple(str(f) for f in data["failure_events"]),
            proposal=proposed,
        )


# --------------------------------------------------------------------------- #
# Ablation / contribution testing
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AblationCase:
    name: str
    enabled_agents: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AblationMetrics:
    total_return: float
    expected_value: float
    sharpe: float
    sortino: float
    max_drawdown: float
    stability: float
    execution_adjusted_return: float
    trades: int
    fill_rate: float


@dataclass(frozen=True, slots=True)
class AblationResult:
    case: AblationCase
    metrics: AblationMetrics
    delta: dict[str, float]


@dataclass(frozen=True, slots=True)
class AgentContribution:
    agent: str
    metric_deltas: dict[str, float]
    verdict: str  # "keep" | "reduce" | "remove"


@dataclass(frozen=True, slots=True)
class AblationReport:
    baseline: AblationCase
    results: tuple[AblationResult, ...]
    contributions: tuple[AgentContribution, ...]
    recommendation: str


__all__ = [
    "ALLOWED_AGENTS",
    "AblationCase",
    "AblationMetrics",
    "AblationReport",
    "AblationResult",
    "AgentContribution",
    "AgentSignal",
    "AgentSignalSet",
    "AgentWeightsConfig",
    "AiDecisionRecord",
    "AssetContext",
    "DecisionProposal",
    "ExecutionAssumptions",
    "ExecutionOutcome",
    "ExcludedStrategy",
    "FailureEvent",
    "FailureReport",
    "FailureSeverity",
    "GateVerdict",
    "NewsAssessment",
    "ProposalVerdict",
    "RegimeAssessment",
    "RiskGateResult",
    "SelectorConfig",
    "SelectorReport",
    "SentimentResult",
    "StrategySelection",
    "_clip",
    "_now",
]
