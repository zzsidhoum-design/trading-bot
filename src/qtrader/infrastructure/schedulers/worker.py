"""arq worker entrypoint.

Run with: ``arq qtrader.infrastructure.schedulers.worker.WorkerSettings``
"""

from __future__ import annotations

from qtrader.infrastructure.schedulers.tasks import WorkerSettings

__all__ = ["WorkerSettings"]
