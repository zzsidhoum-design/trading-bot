"""Unit tests for deterministic symbol sharding (Phase 8)."""

from __future__ import annotations

from qtrader.application.services.shard import owned_symbols, shard_for, shard_for_cached


def test_shard_for_in_range() -> None:
    symbols = ["AAPL", "MSFT", "TSLA", "NVDA", "AMZN", "GOOG"]
    for symbol in symbols:
        for n in (1, 2, 3, 4, 8, 16):
            assert 0 <= shard_for(symbol, n) < n


def test_shard_for_deterministic() -> None:
    assert shard_for("AAPL", 4) == shard_for("AAPL", 4)
    assert shard_for("MSFT", 8) == shard_for("MSFT", 8)
    assert shard_for_cached("TSLA", 8) == shard_for("TSLA", 8)


def test_single_shard_assigns_everything_to_zero() -> None:
    for symbol in ("AAPL", "MSFT", "TSLA"):
        assert shard_for(symbol, 1) == 0


def test_balanced_distribution() -> None:
    universe = [f"SYM{i:04d}" for i in range(200)]
    buckets: dict[int, int] = {}
    for symbol in universe:
        shard = shard_for(symbol, 4)
        buckets[shard] = buckets.get(shard, 0) + 1
    assert len(buckets) == 4
    counts = list(buckets.values())
    assert max(counts) - min(counts) <= max(1, len(universe) // 10)


def test_shards_partition_universe() -> None:
    universe = [f"SYM{i:04d}" for i in range(200)]
    for shard_id in range(4):
        owned = owned_symbols(universe, shard_id, 4)
        assert all(shard_for(s, 4) == shard_id for s in owned)
    total = sum(len(owned_symbols(universe, i, 4)) for i in range(4))
    assert total == len(universe)


def test_no_sharding_returns_all() -> None:
    universe = ["AAPL", "MSFT", "TSLA"]
    assert owned_symbols(universe, 0, 1) == universe


def test_shard_for_rejects_bad_shard_count() -> None:
    import pytest

    with pytest.raises(ValueError):
        shard_for("AAPL", 0)
