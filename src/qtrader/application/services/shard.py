"""Deterministic symbol sharding for horizontally-scaled workers.

The worker pool is divided into ``worker_shards`` logical partitions; each
worker owns symbols whose hash lands on its ``worker_shard_id``. Symbols are
assigned by an MD5 digest so assignments are stable across restarts and
rebalanced simply by changing ``worker_shards``.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache


def shard_for(key: str, num_shards: int) -> int:
    """Stable shard index in ``[0, num_shards)`` for ``key``."""
    if num_shards < 1:
        raise ValueError("num_shards must be >= 1")
    digest = hashlib.md5(key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % num_shards


@lru_cache(maxsize=4096)
def shard_for_cached(key: str, num_shards: int) -> int:
    """Memoized :func:`shard_for` for hot paths (per-symbol scans)."""
    return shard_for(key, num_shards)


def owned_symbols(symbols: list[str], shard_id: int, num_shards: int) -> list[str]:
    """Subset of ``symbols`` assigned to worker shard ``shard_id``.

    Returns the input unchanged when ``num_shards <= 1`` (no sharding).
    """
    if num_shards <= 1:
        return list(symbols)
    return [s for s in symbols if shard_for(s, num_shards) == shard_id]
