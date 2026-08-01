from qtrader.application.agents.chief import ChiefAgent
from qtrader.application.agents.prediction import PredictionAgent
from qtrader.application.services.decision_strategy import EnsembleDecisionStrategy
from qtrader.application.services.feature_store import FeatureStore
from qtrader.application.services.model_trainer import ModelTrainer
from qtrader.config.container import Container
from qtrader.config.settings import Settings
from qtrader.domain.ports import (
    Cache,
    DecisionRepository,
    DecisionStrategy,
    EventBus,
    EventRepository,
    Lock,
    ModelRepository,
    PortfolioRepository,
    PredictionRepository,
    PriceRepository,
    StockRepository,
)


def test_container_resolves_every_registered_port() -> None:
    container = Container(settings=Settings(_env_file=None, _secrets_dir=None))
    try:
        assert isinstance(container.resolve(Settings), Settings)
        assert isinstance(container.resolve(StockRepository), StockRepository)
        assert isinstance(container.resolve(PortfolioRepository), PortfolioRepository)
        assert isinstance(container.resolve(PriceRepository), PriceRepository)
        assert isinstance(container.resolve(EventRepository), EventRepository)
        assert isinstance(container.resolve(EventBus), EventBus)
        assert isinstance(container.resolve(Cache), Cache)
        assert isinstance(container.resolve(Lock), Lock)
        assert isinstance(container.resolve(PredictionRepository), PredictionRepository)
        assert isinstance(container.resolve(DecisionRepository), DecisionRepository)
        assert isinstance(container.resolve(ModelRepository), ModelRepository)
        assert isinstance(container.resolve(DecisionStrategy), DecisionStrategy)
        assert isinstance(container.resolve(FeatureStore), FeatureStore)
        assert isinstance(container.resolve(ModelTrainer), ModelTrainer)
        assert isinstance(container.resolve(PredictionAgent), PredictionAgent)
        assert isinstance(container.resolve(ChiefAgent), ChiefAgent)
        assert isinstance(
            container.resolve(DecisionStrategy), EnsembleDecisionStrategy
        )
    finally:
        import asyncio

        asyncio.run(container.aclose())
