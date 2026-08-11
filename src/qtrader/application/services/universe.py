"""Dynamic trading universe engine (Phase 2).

Replaces the single-purpose helper with a real engine that:

* discovers candidate symbols from a provider (Yahoo screener) with a seeded
  fallback (existing ``stocks`` table) when the provider is unavailable;
* computes configurable liquidity metrics from stored bars;
* applies a base tradability floor, then assigns A/B/C tiers (strict
  supersets — A >= B >= C) with fully configurable thresholds;
* persists membership lifecycle (added / suspended / delisted) and ticker
  changes so point-in-time reconstruction never leaks future information;
* exposes a strategy-compatible filter request API for consumers.

The pure functions (``classify``, ``compute_liquidity_metrics``,
``universe_as_of``, ``resolve_status``, ``detect_symbol_changes``) are
dependency-free and unit-tested; the :class:`UniverseEngine` orchestrates the
async ports around them. See docs/audit/19-phase2-universe.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from statistics import fmean
from typing import Any

from qtrader.config.logging import get_logger
from qtrader.domain.entities import (
    AssetType,
    DiscoveredAsset,
    LiquidityMetrics,
    Stock,
    SymbolChange,
    TradingStatus,
    UniverseMembership,
    UniverseTier,
)
from qtrader.domain.ports import (
    AssetDiscoveryProvider,
    PriceRepository,
    StockRepository,
    UniverseRepository,
)
from qtrader.domain.value_objects import Interval, PriceBar

_TIER_ORDER = {UniverseTier.A: 0, UniverseTier.B: 1, UniverseTier.C: 2}


# --------------------------------------------------------------------------- #
# Point-in-time helpers (kept from Phase 1)
# --------------------------------------------------------------------------- #


def point_in_time_universe(
    listing_dates: dict[str, date], as_of: date
) -> list[str]:
    """Symbols whose listing date is on or before ``as_of``.

    Returns the sorted list of tradeable symbols at ``as_of``.
    """
    return sorted(sym for sym, first in listing_dates.items() if first <= as_of)


def listing_date_from_first_bar(first_bar_ts: datetime) -> date:
    """Extract a listing date from a first-bar timestamp."""
    return first_bar_ts.date()


# --------------------------------------------------------------------------- #
# Configuration & pure decision logic
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class UniverseThresholds:
    """All universe filters/tiers are configurable (env-driven via settings).

    ``None`` disables a check (spread / market cap).
    """

    min_trading_days: int = 30
    min_price: float = 2.0
    min_dollar_volume: float = 1_000_000.0
    min_avg_volume: float = 200_000.0
    max_spread_pct: float | None = 2.0
    min_market_cap: float | None = None
    liquidity_window_days: int = 21
    tier_a_min_dollar_volume: float = 20_000_000.0
    tier_a_min_price: float = 10.0
    tier_b_min_dollar_volume: float = 5_000_000.0
    tier_b_min_price: float = 5.0
    stale_suspend_days: int = 15
    stale_delist_days: int = 60
    max_candidates: int = 500
    seed_from_watchlist: bool = True

    @classmethod
    def from_settings(cls, settings: Any) -> UniverseThresholds:
        """Build from the env-driven :class:`~qtrader.config.settings.Settings`."""
        return cls(
            min_trading_days=settings.universe_min_trading_days,
            min_price=settings.universe_min_price,
            min_dollar_volume=settings.universe_min_dollar_volume,
            min_avg_volume=settings.universe_min_avg_volume,
            max_spread_pct=settings.universe_max_spread_pct,
            min_market_cap=settings.universe_min_market_cap,
            liquidity_window_days=settings.universe_liquidity_window_days,
            tier_a_min_dollar_volume=settings.universe_tier_a_min_dollar_volume,
            tier_a_min_price=settings.universe_tier_a_min_price,
            tier_b_min_dollar_volume=settings.universe_tier_b_min_dollar_volume,
            tier_b_min_price=settings.universe_tier_b_min_price,
            stale_suspend_days=settings.universe_stale_suspend_days,
            stale_delist_days=settings.universe_stale_delist_days,
            max_candidates=settings.universe_max_candidates,
            seed_from_watchlist=settings.universe_seed_from_watchlist,
        )


@dataclass(frozen=True, slots=True)
class Classification:
    """Result of applying the base floor + tier thresholds to one symbol."""

    tradable: bool
    tier: UniverseTier | None
    reasons: list[str] = field(default_factory=list)


def estimate_spread_pct(intraday_bars: list[PriceBar]) -> float | None:
    """Mean (high-low)/close % across intraday bars, or ``None`` if unusable."""
    valid: list[float] = []
    for bar in intraday_bars:
        close = float(bar.close)
        if close <= 0:
            continue
        valid.append((float(bar.high) - float(bar.low)) / close * 100.0)
    return fmean(valid) if valid else None


def compute_liquidity_metrics(
    bars: list[PriceBar],
    *,
    window_days: int = 21,
    market_cap: float | None = None,
    mean_spread_pct: float | None = None,
) -> LiquidityMetrics | None:
    """Aggregate rolling liquidity metrics from daily bars (pure).

    Returns ``None`` when there is not enough history to judge tradability
    (fewer than 2 bars) — the caller treats that as "not yet confirmed".
    """
    if len(bars) < 2:
        return None
    window = bars[-max(1, window_days):]
    closes = [float(b.close) for b in window]
    volumes = [float(b.volume) for b in window]
    avg_dollar_volume = fmean(c * v for c, v in zip(closes, volumes, strict=True))
    avg_volume = fmean(volumes)
    return LiquidityMetrics(
        symbol=bars[-1].symbol,
        last_price=closes[-1],
        avg_dollar_volume=avg_dollar_volume,
        avg_volume=avg_volume,
        mean_spread_pct=mean_spread_pct,
        market_cap=market_cap,
        trading_days=len(bars),
        first_bar=bars[0].ts,
        last_bar=bars[-1].ts,
    )


def classify(metrics: LiquidityMetrics, thresholds: UniverseThresholds) -> Classification:
    """Base tradability floor then tier assignment (A >= B >= C). Pure."""
    reasons: list[str] = []
    if metrics.trading_days < thresholds.min_trading_days:
        reasons.append(f"trading_days {metrics.trading_days} < {thresholds.min_trading_days}")
    if metrics.last_price < thresholds.min_price:
        reasons.append(f"price {metrics.last_price:.2f} < {thresholds.min_price:.2f}")
    if metrics.avg_dollar_volume < thresholds.min_dollar_volume:
        reasons.append(
            f"dollar_volume {metrics.avg_dollar_volume:,.0f} < {thresholds.min_dollar_volume:,.0f}"
        )
    if metrics.avg_volume < thresholds.min_avg_volume:
        reasons.append(f"volume {metrics.avg_volume:,.0f} < {thresholds.min_avg_volume:,.0f}")
    if (
        thresholds.max_spread_pct is not None
        and metrics.mean_spread_pct is not None
        and metrics.mean_spread_pct > thresholds.max_spread_pct
    ):
        reasons.append(f"spread {metrics.mean_spread_pct:.2f}% > {thresholds.max_spread_pct:.2f}%")
    if (
        thresholds.min_market_cap is not None
        and metrics.market_cap is not None
        and metrics.market_cap < thresholds.min_market_cap
    ):
        reasons.append(
            f"market_cap {metrics.market_cap:,.0f} < {thresholds.min_market_cap:,.0f}"
        )
    if reasons:
        return Classification(tradable=False, tier=None, reasons=reasons)

    if (
        metrics.avg_dollar_volume >= thresholds.tier_a_min_dollar_volume
        and metrics.last_price >= thresholds.tier_a_min_price
    ):
        tier = UniverseTier.A
    elif (
        metrics.avg_dollar_volume >= thresholds.tier_b_min_dollar_volume
        and metrics.last_price >= thresholds.tier_b_min_price
    ):
        tier = UniverseTier.B
    else:
        tier = UniverseTier.C
    return Classification(tradable=True, tier=tier, reasons=[])


def tier_gte(tier: UniverseTier, minimum: UniverseTier) -> bool:
    """True when ``tier`` is at least as strict as ``minimum`` (A >= B >= C)."""
    return _TIER_ORDER[tier] <= _TIER_ORDER[minimum]


def universe_as_of(
    memberships: list[UniverseMembership], as_of: date
) -> list[str]:
    """Symbols tradable at ``as_of`` (no look-ahead).

    Uses persisted ``added_at``/``removed_at`` timelines: listed on or before
    ``as_of`` and not yet removed. A delisted symbol without a removal date is
    excluded (defensive — the engine always sets ``removed_at`` on removal).
    """
    result: list[str] = []
    for membership in memberships:
        if membership.added_at is None or membership.added_at > as_of:
            continue
        if membership.removed_at is not None and membership.removed_at <= as_of:
            continue
        if membership.removed_at is None and membership.status is TradingStatus.DELISTED:
            continue
        result.append(membership.symbol)
    return sorted(result)


def resolve_status(
    membership: UniverseMembership,
    last_bar: PriceBar | None,
    thresholds: UniverseThresholds,
    as_of: date,
) -> TradingStatus:
    """Next lifecycle status from data staleness (pure).

    A delisted symbol stays delisted; a symbol with no stored bars at all is
    suspended (unverifiable); otherwise the age of the latest bar drives the
    transition ACTIVE -> SUSPENDED -> DELISTED, and a resumed stream flips a
    SUSPENDED symbol back to ACTIVE.
    """
    if membership.status is TradingStatus.DELISTED:
        return TradingStatus.DELISTED
    if last_bar is None:
        if membership.removed_at is not None:
            return TradingStatus.DELISTED
        return TradingStatus.SUSPENDED
    age_days = (as_of - last_bar.ts.date()).days
    if age_days > thresholds.stale_delist_days:
        return TradingStatus.DELISTED
    if age_days > thresholds.stale_suspend_days:
        return TradingStatus.SUSPENDED
    return TradingStatus.ACTIVE


def _normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def detect_symbol_changes(
    memberships: list[UniverseMembership],
    discovered: list[DiscoveredAsset],
    *,
    as_of: date,
) -> list[SymbolChange]:
    """Heuristic ticker-rename detection (pure).

    When an existing (non-delisted) member's symbol is absent from today's
    discovery set but a newly discovered asset carries the *same normalized
    name*, record a symbol change. Conservative: names must match exactly and
    a rename target is only used once.
    """
    discovered_by_name: dict[str, DiscoveredAsset] = {}
    for asset in discovered:
        key = _normalize_name(asset.name or "")
        if key:
            discovered_by_name.setdefault(key, asset)
    discovered_symbols = {asset.symbol for asset in discovered}

    changes: list[SymbolChange] = []
    used_new: set[str] = set()
    for membership in memberships:
        if membership.status is TradingStatus.DELISTED:
            continue
        if membership.symbol in discovered_symbols:
            continue
        target = discovered_by_name.get(_normalize_name(membership.name or ""))
        if target is None or target.symbol == membership.symbol:
            continue
        if target.symbol in used_new:
            continue
        changes.append(
            SymbolChange(
                old_symbol=membership.symbol,
                new_symbol=target.symbol,
                effective_at=as_of,
                reason="name match after ticker change",
            )
        )
        used_new.add(target.symbol)
    return changes


# --------------------------------------------------------------------------- #
# Strategy-compatible filter request
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class UniverseFilterRequest:
    """What a strategy/backtest asks the universe engine for.

    ``min_tier`` follows the strict ordering A >= B >= C. ``min_dollar_volume``
    and ``min_price`` are optional live overrides recomputed from stored bars.
    """

    min_tier: UniverseTier = UniverseTier.C
    as_of: date | None = None
    min_dollar_volume: float | None = None
    min_price: float | None = None
    max_symbols: int | None = None
    symbols: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class UniverseReport:
    """One refresh run's outcome (new members, removals, renames)."""

    source: str
    discovered: int
    added: list[str] = field(default_factory=list)
    suspended: list[str] = field(default_factory=list)
    delisted: list[str] = field(default_factory=list)
    resumed: list[str] = field(default_factory=list)
    symbol_changes: list[SymbolChange] = field(default_factory=list)
    as_of: date = field(default_factory=lambda: datetime.now(UTC).date())


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #


class UniverseEngine:
    """Orchestrates discovery, filtering, tiering and persistence."""

    def __init__(
        self,
        *,
        discovery: AssetDiscoveryProvider,
        universe_repo: UniverseRepository,
        prices: PriceRepository,
        stocks: StockRepository,
        thresholds: UniverseThresholds | None = None,
        logger: Any | None = None,
    ) -> None:
        self._discovery = discovery
        self._repo = universe_repo
        self._prices = prices
        self._stocks = stocks
        self._thresholds = thresholds or UniverseThresholds()
        self._logger = logger or get_logger("qtrader.universe")

    @property
    def thresholds(self) -> UniverseThresholds:
        return self._thresholds

    async def refresh(self) -> UniverseReport:
        """Run one full universe update and persist the result."""
        as_of = datetime.now(UTC).date()
        source, discovered = await self._discover()
        report = UniverseReport(source=source, discovered=len(discovered), as_of=as_of)

        existing = {m.symbol: m for m in await self._repo.list_memberships()}
        touched: set[str] = set()
        for asset in discovered[: self._thresholds.max_candidates]:
            if asset.symbol in touched:
                continue
            metrics, classification = await self._metrics(asset)
            if metrics is None:
                continue
            touched.add(asset.symbol)
            previous = existing.get(asset.symbol)
            if not classification.tradable and previous is None:
                self._logger.info(
                    "universe.untradable",
                    symbol=asset.symbol,
                    reasons=classification.reasons,
                )
                continue
            if classification.tradable:
                status = TradingStatus.ACTIVE
                tier = classification.tier
                removed_at = None
                reason = None
            else:
                # Existing member dipped below the floor: keep membership and
                # history, drop the tradability grade (staleness still governs
                # removal). New candidates below the floor are not persisted.
                assert previous is not None
                status = previous.status
                tier = previous.tier
                removed_at = previous.removed_at
                reason = classification.reasons[0]
            added_on = metrics.first_bar.date() if metrics.first_bar is not None else as_of
            membership = UniverseMembership(
                symbol=asset.symbol,
                status=status,
                tier=tier,
                added_at=previous.added_at
                if previous is not None and previous.added_at is not None
                else added_on,
                removed_at=removed_at,
                last_traded_at=metrics.last_bar,
                asset_type=asset.asset_type,
                name=asset.name or (previous.name if previous else None),
                reason=reason,
                metadata={
                    "avg_dollar_volume": metrics.avg_dollar_volume,
                    "avg_volume": metrics.avg_volume,
                    "last_price": metrics.last_price,
                    "market_cap": metrics.market_cap,
                    "spread_pct": metrics.mean_spread_pct,
                },
            )
            if previous is None:
                report.added.append(asset.symbol)
            await self._repo.upsert_membership(membership)
            self._logger.info(
                "universe.member",
                symbol=asset.symbol,
                tier=tier.value if tier else None,
                tradable=classification.tradable,
                reasons=classification.reasons,
            )

        for symbol, membership in existing.items():
            if symbol in touched or membership.status is TradingStatus.DELISTED:
                continue
            last_bar = await self._prices.latest(symbol, Interval.D1)
            status = resolve_status(membership, last_bar, self._thresholds, as_of)
            if status is membership.status:
                continue
            reason = {
                TradingStatus.SUSPENDED: "no recent price data",
                TradingStatus.DELISTED: "no price data beyond delist threshold",
                TradingStatus.ACTIVE: "price data resumed",
            }[status]
            await self._repo.upsert_membership(
                replace(
                    membership,
                    status=status,
                    removed_at=as_of if status is not TradingStatus.ACTIVE else None,
                    reason=reason,
                )
            )
            if status is TradingStatus.SUSPENDED:
                report.suspended.append(symbol)
            elif status is TradingStatus.DELISTED:
                report.delisted.append(symbol)
            else:
                report.resumed.append(symbol)

        changes = detect_symbol_changes(
            list(existing.values()), discovered, as_of=as_of
        )
        for change in changes:
            await self._repo.record_symbol_change(change)
        report = replace(report, symbol_changes=changes)
        return report

    async def _metrics(
        self, asset: DiscoveredAsset
    ) -> tuple[LiquidityMetrics | None, Classification]:
        """Confirm price history, compute metrics and classify (pure)."""
        bars = await self._prices.history(asset.symbol, Interval.D1, limit=400)
        metrics = compute_liquidity_metrics(
            bars,
            window_days=self._thresholds.liquidity_window_days,
            market_cap=asset.market_cap,
        )
        if metrics is None:
            self._logger.info(
                "universe.insufficient_history", symbol=asset.symbol, bars=len(bars)
            )
            return None, Classification(
                tradable=False, tier=None, reasons=["insufficient history"]
            )

        if self._thresholds.max_spread_pct is not None:
            intraday = await self._prices.history(asset.symbol, Interval.M5, limit=60)
            metrics = replace(
                metrics, mean_spread_pct=estimate_spread_pct(intraday)
            )
        return metrics, classify(metrics, self._thresholds)

    async def _discover(self) -> tuple[str, list[DiscoveredAsset]]:
        try:
            assets = await self._discovery.discover_candidates(
                limit=self._thresholds.max_candidates
            )
            if assets:
                return "provider", assets
        except Exception:
            self._logger.warning(
                "universe.discovery_failed",
                exc_info=True,
                fallback="seed",
            )
        return "seed", await self._seed_candidates()

    async def _seed_candidates(self) -> list[DiscoveredAsset]:
        """Bootstrap universe from the persisted stocks table (fallback)."""
        stocks = await self._stocks.list_active()
        if not stocks and self._thresholds.seed_from_watchlist:
            from qtrader.config.settings import Settings

            stocks = [
                Stock(symbol=symbol, exchange="XNAS", name=symbol)
                for symbol in Settings().watchlist_symbols
            ]
        return [
            DiscoveredAsset(
                symbol=stock.symbol,
                name=stock.name or stock.symbol,
                exchange=stock.exchange,
                asset_type=AssetType.COMMON_STOCK,
            )
            for stock in stocks
        ]

    async def filter_symbols(self, request: UniverseFilterRequest) -> list[str]:
        """Strategy-compatible filter: tradable symbols meeting the request."""
        memberships = await self._repo.list_memberships()
        as_of = request.as_of or datetime.now(UTC).date()
        by_symbol = {m.symbol: m for m in memberships}
        allowed = set(universe_as_of(memberships, as_of))
        if request.symbols is not None:
            allowed &= set(request.symbols)

        selected: list[str] = []
        for symbol in sorted(allowed):
            membership = by_symbol.get(symbol)
            if membership is None or membership.tier is None:
                continue
            if not tier_gte(membership.tier, request.min_tier):
                continue
            if (
                request.min_dollar_volume is not None or request.min_price is not None
            ):
                bars = await self._prices.history(symbol, Interval.D1, limit=400)
                metrics = compute_liquidity_metrics(
                    bars, window_days=self._thresholds.liquidity_window_days
                )
                if metrics is None:
                    continue
                if (
                    request.min_dollar_volume is not None
                    and metrics.avg_dollar_volume < request.min_dollar_volume
                ):
                    continue
                if (
                    request.min_price is not None
                    and metrics.last_price < request.min_price
                ):
                    continue
            selected.append(symbol)
            if request.max_symbols is not None and len(selected) >= request.max_symbols:
                break
        return selected

    async def snapshot(self) -> dict[str, Any]:
        """Current coverage / tradable count for dashboards and the audit report."""
        memberships = await self._repo.list_memberships()
        active = [m for m in memberships if m.status is TradingStatus.ACTIVE]
        tradable = [m for m in active if m.tier is not None]
        return {
            "as_of": datetime.now(UTC).date().isoformat(),
            "total_members": len(memberships),
            "active": len(active),
            "suspended": sum(1 for m in memberships if m.status is TradingStatus.SUSPENDED),
            "delisted": sum(1 for m in memberships if m.status is TradingStatus.DELISTED),
            "tradable": len(tradable),
            "tiers": {
                tier.value: sum(1 for m in active if m.tier is tier)
                for tier in (UniverseTier.A, UniverseTier.B, UniverseTier.C)
            },
            "unassigned": sum(1 for m in active if m.tier is None),
        }
