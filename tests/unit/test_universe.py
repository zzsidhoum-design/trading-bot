"""Unit tests for point-in-time universe selection and Phase 2 pure logic."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from qtrader.application.services.universe import (
    Classification,
    UniverseThresholds,
    classify,
    compute_liquidity_metrics,
    detect_symbol_changes,
    estimate_spread_pct,
    listing_date_from_first_bar,
    point_in_time_universe,
    resolve_status,
    tier_gte,
    universe_as_of,
)
from qtrader.domain.entities import (
    DiscoveredAsset,
    LiquidityMetrics,
    SymbolChange,
    TradingStatus,
    UniverseMembership,
    UniverseTier,
)
from qtrader.domain.value_objects import Interval, PriceBar

LISTINGS = {
    "AAPL": date(2021, 9, 1),
    "GEV": date(2024, 3, 27),
    "SOLV": date(2024, 3, 26),
    "Q": date(2025, 10, 27),
    "FDXF": date(2026, 5, 27),
}


def bar(symbol: str, ts: datetime, close: float, volume: float = 1_000_000) -> PriceBar:
    return PriceBar(
        symbol=symbol,
        interval=Interval.D1,
        ts=ts,
        open=Decimal(str(close)),
        high=Decimal(str(close + 0.1)),
        low=Decimal(str(close - 0.1)),
        close=Decimal(str(close)),
        volume=Decimal(str(volume)),
    )


def metrics(
    *,
    symbol: str = "AAPL",
    last_price: float = 100.0,
    dollar_volume: float = 50_000_000.0,
    avg_volume: float = 1_000_000.0,
    spread: float | None = 0.5,
    market_cap: float | None = 200_000_000_000.0,
    trading_days: int = 250,
) -> LiquidityMetrics:
    return LiquidityMetrics(
        symbol=symbol,
        last_price=last_price,
        avg_dollar_volume=dollar_volume,
        avg_volume=avg_volume,
        mean_spread_pct=spread,
        market_cap=market_cap,
        trading_days=trading_days,
        first_bar=datetime(2021, 1, 1, tzinfo=UTC),
        last_bar=datetime(2026, 8, 10, tzinfo=UTC),
    )


# --------------------------------------------------------------------------- #
# Existing point-in-time helpers
# --------------------------------------------------------------------------- #


def test_returns_only_symbols_listed_by_as_of() -> None:
    assert point_in_time_universe(LISTINGS, date(2022, 1, 1)) == ["AAPL"]
    assert point_in_time_universe(LISTINGS, date(2024, 6, 1)) == ["AAPL", "GEV", "SOLV"]
    assert point_in_time_universe(LISTINGS, date(2026, 8, 6)) == [
        "AAPL", "FDXF", "GEV", "Q", "SOLV",
    ]


def test_includes_symbol_listed_exactly_on_as_of() -> None:
    assert point_in_time_universe(LISTINGS, date(2024, 3, 26)) == ["AAPL", "SOLV"]


def test_empty_input() -> None:
    assert point_in_time_universe({}, date(2026, 1, 1)) == []


def test_listing_date_from_first_bar() -> None:
    ts = datetime(2024, 3, 27, 13, 30, tzinfo=UTC)
    assert listing_date_from_first_bar(ts) == date(2024, 3, 27)


# --------------------------------------------------------------------------- #
# compute_liquidity_metrics / estimate_spread_pct
# --------------------------------------------------------------------------- #


def test_compute_metrics_uses_window_and_returns_aggregates() -> None:
    bars = [bar("AAPL", datetime(2026, 7, 1, tzinfo=UTC), 100.0, volume=1_000_000)] * 30
    result = compute_liquidity_metrics(bars, window_days=21)
    assert result is not None
    assert result.symbol == "AAPL"
    assert result.last_price == 100.0
    assert result.avg_dollar_volume == 100.0 * 1_000_000.0
    assert result.avg_volume == 1_000_000.0
    assert result.trading_days == 30
    assert result.first_bar is not None and result.last_bar is not None


def test_compute_metrics_window_truncation() -> None:
    bars = [bar("AAPL", datetime(2026, 7, 1, tzinfo=UTC), 100.0, volume=1_000_000)] * 40
    bars += [bar("AAPL", datetime(2026, 8, 1, tzinfo=UTC), 200.0, volume=2_000_000)] * 25
    result = compute_liquidity_metrics(bars, window_days=21)
    assert result is not None
    assert result.last_price == 200.0
    assert result.avg_dollar_volume == 200.0 * 2_000_000.0


def test_compute_metrics_requires_two_bars() -> None:
    assert compute_liquidity_metrics([bar("AAPL", datetime(2026, 7, 1, tzinfo=UTC), 1.0)]) is None
    assert compute_liquidity_metrics([]) is None


def test_estimate_spread_pct() -> None:
    tight = PriceBar(
        symbol="X",
        interval=Interval.M5,
        ts=datetime(2026, 8, 10, 14, 0, tzinfo=UTC),
        open=Decimal("100"),
        high=Decimal("100.5"),
        low=Decimal("99.5"),
        close=Decimal("100"),
        volume=Decimal("1000"),
    )
    assert estimate_spread_pct([tight]) == 1.0
    assert estimate_spread_pct([]) is None


def test_compute_metrics_carries_spread_and_market_cap() -> None:
    bars = [bar("AAPL", datetime(2026, 7, 1, tzinfo=UTC), 100.0)] * 10
    result = compute_liquidity_metrics(
        bars, market_cap=1_000_000_000.0, mean_spread_pct=1.25
    )
    assert result is not None
    assert result.market_cap == 1_000_000_000.0
    assert result.mean_spread_pct == 1.25


# --------------------------------------------------------------------------- #
# classify / tier_gte
# --------------------------------------------------------------------------- #


def test_classify_tier_a() -> None:
    result = classify(metrics(), UniverseThresholds())
    assert result == Classification(
        tradable=True, tier=UniverseTier.A, reasons=[]
    )


def test_classify_tier_b() -> None:
    result = classify(
        metrics(dollar_volume=8_000_000.0, last_price=8.0), UniverseThresholds()
    )
    assert result.tradable is True
    assert result.tier is UniverseTier.B


def test_classify_tier_c() -> None:
    result = classify(
        metrics(dollar_volume=1_500_000.0, last_price=3.0), UniverseThresholds()
    )
    assert result.tradable is True
    assert result.tier is UniverseTier.C


def test_classify_fails_base_floor() -> None:
    result = classify(
        metrics(last_price=1.0, dollar_volume=500_000.0), UniverseThresholds()
    )
    assert result.tradable is False
    assert result.tier is None
    assert any("price" in r for r in result.reasons)
    assert any("dollar_volume" in r for r in result.reasons)


def test_classify_rejects_wide_spread() -> None:
    result = classify(metrics(spread=5.0), UniverseThresholds())
    assert result.tradable is False
    assert any("spread" in r for r in result.reasons)


def test_classify_spread_ignored_when_unavailable() -> None:
    result = classify(metrics(spread=None), UniverseThresholds())
    assert result.tradable is True


def test_classify_market_cap_floor() -> None:
    thresholds = UniverseThresholds(min_market_cap=500_000_000.0)
    small = classify(metrics(market_cap=100_000_000.0), thresholds)
    assert small.tradable is False
    big = classify(metrics(market_cap=1_000_000_000.0), thresholds)
    assert big.tradable is True


def test_classify_trading_days_floor() -> None:
    result = classify(metrics(trading_days=10), UniverseThresholds(min_trading_days=30))
    assert result.tradable is False
    assert any("trading_days" in r for r in result.reasons)


def test_classify_disabled_spread_threshold() -> None:
    result = classify(metrics(spread=99.0), UniverseThresholds(max_spread_pct=None))
    assert result.tradable is True


def test_tier_gte_strict_ordering() -> None:
    assert tier_gte(UniverseTier.A, UniverseTier.A)
    assert tier_gte(UniverseTier.A, UniverseTier.B)
    assert tier_gte(UniverseTier.A, UniverseTier.C)
    assert tier_gte(UniverseTier.B, UniverseTier.C)
    assert tier_gte(UniverseTier.C, UniverseTier.C)
    assert not tier_gte(UniverseTier.B, UniverseTier.A)
    assert not tier_gte(UniverseTier.C, UniverseTier.B)


# --------------------------------------------------------------------------- #
# universe_as_of (point-in-time reconstruction)
# --------------------------------------------------------------------------- #


def _membership(symbol: str, added: date, removed: date | None = None) -> UniverseMembership:
    return UniverseMembership(
        symbol=symbol,
        status=TradingStatus.DELISTED if removed else TradingStatus.ACTIVE,
        tier=UniverseTier.A,
        added_at=added,
        removed_at=removed,
    )


def test_universe_as_of_uses_added_removed_timeline() -> None:
    memberships = [
        _membership("AAPL", date(2021, 1, 1)),
        _membership("GEV", date(2024, 3, 27)),
        _membership("SOLV", date(2024, 3, 26), removed=date(2026, 1, 10)),
    ]
    assert universe_as_of(memberships, date(2024, 3, 26)) == ["AAPL", "SOLV"]
    assert universe_as_of(memberships, date(2026, 6, 1)) == ["AAPL", "GEV"]


def test_universe_as_of_excludes_delisted_without_removal_date() -> None:
    memberships = [
        UniverseMembership(
            symbol="GONE",
            status=TradingStatus.DELISTED,
            tier=None,
            added_at=date(2020, 1, 1),
            removed_at=None,
        )
    ]
    assert universe_as_of(memberships, date(2026, 1, 1)) == []


def test_universe_as_of_removed_on_same_day_excluded() -> None:
    memberships = [_membership("X", date(2020, 1, 1), removed=date(2026, 8, 10))]
    assert universe_as_of(memberships, date(2026, 8, 10)) == []


# --------------------------------------------------------------------------- #
# resolve_status (suspensions / delistings from data staleness)
# --------------------------------------------------------------------------- #


def _fresh_bar(days_ago: int = 0) -> PriceBar:
    from datetime import timedelta

    return bar(
        "X",
        datetime.now(UTC) - timedelta(days=days_ago),
        100.0,
    )


def _resolve_status(
    status: TradingStatus, last_bar: PriceBar | None = None, days_ago: int | None = None
) -> TradingStatus:
    if days_ago is not None:
        last_bar = _fresh_bar(days_ago)
    membership = UniverseMembership(symbol="X", status=status)
    return resolve_status(membership, last_bar, UniverseThresholds(), date.today())


def test_resolve_status_active_fresh_stays_active() -> None:
    assert _resolve_status(TradingStatus.ACTIVE, days_ago=1) is TradingStatus.ACTIVE


def test_resolve_status_active_stale_becomes_suspended() -> None:
    assert _resolve_status(TradingStatus.ACTIVE, days_ago=20) is TradingStatus.SUSPENDED


def test_resolve_status_active_very_stale_becomes_delisted() -> None:
    assert _resolve_status(TradingStatus.ACTIVE, days_ago=90) is TradingStatus.DELISTED


def test_resolve_status_suspended_resumes_with_fresh_data() -> None:
    assert _resolve_status(TradingStatus.SUSPENDED, days_ago=2) is TradingStatus.ACTIVE


def test_resolve_status_no_bar_and_removed_stays_delisted() -> None:
    m = UniverseMembership(
        symbol="X", status=TradingStatus.SUSPENDED, removed_at=date(2026, 1, 1)
    )
    assert resolve_status(m, None, UniverseThresholds(), date.today()) is TradingStatus.DELISTED


def test_resolve_status_no_bar_without_removal_suspended() -> None:
    assert _resolve_status(TradingStatus.ACTIVE, None) is TradingStatus.SUSPENDED


def test_resolve_status_delisted_is_terminal() -> None:
    assert _resolve_status(TradingStatus.DELISTED, days_ago=1) is TradingStatus.DELISTED


# --------------------------------------------------------------------------- #
# detect_symbol_changes (ticker renames)
# --------------------------------------------------------------------------- #


def test_detect_symbol_change_on_name_match() -> None:
    memberships = [
        UniverseMembership(symbol="FB", name="Meta Platforms Inc.", status=TradingStatus.ACTIVE)
    ]
    discovered = [
        DiscoveredAsset(symbol="META", name="Meta Platforms Inc."),
        DiscoveredAsset(symbol="AAPL", name="Apple Inc."),
    ]
    changes = detect_symbol_changes(memberships, discovered, as_of=date(2026, 8, 11))
    assert changes == [
        SymbolChange(
            old_symbol="FB",
            new_symbol="META",
            effective_at=date(2026, 8, 11),
            reason="name match after ticker change",
        )
    ]


def test_no_change_when_symbol_still_discovered() -> None:
    memberships = [
        UniverseMembership(symbol="AAPL", name="Apple Inc.", status=TradingStatus.ACTIVE)
    ]
    discovered = [DiscoveredAsset(symbol="AAPL", name="Apple Inc.")]
    assert detect_symbol_changes(memberships, discovered, as_of=date(2026, 8, 11)) == []


def test_no_change_when_name_differs() -> None:
    memberships = [
        UniverseMembership(symbol="OLD", name="Some Co.", status=TradingStatus.ACTIVE)
    ]
    discovered = [DiscoveredAsset(symbol="NEW", name="Completely Different")]
    assert detect_symbol_changes(memberships, discovered, as_of=date(2026, 8, 11)) == []


def test_no_change_for_delisted_members() -> None:
    memberships = [
        UniverseMembership(symbol="DEAD", name="Dead Co.", status=TradingStatus.DELISTED)
    ]
    discovered = [DiscoveredAsset(symbol="DEADX", name="Dead Co.")]
    assert detect_symbol_changes(memberships, discovered, as_of=date(2026, 8, 11)) == []
