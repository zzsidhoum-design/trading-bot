"""Unit tests for the dynamic universe engine (Phase 2)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from qtrader.application.services.universe import (
    UniverseEngine,
    UniverseFilterRequest,
    UniverseThresholds,
)
from qtrader.domain.entities import (
    DiscoveredAsset,
    Stock,
    TradingStatus,
    UniverseMembership,
    UniverseTier,
)
from qtrader.domain.ports import (
    AssetDiscoveryProvider,
    PriceRepository,
    UniverseRepository,
)
from qtrader.domain.value_objects import Interval, PriceBar
from tests.unit.fakes_phase7 import FakeStockRepository


def _member(
    symbol: str,
    *,
    tier: UniverseTier = UniverseTier.C,
    status: TradingStatus = TradingStatus.ACTIVE,
    added_at: date = date(2020, 1, 1),
    removed_at: date | None = None,
    name: str | None = None,
) -> UniverseMembership:
    return UniverseMembership(
        symbol=symbol, status=status, tier=tier, added_at=added_at,
        removed_at=removed_at, name=name,
    )


class FakeDiscoveryProvider(AssetDiscoveryProvider):
    def __init__(
        self,
        assets: list[DiscoveredAsset] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.assets = assets or []
        self.error = error
        self.calls = 0

    async def discover_candidates(self, limit: int = 500) -> list[DiscoveredAsset]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.assets[:limit]


class FakeUniverseRepository(UniverseRepository):
    def __init__(self, memberships: list[UniverseMembership] | None = None) -> None:
        self._memberships: dict[str, UniverseMembership] = {
            m.symbol: m for m in (memberships or [])
        }
        self.changes = []
        self.upserted: list[UniverseMembership] = []

    async def list_memberships(self, status=None) -> list[UniverseMembership]:
        memberships = list(self._memberships.values())
        if status is not None:
            memberships = [m for m in memberships if m.status is status]
        return sorted(memberships, key=lambda m: m.symbol)

    async def get_membership(self, symbol: str) -> UniverseMembership | None:
        return self._memberships.get(symbol)

    async def upsert_membership(self, membership: UniverseMembership) -> UniverseMembership:
        self._memberships[membership.symbol] = membership
        self.upserted.append(membership)
        return membership

    async def record_symbol_change(self, change) -> UniverseMembership:
        self.changes.append(change)
        return change

    async def list_symbol_changes(self) -> list:
        return list(self.changes)


class FakePriceRepo(PriceRepository):
    def __init__(
        self,
        d1: dict[str, list[PriceBar]] | None = None,
        m5: dict[str, list[PriceBar]] | None = None,
    ) -> None:
        self._d1 = d1 or {}
        self._m5 = m5 or {}

    async def upsert_bars(self, bars: list[PriceBar]) -> int:
        return len(bars)

    async def latest(self, symbol: str, interval) -> PriceBar | None:
        source = self._d1 if interval is Interval.D1 else self._m5
        bars = source.get(symbol, [])
        return bars[-1] if bars else None

    async def history(self, symbol, interval, start=None, end=None, limit=500) -> list[PriceBar]:
        source = self._d1 if interval is Interval.D1 else self._m5
        return source.get(symbol, [])[:limit]


def _bar(symbol: str, ts: datetime, price: float, volume: str = "1000000") -> PriceBar:
    return PriceBar(
        symbol=symbol,
        interval=Interval.D1,
        ts=ts,
        open=Decimal(str(price * 0.99)),
        high=Decimal(str(price * 1.01)),
        low=Decimal(str(price * 0.98)),
        close=Decimal(str(price)),
        volume=Decimal(volume),
    )


def _series(
    symbol: str, days: int = 40, price: float = 100.0, volume: str = "1000000"
) -> list[PriceBar]:
    """Daily bars for ``symbol`` from 2026-06-01 (used for added_at checks)."""
    return [
        _bar(symbol, datetime(2026, 6, 1, tzinfo=UTC) + timedelta(days=i), price, volume)
        for i in range(days)
    ]


def _recent_series(symbol: str, price: float = 100.0, volume: str = "1000000") -> list[PriceBar]:
    """Daily bars ending 2026-08-10 (recent enough to stay active today)."""
    return [
        _bar(symbol, datetime(2026, 7, 2, tzinfo=UTC) + timedelta(days=i), price, volume)
        for i in range(40)
    ]


def _asset(symbol: str, name: str = "Co", market_cap: float | None = 1e12) -> DiscoveredAsset:
    return DiscoveredAsset(symbol=symbol, name=f"{symbol} {name}", market_cap=market_cap)


def _engine(
    *,
    assets: list[DiscoveredAsset] | None = None,
    discovery_error: Exception | None = None,
    d1: dict[str, list[PriceBar]] | None = None,
    m5: dict[str, list[PriceBar]] | None = None,
    memberships: list[UniverseMembership] | None = None,
    stocks: list[Stock] | None = None,
    thresholds: UniverseThresholds | None = None,
) -> tuple[UniverseEngine, FakeUniverseRepository, FakePriceRepo, FakeDiscoveryProvider]:
    repo = FakeUniverseRepository(memberships)
    prices = FakePriceRepo(d1=d1, m5=m5)
    discovery = FakeDiscoveryProvider(assets, discovery_error)
    engine = UniverseEngine(
        discovery=discovery,
        universe_repo=repo,
        prices=prices,
        stocks=FakeStockRepository(stocks or []),
        thresholds=thresholds or UniverseThresholds(min_trading_days=2, max_spread_pct=None),
    )
    return engine, repo, prices, discovery


async def test_refresh_adds_members_and_assigns_tiers() -> None:
    d1 = {
        "AAPL": _series("AAPL", price=200.0),
        "SMALL": _series("SMALL", price=3.0, volume="500000"),
    }
    engine, repo, _, _ = _engine(
        assets=[_asset("AAPL"), _asset("SMALL")],
        d1=d1,
    )
    report = await engine.refresh()

    assert report.source == "provider"
    assert report.added == ["AAPL", "SMALL"]
    aapl = repo._memberships["AAPL"]
    assert aapl.status is TradingStatus.ACTIVE
    assert aapl.tier is UniverseTier.A
    assert aapl.added_at == date(2026, 6, 1)
    small = repo._memberships["SMALL"]
    assert small.status is TradingStatus.ACTIVE
    assert small.tier is UniverseTier.C


async def test_refresh_ignores_candidates_below_floor() -> None:
    d1 = {"PENNY": _series("PENNY", price=1.0, volume="500000")}
    engine, repo, _, _ = _engine(
        assets=[_asset("PENNY")],
        d1=d1,
    )
    report = await engine.refresh()
    assert report.added == []
    assert "PENNY" not in repo._memberships


async def test_refresh_falls_back_to_seed_on_provider_error() -> None:
    engine, repo, _, discovery = _engine(
        discovery_error=RuntimeError("provider down"),
        d1={"AAPL": _series("AAPL", price=200.0)},
        stocks=[Stock(symbol="AAPL", exchange="XNAS", name="Apple")],
    )
    report = await engine.refresh()
    assert discovery.calls == 1
    assert report.source == "seed"
    assert "AAPL" in repo._memberships


async def test_refresh_suspends_stale_member() -> None:
    existing = UniverseMembership(
        symbol="STALE",
        status=TradingStatus.ACTIVE,
        tier=UniverseTier.C,
        added_at=date(2026, 1, 1),
    )
    old_bar = _bar("STALE", datetime(2026, 8, 1, tzinfo=UTC), 10.0)
    engine, repo, _, _ = _engine(
        assets=[],  # not discovered today -> staleness pass applies
        d1={"STALE": [old_bar]},
        memberships=[existing],
        thresholds=UniverseThresholds(
            min_trading_days=2, max_spread_pct=None, stale_suspend_days=5, stale_delist_days=200
        ),
    )
    report = await engine.refresh()
    assert report.suspended == ["STALE"]
    assert repo._memberships["STALE"].status is TradingStatus.SUSPENDED
    assert repo._memberships["STALE"].removed_at == datetime.now(UTC).date()


async def test_refresh_resumes_suspended_member_with_fresh_data() -> None:
    existing = UniverseMembership(
        symbol="BACK",
        status=TradingStatus.SUSPENDED,
        tier=UniverseTier.C,
        added_at=date(2026, 1, 1),
        removed_at=date(2026, 8, 1),
    )
    engine, repo, _, _ = _engine(
        assets=[],  # not discovered today -> staleness pass applies
        d1={"BACK": _recent_series("BACK")},
        memberships=[existing],
        thresholds=UniverseThresholds(min_trading_days=2, max_spread_pct=None),
    )
    report = await engine.refresh()
    assert report.resumed == ["BACK"]
    assert repo._memberships["BACK"].status is TradingStatus.ACTIVE
    assert repo._memberships["BACK"].removed_at is None


async def test_refresh_delists_member_with_no_recent_bars() -> None:
    existing = UniverseMembership(
        symbol="GONE",
        status=TradingStatus.ACTIVE,
        tier=UniverseTier.C,
        added_at=date(2026, 1, 1),
    )
    stale_bar = _bar("GONE", datetime(2025, 1, 1, tzinfo=UTC), 10.0)
    engine, repo, _, _ = _engine(
        assets=[],  # not discovered today
        d1={"GONE": [stale_bar]},
        memberships=[existing],
        thresholds=UniverseThresholds(
            min_trading_days=2, max_spread_pct=None, stale_suspend_days=5, stale_delist_days=10
        ),
    )
    report = await engine.refresh()
    assert report.delisted == ["GONE"]
    assert repo._memberships["GONE"].status is TradingStatus.DELISTED


async def test_refresh_records_symbol_changes() -> None:
    existing = UniverseMembership(
        symbol="FB",
        name="Meta Platforms Inc.",
        status=TradingStatus.ACTIVE,
        tier=UniverseTier.A,
        added_at=date(2021, 1, 1),
    )
    engine, repo, _, _ = _engine(
        assets=[DiscoveredAsset(symbol="META", name="Meta Platforms Inc.", market_cap=1e12)],
        d1={"META": _series("META", price=200.0)},
        memberships=[existing],
    )
    report = await engine.refresh()
    assert len(report.symbol_changes) == 1
    assert report.symbol_changes[0].old_symbol == "FB"
    assert report.symbol_changes[0].new_symbol == "META"
    assert repo.changes == report.symbol_changes


async def test_filter_symbols_respects_min_tier_and_as_of() -> None:
    memberships = [
        _member("AAA", tier=UniverseTier.A),
        _member("BBB", tier=UniverseTier.B),
        _member("CCC", tier=UniverseTier.C),
        _member("LATE", tier=UniverseTier.A, added_at=date(2026, 9, 1)),
        _member(
            "GONE", tier=UniverseTier.A,
            status=TradingStatus.DELISTED, removed_at=date(2026, 1, 1),
        ),
    ]
    engine, _, _, _ = _engine(memberships=memberships)
    assert await engine.filter_symbols(UniverseFilterRequest()) == ["AAA", "BBB", "CCC"]
    assert await engine.filter_symbols(UniverseFilterRequest(min_tier=UniverseTier.A)) == ["AAA"]
    assert await engine.filter_symbols(
        UniverseFilterRequest(min_tier=UniverseTier.A, as_of=date(2026, 12, 1))
    ) == ["AAA", "LATE"]


async def test_filter_symbols_symbol_restriction_and_cap() -> None:
    memberships = [
        _member("AAA", tier=UniverseTier.A),
        _member("BBB", tier=UniverseTier.B),
    ]
    engine, _, _, _ = _engine(memberships=memberships)
    assert await engine.filter_symbols(
        UniverseFilterRequest(symbols=("BBB",), max_symbols=1)
    ) == ["BBB"]


async def test_filter_symbols_live_liquidity_overrides() -> None:
    memberships = [
        _member("BIG", tier=UniverseTier.A),
        _member("TINY", tier=UniverseTier.C),
    ]
    d1 = {
        "BIG": _series("BIG", price=200.0),
        "TINY": _series("TINY", price=3.0, volume="300000"),
    }
    engine, _, _, _ = _engine(memberships=memberships, d1=d1)
    assert await engine.filter_symbols(
        UniverseFilterRequest(min_dollar_volume=100_000_000.0)
    ) == ["BIG"]


async def test_snapshot_reports_coverage() -> None:
    memberships = [
        _member("A1", tier=UniverseTier.A),
        _member("B1", tier=UniverseTier.B),
        _member("C1", tier=UniverseTier.C),
        _member("S1", status=TradingStatus.SUSPENDED),
        _member("D1", status=TradingStatus.DELISTED, tier=None, added_at=date(2019, 1, 1)),
    ]
    engine, _, _, _ = _engine(memberships=memberships)
    snapshot = await engine.snapshot()
    assert snapshot["total_members"] == 5
    assert snapshot["active"] == 3
    assert snapshot["suspended"] == 1
    assert snapshot["delisted"] == 1
    assert snapshot["tradable"] == 3
    assert snapshot["tiers"] == {"A": 1, "B": 1, "C": 1}


async def test_spread_filter_applied_when_intraday_available() -> None:
    wide_high = PriceBar(
        symbol="WIDE",
        interval=Interval.M5,
        ts=datetime(2026, 8, 10, 14, 0, tzinfo=UTC),
        open=Decimal("100"),
        high=Decimal("106"),
        low=Decimal("94"),
        close=Decimal("100"),
        volume=Decimal("1000"),
    )
    engine, repo, _, _ = _engine(
        assets=[_asset("WIDE")],
        d1={"WIDE": _series("WIDE", price=100.0)},
        m5={"WIDE": [wide_high] * 5},
        thresholds=UniverseThresholds(min_trading_days=2, max_spread_pct=1.0),
    )
    report = await engine.refresh()
    assert "WIDE" not in report.added
    assert repo._memberships.get("WIDE") is None or repo._memberships["WIDE"].tier is None
