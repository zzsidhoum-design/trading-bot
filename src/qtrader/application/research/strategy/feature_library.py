"""Feature library — the validated feature set the generator samples from.

Features are named columns evaluated per bar by :class:`StrategyEvaluator`.
Each belongs to exactly one :class:`FeatureCategory` so hypotheses combine
*validated* families (trend, momentum, volume, volatility, exit) instead of
blind indicator soup. Snapshot features come from the production
``IndicatorSnapshot``; price features are derived from raw bars.
"""

from __future__ import annotations

from dataclasses import dataclass

from qtrader.application.research.strategy.specs import FeatureCategory

# Columns copied straight from ``IndicatorSnapshot`` (already computed by the
# production ``IndicatorEngine`` — pandas stays behind that seam).
SNAPSHOT_FEATURES: tuple[str, ...] = (
    "rsi",
    "ema_9",
    "ema_21",
    "sma_50",
    "sma_200",
    "macd",
    "macd_signal",
    "macd_hist",
    "atr",
    "vwap",
    "boll_upper",
    "boll_middle",
    "boll_lower",
    "adx",
    "stoch_k",
    "stoch_d",
)

# Price-derived columns (rolling, computed from raw bars by the evaluator).
PRICE_FEATURES: tuple[str, ...] = (
    "close",
    "ret_1",
    "ret_5",
    "ret_10",
    "ret_20",
    "ret_60",
    "vol_20",
    "atr_pct",
    "volume_ratio",
    "range_ratio",
    "pos_in_range_20",
    "up_ratio_20",
)


@dataclass(frozen=True, slots=True)
class Feature:
    """One evaluable feature column with its research category."""

    name: str
    category: FeatureCategory
    description: str
    source: str = "snapshot"


FEATURES: tuple[Feature, ...] = (
    # Trend
    Feature("ema_9", FeatureCategory.TREND, "9-period exponential moving average"),
    Feature("ema_21", FeatureCategory.TREND, "21-period exponential moving average"),
    Feature("sma_50", FeatureCategory.TREND, "50-period simple moving average"),
    Feature("sma_200", FeatureCategory.TREND, "200-period simple moving average"),
    Feature("vwap", FeatureCategory.TREND, "volume-weighted average price"),
    Feature("boll_middle", FeatureCategory.TREND, "Bollinger middle band (SMA-20)"),
    Feature("adx", FeatureCategory.TREND, "average directional index (trend strength)"),
    # Momentum
    Feature("rsi", FeatureCategory.MOMENTUM, "relative strength index"),
    Feature("macd", FeatureCategory.MOMENTUM, "MACD line"),
    Feature("macd_signal", FeatureCategory.MOMENTUM, "MACD signal line"),
    Feature("macd_hist", FeatureCategory.MOMENTUM, "MACD histogram (line - signal)"),
    Feature("stoch_k", FeatureCategory.MOMENTUM, "stochastic %K"),
    Feature("stoch_d", FeatureCategory.MOMENTUM, "stochastic %D"),
    Feature("ret_5", FeatureCategory.MOMENTUM, "5-bar return"),
    Feature("ret_20", FeatureCategory.MOMENTUM, "20-bar return"),
    Feature("ret_60", FeatureCategory.MOMENTUM, "60-bar return"),
    Feature("pos_in_range_20", FeatureCategory.MOMENTUM, "position within 20-bar range"),
    Feature("up_ratio_20", FeatureCategory.MOMENTUM, "fraction of up bars over 20"),
    # Volume
    Feature("volume_ratio", FeatureCategory.VOLUME, "5-bar volume vs 20-bar average"),
    # Volatility
    Feature("atr", FeatureCategory.VOLATILITY, "average true range"),
    Feature("atr_pct", FeatureCategory.VOLATILITY, "ATR as fraction of close"),
    Feature("vol_20", FeatureCategory.VOLATILITY, "20-bar return volatility"),
    Feature("range_ratio", FeatureCategory.VOLATILITY, "mean bar range as fraction of close"),
    Feature("boll_upper", FeatureCategory.VOLATILITY, "Bollinger upper band"),
    Feature("boll_lower", FeatureCategory.VOLATILITY, "Bollinger lower band"),
    # Exit
    Feature("close", FeatureCategory.EXIT, "current bar close (price context)"),
    Feature("ret_1", FeatureCategory.EXIT, "1-bar return (fade signal)"),
)

# Category templates — the *validated* primitive conditions the generator
# combines. Each maps a category to a set of candidate (feature, op, value)
# shapes; thresholds are drawn from parameter ranges, not tuned on OOS.
CATEGORY_TEMPLATES: dict[FeatureCategory, tuple[str, ...]] = {
    FeatureCategory.TREND: (
        "close>ema_21",
        "close>sma_50",
        "ema_9>ema_21",
        "ema_21>sma_50",
        "close>vwap",
        "adx>25",
    ),
    FeatureCategory.MOMENTUM: (
        "rsi>50",
        "rsi<30",
        "macd_hist>0",
        "macd>macd_signal",
        "stoch_k>stoch_d",
        "ret_20>0",
        "pos_in_range_20>0.5",
    ),
    FeatureCategory.VOLUME: (
        "volume_ratio>1.2",
        "volume_ratio>1.5",
    ),
    FeatureCategory.VOLATILITY: (
        "atr_pct>0.02",
        "atr_pct<0.05",
        "vol_20>0.01",
        "close>boll_lower",
    ),
    FeatureCategory.EXIT: (
        "rsi>70",
        "rsi<30",
        "macd_hist<0",
        "close<boll_middle",
        "ret_1<0",
    ),
}


class FeatureLibrary:
    """Access to the validated feature set and its categories."""

    def all(self) -> list[Feature]:
        return list(FEATURES)

    def names(self) -> list[str]:
        return [f.name for f in FEATURES]

    def categories(self) -> list[FeatureCategory]:
        return [c for c in FeatureCategory]

    def by_category(self, category: FeatureCategory) -> list[Feature]:
        return [f for f in FEATURES if f.category is category]

    def has(self, name: str) -> bool:
        return any(f.name == name for f in FEATURES)

    def get(self, name: str) -> Feature | None:
        for feature in FEATURES:
            if feature.name == name:
                return feature
        return None


__all__ = [
    "CATEGORY_TEMPLATES",
    "FEATURES",
    "Feature",
    "FeatureLibrary",
    "PRICE_FEATURES",
    "SNAPSHOT_FEATURES",
]
