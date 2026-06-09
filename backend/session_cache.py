"""
session_cache.py
================
Thread-safe, bounded LRU cache replacing the unbounded SQL_PLAN_CACHE dict
in server.py.

Why this matters
----------------
The original SQL_PLAN_CACHE grows without bound for the lifetime of the
process.  In a small local environment that's fine; in a production deployment that
processes thousands of QVF files it will eventually exhaust memory.

This module provides a drop-in replacement with:
  - O(1) get / set via collections.OrderedDict
  - Configurable max entries (default 256)
  - Optional per-entry TTL (default 1 hour)
  - Thread-safe reads and writes
  - Prometheus-style hit/miss counters for observability

Usage
-----
    from session_cache import SessionPlanCache

    cache = SessionPlanCache(max_size=128, ttl_seconds=3600)

    cached = cache.get(key)           # None on miss
    cache.set(key, value)
    cache.delete(key)
    cache.clear()

    stats = cache.stats()             # {'hits': N, 'misses': N, 'size': N, ...}
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Optional


class SessionPlanCache:
    """Bounded LRU cache with optional per-entry TTL."""

    def __init__(self, max_size: int = 256, ttl_seconds: Optional[float] = 3600.0):
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._store: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0

    # ── Public API ────────────────────────────────────────────────────────────

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            if key not in self._store:
                self._misses += 1
                return None

            value, ts = self._store[key]

            # TTL check
            if self._ttl is not None and (time.monotonic() - ts) > self._ttl:
                del self._store[key]
                self._misses += 1
                return None

            # Move to end (most recently used)
            self._store.move_to_end(key)
            self._hits += 1
            return value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = (value, time.monotonic())

            # Evict LRU entries when over capacity
            while len(self._store) > self._max_size:
                evicted_key, _ = self._store.popitem(last=False)

    def delete(self, key: str) -> bool:
        with self._lock:
            if key in self._store:
                del self._store[key]
                return True
            return False

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self._hits = 0
            self._misses = 0

    def stats(self) -> dict:
        with self._lock:
            total = self._hits + self._misses
            hit_rate = self._hits / total if total else 0.0
            return {
                'hits': self._hits,
                'misses': self._misses,
                'hit_rate': round(hit_rate, 4),
                'size': len(self._store),
                'max_size': self._max_size,
                'ttl_seconds': self._ttl,
            }

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)

    def __contains__(self, key: str) -> bool:
        return self.get(key) is not None
