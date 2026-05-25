"""
feedback.py
===========
User feedback collection and prompt self-improvement for the QVF Decoder.

Architectural role
------------------
After the AI generates SQL + description, the frontend can POST a thumbs-up /
thumbs-down signal plus an optional free-text comment.  This module:

  1. Persists each feedback signal to SQLite (via the injected get_db()).
  2. On thumbs-down, optionally triggers a lightweight "reflection" pass:
     the model is asked why its output was likely wrong and what it should
     have done differently.  The reflection is stored alongside the feedback
     and surfaced in the UI as a tooltip / badge.
  3. Exposes a /api/feedback/summary endpoint so the team can audit quality
     over time without building a separate BI pipeline.

DB table: migration_feedback
-----------------------------
id TEXT PK, session_id TEXT, file_id TEXT, history_id TEXT,
rating INTEGER (1=positive, -1=negative), comment TEXT,
sql_snapshot TEXT, reflection TEXT,
created_at TEXT, resolved_at TEXT

Flask routes (registered via register_feedback_routes)
------------------------------------------------------
POST /api/feedback              — submit a rating
GET  /api/feedback/<session_id> — list feedback for a session
GET  /api/feedback/summary      — aggregate stats across all sessions

Usage
-----
    from feedback import register_feedback_routes, ensure_feedback_table
    ensure_feedback_table(get_db)
    register_feedback_routes(app, get_db, call_ai=call_openrouter)
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Callable, Optional

from flask import Flask, jsonify, request

logger = logging.getLogger(__name__)

# ─── DB helpers ───────────────────────────────────────────────────────────────

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS migration_feedback (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    file_id     TEXT,
    history_id  TEXT,
    rating      INTEGER NOT NULL,   -- 1 = positive, -1 = negative
    comment     TEXT,
    sql_snapshot TEXT,
    reflection  TEXT,
    created_at  TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_feedback_session ON migration_feedback (session_id);
CREATE INDEX IF NOT EXISTS idx_feedback_rating  ON migration_feedback (rating);
"""


def ensure_feedback_table(get_db: Callable) -> None:
    """Create the feedback table if it doesn't exist. Call once at startup."""
    db = get_db()
    try:
        for stmt in _CREATE_TABLE_SQL.strip().split(';'):
            stmt = stmt.strip()
            if stmt:
                db.execute(stmt)
        db.commit()
    finally:
        db.close()


# ─── Reflection prompt ────────────────────────────────────────────────────────

_REFLECTION_SYSTEM = (
    "You are a QVF-to-dbt migration quality auditor. "
    "When a user rates a migration negatively, analyse the SQL and provide a concise, "
    "actionable explanation of what likely went wrong and how to fix it. "
    "Focus on dbt conventions, Qlik idiom translation errors, and missing business logic. "
    "Respond in 3-5 bullet points. Be specific — name the exact CTE or column that is wrong if you can tell."
)


def _build_reflection_prompt(sql_snapshot: str, comment: str) -> str:
    return (
        f"The following dbt SQL was rated negatively by the user.\n\n"
        f"User comment: {comment or '(no comment)'}\n\n"
        f"Generated SQL:\n```sql\n{sql_snapshot[:6000]}\n```\n\n"
        "What went wrong and how should it be fixed?"
    )


def _generate_reflection(
    sql_snapshot: str,
    comment: str,
    call_ai: Callable,
) -> Optional[str]:
    """Call the AI to produce a self-reflection on a negative rating."""
    prompt = _build_reflection_prompt(sql_snapshot, comment)
    try:
        return call_ai(
            prompt,
            system_prompt=_REFLECTION_SYSTEM,
            temperature=0,
            max_tokens=512,
        )
    except Exception as exc:
        logger.warning("Reflection generation failed: %s", exc)
        return None


# ─── Route registration ───────────────────────────────────────────────────────

def register_feedback_routes(
    app: Flask,
    get_db: Callable,
    call_ai: Optional[Callable] = None,
) -> None:
    """Register all feedback-related Flask routes on the given app."""

    @app.route('/api/feedback', methods=['POST'])
    def submit_feedback():
        """
        Submit a rating for a migration result.

        Body fields:
          sessionId  TEXT  required
          fileId     TEXT  optional
          historyId  TEXT  optional — links to regeneration_history.id
          rating     INT   required  1 = positive, -1 = negative
          comment    TEXT  optional
          sqlSnapshot TEXT optional — the SQL the user was looking at
        """
        data = request.get_json() or {}
        session_id = (data.get('sessionId') or '').strip()
        if not session_id:
            return jsonify({'error': 'sessionId is required'}), 400

        try:
            rating = int(data.get('rating', 0))
        except (ValueError, TypeError):
            return jsonify({'error': 'rating must be 1 (positive) or -1 (negative)'}), 400

        if rating not in (1, -1):
            return jsonify({'error': 'rating must be 1 or -1'}), 400

        fb_id = str(uuid.uuid4())
        comment = (data.get('comment') or '').strip()[:2000]
        sql_snapshot = (data.get('sqlSnapshot') or '')[:8000]
        file_id = data.get('fileId') or ''
        history_id = data.get('historyId') or ''
        now = datetime.utcnow().isoformat()

        reflection = None
        if rating == -1 and call_ai and sql_snapshot:
            logger.info("Triggering reflection for negative feedback %s", fb_id)
            reflection = _generate_reflection(sql_snapshot, comment, call_ai)

        db = get_db()
        try:
            db.execute(
                """INSERT INTO migration_feedback
                   (id, session_id, file_id, history_id, rating, comment,
                    sql_snapshot, reflection, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (fb_id, session_id, file_id, history_id, rating,
                 comment, sql_snapshot, reflection, now),
            )
            db.commit()
        finally:
            db.close()

        return jsonify({
            'success': True,
            'feedbackId': fb_id,
            'reflection': reflection,
        }), 201

    @app.route('/api/feedback/<session_id>', methods=['GET'])
    def get_session_feedback(session_id):
        db = get_db()
        try:
            rows = db.execute(
                'SELECT * FROM migration_feedback WHERE session_id = ? ORDER BY created_at DESC',
                (session_id,),
            ).fetchall()
        finally:
            db.close()

        return jsonify({
            'sessionId': session_id,
            'feedback': [_row_to_dict(r) for r in rows],
        })

    @app.route('/api/feedback/summary', methods=['GET'])
    def feedback_summary():
        db = get_db()
        try:
            total = db.execute('SELECT COUNT(*) FROM migration_feedback').fetchone()[0]
            positive = db.execute(
                'SELECT COUNT(*) FROM migration_feedback WHERE rating = 1'
            ).fetchone()[0]
            negative = db.execute(
                'SELECT COUNT(*) FROM migration_feedback WHERE rating = -1'
            ).fetchone()[0]
            recent = db.execute(
                'SELECT * FROM migration_feedback ORDER BY created_at DESC LIMIT 20'
            ).fetchall()
        finally:
            db.close()

        return jsonify({
            'total': total,
            'positive': positive,
            'negative': negative,
            'positiveRate': round(positive / total, 4) if total else 0.0,
            'recentFeedback': [_row_to_dict(r) for r in recent],
        })

    @app.route('/api/feedback/<feedback_id>/resolve', methods=['POST'])
    def resolve_feedback(feedback_id):
        db = get_db()
        try:
            db.execute(
                'UPDATE migration_feedback SET resolved_at = ? WHERE id = ?',
                (datetime.utcnow().isoformat(), feedback_id),
            )
            db.commit()
        finally:
            db.close()
        return jsonify({'success': True})


# ─── Serialisation ────────────────────────────────────────────────────────────

def _row_to_dict(row) -> dict:
    return {
        'id': row['id'],
        'sessionId': row['session_id'],
        'fileId': row['file_id'],
        'historyId': row['history_id'],
        'rating': row['rating'],
        'comment': row['comment'],
        'reflection': row['reflection'],
        'createdAt': row['created_at'],
        'resolvedAt': row['resolved_at'],
    }
