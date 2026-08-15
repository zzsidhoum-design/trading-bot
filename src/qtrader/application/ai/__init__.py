"""Phase 6 — AI Strategy Selection & Multi-Agent Integration (research only).

The AI layer produces structured, auditable proposals and never trades. The
Phase 5 risk engine remains the final authority; the Phase 4 execution
simulator is research-only. Every score is a pure, deterministic function of
its inputs — nothing here fabricates data.
"""

from qtrader.application.ai.models import (
    AgentSignal,
    AgentSignalSet,
    AgentWeightsConfig,
    AiDecisionRecord,
    AssetContext,
    DecisionProposal,
    FailureEvent,
    FailureReport,
    FailureSeverity,
    NewsAssessment,
    ProposalVerdict,
    RegimeAssessment,
    RiskGateResult,
    SelectorReport,
    SentimentResult,
    StrategySelection,
)

__all__ = [
    "AgentSignal",
    "AgentSignalSet",
    "AgentWeightsConfig",
    "AiDecisionRecord",
    "AssetContext",
    "DecisionProposal",
    "FailureEvent",
    "FailureReport",
    "FailureSeverity",
    "NewsAssessment",
    "ProposalVerdict",
    "RegimeAssessment",
    "RiskGateResult",
    "SelectorReport",
    "SentimentResult",
    "StrategySelection",
]
