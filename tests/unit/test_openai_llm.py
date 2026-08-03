"""Unit tests for the OpenAI LLM client (JSON validation + rate limiting)."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from qtrader.infrastructure.llm.adapters import OpenAILLMClient


class _Sentiment(BaseModel):
    sentiment: str
    score: float


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self._content = content

    def json(self) -> dict:
        return {"choices": [{"message": {"content": self._content}}]}


class _SpyBucket:
    def __init__(self) -> None:
        self.waits = 0

    async def wait(self, tokens: float = 1.0) -> None:
        self.waits += 1


def _client(bucket) -> OpenAILLMClient:
    client = OpenAILLMClient(api_key="test-key", rate_limiter=bucket)

    async def fake_post(client, body, headers):  # noqa: ARG001
        return _FakeResponse(json.dumps({"sentiment": "positive", "score": 0.8}))

    client._post = fake_post  # type: ignore[method-assign]
    return client


async def test_complete_json_validates_and_returns_model() -> None:
    bucket = _SpyBucket()
    client = _client(bucket)
    result = await client.complete_json("sys", "user", _Sentiment)
    assert result == _Sentiment(sentiment="positive", score=0.8)
    assert bucket.waits == 1


async def test_default_rate_limiter_is_active() -> None:
    client = OpenAILLMClient(api_key="test-key")
    assert hasattr(client, "_rate_limiter")
    assert client._rate_limiter.available <= 30


async def test_invalid_json_payload_raises_value_error() -> None:
    client = OpenAILLMClient(api_key="test-key", rate_limiter=_SpyBucket())

    async def fake_post(client, body, headers):  # noqa: ARG001
        return _FakeResponse(json.dumps({"sentiment": "nope"}))

    client._post = fake_post  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="failed schema validation"):
        await client.complete_json("sys", "user", _Sentiment)
