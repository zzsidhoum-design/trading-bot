"""LLM adapters (docs/02-agents.md §4) — provider-agnostic ``LLMClient``.

Production: :class:`OpenAILLMClient` talks to the OpenAI chat-completions API via
httpx and validates the JSON payload with Pydantic.

Fallback: :class:`KeywordLLMClient` is a deterministic, offline stand-in used
when no API key is configured — it keeps the pipeline runnable and testable.
"""

from __future__ import annotations

import json
from typing import TypeVar

import httpx
from pydantic import TypeAdapter, ValidationError

from qtrader.domain.ports import LLMClient

T = TypeVar("T")

OPENAI_COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"

POSITIVE = (
    "beat", "raise", "grow", "profit", "record", "surge", "boost", "upgrade", "gain", "win",
)
NEGATIVE = (
    "miss", "cut", "fall", "drop", "loss", "lawsuit", "probe",
    "downgrade", "layoff", "down", "recall",
)


class OpenAILLMClient(LLMClient):
    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-mini",
        base_url: str = OPENAI_COMPLETIONS_URL,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url

    async def complete_json(self, system_prompt: str, user_prompt: str, schema: type[T]) -> T:
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(self._base_url, json=body, headers=headers)
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        adapter = TypeAdapter(schema)
        try:
            return adapter.validate_json(content)
        except ValidationError as exc:
            raise ValueError(f"LLM output failed schema validation: {exc}") from exc


class KeywordLLMClient(LLMClient):
    """Deterministic offline stand-in: keyword lexicon over the user prompt."""

    async def complete_json(self, system_prompt: str, user_prompt: str, schema: type[T]) -> T:
        text = user_prompt.lower()
        positive = sum(1 for word in POSITIVE if word in text)
        negative = sum(1 for word in NEGATIVE if word in text)
        score = max(-1.0, min(1.0, (positive - negative) / max(1, positive + negative)))
        payload: dict[str, object] = {
            "sentiment_score": score,
            "summary": user_prompt.strip()[:200],
            "expected_market_impact": (
                "HIGH"
                if abs(score) >= 0.8
                else ("MEDIUM" if abs(score) >= 0.4 else "LOW")
            ),
            "impact_direction": 1 if score > 0 else (-1 if score < 0 else 0),
            "relevant_symbols": [],
            "categories": [],
            "confidence": min(0.5 + abs(score) / 2, 0.95),
        }
        adapter = TypeAdapter(schema)
        try:
            return adapter.validate_python(json.loads(json.dumps(payload)))
        except ValidationError as exc:
            raise ValueError(f"keyword analysis failed validation: {exc}") from exc
