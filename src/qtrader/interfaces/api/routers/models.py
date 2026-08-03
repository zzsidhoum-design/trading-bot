"""Model registry router — list, trigger training, promote validated models."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from qtrader.application.services.dashboard_service import DashboardService
from qtrader.application.services.model_trainer import ModelTrainer
from qtrader.config.settings import Settings
from qtrader.domain.exceptions import NotFoundError, ValidationError
from qtrader.domain.ports import ModelRepository
from qtrader.interfaces.api.dependencies import (
    get_container,
    get_dashboard_service,
    get_model_repository,
    get_settings,
    require_api_key,
)
from qtrader.interfaces.api.schemas import RegisteredModelOut

router = APIRouter(prefix="/api/v1/models", tags=["models"])


@router.get(
    "",
    response_model=list[RegisteredModelOut],
    dependencies=[Depends(require_api_key)],
)
async def list_models(
    dashboard: DashboardService = Depends(get_dashboard_service),
) -> list[RegisteredModelOut]:
    models = await dashboard.models()
    return [
        RegisteredModelOut(
            model_id=m.model_id,
            name=m.name,
            version=m.version,
            hyperparams=m.hyperparams or {},
            offline_metrics=m.offline_metrics or {},
            is_active=m.is_active,
            status=m.status,
            trained_at=m.trained_at,
            training_window=m.training_window,
        )
        for m in models
    ]


@router.post(
    "/train",
    dependencies=[Depends(require_api_key)],
)
async def train_models(
    settings: Settings = Depends(get_settings),
    container: Any = Depends(get_container),
) -> dict:
    trainer = container.resolve(ModelTrainer)
    result = await trainer.train(
        symbols=settings.watchlist_symbols,
        interval=settings.scan_interval,
    )
    if result is None:
        raise ValidationError("insufficient samples to train a model")
    return {
        "name": result.name,
        "version": result.version,
        "metrics": result.metrics,
        "promoted": result.promoted,
    }


@router.post(
    "/{model_id}/promote",
    dependencies=[Depends(require_api_key)],
)
async def promote_model(
    model_id: int,
    repo: ModelRepository = Depends(get_model_repository),
    dashboard: DashboardService = Depends(get_dashboard_service),
) -> dict:
    model = next((m for m in await dashboard.models() if m.model_id == model_id), None)
    if model is None:
        raise NotFoundError("model not found")
    await repo.promote(model.name, model.version)
    return {"name": model.name, "version": model.version, "promoted": True}
