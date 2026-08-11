"""Unit tests for settings & the live-trading gate."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from qtrader.config.settings import Settings
from qtrader.domain.value_objects import TradingMode


class TestSettings:
    def test_defaults(self) -> None:
        s = Settings(_env_file=None)
        assert s.qtrader_mode is TradingMode.BACKTEST
        assert s.live_enabled is False
        assert "postgresql+asyncpg://" in s.database_url

    def test_live_requires_mode_and_flag(self) -> None:
        mode_only = Settings(
            qtrader_mode=TradingMode.LIVE, enable_live_trading=False, _env_file=None
        )
        flag_only = Settings(
            qtrader_mode=TradingMode.PAPER, enable_live_trading=True, _env_file=None
        )
        both = Settings(qtrader_mode=TradingMode.LIVE, enable_live_trading=True, _env_file=None)

        assert mode_only.live_enabled is False
        assert flag_only.live_enabled is False
        assert both.live_enabled is True

    def test_invalid_port(self) -> None:
        with pytest.raises(ValidationError):
            Settings(postgres_port=70000, _env_file=None)

    def test_database_url_components(self) -> None:
        s = Settings(
            postgres_host="db.internal",
            postgres_port=5433,
            postgres_user="alice",
            postgres_password="secret",
            postgres_db="prod",
            _env_file=None,
        )
        assert s.database_url == "postgresql+asyncpg://alice:secret@db.internal:5433/prod"

    def test_universe_defaults(self) -> None:
        s = Settings(_env_file=None)
        assert s.universe_min_dollar_volume == 1_000_000.0
        assert s.universe_tier_a_min_dollar_volume == 20_000_000.0
        assert s.universe_tier_b_min_price == 5.0
        assert s.universe_max_spread_pct == 2.0
        assert s.universe_min_market_cap is None
        assert s.universe_refresh_hour == 1
        assert s.universe_seed_from_watchlist is True

    def test_universe_settings_env_override(self) -> None:
        s = Settings(
            universe_min_dollar_volume=5_000_000.0,
            universe_tier_a_min_dollar_volume=50_000_000.0,
            universe_max_spread_pct=None,
            universe_refresh_hour=4,
            _env_file=None,
        )
        assert s.universe_min_dollar_volume == 5_000_000.0
        assert s.universe_tier_a_min_dollar_volume == 50_000_000.0
        assert s.universe_max_spread_pct is None
        assert s.universe_refresh_hour == 4
