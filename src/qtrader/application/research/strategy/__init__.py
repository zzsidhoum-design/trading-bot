"""Phase 2 — Automated Strategy Research Engine.

Pure, declarative strategy specifications; a categorized feature library; a
constrained hypothesis generator; a strategy registry; a rule evaluator that
feeds the production ``BacktestRunner`` through its ``model_outputs`` contract;
anti-data-mining robustness checks; and the research workflow that ties
generation, initial backtest, filtering, walk-forward/OOS and registry together.
"""

from qtrader.application.research.strategy.engine import (
    ResearchPlan,
    ResearchReport,
    ResearchRequest,
    StrategyResearchEngine,
    StrategyWalkForwardValidator,
)
from qtrader.application.research.strategy.evaluator import StrategyEvaluator
from qtrader.application.research.strategy.feature_library import (
    FEATURES,
    Feature,
    FeatureCategory,
    FeatureLibrary,
)
from qtrader.application.research.strategy.generator import (
    GenerationResult,
    SearchLimits,
    StrategyGenerator,
)
from qtrader.application.research.strategy.registry import (
    InMemoryStrategyRegistry,
    StrategyRecord,
    StrategyRegistry,
    StrategyStatus,
)
from qtrader.application.research.strategy.robustness import (
    RobustnessCheck,
    RobustnessChecker,
    RobustnessLimits,
    RobustnessReport,
)
from qtrader.application.research.strategy.specs import (
    Condition,
    EntryRule,
    ExitRule,
    Operator,
    RegimeFilter,
    StrategySpec,
    decode_spec,
    encode_spec,
)

__all__ = [
    "Condition",
    "EntryRule",
    "ExitRule",
    "FEATURES",
    "Feature",
    "FeatureCategory",
    "FeatureLibrary",
    "GenerationResult",
    "InMemoryStrategyRegistry",
    "Operator",
    "RegimeFilter",
    "ResearchPlan",
    "ResearchReport",
    "ResearchRequest",
    "RobustnessCheck",
    "RobustnessChecker",
    "RobustnessLimits",
    "RobustnessReport",
    "SearchLimits",
    "StrategyEvaluator",
    "StrategyGenerator",
    "StrategyRecord",
    "StrategyRegistry",
    "StrategyResearchEngine",
    "StrategySpec",
    "StrategyStatus",
    "StrategyWalkForwardValidator",
    "decode_spec",
    "encode_spec",
]
