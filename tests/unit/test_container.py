from qtrader.config.container import Container
from qtrader.config.settings import Settings
from qtrader.domain.ports import (
    Cache,
    EventBus,
    EventRepository,
    Lock,
    PortfolioRepository,
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
    finally:
        import asyncio

        asyncio.run(container.aclose())
