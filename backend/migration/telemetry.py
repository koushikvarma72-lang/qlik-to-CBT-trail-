"""
backend/migration/telemetry.py
==============================
Lightweight telemetry for Qlik→dbt migration jobs.

Tracks per-job metrics (timing, translation counts, confidence) and surfaces
actionable alerts when quality thresholds are breached.  No external
dependencies — uses only the standard library.

Usage
-----
    from backend.migration.telemetry import MigrationTelemetry

    tel = MigrationTelemetry(job_id="abc123", qlik_app_name="SalesApp")
    tel.start_phase("extraction")
    # ... do extraction ...
    tel.end_phase("extraction")

    tel.record_translation(method="rule_based")   # or "llm_fallback" / "failed"
    tel.set_confidence(0.85)
    tel.finalize()

    summary = tel.to_dict()
    alerts  = tel.alerts()
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List


# ─── Thresholds ───────────────────────────────────────────────────────────────

# If more than this fraction of field translations fell back to the LLM,
# the transform rule registry is missing coverage — flag for review.
LLM_FALLBACK_RATE_THRESHOLD = 0.20

# If the overall confidence score is below this, flag for manual review.
CONFIDENCE_THRESHOLD = 0.70

# If any single phase takes longer than this (seconds), flag as slow.
SLOW_PHASE_THRESHOLD_S = 30.0


# ─── Data types ───────────────────────────────────────────────────────────────

@dataclass
class PhaseTimer:
    name: str
    start_time: float = 0.0
    end_time: float = 0.0
    duration_ms: float = 0.0
    completed: bool = False

    def start(self) -> None:
        self.start_time = time.monotonic()

    def stop(self) -> None:
        self.end_time = time.monotonic()
        self.duration_ms = (self.end_time - self.start_time) * 1000
        self.completed = True

    def to_dict(self) -> dict:
        return {
            "phase": self.name,
            "duration_ms": round(self.duration_ms, 1),
            "completed": self.completed,
        }


@dataclass
class MigrationTelemetry:
    """Collect and report metrics for a single migration job."""

    job_id: str
    qlik_app_name: str = ""

    # Counters
    total_tables: int = 0
    total_fields: int = 0
    rule_based_translations: int = 0
    llm_fallback_translations: int = 0
    failed_translations: int = 0
    concatenate_unions: int = 0
    drop_fields_applied: int = 0
    cte_count: int = 0
    repair_iterations: int = 0

    # Quality
    confidence_score: float = 0.0
    validation_issues: List[str] = field(default_factory=list)

    # Timing
    _phases: Dict[str, PhaseTimer] = field(default_factory=dict, repr=False)
    _job_start: float = field(default_factory=time.monotonic, repr=False)
    total_duration_ms: float = 0.0
    finalized: bool = False

    # ── Phase tracking ────────────────────────────────────────────────────────

    def start_phase(self, name: str) -> None:
        timer = PhaseTimer(name=name)
        timer.start()
        self._phases[name] = timer

    def end_phase(self, name: str) -> None:
        if name in self._phases:
            self._phases[name].stop()

    def phase_duration_ms(self, name: str) -> float:
        t = self._phases.get(name)
        return t.duration_ms if t and t.completed else 0.0

    # ── Translation recording ─────────────────────────────────────────────────

    def record_translation(self, method: str) -> None:
        """Record one field/expression translation.

        Args:
            method: 'rule_based' | 'llm_fallback' | 'failed'
        """
        if method == 'rule_based':
            self.rule_based_translations += 1
        elif method == 'llm_fallback':
            self.llm_fallback_translations += 1
        elif method == 'failed':
            self.failed_translations += 1
        self.total_fields = (
            self.rule_based_translations
            + self.llm_fallback_translations
            + self.failed_translations
        )

    def set_confidence(self, score: float) -> None:
        self.confidence_score = max(0.0, min(1.0, score))

    def add_validation_issue(self, issue: str) -> None:
        self.validation_issues.append(issue)

    # ── Finalization ──────────────────────────────────────────────────────────

    def finalize(self) -> None:
        """Stop all open phase timers and compute total duration."""
        for timer in self._phases.values():
            if not timer.completed:
                timer.stop()
        self.total_duration_ms = (time.monotonic() - self._job_start) * 1000
        self.finalized = True

    # ── Derived metrics ───────────────────────────────────────────────────────

    @property
    def llm_fallback_rate(self) -> float:
        if self.total_fields == 0:
            return 0.0
        return self.llm_fallback_translations / self.total_fields

    @property
    def failure_rate(self) -> float:
        if self.total_fields == 0:
            return 0.0
        return self.failed_translations / self.total_fields

    # ── Alerts ────────────────────────────────────────────────────────────────

    def alerts(self) -> List[str]:
        """Return a list of human-readable alert strings for threshold breaches."""
        msgs: List[str] = []

        if self.llm_fallback_rate > LLM_FALLBACK_RATE_THRESHOLD:
            msgs.append(
                f"High LLM fallback rate: {self.llm_fallback_rate:.0%} of translations "
                f"({self.llm_fallback_translations}/{self.total_fields}) used the LLM. "
                f"Add more transform rules to reduce cost and latency."
            )

        if self.failed_translations > 0:
            msgs.append(
                f"{self.failed_translations} translation(s) failed outright. "
                f"Review the generated SQL for placeholder or empty expressions."
            )

        if self.confidence_score < CONFIDENCE_THRESHOLD and self.confidence_score > 0:
            msgs.append(
                f"Low migration confidence: {self.confidence_score:.0%}. "
                f"Manual review recommended before deploying to production."
            )

        for name, timer in self._phases.items():
            if timer.completed and timer.duration_ms / 1000 > SLOW_PHASE_THRESHOLD_S:
                msgs.append(
                    f"Slow phase '{name}': {timer.duration_ms / 1000:.1f}s "
                    f"(threshold: {SLOW_PHASE_THRESHOLD_S}s)."
                )

        if self.repair_iterations > 3:
            msgs.append(
                f"High repair iteration count: {self.repair_iterations}. "
                f"The LLM required multiple repair passes — check prompt rules."
            )

        return msgs

    # ── Serialisation ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "jobId": self.job_id,
            "qlikAppName": self.qlik_app_name,
            "totals": {
                "tables": self.total_tables,
                "fields": self.total_fields,
                "ctes": self.cte_count,
                "concatenateUnions": self.concatenate_unions,
                "dropFieldsApplied": self.drop_fields_applied,
            },
            "translations": {
                "ruleBased": self.rule_based_translations,
                "llmFallback": self.llm_fallback_translations,
                "failed": self.failed_translations,
                "llmFallbackRate": round(self.llm_fallback_rate, 4),
            },
            "quality": {
                "confidenceScore": round(self.confidence_score, 4),
                "repairIterations": self.repair_iterations,
                "validationIssues": self.validation_issues,
            },
            "timing": {
                "totalDurationMs": round(self.total_duration_ms, 1),
                "phases": [t.to_dict() for t in self._phases.values()],
            },
            "alerts": self.alerts(),
            "finalized": self.finalized,
        }
