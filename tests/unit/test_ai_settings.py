"""Phase 6 — AI settings mixin (weights config, decision/failure/selector configs)."""

from __future__ import annotations

from qtrader.application.ai.decision import DecisionConfig
from qtrader.application.ai.failure import FailureConfig
from qtrader.application.ai.models import AgentWeightsConfig
from qtrader.application.ai.selector import SelectorConfig
from qtrader.config.settings import Settings


def _settings(**overrides: object) -> Settings:
    params: dict[str, object] = dict(_env_file=None, _secrets_dir=None)
    params.update(overrides)
    return Settings(**params)


def test_ai_defaults() -> None:
    s = _settings()
    assert s.ai_weights_version == "1.0"
    assert s.ai_min_ensemble_abs_score == 0.15
    assert s.ai_news_model == "lexicon"
    assert s.ai_execution_scenario == "baseline"
    assert s.ai_ledger_path == "data/ai/decisions.jsonl"


def test_ai_enabled_agent_list_parses() -> None:
    s = _settings()
    agents = s.ai_enabled_agent_list
    assert agents == [
        "technical",
        "news",
        "fundamental",
        "pattern",
        "prediction",
        "regime",
    ]


def test_ai_weights_config_builds_versioned_config() -> None:
    s = _settings()
    config = s.ai_weights_config
    assert isinstance(config, AgentWeightsConfig)
    assert config.version == "1.0"
    assert config.weight("technical") == 1.0
    assert config.weight("news") == 0.8
    assert "technical" in config.enabled


def test_ai_weights_config_respects_overrides() -> None:
    s = _settings(
        ai_weights_version="2.0",
        ai_agent_weights="technical:0.5,news:0.1",
        ai_enabled_agents="technical,news",
    )
    config = s.ai_weights_config
    assert config.version == "2.0"
    assert config.weight("technical") == 0.5
    assert config.effective_agents() == ("technical", "news")


def test_ai_decision_config() -> None:
    s = _settings(ai_position_size_pct=0.05, ai_leverage=2.0)
    config = s.ai_decision_config
    assert isinstance(config, DecisionConfig)
    assert config.position_size_pct == 0.05
    assert config.leverage == 2.0
    assert config.min_ensemble_abs_score == 0.15


def test_ai_failure_config() -> None:
    s = _settings(ai_failure_drift_max_abs=0.7)
    config = s.ai_failure_config
    assert isinstance(config, FailureConfig)
    assert config.drift_max_abs == 0.7
    assert config.news_staleness_hours == 48.0


def test_ai_selector_config() -> None:
    s = _settings()
    config = s.ai_selector_config
    assert isinstance(config, SelectorConfig)
    assert config.oos_sharpe == 1.0
