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
