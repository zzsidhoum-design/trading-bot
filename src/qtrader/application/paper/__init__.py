"""Phase 7 — Paper Trading & Shadow Deployment.

Provides a controlled paper-trading environment that records every decision and
execution into an auditable JSON-lines ledger, measures operational telemetry
(latency, failures, data reliability), compares paper results against research,
evaluates non-profit acceptance criteria and supports shadow mode (decisions
recorded, nothing ever submitted). Live trading stays disabled by
``Settings.live_enabled``.
"""

from qtrader.application.paper.acceptance import (
    AcceptanceCriterion,
    AcceptanceEvaluator,
    AcceptanceResult,
    AcceptanceThresholds,
)
from qtrader.application.paper.brokers import PaperExecutionBroker, ShadowBroker
from qtrader.application.paper.comparison import (
    ComparisonInput,
    ComparisonReport,
    ComparisonRow,
    PaperVsResearchComparator,
)
from qtrader.application.paper.ledger import PaperOrderLedger, ledger_stats
from qtrader.application.paper.models import (
    PaperOrderRecord,
    PaperOrderStatus,
    PaperRunStats,
    RiskInterventionStats,
)
from qtrader.application.paper.service import PaperTradingService, RecoveryReport
from qtrader.application.paper.telemetry import (
    NullTelemetry,
    OperationalSummary,
    OperationalTelemetry,
    TelemetryRecorder,
    operational_summary,
)

__all__ = [
    "AcceptanceCriterion",
    "AcceptanceEvaluator",
    "AcceptanceResult",
    "AcceptanceThresholds",
    "ComparisonInput",
    "ComparisonReport",
    "ComparisonRow",
    "NullTelemetry",
    "OperationalSummary",
    "OperationalTelemetry",
    "PaperExecutionBroker",
    "PaperOrderLedger",
    "PaperOrderRecord",
    "PaperOrderStatus",
    "PaperRunStats",
    "PaperTradingService",
    "PaperVsResearchComparator",
    "RecoveryReport",
    "RiskInterventionStats",
    "ShadowBroker",
    "TelemetryRecorder",
    "ledger_stats",
    "operational_summary",
]
