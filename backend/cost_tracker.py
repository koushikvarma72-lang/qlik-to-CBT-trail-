"""
cost_tracker.py
===============
Token usage and estimated cost tracking per session and per model.

Architectural role
------------------
Every call through the configured AI provider should record:
  - prompt tokens (estimated from character count if the API doesn't return them)
  - completion tokens
  - model name
  - session_id
  - endpoint / purpose label (e.g. 'migration', 'repair', 'chat', 'dbt_agent')

This feeds two things:
  1. A /api/cost/<session_id> endpoint so users can see what each session cost.
  2. A server-wide /api/cost/summary for operators to monitor spend.

Estimation strategy
-------------------
OpenRouter returns usage in most responses but not all.  When missing we fall
back to character-count heuristics (≈4 chars per token for English/SQL).

Pricing table
-------------
Kept as a simple dict so it can be updated without touching business logic.
Prices are USD per 1 000 tokens (prompt / completion) as of 2026-Q2.
Add / update entries when models change.

Usage
-----
    from backend.cost_tracker import CostTracker

    tracker = CostTracker()

    # record after each AI call
    tracker.record(
        session_id='abc',
        model='openai/gpt-4o-mini',
        purpose='migration',
        prompt_text=prompt,
        completion_text=response,
        usage_from_api=None,   # pass api_response['usage'] if available
    )

    # query
    session_cost = tracker.session_summary('abc')
    total_cost   = tracker.global_summary()
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ─── Pricing table (USD / 1K tokens, prompt / completion) ────────────────────
# Source: openrouter.ai/models — update regularly.

PRICING: Dict[str, tuple[float, float]] = {
    'openai/gpt-4o':            (0.005,  0.015),
    'openai/gpt-4o-mini':       (0.00015, 0.0006),
    'openai/gpt-4-turbo':       (0.01,   0.03),
    'openai/o3-mini':           (0.0011, 0.0044),
    'anthropic/claude-3-opus':  (0.015,  0.075),
    'anthropic/claude-3-sonnet':(0.003,  0.015),
    'anthropic/claude-3-haiku': (0.00025, 0.00125),
    'google/gemini-pro-1.5':    (0.00125, 0.005),
    'meta-llama/llama-3-70b':   (0.00059, 0.00079),
    # Fallback — assume mid-tier pricing
    '__default__':              (0.002,  0.006),
}

_CHARS_PER_TOKEN = 4.0  # Rough heuristic for English + SQL


# ─── Data types ───────────────────────────────────────────────────────────────

@dataclass
class UsageRecord:
    session_id: str
    model: str
    purpose: str
    prompt_tokens: int
    completion_tokens: int
    estimated_cost_usd: float
    timestamp: float = field(default_factory=time.time)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> dict:
        return {
            'sessionId': self.session_id,
            'model': self.model,
            'purpose': self.purpose,
            'promptTokens': self.prompt_tokens,
            'completionTokens': self.completion_tokens,
            'totalTokens': self.total_tokens,
            'estimatedCostUsd': round(self.estimated_cost_usd, 6),
            'timestamp': self.timestamp,
        }


# ─── Core tracker ─────────────────────────────────────────────────────────────

class CostTracker:
    """Thread-safe, in-memory cost tracker.

    For production use, persist to the SQLite DB by injecting a
    ``on_record`` callback (see __init__).
    """

    def __init__(self, on_record=None):
        """
        Args:
            on_record: Optional callable(UsageRecord) called after each
                       record() call.  Use it to write to SQLite or an
                       external metrics sink.
        """
        self._lock = threading.Lock()
        self._records: List[UsageRecord] = []
        self._by_session: Dict[str, List[UsageRecord]] = defaultdict(list)
        self._on_record = on_record

    # ── Public API ────────────────────────────────────────────────────────────

    def record(
        self,
        session_id: str,
        model: str,
        purpose: str,
        prompt_text: str = '',
        completion_text: str = '',
        usage_from_api: Optional[dict] = None,
    ) -> UsageRecord:
        """Record a single AI call.

        Args:
            session_id: Active session.
            model:      Model string as sent to OpenRouter.
            purpose:    Human-readable label ('migration', 'repair', 'chat', 'dbt_agent').
            prompt_text:     Full prompt string (used for token estimation).
            completion_text: AI response string (used for token estimation).
            usage_from_api:  Dict with keys 'prompt_tokens', 'completion_tokens'
                             if the API returned them; otherwise None.
        """
        if usage_from_api:
            prompt_tokens = int(usage_from_api.get('prompt_tokens', 0))
            completion_tokens = int(usage_from_api.get('completion_tokens', 0))
        else:
            prompt_tokens = max(1, int(len(prompt_text) / _CHARS_PER_TOKEN))
            completion_tokens = max(1, int(len(completion_text) / _CHARS_PER_TOKEN))

        price_in, price_out = PRICING.get(model, PRICING['__default__'])
        cost = (prompt_tokens * price_in + completion_tokens * price_out) / 1000.0

        rec = UsageRecord(
            session_id=session_id,
            model=model,
            purpose=purpose,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            estimated_cost_usd=cost,
        )

        with self._lock:
            self._records.append(rec)
            self._by_session[session_id].append(rec)

        if self._on_record:
            try:
                self._on_record(rec)
            except Exception:
                pass  # Never let the callback break the main flow

        return rec

    def session_summary(self, session_id: str) -> dict:
        with self._lock:
            recs = list(self._by_session.get(session_id, []))

        total_prompt = sum(r.prompt_tokens for r in recs)
        total_completion = sum(r.completion_tokens for r in recs)
        total_cost = sum(r.estimated_cost_usd for r in recs)

        by_purpose: Dict[str, dict] = {}
        for r in recs:
            entry = by_purpose.setdefault(r.purpose, {
                'calls': 0, 'promptTokens': 0, 'completionTokens': 0, 'costUsd': 0.0
            })
            entry['calls'] += 1
            entry['promptTokens'] += r.prompt_tokens
            entry['completionTokens'] += r.completion_tokens
            entry['costUsd'] += r.estimated_cost_usd

        for v in by_purpose.values():
            v['costUsd'] = round(v['costUsd'], 6)

        return {
            'sessionId': session_id,
            'totalCalls': len(recs),
            'totalPromptTokens': total_prompt,
            'totalCompletionTokens': total_completion,
            'totalTokens': total_prompt + total_completion,
            'estimatedCostUsd': round(total_cost, 6),
            'byPurpose': by_purpose,
            'records': [r.to_dict() for r in recs],
        }

    def global_summary(self) -> dict:
        with self._lock:
            recs = list(self._records)

        total_cost = sum(r.estimated_cost_usd for r in recs)
        by_model: Dict[str, dict] = {}
        for r in recs:
            entry = by_model.setdefault(r.model, {
                'calls': 0, 'tokens': 0, 'costUsd': 0.0
            })
            entry['calls'] += 1
            entry['tokens'] += r.total_tokens
            entry['costUsd'] += r.estimated_cost_usd

        for v in by_model.values():
            v['costUsd'] = round(v['costUsd'], 6)

        return {
            'totalCalls': len(recs),
            'totalTokens': sum(r.total_tokens for r in recs),
            'estimatedCostUsd': round(total_cost, 6),
            'byModel': by_model,
            'uniqueSessions': len(self._by_session),
        }

    def clear_session(self, session_id: str) -> None:
        with self._lock:
            removed = self._by_session.pop(session_id, [])
            removed_set = set(id(r) for r in removed)
            self._records = [r for r in self._records if id(r) not in removed_set]
