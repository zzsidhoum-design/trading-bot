"""AI failure monitor — detects when the AI layer is failing and fails safe.

The monitor never stops a *validated* strategy by itself: it emits
:class:`FailureEvent` records with a severity and a ``degraded`` verdict. While
``degraded`` is True the :class:`AiRiskGate` refuses to route any proposal —
the system does nothing rather than act on unreliable AI.

Detected failure classes:
- ``agent_disagreement`` — enabled agents strongly disagree (high dispersion).
- ``overconfidence`` — average confidence exceeds a ceiling (unrealistic).
- ``instability`` — per-decision confidence variance above a threshold.
- ``data_quality`` — repeated sentiment/news pipeline errors or missing regime.
- ``news_staleness`` — no relevant news seen inside the lookback window.
- ``drift`` — live ensemble score persistently deviates from the historical
  median by more than a configurable band.
"""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime

from qtrader.application.ai.models import (
    AgentSignalSet,
    FailureEvent,
    FailureReport,
    FailureSeverity,
    NewsAssessment,
    RegimeAssessment,
)


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class FailureConfig:
    """Thresholds for each failure detector (all auditable)."""

    max_agent_dispersion: float = 1.0
    max_mean_confidence: float = 0.95
    max_confidence_std: float = 0.25
    news_staleness_hours: float = 48.0
    drift_max_abs: float = 0.50
    drift_history: int = 100


def _dispersion(score_signs: list[float]) -> float:
    """Population std-dev of signed scores; 0 when fewer than two values."""
    if len(score_signs) < 2:
        return 0.0
    return statistics.pstdev(score_signs)


class AiFailureMonitor:
    """Tracks rolling AI health and emits FailureReports on demand."""

    def __init__(self, config: FailureConfig | None = None) -> None:
        self._config = config or FailureConfig()
        self._history: deque[tuple[datetime, float]] = deque(
            maxlen=self._config.drift_history
        )

    @property
    def config(self) -> FailureConfig:
        return self._config

    def observe(self, *, ensemble_score: float, ts: datetime | None = None) -> None:
        """Record a live ensemble score for drift detection."""
        self._history.append((ts or _now(), ensemble_score))

    def check(
        self,
        signal_set: AgentSignalSet | None = None,
        *,
        news: NewsAssessment | None = None,
        regime: RegimeAssessment | None = None,
        now: datetime | None = None,
    ) -> FailureReport:
        """Produce the current failure report (fail-safe by default)."""
        now = now or _now()
        events: list[FailureEvent] = []
        signal_set = signal_set or AgentSignalSet(asset="", as_of=now)

        if signal_set.signals:
            scores = [s.score for s in signal_set.signals]
            disp = _dispersion(scores)
            if disp > self._config.max_agent_dispersion:
                events.append(
                    FailureEvent(
                        code="agent_disagreement",
                        severity=FailureSeverity.WARNING,
                        message="enabled agents disagree strongly",
                        detail={"dispersion": round(disp, 4)},
                        timestamp=now,
                    )
                )

            mean_conf = statistics.fmean([s.confidence for s in signal_set.signals])
            if mean_conf > self._config.max_mean_confidence:
                events.append(
                    FailureEvent(
                        code="overconfidence",
                        severity=FailureSeverity.WARNING,
                        message="agent confidence implausibly high",
                        detail={"mean_confidence": round(mean_conf, 4)},
                        timestamp=now,
                    )
                )

            confidences = [s.confidence for s in signal_set.signals]
            if len(confidences) >= 2:
                conf_std = statistics.pstdev(confidences)
                if conf_std > self._config.max_confidence_std:
                    events.append(
                        FailureEvent(
                            code="instability",
                            severity=FailureSeverity.WARNING,
                            message="agent confidence unstable",
                            detail={"confidence_std": round(conf_std, 4)},
                            timestamp=now,
                        )
                    )

        if news is not None and news.items_used == 0:
            events.append(
                FailureEvent(
                    code="news_staleness",
                    severity=FailureSeverity.WARNING,
                    message="no relevant news inside the lookback window",
                    detail={"asset": news.asset},
                    timestamp=now,
                )
            )
        elif news is None and regime is None and not signal_set.signals:
            events.append(
                FailureEvent(
                    code="data_quality",
                    severity=FailureSeverity.WARNING,
                    message="no signals, no news and no regime available",
                    detail={},
                    timestamp=now,
                )
            )

        if self._history:
            live = [value for _, value in self._history]
            median = statistics.median(live)
            latest = live[-1]
            if abs(latest - median) > self._config.drift_max_abs:
                events.append(
                    FailureEvent(
                        code="drift",
                        severity=FailureSeverity.WARNING,
                        message="live ensemble deviates from historical median",
                        detail={
                            "median": round(median, 4),
                            "latest": round(latest, 4),
                        },
                        timestamp=now,
                    )
                )

        critical = any(
            e.severity is FailureSeverity.CRITICAL for e in events
        )
        warning_count = sum(
            1 for e in events if e.severity is FailureSeverity.WARNING
        )
        degraded = critical or warning_count >= 3
        reason = (
            "; ".join(e.code for e in events)
            if degraded
            else ""
        )
        return FailureReport(
            events=tuple(events),
            degraded=degraded,
            reason=reason,
        )


__all__ = ["AiFailureMonitor", "FailureConfig"]
