"""Unit tests for the Alpaca env-var unification (ALPACA_* + APCA_*)."""

from __future__ import annotations

import pytest

from qtrader.infrastructure.brokers.alpaca import (
    _LIVE_URL,
    _PAPER_URL,
    AlpacaBroker,
)


def test_constructor_honors_project_alpaca_env(monkeypatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "pk_123")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "sk_456")
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)

    broker = AlpacaBroker()
    assert broker._api_key == "pk_123"  # noqa: SLF001
    assert broker._secret == "sk_456"  # noqa: SLF001
    assert broker._base_url == _PAPER_URL


def test_constructor_honors_alpaca_native_env(monkeypatch) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    monkeypatch.setenv("APCA_API_KEY_ID", "pk_native")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "sk_native")

    broker = AlpacaBroker()
    assert broker._api_key == "pk_native"  # noqa: SLF001
    assert broker._secret == "sk_native"  # noqa: SLF001


def test_constructor_explicit_args_win_over_env(monkeypatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "pk_env")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "sk_env")

    broker = AlpacaBroker(api_key="pk_arg", secret="sk_arg")
    assert broker._api_key == "pk_arg"  # noqa: SLF001
    assert broker._secret == "sk_arg"  # noqa: SLF001


def test_constructor_defaults_to_paper_url(monkeypatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "pk")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "sk")
    monkeypatch.delenv("ALPACA_LIVE", raising=False)

    broker = AlpacaBroker()
    assert broker._base_url == _PAPER_URL  # noqa: SLF001


def test_constructor_live_env_forces_live_url(monkeypatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "pk")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "sk")
    monkeypatch.setenv("ALPACA_LIVE", "true")

    broker = AlpacaBroker()
    assert broker._base_url == _LIVE_URL  # noqa: SLF001


def test_constructor_live_arg_forces_live_url(monkeypatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "pk")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "sk")
    monkeypatch.delenv("ALPACA_LIVE", raising=False)

    broker = AlpacaBroker(live=True)
    assert broker._base_url == _LIVE_URL  # noqa: SLF001


def test_constructor_raises_without_credentials(monkeypatch) -> None:
    for name in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="credentials are not configured"):
        AlpacaBroker()
