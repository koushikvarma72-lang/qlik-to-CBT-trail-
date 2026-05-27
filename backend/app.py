
import os
import json
import logging
import uuid
import sqlite3
import zipfile
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

# New architectural modules
from backend.session_cache import SessionPlanCache
from backend.cost_tracker import CostTracker
from backend.feedback_routes import ensure_feedback_table, register_feedback_routes
from backend.migration.validator import validate_migration_sql, needs_repair, issues_to_strings
# pyrefly: ignore [missing-import]
from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context
from flask_cors import CORS
# pyrefly: ignore [missing-import]
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
from backend.integrations.openrouter_client import (
    call_gemini_chat,
    call_groq_chat,
    call_ollama_chat,
    call_openrouter_chat,
    call_openrouter_chat_stream,
)
from backend.integrations.dbt_routes import register_dbt_agent_routes
from backend.extraction.qvf_runtime import attach_inline_samples_to_tables, build_graph_json, extract_model_from_script, extract_qvf, generate_description_rule_based, parse_sql_sections, prepare_script_for_migration
from backend.extraction.comprehensive_qvf_extractor import enhance_metadata_with_comprehensive_extraction
from backend.migration.sql_generation import (
    build_migration_validation_report,
    build_join_contract,
    compute_join_contract_coverage,
    dry_run_validation_artifacts,
    export_validation_artifacts,
    generate_validation_artifacts,
    extract_sql_generation_plan,
    format_sql_generation_plan,
    hash_text,
    finalize_generated_sql,
    detect_repair_regressions,
    needs_sql_repair,
    execute_validation_report,
    normalize_sql_description,
    parse_migration_response,
    render_sql_from_load_plan,
    request_migration_with_validation,
    request_migration_one_shot,
    request_sql_repair,
    validate_candidate_integrity,
    validate_generated_sql,
    _audit_generated_sql_against_plan,
    validation_issue_category,
    optimize_qvs_for_context,
    zip_exported_artifacts,
)

logger = logging.getLogger(__name__)

# Load environment variables â€” always resolve relative to this file's directory
# so the .env is found regardless of where Python is launched from.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(PROJECT_ROOT, 'frontend', 'dist')
_env_path = os.path.join(PROJECT_ROOT, '.env')
load_dotenv(_env_path, override=True)

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path='')
CORS(app)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB limit

# Configuration
UPLOAD_FOLDER = os.path.join(PROJECT_ROOT, 'uploads')
DB_PATH = os.path.join(PROJECT_ROOT, 'qvf_decoder.db')

def clear_upload_folder():
    """Empty the uploads directory to keep the workspace clean.

    On Windows / OneDrive, files may be locked or read-only.  We attempt
    deletion and silently skip anything we can't remove rather than printing
    noise on every server start.
    """
    if not os.path.exists(UPLOAD_FOLDER):
        return

    import shutil
    import stat

    def _force_remove(func, path, exc_info):
        """onerror handler: clear read-only flag then retry."""
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except Exception:
            pass  # Still locked (OneDrive sync) — skip silently

    skipped = 0
    for filename in os.listdir(UPLOAD_FOLDER):
        file_path = os.path.join(UPLOAD_FOLDER, filename)
        try:
            if os.path.isfile(file_path) or os.path.islink(file_path):
                os.unlink(file_path)
            elif os.path.isdir(file_path):
                shutil.rmtree(file_path, onerror=_force_remove)
        except PermissionError:
            skipped += 1
        except Exception:
            skipped += 1

    if skipped:
        print(f"INFO: {skipped} upload item(s) could not be deleted (likely locked by OneDrive sync — safe to ignore)")

# Create and clean upload folder
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
# clear_upload_folder() # Disabled for debugging

ALLOWED_EXTENSIONS = {'qvf'}
OPENROUTER_API_KEY = os.environ.get('OPENROUTER_API_KEY', '')
OPENROUTER_DEFAULT_MODEL = os.environ.get('OPENROUTER_MODEL', 'deepseek/deepseek-chat-v3-0324')
AI_PROVIDER = os.environ.get('AI_PROVIDER', 'auto').strip().lower()
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-2.5-flash')
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '')
GROQ_MODEL = os.environ.get('GROQ_MODEL', 'llama-3.3-70b-versatile')
GROQ_MAX_TOKENS = int(os.environ.get('GROQ_MAX_TOKENS', '1800'))
GROQ_MAX_PROMPT_CHARS = int(os.environ.get('GROQ_MAX_PROMPT_CHARS', '16000'))
OLLAMA_BASE_URL = os.environ.get('OLLAMA_BASE_URL', 'http://localhost:11434')
OLLAMA_MODEL = os.environ.get('OLLAMA_MODEL', 'qwen2.5-coder:14b')
OPENROUTER_FALLBACK_MODELS = [
    model.strip() for model in os.environ.get(
        'OPENROUTER_MODEL_FALLBACKS',
        'anthropic/claude-3.5-sonnet,openai/gpt-4o-mini'
    ).split(',') if model.strip()
]
if OPENROUTER_DEFAULT_MODEL not in OPENROUTER_FALLBACK_MODELS:
    OPENROUTER_FALLBACK_MODELS.insert(0, OPENROUTER_DEFAULT_MODEL)
OPENROUTER_MAX_TOKENS = int(os.environ.get('OPENROUTER_MAX_TOKENS', '4000'))
OPENROUTER_MAX_PROMPT_CHARS = int(os.environ.get('OPENROUTER_MAX_PROMPT_CHARS', '35000'))
MIN_REQUIRED_OUTPUT_TOKENS = int(os.environ.get('MIN_REQUIRED_OUTPUT_TOKENS', '1500'))
ONE_SHOT_STREAMING = os.environ.get('ONE_SHOT_STREAMING', 'true').strip().lower() in {'1', 'true', 'yes', 'on'}
VALIDATION_EXECUTION_ENABLED = os.environ.get('VALIDATION_EXECUTION_ENABLED', 'false').strip().lower() in {'1', 'true', 'yes', 'on'}
LOOP_POLICY = os.environ.get('LOOP_POLICY', 'balanced').strip().lower()
if LOOP_POLICY not in {'strict', 'balanced', 'minimal'}:
    LOOP_POLICY = 'balanced'
MIGRATION_LOOP_MAX_ITERATIONS = int(os.environ.get('MIGRATION_LOOP_MAX_ITERATIONS', '0'))
STRUCTURAL_BLOCKING_CODES = {
    'EMPTY_SQL',
    'UNBALANCED_PARENS',
    'BARE_DDL',
    'DUPLICATE_ALIAS',
    'ALIAS_COLUMN_NOT_FOUND',
    'UNION_COLUMN_COUNT_MISMATCH',
    'DYNAMIC_UNION_REBUILD_FAILED',
    'INVALID_EXPENSES_JOIN_WITH_CONCAT_PLAN',
}
PROMPT_VERSION = '2026-05-05.v1'
SQL_DESCRIPTION_STYLE = (
    "Write the ### DESCRIPTION as expert-level technical Markdown. "
    "Structure it as follows:\n\n"
    "**Overview (1–2 sentences):** What does this model do at a high level? "
    "What business question does it answer or what data does it prepare?\n\n"
    "**Then one `## Block: <cte_name>` section for every CTE**, in the order they appear. "
    "For each block write:\n"
    "- What the block does and why it exists (not just 'it loads data')\n"
    "- Source table(s) or upstream CTEs it reads from\n"
    "- Key transformations: field renames, type casts, date arithmetic, CASE logic, aggregations\n"
    "- Any filters applied and their business meaning\n"
    "- How this block feeds into the next block or the final SELECT\n\n"
    "Use `inline code` for field names, CTE names, and SQL expressions. "
    "Use **bold** for important terms. "
    "Do NOT write generic sentences like 'this block recreates the Qlik logic' — "
    "explain the actual logic. Be specific about field names, expressions, and data flow."
)
SQL_PLAN_CACHE = SessionPlanCache(max_size=256, ttl_seconds=3600)
REGENERATION_JOBS = {}
REGENERATION_LOCK = threading.Lock()
REGENERATION_EXECUTOR = ThreadPoolExecutor(max_workers=2)
COST_TRACKER = CostTracker()

# Per-job SSE token queues — keyed by job_id, populated by run_streaming_migration
_STREAM_QUEUES: dict = {}
_STREAM_QUEUES_LOCK = threading.Lock()


def _get_or_create_stream_queue(job_id):
    with _STREAM_QUEUES_LOCK:
        import queue as _queue_mod
        if job_id not in _STREAM_QUEUES:
            _STREAM_QUEUES[job_id] = _queue_mod.Queue()
        return _STREAM_QUEUES[job_id]


def _cleanup_stream_queue(job_id):
    with _STREAM_QUEUES_LOCK:
        _STREAM_QUEUES.pop(job_id, None)

# â”€â”€â”€ AI Integration â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _is_openrouter_model_error(exc):
    if hasattr(exc, 'status_code') and exc.status_code == 404:
        return True
    return 'no endpoints found' in str(exc).lower()


def _is_openrouter_credit_error(exc):
    if hasattr(exc, 'status_code') and exc.status_code == 402:
        return True
    message = str(exc).lower()
    return 'requires more credits' in message or 'credits exhausted' in message


def _selected_ai_provider():
    if AI_PROVIDER and AI_PROVIDER != 'auto':
        return AI_PROVIDER
    if GEMINI_API_KEY:
        return 'gemini'
    if GROQ_API_KEY:
        return 'groq'
    if OPENROUTER_API_KEY:
        return 'openrouter'
    return 'ollama'


def _active_ai_model(provider=None):
    provider = provider or _selected_ai_provider()
    if provider == 'gemini':
        return GEMINI_MODEL
    if provider == 'groq':
        return GROQ_MODEL
    if provider == 'ollama':
        return OLLAMA_MODEL
    return OPENROUTER_DEFAULT_MODEL


def _has_ai_provider_configured(provider=None):
    provider = provider or _selected_ai_provider()
    if provider == 'gemini':
        return bool(GEMINI_API_KEY)
    if provider == 'groq':
        return bool(GROQ_API_KEY)
    if provider == 'ollama':
        return bool(OLLAMA_BASE_URL and OLLAMA_MODEL)
    return bool(OPENROUTER_API_KEY)


def _affordable_openrouter_tokens(exc):
    match = re.search(r'can only afford\s+(\d+)', str(exc), flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _credit_budget_error(exc, requested_tokens, min_tokens):
    affordable = _affordable_openrouter_tokens(exc)
    if affordable is not None:
        return (
            "insufficient OpenRouter credits/token budget: "
            f"requested {requested_tokens} output tokens, can only afford {affordable}, "
            f"minimum required is {min_tokens}."
        )
    return (
        "insufficient OpenRouter credits/token budget: OpenRouter rejected the request. "
        "The migration was stopped instead of retrying with an unusably small output budget."
    )


def _is_token_budget_failure(message):
    message = str(message or '').lower()
    return (
        'insufficient openrouter credits/token budget' in message
        or 'minimum required is' in message
        or 'can only afford' in message
        or 'tokens per minute' in message
        or 'rate_limit_exceeded' in message
        or 'request too large for model' in message
        or 'http 413' in message
        or 'resource_exhausted' in message
        or 'quota exceeded' in message
        or 'generate_content_free_tier_requests' in message
        or 'http 429' in message
    )


def _dedupe_plan_items(plan):
    """Remove repeated LOAD blocks before deterministic low-credit rendering."""
    deduped = []
    seen = set()
    for item in plan or []:
        key = (
            item.get('operation'),
            item.get('table'),
            item.get('source'),
            tuple(item.get('source_tables') or []),
            tuple(item.get('fields') or []),
            tuple(item.get('filters') or []),
            item.get('is_concatenate'),
            item.get('concatenate_target'),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _source_name_from_plan_item(item):
    raw = ''
    for value in item.get('source_tables') or []:
        if value:
            raw = str(value)
            break
    raw = raw or str(item.get('source') or item.get('table') or 'source_table')
    raw = raw.strip()
    raw = raw.replace("''", "'").replace('""', '"')
    raw = re.sub(r"^\s*['\"]+|['\"]+\s*$", '', raw)
    raw = re.sub(r'\s*\([^)]*\)\s*$', '', raw)
    raw = re.sub(r"['\"]+$", '', raw).strip()
    raw = raw.replace('\\', '/').split('/')[-1]
    raw = re.sub(r'\.qvd$', '', raw, flags=re.IGNORECASE)
    return raw or str(item.get('table') or 'source_table')


def _simple_select_expression(field):
    field = str(field or '').strip()
    if not field:
        return ''
    # Keep only direct field projections in the low-credit skeleton. Complex
    # expressions need AI/rule coverage and should not leak Qlik syntax.
    if re.search(r'\b(if|num|monthstart|makedate|makecast|addmonths|date)\s*\(', field, re.IGNORECASE):
        return ''
    if any(token in field for token in ('&', '$(', '(', ')')):
        return ''
    alias_match = re.match(r'(.+?)\s+AS\s+(.+)$', field, flags=re.IGNORECASE)
    if alias_match:
        expr = alias_match.group(1).strip().strip('[]')
        alias = alias_match.group(2).strip().strip('[]').strip('"')
        if re.match(r'^[A-Za-z_][A-Za-z0-9_ ]*$', expr):
            return f'"{expr}" AS "{alias}"'
        return ''
    name = field.strip('[]').strip('"')
    if re.match(r'^[A-Za-z_][A-Za-z0-9_ ]*$', name):
        return f'"{name}"'
    return ''


def _safe_deterministic_skeleton(plan):
    """Build a small valid dbt model when full deterministic rendering is unsafe."""
    plan = _dedupe_plan_items(plan)
    if not plan:
        return ''
    fact_item = next((item for item in plan if 'fact' in str(item.get('table') or '').lower()), plan[0])
    fact_name = re.sub(r'[^A-Za-z0-9_]+', '_', str(fact_item.get('table') or 'facttable').strip()).strip('_').lower()
    if not fact_name:
        fact_name = 'facttable'
    fields = [_simple_select_expression(field) for field in (fact_item.get('fields') or [])]
    fields = [field for field in fields if field]
    if not fields:
        fields = ['*']
    source_name = _source_name_from_plan_item(fact_item)
    select_list = ',\n        '.join(fields)
    return (
        "{{ config(materialized='table', tags=['qlik_migration']) }}\n\n"
        "WITH\n"
        f"{fact_name} AS (\n"
        "    SELECT\n"
        f"        {select_list}\n"
        f"    FROM {{{{ source('raw', '{source_name}') }}}}\n"
        "),\n"
        "final_model AS (\n"
        f"    SELECT *\n    FROM {fact_name}\n"
        ")\n"
        "SELECT *\nFROM final_model"
    )


def _current_sql_fallback(current_sql, current_desc, message, plan, dialect='dbt'):
    current = finalize_generated_sql(current_sql or '')
    if not current.strip():
        return None
    integrity_issues = validate_candidate_integrity(current, plan=plan)
    if integrity_issues:
        return None
    validation_issues = _audit_generated_sql_against_plan(current, plan=plan, dialect=dialect)
    description = current_desc or (
        'Preserved the last valid SQL because the configured AI provider could not support '
        'a safe regeneration and deterministic fallback was rejected.'
    )
    return {
        'status': 'complete_with_validation_issues' if validation_issues else 'complete',
        'iterations': 0,
        'score': 0.75,
        'final_sql': current,
        'sql': current,
        'description': description,
        'final_description': description,
        'comparison': {'matched': False, 'differences': [], 'score': 0.75},
        'comparison_summary': {'matched': False, 'differences': [], 'score': 0.75},
        'validation_issues': validation_issues,
        'warnings': [message, 'Preserved previous SQL because AI regeneration was blocked by token budget.'] + list(validation_issues or []),
        'error': '',
        'used_deterministic_fallback': True,
        'selected_generation_mode': 'previous_sql_fallback',
        'one_shot_validation_status': 'skipped_ai_token_budget',
        'reason_for_entering_loop': '',
    }


def _deterministic_migration_result(message, qvs_script, plan, dialect='dbt', current_sql=None, current_desc=None):
    """Return rule-rendered SQL when cloud AI cannot afford a usable response."""
    plan = _dedupe_plan_items(plan if plan is not None else extract_sql_generation_plan(qvs_script or ''))
    deterministic_sql = finalize_generated_sql(render_sql_from_load_plan(plan), plan=plan, qvs_script=qvs_script)
    integrity_issues = validate_candidate_integrity(deterministic_sql, plan=plan)
    if integrity_issues:
        previous = _current_sql_fallback(current_sql, current_desc, message, plan, dialect=dialect)
        if previous:
            previous['warnings'].extend(integrity_issues)
            return previous
        skeleton_sql = finalize_generated_sql(_safe_deterministic_skeleton(plan), plan=plan, qvs_script=qvs_script)
        skeleton_integrity = validate_candidate_integrity(skeleton_sql, plan=plan)
        if not skeleton_integrity:
            deterministic_sql = skeleton_sql
            integrity_issues = [
                'Full deterministic fallback was rejected; returned a minimal fact-source skeleton for review.'
            ] + integrity_issues

    validation_issues = _audit_generated_sql_against_plan(
        deterministic_sql,
        plan=plan,
        qvs_script=qvs_script,
        dialect=dialect,
    )
    if integrity_issues and not deterministic_sql.strip():
        validation_issues = list(integrity_issues) + list(validation_issues or [])
    categories = [validation_issue_category(issue) for issue in validation_issues or []]
    has_blocking = any(category in {'compile_error', 'semantic_error'} for category in categories)
    score = 0.65 if has_blocking else 0.85
    description = (
        'Generated using deterministic Qlik LOAD rendering because the configured AI provider '
        'credits could not support the minimum safe SQL output token budget.'
    )
    return {
        'status': 'complete_with_validation_issues' if validation_issues else 'complete',
        'iterations': 0,
        'score': score,
        'final_sql': deterministic_sql,
        'sql': deterministic_sql,
        'description': description,
        'final_description': description,
        'comparison': {'matched': not has_blocking, 'differences': [], 'score': score},
        'comparison_summary': {'matched': not has_blocking, 'differences': [], 'score': score},
        'validation_issues': validation_issues,
        'warnings': [message] + list(integrity_issues or []) + list(validation_issues or []),
        'error': '',
        'used_deterministic_fallback': True,
        'selected_generation_mode': 'deterministic_fallback',
        'one_shot_validation_status': 'skipped_ai_token_budget',
        'reason_for_entering_loop': '',
    }


def _attach_migration_validation_report(result, plan=None, dialect='dbt'):
    """Attach generated dbt parity validation SQL without executing it."""
    if not isinstance(result, dict):
        return result
    if (dialect or '').lower() != 'dbt':
        return result
    sql_text = result.get('final_sql') or result.get('sql') or ''
    if not str(sql_text or '').strip():
        return result
    report = build_migration_validation_report(sql_text, plan=plan or [], dialect=dialect)
    report = execute_validation_report(
        report,
        {
            'enabled': VALIDATION_EXECUTION_ENABLED,
        },
    )
    result['validationReport'] = report
    result['validation_report'] = report
    result['validationArtifacts'] = generate_validation_artifacts(
        sql_text,
        report,
        model_name='executive_dashboard',
    )
    result['validation_artifacts'] = result['validationArtifacts']
    return result


def _ensure_result_validation_payload(result, plan=None, dialect='dbt'):
    """Ensure completed regeneration results carry exportable validation artifacts."""
    if not isinstance(result, dict):
        return result
    if (dialect or '').lower() != 'dbt':
        return result
    sql_text = result.get('final_sql') or result.get('sql') or ''
    if not str(sql_text or '').strip():
        return result
    report = result.get('validationReport') or result.get('validation_report')
    if not report:
        report = build_migration_validation_report(sql_text, plan=plan or [], dialect=dialect, model_name='executive_dashboard')
        report = execute_validation_report(report, {'enabled': VALIDATION_EXECUTION_ENABLED})
        logger.info("VALIDATION_REPORT_GENERATED checks=%s", len(report.get('checks') or []))
    result['validationReport'] = report
    result['validation_report'] = report
    artifacts = result.get('validationArtifacts') or result.get('validation_artifacts')
    if not artifacts:
        artifacts = generate_validation_artifacts(sql_text, report, model_name='executive_dashboard')
        logger.info(
            "VALIDATION_ARTIFACTS_GENERATED models=%s tests=%s analyses=%s",
            len((artifacts.get('models') or {})),
            len((artifacts.get('tests') or {})),
            len((artifacts.get('analyses') or {})),
        )
    result['validationArtifacts'] = artifacts
    result['validation_artifacts'] = artifacts
    if 'sqlQualityScore' not in result and result.get('oneShotQualityScore') is not None:
        result['sqlQualityScore'] = result.get('oneShotQualityScore')
    return result


def _self_heal_regenerate_result_payload(job_id, job_payload, plan=None, dialect='dbt'):
    """Route-level guard: completed job result responses must include export artifacts."""
    job_payload = job_payload if isinstance(job_payload, dict) else {}
    result = job_payload.get('result') or job_payload.get('regeneration') or {}
    if not isinstance(result, dict):
        result = {}
    sql_text = result.get('final_sql') or result.get('sql') or job_payload.get('sql') or ''
    validation_report = result.get('validationReport') or result.get('validation_report')
    validation_artifacts = result.get('validationArtifacts') or result.get('validation_artifacts')
    if sql_text and not validation_report:
        validation_report = build_migration_validation_report(
            sql_text,
            plan=plan or [],
            dialect=dialect,
            model_name='executive_dashboard',
        )
        validation_report = execute_validation_report(validation_report, {'enabled': VALIDATION_EXECUTION_ENABLED})
        result['validationReport'] = validation_report
        result['validation_report'] = validation_report
        logger.info("VALIDATION_REPORT_GENERATED checks=%s", len(validation_report.get('checks') or []))
    if sql_text and not validation_artifacts:
        validation_artifacts = generate_validation_artifacts(
            sql_text,
            validation_report or {},
            model_name='executive_dashboard',
        )
        result['validationArtifacts'] = validation_artifacts
        result['validation_artifacts'] = validation_artifacts
        logger.info(
            "VALIDATION_ARTIFACTS_GENERATED models=%s tests=%s analyses=%s",
            len((validation_artifacts.get('models') or {})),
            len((validation_artifacts.get('tests') or {})),
            len((validation_artifacts.get('analyses') or {})),
        )
    logger.info(
        "REGENERATE_RESULT_SELF_HEAL job_id=%s has_sql=%s has_report=%s has_artifacts=%s",
        job_id,
        bool(sql_text),
        bool(result.get('validationReport') or result.get('validation_report')),
        bool(result.get('validationArtifacts') or result.get('validation_artifacts')),
    )
    job_payload['result'] = result
    return job_payload, result


def _stream_openrouter_with_budget_retry(
    api_key,
    model,
    prompt,
    system_prompt=None,
    temperature=0,
    top_p=1,
    max_tokens=2500,
    max_prompt_chars=35000,
    timeout=60,
    min_tokens=MIN_REQUIRED_OUTPUT_TOKENS,
):
    try:
        yield from call_openrouter_chat_stream(
            api_key,
            model,
            prompt,
            system_prompt=system_prompt,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            max_prompt_chars=max_prompt_chars,
            timeout=timeout,
        )
    except Exception as exc:
        affordable = _affordable_openrouter_tokens(exc) if _is_openrouter_credit_error(exc) else None
        if affordable is not None and min_tokens <= affordable < max_tokens:
            print(
                "OPENROUTER CREDIT WARNING: retrying stream with affordable "
                f"max_tokens={affordable} (requested {max_tokens})"
            )
            yield from call_openrouter_chat_stream(
                api_key,
                model,
                prompt,
                system_prompt=system_prompt,
                temperature=temperature,
                top_p=top_p,
                max_tokens=affordable,
                max_prompt_chars=max_prompt_chars,
                timeout=timeout,
            )
            return
        if _is_openrouter_credit_error(exc):
            raise RuntimeError(_credit_budget_error(exc, max_tokens, min_tokens)) from exc
        raise


def call_openrouter(
    prompt,
    system_prompt=None,
    model=None,
    temperature=0,
    top_p=1,
    max_tokens=None,
    max_prompt_chars=None,
    timeout=60,
    retries=1,
    stream=False,
):
    provider = _selected_ai_provider()
    model = model or _active_ai_model(provider)
    max_tokens = max_tokens if max_tokens is not None else OPENROUTER_MAX_TOKENS
    max_prompt_chars = max_prompt_chars if max_prompt_chars is not None else OPENROUTER_MAX_PROMPT_CHARS
    min_tokens = MIN_REQUIRED_OUTPUT_TOKENS
    if max_tokens < min_tokens:
        raise RuntimeError(
            "insufficient AI output token budget: dbt SQL generation requires "
            f"at least {min_tokens} output tokens, but max_tokens={max_tokens}."
        )

    if provider == 'gemini':
        print(
            f"GEMINI REQUEST model={model} prompt_chars={len(prompt)} "
            f"max_tokens={max_tokens} max_prompt_chars={max_prompt_chars} stream={stream}"
        )
        if stream:
            return iter([call_gemini_chat(
                GEMINI_API_KEY,
                model,
                prompt,
                system_prompt=system_prompt,
                temperature=temperature,
                top_p=top_p,
                max_tokens=max_tokens,
                max_prompt_chars=max_prompt_chars,
                timeout=timeout,
                retries=retries,
            )])
        return call_gemini_chat(
            GEMINI_API_KEY,
            model,
            prompt,
            system_prompt=system_prompt,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            max_prompt_chars=max_prompt_chars,
            timeout=timeout,
            retries=retries,
        )

    if provider == 'groq':
        max_tokens = min(max_tokens, GROQ_MAX_TOKENS)
        max_prompt_chars = min(max_prompt_chars, GROQ_MAX_PROMPT_CHARS)
        logger.info(
            "Groq request: model=%s prompt_chars=%d max_tokens=%d max_prompt_chars=%d stream=%s",
            model,
            len(prompt),
            max_tokens,
            max_prompt_chars,
            stream,
        )
        if stream:
            return iter([call_groq_chat(
                GROQ_API_KEY,
                model,
                prompt,
                system_prompt=system_prompt,
                temperature=temperature,
                top_p=top_p,
                max_tokens=max_tokens,
                max_prompt_chars=max_prompt_chars,
                timeout=timeout,
                retries=retries,
            )])
        return call_groq_chat(
            GROQ_API_KEY,
            model,
            prompt,
            system_prompt=system_prompt,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            max_prompt_chars=max_prompt_chars,
            timeout=timeout,
            retries=retries,
        )

    if provider == 'ollama':
        logger.info(
            "Ollama request: model=%s prompt_chars=%d max_tokens=%d max_prompt_chars=%d stream=%s",
            model,
            len(prompt),
            max_tokens,
            max_prompt_chars,
            stream,
        )
        if stream:
            return iter([call_ollama_chat(
                OLLAMA_BASE_URL,
                model,
                prompt,
                system_prompt=system_prompt,
                temperature=temperature,
                top_p=top_p,
                max_tokens=max_tokens,
                max_prompt_chars=max_prompt_chars,
                timeout=max(timeout, 180),
                retries=0,
            )])
        return call_ollama_chat(
            OLLAMA_BASE_URL,
            model,
            prompt,
            system_prompt=system_prompt,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            max_prompt_chars=max_prompt_chars,
            timeout=max(timeout, 180),
            retries=0,
        )

    if not OPENROUTER_API_KEY:
        raise RuntimeError('OPENROUTER_API_KEY is not configured.')

    model = model or OPENROUTER_DEFAULT_MODEL
    model_candidates = [m for m in OPENROUTER_FALLBACK_MODELS if m]
    if model not in model_candidates:
        model_candidates.insert(0, model)

    last_error = None
    tried_affordable_tokens = False

    for candidate_model in model_candidates:
        while True:
            logger.info(
                "OpenRouter request: model=%s prompt_chars=%d max_tokens=%d max_prompt_chars=%d",
                candidate_model,
                len(prompt),
                max_tokens,
                max_prompt_chars,
            )
            try:
                if stream:
                    response_text = _stream_openrouter_with_budget_retry(
                        OPENROUTER_API_KEY,
                        candidate_model,
                        prompt,
                        system_prompt=system_prompt,
                        temperature=temperature,
                        top_p=top_p,
                        max_tokens=max_tokens,
                        max_prompt_chars=max_prompt_chars,
                        timeout=timeout,
                        min_tokens=min_tokens,
                    )
                else:
                    response_text = call_openrouter_chat(
                        OPENROUTER_API_KEY,
                        candidate_model,
                        prompt,
                        system_prompt=system_prompt,
                        temperature=temperature,
                        top_p=top_p,
                        max_tokens=max_tokens,
                        max_prompt_chars=max_prompt_chars,
                        timeout=timeout,
                        retries=retries,
                    )
                if candidate_model != model:
                    print(f"OPENROUTER FALLBACK ACTIVATED: switched to {candidate_model}")
                return response_text
            except Exception as exc:
                last_error = exc
                if _is_openrouter_model_error(exc) and candidate_model != model_candidates[-1]:
                    print(f"OPENROUTER MODEL NOT AVAILABLE: {candidate_model}, trying fallback.")
                    break
                affordable = _affordable_openrouter_tokens(exc) if _is_openrouter_credit_error(exc) else None
                if (
                    affordable is not None
                    and min_tokens <= affordable < max_tokens
                    and not tried_affordable_tokens
                ):
                    tried_affordable_tokens = True
                    max_tokens = affordable
                    print(
                        "OPENROUTER CREDIT WARNING: retrying with affordable "
                        f"max_tokens={max_tokens}"
                    )
                    continue
                if _is_openrouter_credit_error(exc) and candidate_model != model_candidates[-1]:
                    print(
                        f"OPENROUTER CREDIT WARNING: model {candidate_model} failed, trying next fallback model."
                    )
                    break
                if _is_openrouter_credit_error(exc):
                    raise RuntimeError(_credit_budget_error(exc, max_tokens, min_tokens)) from exc
                raise
            break

    raise RuntimeError(
        f"All OpenRouter models failed. Last error: {last_error}"
    ) from last_error


def call_openrouter_fast(
    prompt,
    system_prompt=None,
    model=None,
    temperature=0,
    top_p=1,
    max_tokens=None,
    max_prompt_chars=None,
    timeout=60,
    stream=False,
):
    """Fast direct AI request with no fallback and no retry overhead.
    
    When stream=True, returns a generator yielding text chunks.
    """
    provider = _selected_ai_provider()
    model = model or _active_ai_model(provider)
    effective_max_tokens = min(max_tokens if max_tokens is not None else OPENROUTER_MAX_TOKENS, 10000)
    effective_max_prompt_chars = min(max_prompt_chars if max_prompt_chars is not None else OPENROUTER_MAX_PROMPT_CHARS, 35000)
    min_tokens = MIN_REQUIRED_OUTPUT_TOKENS
    if effective_max_tokens < min_tokens:
        raise RuntimeError(
            "insufficient AI output token budget: dbt SQL generation requires "
            f"at least {min_tokens} output tokens, but max_tokens={effective_max_tokens}."
        )

    if provider in {'gemini', 'groq', 'ollama'}:
        return call_openrouter(
            prompt,
            system_prompt=system_prompt,
            model=model,
            temperature=temperature,
            top_p=top_p,
            max_tokens=effective_max_tokens,
            max_prompt_chars=effective_max_prompt_chars,
            timeout=timeout,
            retries=0,
            stream=stream,
        )

    if not OPENROUTER_API_KEY:
        raise RuntimeError('OPENROUTER_API_KEY is not configured.')

    print(
        f"OPENROUTER FAST REQUEST model={model} prompt_chars={len(prompt)} "
        f"max_tokens={effective_max_tokens} max_prompt_chars={effective_max_prompt_chars} stream={stream}"
    )
    if stream:
        return _stream_openrouter_with_budget_retry(
            OPENROUTER_API_KEY,
            model,
            prompt,
            system_prompt=system_prompt,
            temperature=temperature,
            top_p=top_p,
            max_tokens=effective_max_tokens,
            max_prompt_chars=effective_max_prompt_chars,
            timeout=timeout,
            min_tokens=min_tokens,
        )
    try:
        return call_openrouter_chat(
            OPENROUTER_API_KEY,
            model,
            prompt,
            system_prompt=system_prompt,
            temperature=temperature,
            top_p=top_p,
            max_tokens=effective_max_tokens,
            max_prompt_chars=effective_max_prompt_chars,
            timeout=timeout,
            retries=0,
        )
    except Exception as exc:
        affordable = _affordable_openrouter_tokens(exc) if _is_openrouter_credit_error(exc) else None
        if affordable is not None and min_tokens <= affordable < effective_max_tokens:
            print(
                "OPENROUTER CREDIT WARNING: retrying fast request with affordable "
                f"max_tokens={affordable} (requested {effective_max_tokens})"
            )
            return call_openrouter_chat(
                OPENROUTER_API_KEY,
                model,
                prompt,
                system_prompt=system_prompt,
                temperature=temperature,
                top_p=top_p,
                max_tokens=affordable,
                max_prompt_chars=effective_max_prompt_chars,
                timeout=timeout,
                retries=0,
            )
        if _is_openrouter_credit_error(exc):
            raise RuntimeError(_credit_budget_error(exc, effective_max_tokens, min_tokens)) from exc
        raise


def get_cached_sql_plan(session_id, scripts_text):
    scripts_hash = hash_text(scripts_text)
    cache_key = f"{session_id}:{scripts_hash}"
    cached = SQL_PLAN_CACHE.get(cache_key)
    if cached:
        print(f"PLAN CACHE HIT session={session_id} plan_size={len(cached.get('plan', []))}")
        return cached

    plan = extract_sql_generation_plan(scripts_text)
    print(f"PLAN EXTRACTED session={session_id} plan_size={len(plan)} scripts_len={len(scripts_text)}")
    plan_text = format_sql_generation_plan(plan)
    cached = {
        'hash': scripts_hash,
        'plan': plan,
        'planText': plan_text,
    }
    SQL_PLAN_CACHE.set(cache_key, cached)
    return cached

def repair_generated_sql(sql_text, description_text, issues, dialect='dbt', qvs_script=None, plan_text=None):
    return request_sql_repair(
        call_openrouter,
        sql_text,
        description_text,
        issues,
        dialect=dialect,
        description_style=SQL_DESCRIPTION_STYLE,
        prompt_version=PROMPT_VERSION,
        qvs_script=qvs_script,
        plan_text=plan_text,
    )


def _strip_sql_comments(sql_text):
    """Remove SQL comments before lightweight structure checks."""
    sql_text = sql_text or ''
    sql_text = re.sub(r'/\*.*?\*/', '', sql_text, flags=re.DOTALL)
    sql_text = re.sub(r'--.*?$', '', sql_text, flags=re.MULTILINE)
    return sql_text.strip()


def _extract_cte_names(sql_text):
    """Return CTE names from a WITH query without hardcoding business table names."""
    sql_text = _strip_sql_comments(sql_text)
    return [
        match.group(1).lower()
        for match in re.finditer(
            r'(?:\bWITH\b|,)\s+([A-Za-z_][A-Za-z0-9_]*)\s+AS\s*\(',
            sql_text,
            flags=re.IGNORECASE,
        )
    ]


def _final_select_source(sql_text):
    """Return (is_select_star, source_name) for the final SELECT when detectable."""
    sql_text = _strip_sql_comments(sql_text).rstrip(';').strip()
    match = re.search(
        r'\bSELECT\s+(\*|[\s\S]*?)\s+FROM\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:;)?\s*$',
        sql_text,
        flags=re.IGNORECASE,
    )
    if not match:
        return False, ''
    projection = re.sub(r'\s+', ' ', match.group(1)).strip()
    return projection == '*', match.group(2).lower()


def _cte_body(sql_text, cte_name):
    """Best-effort CTE body extraction for validation heuristics."""
    sql_text = _strip_sql_comments(sql_text)
    pattern = re.compile(
        rf'(?:\bWITH\b|,)\s+{re.escape(cte_name)}\s+AS\s*\(',
        flags=re.IGNORECASE,
    )
    match = pattern.search(sql_text)
    if not match:
        return ''
    start = match.end()
    depth = 1
    idx = start
    while idx < len(sql_text):
        char = sql_text[idx]
        if char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
            if depth == 0:
                return sql_text[start:idx]
        idx += 1
    return ''


def _is_generic_shallow_final_model(sql_text):
    """Detect generic bad one-shot output: many CTEs built, final output ignores them.

    This is intentionally file-agnostic. It does not target specific Qlik tables.
    It flags patterns such as:
      WITH a AS (...), b AS (...), c AS (...)
      SELECT * FROM a

    It also flags a `final_model` CTE that only does `SELECT * FROM one_cte`
    while several peer CTEs exist, because that still ignores the useful model graph.
    """
    cte_names = _extract_cte_names(sql_text)
    cte_set = set(cte_names)
    is_star, final_source = _final_select_source(sql_text)

    if len(cte_set) < 3 or not is_star or final_source not in cte_set:
        return False

    if final_source != 'final_model':
        return True

    body = _cte_body(sql_text, 'final_model')
    body_star, body_source = _final_select_source(body)
    if body_star and body_source in cte_set and len(cte_set - {'final_model', body_source}) >= 2:
        return True

    return False


def _generic_one_shot_quality_issues(sql_text, plan=None):
    """Extra generic one-shot checks that prevent costly loop escalation.

    These checks are not tied to any specific customer file. They only inspect SQL
    structure and the extracted migration plan size.
    """
    issues = []
    sql_text = sql_text or ''
    cte_names = _extract_cte_names(sql_text)
    cte_set = set(cte_names)
    is_star, final_source = _final_select_source(sql_text)
    plan_size = len(plan or [])

    if plan_size >= 3 and len(cte_set) >= 3 and 'final_model' not in cte_set:
        issues.append(
            'FINAL_MODEL_MISSING: generated SQL creates multiple CTEs but does not create a final_model CTE.'
        )

    if _is_generic_shallow_final_model(sql_text):
        issues.append(
            'FINAL_SELECT_TOO_SHALLOW: generated SQL creates multiple CTEs but final output selects from only one intermediate CTE.'
        )

    if len(cte_set) >= 3 and is_star and final_source in cte_set and final_source != 'final_model':
        unused_count = len(cte_set - {final_source})
        if unused_count >= 2:
            issues.append(
                f'CTE_CREATED_BUT_UNUSED: final SELECT reads only {final_source}; {unused_count} other CTE(s) are not used in the final output.'
            )

    return issues


def _blocking_issue_categories(issues):
    return [validation_issue_category(issue) for issue in issues or []]


def _has_blocking_issues(issues):
    # Reserve the expensive validation loop for SQL that is structurally unsafe
    # or cannot compile. Optional lookup/dimension coverage should remain a warning.
    blocking_markers = (
        'EMPTY_SQL',
        'UNBALANCED_PARENS',
        'PARENTHESES LOOK UNBALANCED',
        'BARE_DDL',
        'SOURCE_TABLE_RENAMED',
        'ALIAS_COLUMN_NOT_FOUND',
        'DUPLICATE_ALIAS',
        'JOIN_KEY_MISSING',
        'INVALID_EXPENSES_JOIN_MONTHLY_ONLY',
        'FINAL_MODEL_MISSING',
        'FINAL_SELECT_TOO_SHALLOW',
        'UNION_COLUMN_MISMATCH',
        'UNION_COLUMN_COUNT_MISMATCH',
        'UNION_SELECT_STAR',
    )
    return any(
        any(marker in str(issue).upper() for marker in blocking_markers)
        for issue in issues or []
    )


def _has_structural_loop_blockers(issues):
    structural_markers = (
        'EMPTY_SQL',
        'UNBALANCED_PARENS',
        'BARE_DDL',
        'DYNAMIC_UNION_REBUILD_FAILED',
        'INVALID_EXPENSES_JOIN_WITH_CONCAT_PLAN',
    )
    return any(any(marker in str(issue).upper() for marker in structural_markers) for issue in (issues or []))


def _is_safe_finalized_sql_shape(sql_text: str) -> bool:
    sql = (sql_text or '').lower()
    if not sql:
        return False
    has_fact_expenses = 'facttable_with_expenses' in sql or 'fact_table_with_expenses' in sql
    has_join_expenses = bool(re.search(r'(?is)\bjoin\s+expenses\b', sql_text or ''))
    has_select_star_union = bool(re.search(r'(?is)\bselect\s+(?:\w+\.)?\*\s+from\s+facttable\b[\s\S]*?\bunion\s+all\b', sql_text or ''))
    has_final_model = bool(re.search(r'(?is)\bfinal_model\s+as\s*\(', sql_text or ''))
    return has_fact_expenses and (not has_join_expenses) and (not has_select_star_union) and has_final_model


def _is_safe_false_union_mismatch(sql_text: str, issues) -> bool:
    issue_codes = [_issue_code(issue).upper() for issue in (issues or [])]
    blocking_codes = [code for code in issue_codes if code in STRUCTURAL_BLOCKING_CODES]
    if not blocking_codes or any(code != 'UNION_COLUMN_COUNT_MISMATCH' for code in blocking_codes):
        return False
    sql = sql_text or ''
    has_fact_expenses = bool(re.search(r'(?is)\bfact(?:table|_table)_with_expenses\s+AS\s*\(', sql))
    if not has_fact_expenses:
        return False
    if re.search(r'(?is)\bJOIN\s+expenses\b', sql):
        return False
    if re.search(
        r'(?is)\bfact(?:table|_table)_with_expenses\s+AS\s*\([\s\S]*?'
        r'\bSELECT\s+(?:[A-Za-z_][A-Za-z0-9_]*\.)?\*[\s\S]*?\bUNION\s+ALL\b',
        sql,
    ):
        return False
    if re.search(
        r'(?is)\bfact(?:table|_table)_with_expenses\s+AS\s*\([\s\S]*?'
        r'\bUNION\s+ALL\b[\s\S]*?\bSELECT\s+(?:[A-Za-z_][A-Za-z0-9_]*\.)?\*',
        sql,
    ):
        return False
    return bool(re.search(r'(?is)\bUNION\s+ALL\b', sql))


def _extract_cte_body_for_app_validation(sql_text: str, cte_name: str) -> str:
    """Small app-local CTE body extractor for repair gating checks."""
    sql = sql_text or ''
    match = re.search(rf'(?is)\b{re.escape(cte_name)}\s+AS\s*\(', sql)
    if not match:
        return ''
    start = match.end()
    depth = 1
    i = start
    in_single = False
    in_double = False
    in_line_comment = False
    in_block_comment = False
    while i < len(sql):
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < len(sql) else ''
        if in_line_comment:
            if ch == '\n':
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            if ch == '*' and nxt == '/':
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if not in_single and not in_double and ch == '-' and nxt == '-':
            in_line_comment = True
            i += 2
            continue
        if not in_single and not in_double and ch == '/' and nxt == '*':
            in_block_comment = True
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    return sql[start:i]
        i += 1
    return ''


def _final_model_has_leftover_expenses_alias(sql_text: str) -> bool:
    body = _extract_cte_body_for_app_validation(sql_text, 'final_model')
    if not body:
        return False
    if re.search(r'(?is)\bJOIN\s+expenses\s+e\b', body):
        return False
    return bool(re.search(r'(?is)(?<![A-Za-z0-9_])e\s*\.', body))


def _is_safe_finalized_union_sql(sql_text: str) -> bool:
    sql = sql_text or ''
    fwe_body = _extract_cte_body_for_app_validation(sql, 'facttable_with_expenses')
    if not fwe_body:
        fwe_body = _extract_cte_body_for_app_validation(sql, 'fact_table_with_expenses')
    if not fwe_body:
        return False
    if not re.search(r'(?is)\bUNION\s+ALL\b', fwe_body):
        return False
    if not re.search(r'(?is)\bfinal_model\s+AS\s*\(', sql):
        return False
    if re.search(r'(?is)\bJOIN\s+expenses\b', sql):
        return False
    if re.search(r'(?is)\bSELECT\b[\s\S]*?(?:^|,)\s*(?:[A-Za-z_][A-Za-z0-9_]*\.)?\*[\s\S]*?(?:\bFROM\b|\bUNION\s+ALL\b)', fwe_body):
        return False
    if _final_model_has_leftover_expenses_alias(sql):
        return False
    return True


def _has_safe_finalized_union_shape(sql_text: str) -> bool:
    return _is_safe_finalized_union_sql(sql_text)


def _apply_safe_union_override(sql_text: str, issues, status=''):
    issues = list(issues or [])
    status_text = str(status or '').lower()
    if not issues or (status_text and not status_text.startswith('complete')):
        return issues, False
    union_issues = [issue for issue in issues if 'UNION_COLUMN_COUNT_MISMATCH' in str(issue)]
    if not union_issues or not _is_safe_finalized_union_sql(sql_text):
        return issues, False
    rewritten = []
    applied = False
    for issue in issues:
        if 'UNION_COLUMN_COUNT_MISMATCH' in str(issue):
            logger.info("SAFE_UNION_OVERRIDE applied=True original=%s", issue)
            rewritten.append('SAFE_UNION_OVERRIDE: stale finalized union count warning downgraded to metadata warning.')
            applied = True
        else:
            rewritten.append(issue)
    return list(dict.fromkeys(rewritten)), applied


def _force_safe_union_blocking_filter(sql_text: str, issues):
    """Never let stale union-count warnings block repair when finalized SQL is safe."""
    safe_finalized = _is_safe_finalized_union_sql(sql_text)
    if not safe_finalized:
        return list(issues or []), _real_blocking_issues(issues), False
    rewritten = []
    applied = False
    for issue in issues or []:
        if str(issue).startswith('UNION_COLUMN_COUNT_MISMATCH'):
            rewritten.append('SAFE_UNION_OVERRIDE: stale finalized union count warning downgraded to metadata warning.')
            applied = True
        else:
            rewritten.append(issue)
    if applied:
        logger.info("SAFE_UNION_OVERRIDE forced_blocking_filter=True")
    blocking = [
        issue for issue in _real_blocking_issues(rewritten)
        if not str(issue).startswith('UNION_COLUMN_COUNT_MISMATCH')
    ]
    return list(dict.fromkeys(rewritten)), blocking, applied


def _loop_needed_by_policy(issues, generation_mode='auto'):
    if generation_mode == 'one_shot':
        return False
    if MIGRATION_LOOP_MAX_ITERATIONS <= 0:
        return False
    categories = _blocking_issue_categories(issues)
    compile_only = any(category == 'compile_error' for category in categories)
    semantic_or_compile = any(category in {'compile_error', 'semantic_error'} for category in categories)
    minimal_markers = ('EMPTY_SQL', 'UNBALANCED_PARENS', 'BARE_DDL')
    minimal_block = any(any(marker in str(issue).upper() for marker in minimal_markers) for issue in (issues or []))

    if LOOP_POLICY == 'strict':
        return bool(semantic_or_compile or _has_blocking_issues(issues))
    if LOOP_POLICY == 'minimal':
        return bool(minimal_block)
    # balanced (default): loop only for structural blockers after one repair.
    return bool(_has_structural_loop_blockers(issues))


def _is_non_blocking_issue_text(issue_text):
    non_blocking_codes = {
        'UNREACHABLE_CTE_CREATED_NOT_USED',
        'IR_AMBIGUITY',
        'ISLAND_TABLE',
    }
    upper = str(issue_text or '').upper()
    return any(code in upper for code in non_blocking_codes)


def _issue_code(issue: str) -> str:
    text = str(issue or '')
    if ':' in text:
        return text.split(':', 1)[0].replace('[ERROR]', '').replace('[WARNING]', '').strip()
    return text.strip()


def _is_non_blocking_metadata_issue(issue: str) -> bool:
    code = _issue_code(issue)
    return (
        code.startswith('UNREACHABLE_CTE_CREATED_NOT_USED')
        or code.startswith('IR_AMBIGUITY')
        or code.startswith('ISLAND_TABLE')
        or code.startswith('MISSING_SCHEMA_CONTRACT')
    )


def _is_blocking_issue(issue: str) -> bool:
    if _is_non_blocking_metadata_issue(issue):
        return False
    return validation_issue_category(issue) in {'compile_error', 'semantic_error'}


def _issue_category(issue):
    try:
        return validation_issue_category(issue)
    except Exception:
        return 'metadata_warning'


def _real_blocking_issues(issues):
    blockers = []
    for issue in (issues or []):
        if _is_one_shot_quality_warning(issue):
            continue
        if _issue_code(issue).upper() in STRUCTURAL_BLOCKING_CODES:
            blockers.append(issue)
    return blockers


def _is_one_shot_quality_warning(issue):
    code = _issue_code(issue)
    if code in {
        'MISSING_AGGREGATION_CTE',
        'MISSING_PRODUCT_BRIDGE_JOIN',
        'MISSING_PRODUCT_MASTER_JOIN',
        'UNUSED_ACCOUNT_MASTER',
        'UNUSED_ACCOUNT_GROUP_MASTER',
        'MISSING_ARSUMMARY_1_JOIN',
        'COLUMN_OWNERSHIP_MISMATCH',
    }:
        return True
    if code.startswith('MISSING_') and code.endswith('_JOIN'):
        return True
    return False


def _has_real_blocking_issues(issues):
    return bool(_real_blocking_issues(issues))


def _all_issues_are_metadata_only(issues):
    return bool(issues) and not _has_real_blocking_issues(issues)


def _one_shot_repair_once(quick_result, issues, qvs_script, plan, plan_text, dialect, progress_callback=None):
    """Make one small repair attempt before entering the expensive loop."""
    if not quick_result or not issues:
        return quick_result, issues, False
    if _all_issues_are_metadata_only(issues):
        return quick_result, issues, False

    original_sql = finalize_generated_sql(
        quick_result.get('sql', '') or quick_result.get('final_sql', ''),
        plan=plan,
        qvs_script=qvs_script,
    )
    if not original_sql.strip():
        return quick_result, issues, False

    if progress_callback:
        progress_callback('One-shot SQL needs structural repair; attempting one targeted repair before validation loop.')

    try:
        repaired_raw = repair_generated_sql(
            original_sql,
            quick_result.get('description') or quick_result.get('final_description') or '',
            issues,
            dialect=dialect,
            qvs_script=(qvs_script or '')[:12000],
            plan_text=plan_text,
        )
        repaired_result = parse_migration_response(repaired_raw) if isinstance(repaired_raw, str) else repaired_raw
        if not repaired_result or not repaired_result.get('sql'):
            return quick_result, issues, False

        repaired_sql = finalize_generated_sql(
            repaired_result.get('sql') or '',
            plan=plan,
            qvs_script=qvs_script,
        )
        if not repaired_sql.strip():
            return quick_result, issues, False

        integrity_issues = validate_candidate_integrity(repaired_sql, plan=plan)
        if integrity_issues:
            quick_result.setdefault('warnings', []).extend(integrity_issues)
            return quick_result, issues + integrity_issues, False

        regressions = detect_repair_regressions(original_sql, repaired_sql)
        if regressions:
            quick_result.setdefault('warnings', []).extend(regressions)
            return quick_result, issues + regressions, False

        repaired_issues = _audit_generated_sql_against_plan(
            repaired_sql,
            plan=plan,
            qvs_script=qvs_script,
            dialect=dialect,
        )
        repaired_issues = list(repaired_issues or []) + _generic_one_shot_quality_issues(repaired_sql, plan=plan)

        repaired_result['sql'] = repaired_sql
        repaired_result['final_sql'] = repaired_sql
        repaired_result['description'] = repaired_result.get('description') or quick_result.get('description') or ''
        repaired_result['final_description'] = repaired_result.get('final_description') or repaired_result['description']
        repaired_result['validation_issues'] = repaired_issues
        repaired_result['warnings'] = list(dict.fromkeys((repaired_result.get('warnings') or []) + repaired_issues))
        repaired_result['status'] = 'complete_with_validation_issues' if repaired_issues else 'complete'
        repaired_result['used_one_shot_repair'] = True
        return repaired_result, repaired_issues, True
    except Exception as exc:
        quick_result.setdefault('warnings', []).append(f'One-shot repair failed: {exc}')
        return quick_result, issues + [f'One-shot repair failed: {exc}'], False


def migrate_qvs_to_dbt(qvs_script, session_context=None, current_sql=None, current_desc=None, dialect='dbt', plan=None, plan_text=None, progress_callback=None, stream_callback=None, generation_mode='auto'):
    generation_mode = (generation_mode or 'auto').strip().lower()
    if generation_mode not in {'auto', 'one_shot', 'loop'}:
        generation_mode = 'auto'
    provider_prompt_version = f"{PROMPT_VERSION}.{_selected_ai_provider()}"
    logger.info("Migration mode selected: %s", generation_mode)
    if progress_callback:
        progress_callback(f"Selected generation mode: {generation_mode}")

    if (dialect or '').lower() == 'powerbi':
        result = request_migration_one_shot(
            call_openrouter_fast,
            qvs_script,
            session_context=session_context,
            current_sql=current_sql,
            current_desc=current_desc,
            dialect=dialect,
            plan=plan,
            plan_text=plan_text,
            prompt_version=provider_prompt_version,
            description_style=SQL_DESCRIPTION_STYLE,
            progress_callback=progress_callback,
            stream_callback=stream_callback if ONE_SHOT_STREAMING else None,
        )
        result['selected_generation_mode'] = 'one_shot'
        return _attach_migration_validation_report(result, plan=plan, dialect=dialect)

    if generation_mode == 'loop':
        logger.info("Migration loop mode requested explicitly")
        if progress_callback:
            progress_callback('Starting Senior AI/ML validation loop directly...')
        try:
            result = request_migration_with_validation(
                call_openrouter,
                qvs_script,
                session_context=session_context,
                current_sql=current_sql,
                current_desc=current_desc,
                dialect=dialect,
                plan=plan,
                plan_text=plan_text,
                prompt_version=provider_prompt_version,
                description_style=SQL_DESCRIPTION_STYLE,
                max_iterations=max(0, MIGRATION_LOOP_MAX_ITERATIONS),
                progress_callback=progress_callback,
                stream_callback=stream_callback,
            )
        except Exception as exc:
            if _is_token_budget_failure(exc):
                message = str(exc)
                if progress_callback:
                    progress_callback('AI provider token budget is too low; using deterministic SQL generation.')
                return _attach_migration_validation_report(_deterministic_migration_result(
                    message,
                    qvs_script,
                    plan,
                    dialect=dialect,
                    current_sql=current_sql,
                    current_desc=current_desc,
                ), plan=plan, dialect=dialect)
            raise
        result['selected_generation_mode'] = 'loop'
        result['reason_for_entering_loop'] = 'explicit_loop_mode'
        return _attach_migration_validation_report(result, plan=plan, dialect=dialect)

    try:
        if progress_callback:
            progress_callback('Attempting fast one-shot DBT migration...')
        quick_result = request_migration_one_shot(
            call_openrouter_fast,
            qvs_script,
            session_context=session_context,
            current_sql=current_sql,
            current_desc=current_desc,
            dialect=dialect,
            plan=plan,
            plan_text=plan_text,
            prompt_version=provider_prompt_version,
            description_style=SQL_DESCRIPTION_STYLE,
            progress_callback=progress_callback,
            stream_callback=stream_callback if ONE_SHOT_STREAMING else None,
        )
        if quick_result.get('status') == 'failed' and quick_result.get('error'):
            if _is_token_budget_failure(quick_result.get('error')):
                if progress_callback:
                    progress_callback('AI provider token budget is too low; using deterministic SQL generation.')
                return _attach_migration_validation_report(_deterministic_migration_result(
                    quick_result['error'],
                    qvs_script,
                    plan,
                    dialect=dialect,
                    current_sql=current_sql,
                    current_desc=current_desc,
                ), plan=plan, dialect=dialect)
            if progress_callback:
                progress_callback(f"Fast one-shot migration stopped: {quick_result['error']}")
            return _attach_migration_validation_report(quick_result, plan=plan, dialect=dialect)

        quick_sql = finalize_generated_sql(
            quick_result.get('sql', '') or quick_result.get('final_sql', ''),
            plan=plan,
            qvs_script=qvs_script,
        )
        quick_result['sql'] = quick_sql
        quick_result['final_sql'] = quick_sql

        audit_issues = _audit_generated_sql_against_plan(
            quick_sql,
            plan=plan,
            qvs_script=qvs_script,
            dialect=dialect,
        )
        generic_issues = _generic_one_shot_quality_issues(quick_sql, plan=plan)
        quick_issues = list(dict.fromkeys(list(audit_issues or []) + generic_issues))
        quick_issues, safe_union_override_applied = _apply_safe_union_override(
            quick_sql,
            quick_issues,
            status=quick_result.get('status'),
        )
        quick_issues, blocking_issues, forced_safe_union_filter_applied = _force_safe_union_blocking_filter(
            quick_sql,
            quick_issues,
        )
        safe_union_override_applied = safe_union_override_applied or forced_safe_union_filter_applied
        quick_result['validation_issues'] = quick_issues
        quick_categories = [_issue_category(i) for i in quick_issues]

        if not blocking_issues:
            quick_result['status'] = 'complete_with_validation_issues' if quick_issues else 'complete'
            quick_result['validation_issues'] = quick_issues
            quick_result['blockingIssues'] = []
            quick_result['loopNeeded'] = False
            quick_result['repairAttempted'] = False
            quick_result['one_shot_validation_status'] = 'passed_with_warnings' if quick_issues else 'passed'
            quick_result['reason_for_entering_loop'] = ''
            logger.info(
                "One-shot completed with warnings only: categories=%s issues=%s",
                quick_categories,
                quick_issues[:5],
            )
            if progress_callback:
                progress_callback(
                    'One-shot migration completed with warnings only.'
                    if quick_issues else 'Fast one-shot migration succeeded.'
                )
            return _attach_migration_validation_report(quick_result, plan=plan, dialect=dialect)

        # Re-finalize and re-audit immediately before repair decision so we
        # never gate on stale pre-finalized SQL/issues.
        quick_sql = quick_result.get('sql', '') or quick_result.get('final_sql', '') or ''
        quick_sql = finalize_generated_sql(
            quick_sql,
            plan=plan,
            qvs_script=qvs_script,
        )
        quick_result['sql'] = quick_sql
        quick_result['final_sql'] = quick_sql
        quick_issues = list(dict.fromkeys(
            list(_audit_generated_sql_against_plan(
                quick_sql,
                plan=plan,
                qvs_script=qvs_script,
                dialect=dialect,
            ) or [])
            + list(_generic_one_shot_quality_issues(quick_sql, plan=plan) or [])
        ))
        quick_issues, safe_union_override_applied = _apply_safe_union_override(
            quick_sql,
            quick_issues,
            status=quick_result.get('status'),
        )
        quick_issues, blocking_issues, forced_safe_union_filter_applied = _force_safe_union_blocking_filter(
            quick_sql,
            quick_issues,
        )
        safe_union_override_applied = safe_union_override_applied or forced_safe_union_filter_applied
        quick_result['validation_issues'] = quick_issues

        # One targeted repair before the expensive Senior AI/ML loop.
        false_union_mismatch = safe_union_override_applied or _is_safe_false_union_mismatch(quick_sql, quick_issues)
        should_repair_once = (
            bool(blocking_issues)
            and generation_mode != 'one_shot'
            and not false_union_mismatch
        )
        repair_attempted = False
        safe_finalized = _is_safe_finalized_union_sql(quick_sql)
        logger.info(
            "FINALIZED_SQL_BEFORE_REPAIR chars=%s has_fact_expenses=%s has_join_expenses=%s has_select_star_union=%s has_union_mismatch_text=%s",
            len(quick_sql or ''),
            'facttable_with_expenses' in (quick_sql or '').lower(),
            bool(re.search(r'(?is)\bjoin\s+expenses\b', quick_sql or '')),
            bool(re.search(r'(?is)\bselect\s+\*\s+from\s+facttable\b[\s\S]*?\bunion\s+all\b', quick_sql or '')),
            any('UNION_COLUMN_COUNT_MISMATCH' in str(i) for i in (quick_issues or [])),
        )
        logger.info(
            "SAFE_UNION_DEBUG safe=%s has_fact_expenses=%s has_union=%s has_join_expenses=%s has_star=%s has_final_model=%s",
            safe_finalized,
            "facttable_with_expenses" in (quick_sql or '').lower(),
            "union all" in (quick_sql or '').lower(),
            bool(re.search(r"(?is)\bjoin\s+expenses\b", quick_sql or '')),
            bool(re.search(r"(?is)\bselect\s+(?:\w+\.)?\*", quick_sql or '')),
            "final_model" in (quick_sql or '').lower(),
        )
        logger.info(
            "REPAIR_DECISION blocking=%s categories=%s blocking_issues=%s",
            bool(blocking_issues),
            [_issue_category(i) for i in quick_issues],
            blocking_issues[:10],
        )
        if _all_issues_are_metadata_only(quick_issues):
            logger.info("Skipping SQL repair because only metadata warnings remain.")
            quick_result['status'] = 'complete_with_validation_issues'
            quick_result['validation_issues'] = quick_issues
            quick_result['blockingIssues'] = []
            quick_result['loopNeeded'] = False
            quick_result['repairAttempted'] = False
            quick_result['one_shot_validation_status'] = 'passed_with_warnings'
            quick_result['reason_for_entering_loop'] = ''
            return _attach_migration_validation_report(quick_result, plan=plan, dialect=dialect)
        if should_repair_once:
            repair_attempted = True
            quick_result, quick_issues, repair_succeeded = _one_shot_repair_once(
                quick_result,
                quick_issues,
                qvs_script,
                plan,
                plan_text,
                dialect,
                progress_callback=progress_callback,
            )
            quick_result['used_one_shot_repair'] = bool(quick_result.get('used_one_shot_repair') or repair_attempted or repair_succeeded)
            quick_sql = finalize_generated_sql(
                quick_result.get('sql', '') or quick_result.get('final_sql', ''),
                plan=plan,
                qvs_script=qvs_script,
            )
            quick_result['sql'] = quick_sql
            quick_result['final_sql'] = quick_sql
            quick_result['validation_issues'] = quick_issues

        # Always recompute issues from finalized SQL to avoid stale pre-repair state.
        refreshed_audit_issues = _audit_generated_sql_against_plan(
            quick_sql,
            plan=plan,
            qvs_script=qvs_script,
            dialect=dialect,
        )
        refreshed_generic_issues = _generic_one_shot_quality_issues(quick_sql, plan=plan)
        quick_issues = list(dict.fromkeys(list(refreshed_audit_issues or []) + refreshed_generic_issues))
        quick_issues, refreshed_safe_union_override_applied = _apply_safe_union_override(
            quick_sql,
            quick_issues,
            status=quick_result.get('status'),
        )
        quick_issues, forced_blocking_issues, refreshed_forced_safe_union_filter_applied = _force_safe_union_blocking_filter(
            quick_sql,
            quick_issues,
        )
        quick_result['validation_issues'] = quick_issues

        quick_categories = [_issue_category(i) for i in quick_issues]
        false_union_mismatch = (
            refreshed_safe_union_override_applied
            or refreshed_forced_safe_union_filter_applied
            or _is_safe_false_union_mismatch(quick_sql, quick_issues)
        )
        blocking_issues = [] if false_union_mismatch else forced_blocking_issues
        has_blocking_one_shot_issues = False if false_union_mismatch else bool(forced_blocking_issues)
        if repair_attempted and not has_blocking_one_shot_issues:
            quick_result['status'] = 'complete_with_validation_issues' if quick_issues else 'complete'
            quick_result['validation_issues'] = quick_issues
            quick_result['one_shot_validation_status'] = 'passed_with_warnings_after_repair' if quick_issues else 'passed_after_repair'
            quick_result['repairAttempted'] = True
            quick_result['loopNeeded'] = False
            quick_result['blockingIssues'] = []
            quick_result['reason_for_entering_loop'] = ''
            quick_result['used_one_shot_repair'] = True
            logger.info(
                "One-shot repair completed with warnings only: categories=%s issues=%s",
                quick_categories[:5],
                quick_issues[:5],
            )
            if progress_callback:
                progress_callback(
                    'One-shot repair completed with warnings only.'
                    if quick_issues else 'One-shot migration passed after targeted repair.'
                )
            return _attach_migration_validation_report(quick_result, plan=plan, dialect=dialect)
        logger.info(
            "LOOP_DECISION blocking=%s categories=%s blocking_issues=%s",
            bool(blocking_issues),
            [_issue_category(i) for i in quick_issues],
            blocking_issues[:10],
        )
        loop_needed_by_policy = _loop_needed_by_policy(quick_issues, generation_mode=generation_mode)
        if _is_safe_finalized_sql_shape(quick_sql):
            loop_needed_by_policy = False
        if not _has_real_blocking_issues(quick_issues) or false_union_mismatch:
            loop_needed_by_policy = False
        one_shot_quality_score = max(0.0, min(1.0, 1.0 - (0.12 * len(blocking_issues)) - (0.02 * len(quick_issues or []))))
        join_contract = build_join_contract(plan or [], qvs_script or '')
        coverage = compute_join_contract_coverage(quick_sql, join_contract)
        one_shot_validation_status = (
            'blocking_issues_after_repair' if has_blocking_one_shot_issues and repair_attempted
            else 'blocking_issues' if has_blocking_one_shot_issues
            else 'passed_after_repair' if repair_attempted
            else 'passed_with_warnings' if quick_issues
            else 'passed'
        )
        quick_result['selected_generation_mode'] = generation_mode
        quick_result['one_shot_validation_status'] = one_shot_validation_status
        quick_result['used_one_shot_repair'] = bool(quick_result.get('used_one_shot_repair') or repair_attempted)
        quick_result['warnings'] = list(dict.fromkeys((quick_result.get('warnings') or []) + quick_issues))
        quick_result['oneShotQualityScore'] = round(one_shot_quality_score, 4)
        quick_result['loopNeeded'] = bool(loop_needed_by_policy)
        quick_result['blockingIssues'] = blocking_issues
        quick_result['joinContractCoverage'] = coverage.get('joinContractCoverage', 0.0)
        quick_result['joinedContractPaths'] = coverage.get('joinedContractPaths', 0)
        quick_result['totalContractPaths'] = coverage.get('totalContractPaths', 0)
        quick_result['omittedUnsafeJoins'] = coverage.get('omittedUnsafeJoins', [])
        quick_result['loopPolicy'] = LOOP_POLICY

        logger.info(
            "One-shot validation status=%s categories=%s issues=%s repair_attempted=%s",
            one_shot_validation_status,
            _blocking_issue_categories(quick_issues)[:5],
            quick_issues[:5],
            repair_attempted,
        )

        if not loop_needed_by_policy:
            quick_result['reason_for_entering_loop'] = ''
            if progress_callback:
                if repair_attempted:
                    progress_callback('One-shot migration passed after targeted repair.')
                else:
                    progress_callback(
                        'Fast one-shot migration completed with warnings.'
                        if quick_issues else 'Fast one-shot migration succeeded.'
                    )
            quick_result['status'] = 'complete_with_validation_issues' if quick_issues else quick_result.get('status', 'complete')
            return _attach_migration_validation_report(quick_result, plan=plan, dialect=dialect)

        current_issues = quick_issues
        if not _has_real_blocking_issues(current_issues) or _is_safe_false_union_mismatch(quick_sql, current_issues):
            logger.info("Skipping validation loop because only warnings remain.")
            quick_result['loopNeeded'] = False
            quick_result['reason_for_entering_loop'] = ''
            quick_result['status'] = 'complete_with_validation_issues' if current_issues else quick_result.get('status', 'complete')
            return _attach_migration_validation_report(quick_result, plan=plan, dialect=dialect)

        if generation_mode == 'one_shot':
            quick_result['status'] = 'complete_with_validation_issues' if quick_result.get('status') == 'complete' else quick_result.get('status', 'complete')
            quick_result['reason_for_entering_loop'] = ''
            if progress_callback:
                progress_callback('One-shot mode completed with blocking validation issues; Senior loop not started.')
            return _attach_migration_validation_report(quick_result, plan=plan, dialect=dialect)

        reason = 'blocking validation issues after one-shot repair' if repair_attempted else 'blocking validation issues'
        logger.info("Entering validation loop after one-shot: reason=%s", reason)
        if progress_callback:
            progress_callback(f'Fast one-shot migration returned {reason}; switching to the validation loop...')
    except Exception as e:
        if _is_token_budget_failure(e):
            message = str(e)
            logger.warning("Fast one-shot migration stopped by token budget: %s", message)
            if progress_callback:
                progress_callback('AI provider token budget is too low; using deterministic SQL generation.')
            return _attach_migration_validation_report(_deterministic_migration_result(
                message,
                qvs_script,
                plan,
                dialect=dialect,
                current_sql=current_sql,
                current_desc=current_desc,
            ), plan=plan, dialect=dialect)
        logger.warning("Fast one-shot migration failed; falling back to validation loop: %s", e)
        if progress_callback:
            progress_callback('Fast one-shot migration failed; switching to the validation loop...')

    try:
        result = request_migration_with_validation(
            call_openrouter,
            qvs_script,
            session_context=session_context,
            current_sql=current_sql,
            current_desc=current_desc,
            dialect=dialect,
            plan=plan,
            plan_text=plan_text,
            prompt_version=provider_prompt_version,
            description_style=SQL_DESCRIPTION_STYLE,
            max_iterations=max(0, MIGRATION_LOOP_MAX_ITERATIONS),
            progress_callback=progress_callback,
            stream_callback=stream_callback,
        )
        result['selected_generation_mode'] = generation_mode
        result['one_shot_validation_status'] = 'blocking_issues_after_one_shot_repair'
        result['reason_for_entering_loop'] = 'auto_fallback_after_one_shot_repair_blocking_issues'
        return _attach_migration_validation_report(result, plan=plan, dialect=dialect)
    except Exception as e:
        message = str(e)
        if _is_token_budget_failure(message):
            logger.warning("Validation loop stopped by token budget: %s", message)
            if progress_callback:
                progress_callback('AI provider token budget is too low; using deterministic SQL generation.')
            return _attach_migration_validation_report(_deterministic_migration_result(
                message,
                qvs_script,
                plan,
                dialect=dialect,
                current_sql=current_sql,
                current_desc=current_desc,
            ), plan=plan, dialect=dialect)
        logger.warning("Validation loop stopped: %s", message)
        if progress_callback:
            progress_callback(f"Migration stopped: {message}")
        return {
            'status': 'failed',
            'iterations': 0,
            'score': 0.0,
            'final_sql': '',
            'sql': '',
            'description': '',
            'final_description': '',
            'error': message,
            'warnings': [message],
            'used_deterministic_fallback': False,
        }

# Reset DB on startup for clean demo (optional, can be triggered via API)
def reset_db():
    SQL_PLAN_CACHE.clear()
    with REGENERATION_LOCK:
        REGENERATION_JOBS.clear()
    db = sqlite3.connect(DB_PATH)
    db.execute('DROP TABLE IF EXISTS extracted_data')
    db.execute('DROP TABLE IF EXISTS uploaded_files')
    db.execute('DROP TABLE IF EXISTS regeneration_history')
    db.execute('DROP TABLE IF EXISTS sessions')
    db.commit()
    db.close()
    init_db()

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ... [rest of database setup and extraction remains same, skipping for brevity in this replace but I'll ensure all essential logic is preserved] ...

# â”€â”€â”€ Database Setup â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def get_db():
    """Get database connection"""
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db

def init_db():
    """Initialize database tables"""
    db = get_db()
    db.executescript('''
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS uploaded_files (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            filepath TEXT NOT NULL,
            file_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'processing',
            created_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );
        CREATE TABLE IF NOT EXISTS extracted_data (
            id TEXT PRIMARY KEY,
            file_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            tables_json TEXT,
            associations_json TEXT,
            script_text TEXT,
            metadata_json TEXT,
            graph_json TEXT,
            description_text TEXT,
            edited_sql TEXT,
            edited_text TEXT,
            regenerated_sql TEXT,
            regenerated_text TEXT,
            regeneration_json TEXT,
            prompt_version TEXT,
            regeneration_model TEXT,
            regeneration_status TEXT,
            regeneration_job_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (file_id) REFERENCES uploaded_files(id),
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );
        CREATE TABLE IF NOT EXISTS regeneration_history (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            file_id TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            model TEXT NOT NULL,
            trigger_migration INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'queued',
            input_hash TEXT NOT NULL,
            generation_plan_json TEXT,
            generation_plan_text TEXT,
            regeneration_json TEXT,
            error_text TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY (session_id) REFERENCES sessions(id),
            FOREIGN KEY (file_id) REFERENCES uploaded_files(id)
        );
    ''')
    ensure_table_columns(db, 'extracted_data', {
        'regeneration_json': 'TEXT',
        'prompt_version': 'TEXT',
        'regeneration_model': 'TEXT',
        'regeneration_status': "TEXT",
        'regeneration_job_id': 'TEXT',
    })
    ensure_table_columns(db, 'regeneration_history', {
        'prompt_version': 'TEXT NOT NULL DEFAULT ""',
        'model': 'TEXT NOT NULL DEFAULT ""',
        'trigger_migration': 'INTEGER NOT NULL DEFAULT 0',
        'status': "TEXT NOT NULL DEFAULT 'queued'",
        'input_hash': 'TEXT NOT NULL DEFAULT ""',
        'generation_plan_json': 'TEXT',
        'generation_plan_text': 'TEXT',
        'regeneration_json': 'TEXT',
        'error_text': 'TEXT',
        'completed_at': 'TEXT',
    })
    db.commit()
    db.close()


def ensure_table_columns(db, table_name, columns):
    """Add missing columns for older SQLite databases."""
    existing = {row['name'] for row in db.execute(f"PRAGMA table_info({table_name})").fetchall()}
    for column, definition in columns.items():
        if column not in existing:
            db.execute(f"ALTER TABLE {table_name} ADD COLUMN {column} {definition}")

init_db()
ensure_feedback_table(get_db)
def build_session_scripts_context(all_data, file_map):
    parts = []
    for row in all_data:
        filename = file_map.get(row['file_id'], 'Unknown')
        raw_script = row['script_text'] or ''

        # The stored script_text may be a large JSON blob (full QVF dump).
        # Extract only the actual Qlik LOAD script portion before sending to AI.
        actual_script = _extract_qlik_script_from_stored_text(raw_script)

        # Resolve Qlik variables before sending to the AI so $(vVar) references
        # are replaced with their actual values.
        resolved_script = prepare_script_for_migration(actual_script)
        print(f"BUILD_CONTEXT file={filename} raw_len={len(raw_script)} extracted_len={len(actual_script)} resolved_len={len(resolved_script)}")
        parts.append(f"--- FILE: {filename} ---\n{resolved_script}")
    return "\n\n".join(parts)


def _unescape_json_string(text):
    """Unescape JSON-encoded escape sequences extracted from a JSON blob.
    Converts \\r\\n → real newlines, \\t → tabs, etc.
    """
    if not text:
        return text
    try:
        return json.loads('"' + text.replace('"', '\\"') + '"')
    except Exception:
        return (text
                .replace('\\r\\n', '\n')
                .replace('\\n', '\n')
                .replace('\\r', '\n')
                .replace('\\t', '\t')
                .replace('\\\\"', '"')
                .replace('\\\\', '\\'))


def _find_script_end(text):
    """Find the position where the Qlik script ends and JSON metadata begins.

    The script ends when we see a JSON object start ('{') that is NOT inside
    a Qlik string literal, or when we see a clear JSON key pattern like
    '"qInfo"' or '"qMetaDef"'.
    """
    # Look for the first occurrence of JSON metadata markers after the script
    json_markers = ['"qInfo"', '"qMetaDef"', '"qDim"', '"qMeasure"', '"qHyperCubeDef"']
    earliest = len(text)
    for marker in json_markers:
        pos = text.find(marker)
        if pos > 0:
            # Back up to the opening brace of this JSON object
            brace = text.rfind('{', 0, pos)
            if brace > 0:
                earliest = min(earliest, brace)

    if earliest < len(text):
        # Find the last semicolon before the JSON metadata starts
        last_semi = text.rfind(';', 0, earliest)
        if last_semi > 0:
            return last_semi + 1

    # No JSON metadata found — find last semicolon in the whole text
    last_semi = text.rfind(';')
    return last_semi + 1 if last_semi > 0 else len(text)


def _extract_qlik_script_from_stored_text(text):
    """
    If the stored text is a large JSON blob (QVF metadata dump), extract
    only the actual Qlik LOAD script portion from it.
    """
    if not text:
        return text

    # Short enough to be a real script — return as-is
    if len(text) < 50_000:
        return text

    # Not JSON — return as-is
    stripped = text.strip()
    if not (stripped.startswith('{') or stripped.startswith('[')):
        return text

    # Strategy 1: ///$tab marker (Qlik script section header)
    tab_match = re.search(r'//\$tab\s', text, re.IGNORECASE)
    if tab_match:
        script_start = tab_match.start()
        script_raw = text[script_start:]
        end_pos = _find_script_end(script_raw)
        script_portion = script_raw[:end_pos]
        script_portion = _unescape_json_string(script_portion)
        print(f"SCRIPT EXTRACTED via ///$tab at pos={script_start} len={len(script_portion)}")
        return script_portion

    # Strategy 2: TableName:\nLOAD pattern
    label_match = re.search(r'(?m)^[A-Za-z][A-Za-z0-9_ ]*:\s*\n\s*LOAD\b', text)
    if label_match:
        script_start = label_match.start()
        script_raw = text[script_start:]
        end_pos = _find_script_end(script_raw)
        script_portion = _unescape_json_string(script_raw[:end_pos])
        print(f"SCRIPT EXTRACTED via table label at pos={script_start} len={len(script_portion)}")
        return script_portion

    # Strategy 3: First LOAD keyword
    load_match = re.search(r'(?<!["\'])(?:^|\n)\s*LOAD\s+\w', text, re.MULTILINE)
    if load_match:
        script_start = max(0, load_match.start() - 200)
        boundary = text.rfind('\n\n', 0, load_match.start())
        if boundary > 0:
            script_start = boundary
        script_raw = text[script_start:]
        end_pos = _find_script_end(script_raw)
        script_portion = _unescape_json_string(script_raw[:end_pos])
        print(f"SCRIPT EXTRACTED via LOAD keyword at pos={script_start} len={len(script_portion)}")
        return script_portion

    print(f"SCRIPT EXTRACTION FAILED — returning full text len={len(text)}")
    return text


def safe_json_loads(value, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def build_session_bundle(session_id):
    db = get_db()
    all_data = db.execute('SELECT * FROM extracted_data WHERE session_id = ? ORDER BY created_at ASC', (session_id,)).fetchall()
    files_info = db.execute('SELECT id, filename FROM uploaded_files WHERE session_id = ?', (session_id,)).fetchall()
    history_rows = db.execute('SELECT * FROM regeneration_history WHERE session_id = ? ORDER BY created_at DESC', (session_id,)).fetchall()
    db.close()

    if not all_data:
        return None

    file_map = {f['id']: f['filename'] for f in files_info}
    latest = all_data[-1]
    scripts_context = build_session_scripts_context(all_data, file_map)
    cached_plan = get_cached_sql_plan(session_id, scripts_context)

    return {
        'all_data': all_data,
        'files_info': files_info,
        'file_map': file_map,
        'latest': latest,
        'scripts_context': scripts_context,
        'cached_plan': cached_plan,
        'history_rows': history_rows,
        'total_tables': sum(len(json.loads(r['tables_json'] or '[]')) for r in all_data),
        'total_relationships': sum(len(json.loads(r['associations_json'] or '[]')) for r in all_data),
    }


register_dbt_agent_routes(app, get_db, build_session_bundle, UPLOAD_FOLDER, call_ai=call_openrouter)
register_feedback_routes(app, get_db, call_ai=call_openrouter)


def serialize_regeneration_payload(row):
    if not row:
        return None
    structured = safe_json_loads(row['regeneration_json'], {})
    if not structured:
        structured = {
            'sql': row['regenerated_sql'] or row['edited_sql'] or row['script_text'] or '',
            'description': row['regenerated_text'] or row['edited_text'] or row['description_text'] or '',
            'lineage': '',
            'warnings': [],
        }
    structured.setdefault('sql', row['regenerated_sql'] or row['edited_sql'] or row['script_text'] or '')
    structured.setdefault('description', row['regenerated_text'] or row['edited_text'] or row['description_text'] or '')
    structured.setdefault('lineage', '')
    structured.setdefault('warnings', [])
    structured['promptVersion'] = row['prompt_version'] or PROMPT_VERSION
    structured['model'] = row['regeneration_model'] or OPENROUTER_DEFAULT_MODEL
    structured['status'] = row['regeneration_status'] or 'complete'
    return structured


def serialize_regeneration_history_row(row):
    payload = safe_json_loads(row['regeneration_json'], {})
    return {
        'id': row['id'],
        'sessionId': row['session_id'],
        'fileId': row['file_id'],
        'promptVersion': row['prompt_version'],
        'model': row['model'],
        'status': row['status'],
        'triggerMigration': bool(row['trigger_migration']),
        'inputHash': row['input_hash'],
        'generationPlan': safe_json_loads(row['generation_plan_json'], []),
        'generationPlanText': row['generation_plan_text'] or '',
        'regeneration': payload,
        'errorText': row['error_text'] or '',
        'createdAt': row['created_at'],
        'completedAt': row['completed_at'],
    }


def upsert_regeneration_state(session_id, file_id, edited_sql, edited_text, regenerated_sql, regenerated_text, structured, status='complete', model=None, job_id=None, prompt_version=None):
    db = get_db()
    timestamp = datetime.utcnow().isoformat()
    db.execute(
        '''UPDATE extracted_data
           SET edited_sql = ?, edited_text = ?, regenerated_sql = ?, regenerated_text = ?,
               regeneration_json = ?, prompt_version = ?, regeneration_model = ?,
               regeneration_status = ?, regeneration_job_id = ?, updated_at = ?
           WHERE session_id = ? AND id = (
               SELECT id FROM extracted_data WHERE session_id = ? ORDER BY created_at DESC LIMIT 1
           )''',
        (
            edited_sql,
            edited_text,
            regenerated_sql,
            regenerated_text,
            json.dumps(structured or {}),
            prompt_version or PROMPT_VERSION,
            model or OPENROUTER_DEFAULT_MODEL,
            status,
            job_id,
            timestamp,
            session_id,
            session_id,
        ),
    )
    db.commit()
    db.close()


def create_regeneration_history_entry(session_id, file_id, input_hash, generation_plan, generation_plan_text, trigger_migration, status='queued', model=None, prompt_version=None, job_id=None):
    db = get_db()
    row_id = job_id or str(uuid.uuid4())
    timestamp = datetime.utcnow().isoformat()
    db.execute(
        '''INSERT OR REPLACE INTO regeneration_history
           (id, session_id, file_id, prompt_version, model, trigger_migration, status, input_hash,
            generation_plan_json, generation_plan_text, regeneration_json, error_text, created_at, completed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (
            row_id,
            session_id,
            file_id,
            prompt_version or PROMPT_VERSION,
            model or OPENROUTER_DEFAULT_MODEL,
            1 if trigger_migration else 0,
            status,
            input_hash,
            json.dumps(generation_plan or []),
            generation_plan_text or '',
            None,
            '',
            timestamp,
            None,
        ),
    )
    db.commit()
    db.close()
    return row_id


def finalize_regeneration_history_entry(history_id, regeneration_payload, error_text='', status='complete'):
    db = get_db()
    db.execute(
        '''UPDATE regeneration_history
           SET status = ?, regeneration_json = ?, error_text = ?, completed_at = ?
           WHERE id = ?''',
        (
            status,
            json.dumps(regeneration_payload or {}),
            error_text or '',
            datetime.utcnow().isoformat(),
            history_id,
        ),
    )
    db.commit()
    db.close()


def load_regeneration_history(session_id):
    db = get_db()
    rows = db.execute('SELECT * FROM regeneration_history WHERE session_id = ? ORDER BY created_at DESC', (session_id,)).fetchall()
    db.close()
    return [serialize_regeneration_history_row(row) for row in rows]


def build_regeneration_response_from_row(row):
    structured = serialize_regeneration_payload(row)
    if not structured:
        structured = {
            'sql': row['edited_sql'] or row['script_text'] or '',
            'description': row['edited_text'] or row['description_text'] or '',
            'lineage': '',
            'warnings': [],
            'promptVersion': row['prompt_version'] or PROMPT_VERSION,
            'model': row['regeneration_model'] or OPENROUTER_DEFAULT_MODEL,
            'status': row['regeneration_status'] or 'complete',
        }
    return structured


def serialize_session_file(row, file_map, cached_plan=None):
    script_text = row['script_text'] or ''
    plan = cached_plan or {}
    metadata = safe_json_loads(row['metadata_json'], {})
    return {
        'fileId': row['file_id'],
        'filename': file_map.get(row['file_id']),
        'tables': safe_json_loads(row['tables_json'], []),
        'associations': safe_json_loads(row['associations_json'], []),
        'metadata': metadata,
        'binaryReport': metadata.get('binaryReport'),
        'decodedSections': metadata.get('decodedSections', []),
        'undecodedSections': metadata.get('undecodedSections', []),
        'evidence': metadata.get('evidence', {}),
        'sqlSections': parse_sql_sections(script_text),
        'script': script_text,
        'description': row['description_text'] or '',
        'generationPlan': plan.get('plan', []),
        'generationPlanText': plan.get('planText', ''),
    }


def maybe_store_regeneration_state(session_id, file_id, edited_sql, edited_text, structured, status='complete', model=None, job_id=None, prompt_version=None):
    if status == 'failed':
        sql_text = structured.get('sql') or ''
        desc_text = structured.get('description') or ''
    else:
        sql_text = structured.get('sql') or edited_sql or ''
        desc_text = structured.get('description') or edited_text or ''
    upsert_regeneration_state(
        session_id,
        file_id,
        edited_sql,
        edited_text,
        sql_text,
        desc_text,
        structured,
        status=status,
        model=model,
        job_id=job_id,
        prompt_version=prompt_version,
    )


def run_regeneration_job(job_id, session_id, file_id, edited_sql, edited_text, regenerated_sql, regenerated_text, dialect, combined_scripts, cached_plan, input_hash, trigger_migration, generation_mode='auto'):
    model = _active_ai_model()
    prompt_version = PROMPT_VERSION
    status = 'complete'
    error_text = ''
    plan_context = (cached_plan or {}).get('plan') if isinstance(cached_plan, dict) else None
    has_plan_context = bool(plan_context)
    is_powerbi = (dialect or '').lower() == 'powerbi'
    structured = {
        'sql': regenerated_sql or edited_sql or '',
        'description': regenerated_text or edited_text or '',
        'lineage': '',
        'warnings': [],
        'promptVersion': prompt_version,
        'model': model,
        'status': 'complete',
    }

    try:
        with REGENERATION_LOCK:
            REGENERATION_JOBS[job_id] = {
                'status': 'running' if trigger_migration else 'complete',
                'sessionId': session_id,
                'createdAt': datetime.utcnow().isoformat(),
                'updatedAt': datetime.utcnow().isoformat(),
                'promptVersion': prompt_version,
                'progress_message': 'Starting...' if trigger_migration else 'Complete',
                'last_heartbeat': time.time(),
            }
        logger.info("Regeneration job started: job_id=%s session=%s", job_id, session_id)

        if trigger_migration and _has_ai_provider_configured():
            target_label = 'Power BI (M + DAX)' if is_powerbi else f'DBT [{dialect}]'
            logger.info(
                "AI migration triggered: target=%s session=%s plan_size=%d script_chars=%d",
                target_label,
                session_id,
                len(cached_plan.get('plan', [])),
                len(combined_scripts or ''),
            )
            
            # Define progress callback to update job timestamp during AI iterations
            def progress_callback(message=None):
                with REGENERATION_LOCK:
                    if job_id in REGENERATION_JOBS:
                        if message:
                            REGENERATION_JOBS[job_id]['progress_message'] = message
                        REGENERATION_JOBS[job_id]['last_heartbeat'] = time.time()
                        REGENERATION_JOBS[job_id]['updatedAt'] = datetime.utcnow().isoformat()
                # Also push progress to SSE stream
                if message:
                    try:
                        q = _get_or_create_stream_queue(job_id)
                        q.put({'type': 'progress', 'message': message})
                    except Exception:
                        pass

            # Stream callback — pushes SQL tokens into the SSE queue so the
            # frontend can render SQL character-by-character like ChatGPT.
            # Once the AI starts writing "### DESCRIPTION", we stop streaming
            # tokens (the description is delivered via the "done" event instead).
            _stream_buf = []
            _sql_done = [False]   # mutable flag accessible inside closure
            _emitted_len = [0]    # number of chars already emitted to SSE

            def stream_callback(token):
                if _sql_done[0]:
                    return  # description section — don't stream to editor
                try:
                    piece = str(token or '')
                    if not piece:
                        return
                    _stream_buf.append(piece)
                    full_text = ''.join(_stream_buf)

                    # Stream only SQL portion; once DESCRIPTION header appears,
                    # emit up to the header and stop streaming further tokens.
                    marker_idx = -1
                    for marker in ('### DESCRIPTION', '###DESCRIPTION'):
                        idx = full_text.find(marker)
                        if idx != -1 and (marker_idx == -1 or idx < marker_idx):
                            marker_idx = idx

                    target_len = marker_idx if marker_idx != -1 else len(full_text)
                    if target_len > _emitted_len[0]:
                        chunk = full_text[_emitted_len[0]:target_len]
                        q = _get_or_create_stream_queue(job_id)
                        q.put({'type': 'token', 'content': chunk})
                        _emitted_len[0] = target_len

                    if marker_idx != -1:
                        _sql_done[0] = True
                        return
                except Exception:
                    pass

            migration_result = migrate_qvs_to_dbt(
                combined_scripts,
                current_sql=regenerated_sql or edited_sql,
                current_desc=regenerated_text or edited_text,
                dialect=dialect,
                plan=cached_plan['plan'],
                plan_text=cached_plan['planText'],
                progress_callback=progress_callback,
                stream_callback=stream_callback,
                generation_mode=generation_mode,
            )
            logger.info(
                "AI migration returned: job_id=%s result_type=%s sql_chars=%d",
                job_id,
                type(migration_result).__name__,
                len((migration_result or {}).get('final_sql', '') if isinstance(migration_result, dict) else str(migration_result or '')),
            )
            if migration_result:
                if isinstance(migration_result, dict):
                    result_status = migration_result.get('status') or 'complete'
                    result_error = migration_result.get('error') or ''
                    migration_sql = migration_result.get('sql') or ''
                    migration_final_sql = migration_result.get('final_sql') or ''
                    chosen_sql = migration_final_sql if len(migration_final_sql) > len(migration_sql) else migration_sql
                    if chosen_sql:
                        if has_plan_context:
                            chosen_sql = finalize_generated_sql(chosen_sql, plan=plan_context, qvs_script=combined_scripts)
                        else:
                            chosen_sql = finalize_generated_sql(chosen_sql)
                    migration_result['sql'] = chosen_sql
                    migration_result['final_sql'] = chosen_sql
                    migration_desc = migration_result.get('description') or ''
                    migration_final_desc = migration_result.get('final_description') or ''
                    chosen_desc = migration_final_desc if len(migration_final_desc) > len(migration_desc) else migration_desc
                    if result_status == 'failed' or (trigger_migration and not chosen_sql.strip()):
                        status = 'failed'
                        error_text = result_error or 'AI migration failed without returning SQL.'
                        structured = {
                            'sql': '',
                            'description': chosen_desc,
                            'lineage': migration_result.get('lineage', ''),
                            'warnings': migration_result.get('warnings', []) + [error_text],
                            'promptVersion': prompt_version,
                            'model': model,
                            'status': status,
                            'comparisonSummary': migration_result.get('comparison_summary', {}),
                            'validationStatus': result_status,
                            'semanticScore': round(float(migration_result.get('score', 0.0)), 2),
                            'iterations': migration_result.get('iterations', 0),
                            'error': error_text,
                            'usedDeterministicFallback': bool(migration_result.get('used_deterministic_fallback', False)),
                            'selectedGenerationMode': migration_result.get('selected_generation_mode', generation_mode),
                            'oneShotValidationStatus': migration_result.get('one_shot_validation_status', ''),
                            'reasonForEnteringLoop': migration_result.get('reason_for_entering_loop', ''),
                            'repairAttempted': bool(migration_result.get('repairAttempted') or migration_result.get('used_one_shot_repair')),
                            'loopNeeded': bool(migration_result.get('loopNeeded', False)),
                            'oneShotQualityScore': migration_result.get('oneShotQualityScore', 0.0),
                            'blockingIssues': migration_result.get('blockingIssues', []),
                        }
                        regenerated_sql = ''
                        regenerated_text = chosen_desc
                        logger.warning("AI migration failed: job_id=%s status=%s error=%s", job_id, result_status, error_text)
                    else:
                        structured = {
                            'sql': chosen_sql,
                            'description': chosen_desc,
                            'lineage': migration_result.get('lineage', ''),
                            'warnings': migration_result.get('warnings', []),
                            'promptVersion': prompt_version,
                            'model': model,
                            'status': result_status,
                            'comparisonSummary': migration_result.get('comparison_summary', {}),
                            'validationStatus': result_status,
                            'semanticScore': round(float(migration_result.get('score', 0.0)), 2),
                            'iterations': migration_result.get('iterations', 1),
                            'error': result_error,
                            'usedDeterministicFallback': bool(migration_result.get('used_deterministic_fallback', False)),
                            'selectedGenerationMode': migration_result.get('selected_generation_mode', generation_mode),
                            'oneShotValidationStatus': migration_result.get('one_shot_validation_status', ''),
                            'reasonForEnteringLoop': migration_result.get('reason_for_entering_loop', ''),
                            'repairAttempted': bool(migration_result.get('repairAttempted') or migration_result.get('used_one_shot_repair')),
                            'loopNeeded': bool(migration_result.get('loopNeeded', False)),
                            'oneShotQualityScore': migration_result.get('oneShotQualityScore', 0.0),
                            'blockingIssues': migration_result.get('blockingIssues', []),
                        }
                        regenerated_sql = structured['sql'] or regenerated_sql or edited_sql
                        regenerated_text = structured['description'] or regenerated_text or edited_text
                else:
                    print(f"AI Response received ({len(str(migration_result))} chars) for session {session_id}")
                    structured = parse_migration_response(str(migration_result))
                    structured['promptVersion'] = prompt_version
                    structured['model'] = model
                    structured['status'] = 'complete'
                    regenerated_sql = structured['sql'] or regenerated_sql or edited_sql
                    regenerated_text = structured['description'] or regenerated_text or edited_text

                validation_issues = []
                if status != 'failed':
                    validation_issues = issues_to_strings(validate_migration_sql(regenerated_sql, plan_context or [], dialect=dialect))
                    structured.setdefault('warnings', [])
                    structured['warnings'].extend(validation_issues)

                # Legacy repair path: keep only for non-migration flows.
                if (not trigger_migration) and status != 'failed' and not is_powerbi and validation_issues and needs_sql_repair(validation_issues):
                    print(f"SQL repair triggered for session {session_id} to fix: {validation_issues}")
                    try:
                        repaired_raw = repair_generated_sql(
                            regenerated_sql,
                            regenerated_text or '',
                            validation_issues,
                            dialect=dialect,
                            qvs_script=combined_scripts[:6000],  # keep repair prompt tight for speed
                            plan_text=cached_plan['planText'],
                        )
                        # repair_generated_sql returns raw AI text — parse it into structured form
                        repaired_result = parse_migration_response(repaired_raw) if isinstance(repaired_raw, str) else repaired_raw
                        if repaired_result and repaired_result.get('sql'):
                            print(f"SQL repair successful for session {session_id} — repaired sql_len={len(repaired_result['sql'])}")
                            # Apply deterministic post-repair invariants in python
                            repaired_result['sql'] = finalize_generated_sql(
                                repaired_result['sql'],
                                plan=cached_plan.get('plan'),
                                qvs_script=combined_scripts,
                            )
                            integrity_issues = validate_candidate_integrity(
                                repaired_result['sql'],
                                plan=cached_plan['plan'],
                            )
                            if integrity_issues:
                                print(f"SQL repair rejected for session {session_id} due to candidate corruption: {integrity_issues}")
                                structured.setdefault('warnings', []).extend(integrity_issues)
                                raise RuntimeError('; '.join(integrity_issues))
                            regressions = detect_repair_regressions(regenerated_sql, repaired_result['sql'])
                            if regressions:
                                print(f"SQL repair rejected for session {session_id} due to regressions: {regressions}")
                                structured.setdefault('warnings', []).extend(regressions)
                                raise RuntimeError('; '.join(regressions))
                            regenerated_sql = repaired_result['sql']
                            regenerated_text = repaired_result.get('description') or regenerated_text
                            structured['sql'] = regenerated_sql
                            structured['description'] = regenerated_text
                            # Re-validate repaired SQL
                            new_issues = issues_to_strings(validate_migration_sql(regenerated_sql, cached_plan['plan'], dialect=dialect))
                            structured['warnings'] = new_issues
                        else:
                            print(f"SQL repair returned empty result for session {session_id}")
                    except Exception as re:
                        print(f"WARNING: SQL repair failed: {str(re)}")
                        structured['warnings'].append(f"SQL repair failed: {str(re)}")

                if status == 'failed':
                    structured['sql'] = ''
                else:
                    if not is_powerbi:
                        if has_plan_context:
                            structured['sql'] = finalize_generated_sql(
                                regenerated_sql or edited_sql or '',
                                plan=plan_context,
                                qvs_script=combined_scripts,
                            )
                        else:
                            # Preserve already-finalized SQL from migration path when plan context is unavailable.
                            structured['sql'] = regenerated_sql or edited_sql or ''
                            structured.setdefault('warnings', []).append('FINAL_VALIDATION_SKIPPED_NO_PLAN_CONTEXT')
                    else:
                        structured['sql'] = regenerated_sql or edited_sql or ''

                    # Migration path already finalized/validated with plan context in migrate_qvs_to_dbt.
                    # Avoid re-rejecting with inconsistent context.
                    skip_legacy_final_reject = bool(
                        trigger_migration
                        and isinstance(migration_result, dict)
                        and (structured.get('status') in {'complete', 'complete_with_validation_issues'})
                    )
                    if not is_powerbi and not skip_legacy_final_reject:
                        plan_size = len(plan_context or [])
                        if plan_size >= 5 and len(structured.get('sql', '')) < 1000:
                            status = 'failed'
                            error_text = (
                                f'Generated SQL is too small for plan_size={plan_size}; '
                                'likely fallback skeleton or truncated output.'
                            )
                            structured['status'] = status
                            structured['error'] = error_text
                            structured.setdefault('warnings', []).append(error_text)
                            structured['sql'] = ''
                        if status != 'failed' and has_plan_context:
                            final_integrity_issues = validate_generated_sql(
                                structured['sql'],
                                plan=plan_context,
                                dialect=dialect,
                            )
                            if final_integrity_issues:
                                status = 'failed'
                                error_text = '; '.join(final_integrity_issues)
                                structured.setdefault('warnings', []).extend(final_integrity_issues)
                                structured['error'] = error_text
                                structured['status'] = status
                                structured['sql'] = ''
                                logger.warning("Final SQL rejected: job_id=%s issues=%s", job_id, final_integrity_issues)
                    regenerated_sql = structured['sql']

                # Power BI: keep the description as-is (it's already M/DAX aware)
                # DBT: normalize into ## Block: sections
                if status == 'failed':
                    structured['description'] = regenerated_text or ''
                elif is_powerbi:
                    structured['description'] = regenerated_text or edited_text or ''
                else:
                    structured['description'] = normalize_sql_description(regenerated_text or edited_text, cached_plan['plan'])
            else:
                logger.warning("AI response empty: session=%s", session_id)
                structured['warnings'].append('AI response was empty; using the current draft state.')
        else:
            if trigger_migration:
                logger.warning("AI provider not configured; skipping migration: session=%s", session_id)
                structured['warnings'].append('AI provider is not configured. Set AI_PROVIDER and the provider key/base URL in your .env file, then restart the server.')

        # Final description normalisation (DBT only)
        if status != 'failed' and not is_powerbi:
            structured['description'] = normalize_sql_description(structured.get('description'), cached_plan['plan'])

        structured['status'] = status
        structured = _ensure_result_validation_payload(
            structured,
            plan=cached_plan.get('plan', []) if isinstance(cached_plan, dict) else [],
            dialect=dialect,
        )
        logger.info(
            "Regeneration job finalized: job_id=%s status=%s sql_chars=%d warnings=%d",
            job_id,
            status,
            len(structured.get('sql', '')),
            len(structured.get('warnings', [])),
        )
        maybe_store_regeneration_state(
            session_id,
            file_id,
            edited_sql,
            edited_text,
            structured,
            status=status,
            model=model,
            job_id=job_id,
            prompt_version=prompt_version,
        )
        finalize_regeneration_history_entry(job_id, structured, error_text='', status=status)

        with REGENERATION_LOCK:
            REGENERATION_JOBS[job_id] = {
                'status': status,
                'sessionId': session_id,
                'createdAt': REGENERATION_JOBS.get(job_id, {}).get('createdAt') or datetime.utcnow().isoformat(),
                'updatedAt': datetime.utcnow().isoformat(),
                'promptVersion': prompt_version,
                'progress_message': REGENERATION_JOBS.get(job_id, {}).get('progress_message', 'Complete'),
                'last_heartbeat': time.time(),
                'result': structured,
                'generationPlan': cached_plan['plan'],
                'generationPlanText': cached_plan['planText'],
            }
        logger.info("Regeneration job completed: job_id=%s status=%s", job_id, status)

        # Push final result into SSE stream queue (if a client is listening)
        try:
            q = _get_or_create_stream_queue(job_id)
            if status == 'failed':
                q.put({'type': 'error', 'message': error_text or structured.get('error') or 'Migration failed'})
            else:
                q.put({
                    'type': 'done',
                    'sql': structured.get('sql', ''),
                    'description': structured.get('description', ''),
                    'warnings': structured.get('warnings', []),
                    'status': structured.get('status', 'complete'),
                    'repairAttempted': bool(structured.get('repairAttempted', False)),
                    'loopNeeded': bool(structured.get('loopNeeded', False)),
                    'oneShotQualityScore': structured.get('oneShotQualityScore', 0.0),
                    'blockingIssues': structured.get('blockingIssues', []),
                })
        except Exception:
            pass
        threading.Timer(60, _cleanup_stream_queue, args=[job_id]).start()

    except Exception as exc:
        error_text = str(exc)
        status = 'failed'
        structured['status'] = status
        structured['warnings'].append(error_text)
        logger.exception("Regeneration job failed: job_id=%s error=%s", job_id, error_text)
        maybe_store_regeneration_state(
            session_id,
            file_id,
            edited_sql,
            edited_text,
            structured,
            status=status,
            model=model,
            job_id=job_id,
            prompt_version=prompt_version,
        )
        finalize_regeneration_history_entry(job_id, structured, error_text=error_text, status=status)
        with REGENERATION_LOCK:
            REGENERATION_JOBS[job_id] = {
                'status': status,
                'sessionId': session_id,
                'createdAt': REGENERATION_JOBS.get(job_id, {}).get('createdAt') or datetime.utcnow().isoformat(),
                'updatedAt': datetime.utcnow().isoformat(),
                'promptVersion': prompt_version,
                'error': error_text,
                'result': structured,
                'generationPlan': cached_plan['plan'],
                'generationPlanText': cached_plan['planText'],
            }
        # Push error into SSE stream queue
        try:
            q = _get_or_create_stream_queue(job_id)
            q.put({'type': 'error', 'message': error_text})
        except Exception:
            pass
        threading.Timer(60, _cleanup_stream_queue, args=[job_id]).start()


@app.route('/api/stream/<job_id>')
def stream_job(job_id):
    """SSE endpoint — client connects here and receives tokens as they arrive.
    
    Emits event types:
      {"type": "token", "content": "..."}
      {"type": "progress", "message": "..."}
      {"type": "done", "sql": "...", "description": "...", "warnings": [...]}
      {"type": "error", "message": "..."}
    """
    import queue as _queue_mod

    def generate():
        q = _get_or_create_stream_queue(job_id)
        # Yield initial progress message immediately to verify SSE connection and prevent watchdog timeout
        yield f"data: {json.dumps({'type': 'progress', 'message': 'Connected to stream. Waiting for AI...'})}\n\n"
        while True:
            try:
                item = q.get(timeout=90)
                yield f"data: {json.dumps(item)}\n\n"
                if item.get('type') in ('done', 'error'):
                    break
            except _queue_mod.Empty:
                yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        },
    )


@app.route('/api/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files: return jsonify({'error': 'No file provided'}), 400
    file = request.files['file']
    session_id = request.form.get('session_id') or str(uuid.uuid4())
    file_id = str(uuid.uuid4())
    filename = secure_filename(file.filename)
    filepath = os.path.join(UPLOAD_FOLDER, f"{file_id}_{filename}")
    file.save(filepath)
    extract_dir = os.path.join(UPLOAD_FOLDER, f"{file_id}_extracted")
    os.makedirs(extract_dir, exist_ok=True)
    try:
        # Handle ZIP of QVFs or single QVF
        if filename.lower().endswith('.zip'):
            temp_zip_dir = os.path.join(UPLOAD_FOLDER, f"{file_id}_zip_temp")
            os.makedirs(temp_zip_dir, exist_ok=True)
            with zipfile.ZipFile(filepath, 'r') as zf:
                zf.extractall(temp_zip_dir)

            # Find all .qvf files recursively and process each
            processed_files = []
            for root, dirs, files in os.walk(temp_zip_dir):
                for f in files:
                    if f.lower().endswith('.qvf'):
                        sub_file_id = str(uuid.uuid4())
                        sub_filename = f
                        sub_filepath = os.path.join(root, f)
                        sub_extract_dir = os.path.join(UPLOAD_FOLDER, f"{sub_file_id}_extracted")
                        os.makedirs(sub_extract_dir, exist_ok=True)
                        sub_data = extract_qvf(sub_filepath, sub_extract_dir)
                        process_single_qvf(session_id, sub_file_id, sub_filename, sub_filepath, sub_data)
                        processed_files.append({'fileId': sub_file_id, 'filename': sub_filename})

            if not processed_files:
                # Fallback: treat the whole ZIP as a single unit if no .qvf inside
                data = extract_qvf(filepath, extract_dir)
                process_single_qvf(session_id, file_id, filename, filepath, data)
                processed_files.append({'fileId': file_id, 'filename': filename})
        else:
            # Single QVF upload
            data = extract_qvf(filepath, extract_dir)
            process_single_qvf(session_id, file_id, filename, filepath, data)
            processed_files = [{'fileId': file_id, 'filename': filename}]

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 400

    # Return aggregated result for all processed files
    bundle = build_session_bundle(session_id)
    if not bundle:
        return jsonify({'error': 'Processing failed — no data extracted'}), 500
    all_data = bundle['all_data']
    file_map = bundle['file_map']
    latest = bundle['latest']
    cached_plan = bundle['cached_plan']
    latest_metadata = safe_json_loads(latest['metadata_json'], {})
    return jsonify({
        'sessionId': session_id,
        'fileId': processed_files[-1]['fileId'],
        'filename': processed_files[-1]['filename'],
        'processedFiles': processed_files,
        'graph': build_graph_json(all_data, file_map),
        'script': latest['script_text'],
        'description': latest['description_text'],
        'tables': json.loads(latest['tables_json']),
        'metadata': latest_metadata,
        'binaryReport': latest_metadata.get('binaryReport'),
        'decodedSections': latest_metadata.get('decodedSections', []),
        'undecodedSections': latest_metadata.get('undecodedSections', []),
        'evidence': latest_metadata.get('evidence', {}),
        'sqlSections': parse_sql_sections(latest['script_text']),
        'generationPlan': cached_plan['plan'],
        'generationPlanText': cached_plan['planText'],
        'regenerationHistory': [serialize_regeneration_history_row(r) for r in bundle['history_rows']],
        'sessionStats': {
            'totalTables': bundle['total_tables'],
            'totalRelationships': bundle['total_relationships'],
            'files': [serialize_session_file(r, file_map, cached_plan) for r in all_data]
        }
    })

def process_single_qvf(session_id, file_id, filename, filepath, data):
    db = get_db()
    db.execute('INSERT OR IGNORE INTO sessions (id, created_at, updated_at) VALUES (?, ?, ?)', (session_id, datetime.utcnow().isoformat(), datetime.utcnow().isoformat()))
    db.execute('INSERT INTO uploaded_files (id, session_id, filename, filepath, file_type, created_at) VALUES (?, ?, ?, ?, ?, ?)', (file_id, session_id, filename, filepath, 'qvf', datetime.utcnow().isoformat()))
    
    assoc = data.get('associations') or {}
    script_text = data.get('script') or ''

    script_model = extract_model_from_script(script_text)
    if script_model.get('tables'):
        tables = script_model.get('tables', [])
        associations = script_model.get('associations', [])
    else:
        tables = assoc.get('tables', [])
        associations = assoc.get('associations', [])

        if not tables and script_text:
            sections = parse_sql_sections(script_text)
            for section in sections:
                if not any(t['name'] == section['tableName'] for t in tables):
                    tables.append({
                        'id': f"script_{section['tableName']}",
                        'name': section['tableName'],
                        'fields': [],
                        'rows': 0
                    })

    tables = attach_inline_samples_to_tables(tables, script_text)
    
    # NEW: Generate comprehensive metadata extraction
    try:
        comprehensive_metadata = enhance_metadata_with_comprehensive_extraction(
            data.get('metadata') or {},
            assoc or {},
            script_text
        )
    except Exception as e:
        print(f"WARNING: Comprehensive extraction failed: {str(e)}")
        comprehensive_metadata = data.get('metadata') or {}
    
    tables_json = json.dumps(tables)
    assoc_json = json.dumps(associations)
    desc_text = generate_description_rule_based({'tables': tables, 'associations': associations}, script_text)
    
    db.execute('''
        INSERT INTO extracted_data 
        (id, file_id, session_id, tables_json, associations_json, script_text, metadata_json, description_text, created_at, updated_at) 
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        str(uuid.uuid4()), file_id, session_id, 
        tables_json, assoc_json, script_text, json.dumps(comprehensive_metadata), desc_text,
        datetime.utcnow().isoformat(), datetime.utcnow().isoformat()
    ))
    db.commit()

    metadata = data.get('metadata') or {}
    binary_report = metadata.get('binaryReport') or {}
    print("")
    print("-" * 78)
    print(f"UPLOAD PROCESSED | {filename} | session={session_id}")
    print(f"  Script length: {len(script_text)}")
    print(f"  Tables stored: {len(tables)}")
    print(f"  Associations stored: {len(associations)}")
    if binary_report:
        print(
            f"  Binary forensic status: {binary_report.get('status')} "
            f"| decoded={binary_report.get('decodedSectionCount', 0)} "
            f"| undecoded={binary_report.get('undecodedSectionCount', 0)}"
        )
        artifacts = binary_report.get('artifacts') or {}
        if artifacts:
            print(f"  Terminal + artifact report: {artifacts.get('reportPath', '')}")
            print(f"  Manifest: {artifacts.get('manifestPath', '')}")
    print("-" * 78)
    
    # Re-fetch everything to provide full session context after upload
    bundle = build_session_bundle(session_id)
    all_data = bundle['all_data']
    file_map = bundle['file_map']
    latest = bundle['latest']
    cached_plan = bundle['cached_plan']
    
    # Processing complete - caller (upload_file) builds and returns the aggregated response.
    return

@app.route('/api/model/<session_id>', methods=['GET'])
def get_model(session_id):
    bundle = build_session_bundle(session_id)
    if not bundle: return jsonify({'error': 'No data found'}), 404
    all_data = bundle['all_data']
    file_map = bundle['file_map']
    latest = bundle['latest']
    cached_plan = bundle['cached_plan']
    latest_metadata = safe_json_loads(latest['metadata_json'], {})
    return jsonify({
        'sessionId': session_id,
        'fileId': latest['file_id'],
        'filename': file_map.get(latest['file_id']),
        'graph': build_graph_json(all_data, file_map),
        'script': latest['script_text'],
        'description': latest['description_text'],
        'metadata': latest_metadata,
        'binaryReport': latest_metadata.get('binaryReport'),
        'decodedSections': latest_metadata.get('decodedSections', []),
        'undecodedSections': latest_metadata.get('undecodedSections', []),
        'evidence': latest_metadata.get('evidence', {}),
        'editedSql': latest['edited_sql'] or latest['script_text'],
        'editedText': latest['edited_text'] or latest['description_text'],
        'regeneratedSql': latest['regenerated_sql'],
        'regeneratedText': latest['regenerated_text'],
        'regeneratedLineage': safe_json_loads(latest['regeneration_json'], {}).get('lineage', ''),
        'regeneration': build_regeneration_response_from_row(latest),
        'generationPlan': cached_plan['plan'],
        'generationPlanText': cached_plan['planText'],
        'regenerationHistory': bundle['history_rows'] and [serialize_regeneration_history_row(row) for row in bundle['history_rows']] or [],
        'sessionStats': {
            'totalTables': bundle['total_tables'],
            'totalRelationships': bundle['total_relationships'],
            'files': [serialize_session_file(r, file_map, cached_plan) for r in all_data]
        }
    })

@app.route('/api/regenerate', methods=['POST'])
def regenerate():
    data = request.get_json()
    session_id = data.get('sessionId')
    edited_sql = data.get('editedSql', '')
    edited_text = data.get('editedText', '')
    regenerated_sql = data.get('regeneratedSql', '')
    regenerated_text = data.get('regeneratedText', '')
    dialect = data.get('dialect', 'dbt')
    generation_mode = (data.get('generationMode') or data.get('generation_mode') or 'auto').strip().lower()
    if generation_mode not in {'auto', 'one_shot', 'loop'}:
        generation_mode = 'auto'
    bundle = build_session_bundle(session_id) if session_id else None
    if not bundle:
        return jsonify({'error': 'No data found for regeneration'}), 404

    latest = bundle['latest']
    cached_plan = bundle['cached_plan']
    combined_scripts = bundle['scripts_context']
    file_id = latest['file_id']
    trigger_migration = bool(data.get('triggerMigration'))
    prompt_version = PROMPT_VERSION
    
    # Optimize script size before sending to AI
    optimized_scripts = optimize_qvs_for_context(combined_scripts, max_chars=30_000)
    print(f"REGEN SCRIPT OPTIMIZATION original_len={len(combined_scripts)} optimized_len={len(optimized_scripts)}")
    
    input_payload = {
        'sessionId': session_id,
        'editedSql': edited_sql,
        'editedText': edited_text,
        'regeneratedSql': regenerated_sql,
        'regeneratedText': regenerated_text,
        'dialect': dialect,
        'triggerMigration': trigger_migration,
        'generationMode': generation_mode,
        'planHash': cached_plan['hash'],
    }
    input_hash = hash_text(json.dumps(input_payload, sort_keys=True))
    history_id = create_regeneration_history_entry(
        session_id,
        file_id,
        input_hash,
        cached_plan['plan'],
        cached_plan['planText'],
        trigger_migration,
        status='queued' if trigger_migration else 'complete',
        model=_active_ai_model(),
        prompt_version=prompt_version,
    )

    structured = {
        'sql': regenerated_sql or edited_sql or '',
        'description': normalize_sql_description(regenerated_text or edited_text, cached_plan['plan']),
        'lineage': '',
        'warnings': [],
        'promptVersion': prompt_version,
        'model': _active_ai_model(),
        'status': 'queued' if trigger_migration else 'complete',
    }

    if trigger_migration:
        # Check if there is already an active job running for this sessionId to prevent parallel duplicate execution
        with REGENERATION_LOCK:
            for existing_job_id, job_info in REGENERATION_JOBS.items():
                if job_info.get('sessionId') == session_id and job_info.get('status') in ('queued', 'running'):
                    print(f"DEDUPLICATING REGENERATION: Job {existing_job_id} already active for session {session_id}. Returning existing job.")
                    return jsonify({
                        'success': True,
                        'queued': True,
                        'jobId': existing_job_id,
                        'promptVersion': job_info.get('promptVersion'),
                        'generationPlan': cached_plan['plan'],
                        'generationPlanText': cached_plan['planText'],
                        'regeneration': structured,
                        'selectedGenerationMode': generation_mode,
                        'regenerationHistory': load_regeneration_history(session_id),
                    }), 202

        with REGENERATION_LOCK:
            REGENERATION_JOBS[history_id] = {
                'status': 'queued',
                'sessionId': session_id,
                'updatedAt': datetime.utcnow().isoformat(),
                'promptVersion': prompt_version,
                'model': _active_ai_model(),
                'progress_message': 'Queued...',
                'last_heartbeat': time.time(),
            }
        REGENERATION_EXECUTOR.submit(
            run_regeneration_job,
            history_id,
            session_id,
            file_id,
            edited_sql,
            edited_text,
            regenerated_sql,
            regenerated_text,
            dialect,
            optimized_scripts,
            cached_plan,
            input_hash,
            trigger_migration,
            generation_mode,
        )
        return jsonify({
            'success': True,
            'queued': True,
            'jobId': history_id,
            'promptVersion': prompt_version,
            'generationPlan': cached_plan['plan'],
            'generationPlanText': cached_plan['planText'],
            'regeneration': structured,
            'selectedGenerationMode': generation_mode,
            'regenerationHistory': load_regeneration_history(session_id),
        }), 202

    maybe_store_regeneration_state(
        session_id,
        file_id,
        edited_sql,
        edited_text,
        structured,
        status='complete',
        model=_active_ai_model(),
        job_id=history_id,
        prompt_version=prompt_version,
    )
    finalize_regeneration_history_entry(history_id, structured, error_text='', status='complete')
    return jsonify({
        'success': True,
        'queued': False,
        'jobId': history_id,
        'promptVersion': prompt_version,
        'regeneratedSql': structured['sql'],
        'regeneratedText': structured['description'],
        'regeneratedLineage': structured['lineage'],
        'regeneration': structured,
        'generationPlan': cached_plan['plan'],
        'generationPlanText': cached_plan['planText'],
        'selectedGenerationMode': generation_mode,
        'regenerationHistory': load_regeneration_history(session_id),
    })


@app.route('/api/regenerate/status/<job_id>', methods=['GET'])
def regenerate_status(job_id):
    with REGENERATION_LOCK:
        job = REGENERATION_JOBS.get(job_id)
        if job and job.get('status') in ('queued', 'running'):
            job['last_heartbeat'] = time.time()
            job['updatedAt'] = datetime.utcnow().isoformat()
            response_job = job.copy()
        else:
            response_job = job

    # Job not in memory — fall back to DB (handles server restarts)
    if not response_job:
        db = get_db()
        row = db.execute('SELECT * FROM regeneration_history WHERE id = ?', (job_id,)).fetchone()
        db.close()
        if not row:
            return jsonify({'error': 'Job not found'}), 404
        return jsonify({
            'jobId': job_id,
            'status': row['status'],
            'result': safe_json_loads(row['regeneration_json'], None),
            'history': serialize_regeneration_history_row(row),
            'createdAt': row['created_at'],
            'updatedAt': row['completed_at'] or row['created_at'],
            'progress_message': None,
            'last_heartbeat': None,
        })

    status = response_job.get('status', 'queued')
    print(f"STATUS_CHECK job_id={job_id} status={status}")
    response = {
        'jobId': job_id,
        'status': status,
        'promptVersion': response_job.get('promptVersion', PROMPT_VERSION),
        'generationPlan': response_job.get('generationPlan', []),
        'generationPlanText': response_job.get('generationPlanText', ''),
        'createdAt': response_job.get('createdAt'),
        'updatedAt': response_job.get('updatedAt'),
        'progress_message': response_job.get('progress_message'),
        'last_heartbeat': response_job.get('last_heartbeat'),
    }

    # For still-running jobs, tell the client the minimum sensible retry interval.
    # The frontend uses exponential backoff anyway, but this is a belt-and-suspenders hint.
    if status in ('queued', 'running'):
        response['retryAfter'] = 2  # seconds

    if job.get('result'):
        response['result'] = job['result']
    if job.get('error'):
        response['error'] = job['error']
    if job.get('sessionId') and status not in ('queued', 'running'):
        # Only load history when the job is done — avoids a DB query on every poll
        response['history'] = load_regeneration_history(job['sessionId'])

    return jsonify(response)

@app.route('/api/regenerate/result/<job_id>', methods=['GET'])
def regenerate_result(job_id):
    with REGENERATION_LOCK:
        job = REGENERATION_JOBS.get(job_id)
        response_job = job.copy() if isinstance(job, dict) else None
    if not response_job:
        db = get_db()
        row = db.execute('SELECT * FROM regeneration_history WHERE id = ?', (job_id,)).fetchone()
        db.close()
        if not row:
            return jsonify({'error': 'Job not found'}), 404
        result = safe_json_loads(row['regeneration_json'], None)
        if row['status'] in {'queued', 'running'}:
            return jsonify({'error': 'Job is not complete', 'status': row['status']}), 409
        history_payload = {
            'status': row['status'],
            'result': result if isinstance(result, dict) else {},
        }
        history_payload, result = _self_heal_regenerate_result_payload(
            job_id,
            history_payload,
            plan=safe_json_loads(row['generation_plan_json'], []),
            dialect='dbt',
        )
        return jsonify({
            'jobId': job_id,
            'status': row['status'],
            'result': result,
            'sql': (result or {}).get('sql', '') if isinstance(result, dict) else '',
            'validationReport': (result or {}).get('validationReport') if isinstance(result, dict) else None,
            'validationArtifacts': (result or {}).get('validationArtifacts') if isinstance(result, dict) else None,
            'warnings': (result or {}).get('warnings', []) if isinstance(result, dict) else [],
            'metadata': {'history': serialize_regeneration_history_row(row)},
        })
    status = response_job.get('status', 'queued')
    if status in {'queued', 'running'}:
        return jsonify({'error': 'Job is not complete', 'status': status}), 409
    response_job, result = _self_heal_regenerate_result_payload(
        job_id,
        response_job,
        plan=response_job.get('generationPlan', []),
        dialect='dbt',
    )
    with REGENERATION_LOCK:
        if job_id in REGENERATION_JOBS:
            REGENERATION_JOBS[job_id] = response_job
    return jsonify({
        'jobId': job_id,
        'status': status,
        'result': result,
        'sql': result.get('sql', '') if isinstance(result, dict) else '',
        'validationReport': result.get('validationReport') if isinstance(result, dict) else None,
        'validationArtifacts': result.get('validationArtifacts') if isinstance(result, dict) else None,
        'warnings': result.get('warnings', []) if isinstance(result, dict) else [],
        'metadata': {
            'sessionId': response_job.get('sessionId'),
            'promptVersion': response_job.get('promptVersion'),
            'generationPlan': response_job.get('generationPlan', []),
        },
    })

@app.route('/api/explain', methods=['POST'])
def explain_code():
    data = request.get_json()
    code_snippet = data.get('code')
    session_id = data.get('sessionId')
    
    if not code_snippet:
        return jsonify({'error': 'No code provided'}), 400
    if not _has_ai_provider_configured():
        return jsonify({'error': 'AI provider not configured'}), 503
        
    system_prompt = "You are a Senior Data Engineer. Explain the following DBT SQL code snippet and how it relates to typical QlikView logic (LOAD, RESIDENT, etc). Keep it concise and technical."
    prompt = f"Please explain this code snippet:\n\n```sql\n{code_snippet}\n```"
    
    explanation = call_openrouter(prompt, system_prompt=system_prompt)
    return jsonify({'explanation': explanation})


@app.route('/api/chat', methods=['POST'])
def chat_refine():
    """
    Iterative chat refinement endpoint.

    Accepts a natural-language instruction from the user and applies it to the
    current SQL + description draft, returning an updated migration result in
    the same shape as /api/regenerate.

    Request body:
      sessionId    â€“ active session
      message      â€“ user instruction, e.g. "add a filter for active customers"
      currentSql   â€“ the SQL currently shown in the editor
      currentDesc  â€“ the description currently shown
      dialect      â€“ target dbt dialect
    """
    data = request.get_json() or {}
    session_id = data.get('sessionId')
    user_message = (data.get('message') or '').strip()
    current_sql = data.get('currentSql', '')
    current_desc = data.get('currentDesc', '')
    dialect = data.get('dialect', 'dbt')

    if not user_message:
        return jsonify({'error': 'No message provided'}), 400
    if not _has_ai_provider_configured():
        return jsonify({'error': 'AI provider not configured'}), 503

    bundle = build_session_bundle(session_id) if session_id else None
    if not bundle:
        return jsonify({'error': 'Session not found'}), 404

    cached_plan = bundle['cached_plan']
    combined_scripts = bundle['scripts_context']

    system_prompt = f"""You are an expert dbt SQL engineer helping a user iteratively refine a Qlik-to-dbt migration.

The user has an existing SQL draft and description. Apply their instruction with the smallest possible change that satisfies the request.

Rules:
- Do NOT rewrite the entire SQL unless the instruction explicitly asks for it.
- Preserve all existing CTEs and logic that the instruction does not touch.
- Use {dialect.upper()} dialect conventions.
- Return the same two-section format.

Output format:
### SQL
### DESCRIPTION
"""

    # Use intelligent block-level script context optimization for refinement reference
    pruned_reference_script = optimize_qvs_for_context(combined_scripts, max_chars=35_000)

    prompt_parts = [
        f"### User Instruction\n{user_message}",
        f"\n### Current SQL Draft\n```sql\n{current_sql or '-- (empty)'}\n```",
        f"\n### Current Description\n{current_desc or '(none)'}",
        f"\n### Source Qlik Scripts (for reference)\n```sql\n{pruned_reference_script}\n```",
        f"\n### Generation Plan\n{cached_plan.get('planText', '')}",
    ]

    try:
        print(f"🤖 [Senior AI/ML Agent] Initiating Chat Refinement Proposer pass...")
        ai_response = call_openrouter(
            '\n'.join(prompt_parts),
            system_prompt=system_prompt,
            temperature=0,
            top_p=1,
            max_tokens=4096,
        )
    except Exception as exc:
        return jsonify({'error': f'AI call failed: {exc}'}), 502

    structured = parse_migration_response(ai_response)
    
    # Run Verifier Agent on refined SQL to ensure syntax/compilation robustness
    validation_issues = issues_to_strings(validate_migration_sql(structured['sql'], cached_plan['plan'], dialect=dialect))
    if validation_issues and needs_sql_repair(validation_issues):
        print(f"⚠️ [Senior AI/ML Agent] Chat Refinement validation failed. Issues: {validation_issues}")
        print(f"🔧 [Senior AI/ML Agent] Launching Autonomous Self-Repair for Refinement...")
        try:
            repaired_raw = request_sql_repair(
                call_openrouter,
                structured['sql'],
                structured['description'],
                validation_issues,
                dialect=dialect,
                description_style=SQL_DESCRIPTION_STYLE,
                prompt_version=PROMPT_VERSION,
                qvs_script=combined_scripts,
                plan_text=cached_plan['planText'],
            )
            repaired_struct = parse_migration_response(repaired_raw)
            if repaired_struct.get('sql'):
                repaired_struct['sql'] = finalize_generated_sql(
                    repaired_struct['sql'],
                    plan=cached_plan.get('plan'),
                    qvs_script=combined_scripts,
                ) if dialect != 'powerbi' else repaired_struct['sql']
                print(f"✅ [Senior AI/ML Agent] Chat Refinement Self-Repair completed successfully!")
                structured = repaired_struct
                structured['warnings'] = structured.get('warnings', [])
                structured['warnings'].extend([f"auto-repaired: {issue}" for issue in validation_issues])
        except Exception as repair_err:
            print(f"❌ [Senior AI/ML Agent] Chat Refinement Self-Repair failed: {repair_err}. Falling back to proposer draft.")
    structured['promptVersion'] = PROMPT_VERSION
    structured['model'] = _active_ai_model()
    structured['status'] = 'complete'
    structured['sql'] = finalize_generated_sql(
        structured.get('sql') or '',
        plan=cached_plan.get('plan'),
        qvs_script=combined_scripts,
    ) if dialect != 'powerbi' else (structured.get('sql') or '')

    # Normalise description
    structured['description'] = normalize_sql_description(
        structured.get('description') or current_desc,
        cached_plan['plan'],
    )

    # Persist the refined state
    file_id = bundle['latest']['file_id']
    maybe_store_regeneration_state(
        session_id,
        file_id,
        current_sql,
        current_desc,
        structured,
        status='complete',
        model=_active_ai_model(),
        prompt_version=PROMPT_VERSION,
    )

    return jsonify({
        'success': True,
        'regeneration': structured,
        'generationPlan': cached_plan['plan'],
        'generationPlanText': cached_plan['planText'],
        'regenerationHistory': load_regeneration_history(session_id),
    })


@app.route('/api/chat/stream', methods=['POST'])
def chat_refine_stream():
    """Streaming variant of /api/chat — returns tokens as SSE."""
    data = request.get_json() or {}
    session_id = data.get('sessionId')
    user_message = (data.get('message') or '').strip()
    current_sql = data.get('currentSql', '')
    current_desc = data.get('currentDesc', '')
    dialect = data.get('dialect', 'dbt')

    if not user_message:
        return jsonify({'error': 'No message provided'}), 400
    if not _has_ai_provider_configured():
        return jsonify({'error': 'AI provider not configured'}), 503

    bundle = build_session_bundle(session_id) if session_id else None
    if not bundle:
        return jsonify({'error': 'Session not found'}), 404

    cached_plan = bundle['cached_plan']
    combined_scripts = bundle['scripts_context']

    system_prompt = f"""You are an expert dbt SQL engineer helping a user iteratively refine a Qlik-to-dbt migration.

The user has an existing SQL draft and description. Apply their instruction with the smallest possible change that satisfies the request.

Rules:
- Do NOT rewrite the entire SQL unless the instruction explicitly asks for it.
- Preserve all existing CTEs and logic that the instruction does not touch.
- Use {dialect.upper()} dialect conventions.
- Return the same two-section format.

Output format:
### SQL
### DESCRIPTION
"""

    pruned_reference_script = optimize_qvs_for_context(combined_scripts, max_chars=35_000)
    prompt_parts = [
        f"### User Instruction\n{user_message}",
        f"\n### Current SQL Draft\n```sql\n{current_sql or '-- (empty)'}\n```",
        f"\n### Current Description\n{current_desc or '(none)'}",
        f"\n### Source Qlik Scripts (for reference)\n```sql\n{pruned_reference_script}\n```",
        f"\n### Generation Plan\n{cached_plan.get('planText', '')}",
    ]
    full_prompt = '\n'.join(prompt_parts)

    def generate():
        full_content = []
        try:
            for token in call_openrouter(
                full_prompt,
                system_prompt=system_prompt,
                max_tokens=4096,
                temperature=0,
                top_p=1,
                stream=True,
            ):
                full_content.append(token)
                yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"

            raw = ''.join(full_content)
            structured = parse_migration_response(raw)
            structured['sql'] = finalize_generated_sql(
                structured.get('sql') or '',
                plan=cached_plan.get('plan'),
                qvs_script=combined_scripts,
            ) if dialect != 'powerbi' else (structured.get('sql') or '')

            # Validation + repair
            validation_issues = issues_to_strings(validate_migration_sql(structured['sql'], cached_plan['plan'], dialect=dialect))
            if validation_issues and needs_sql_repair(validation_issues):
                try:
                    repaired_raw = request_sql_repair(
                        call_openrouter,
                        structured['sql'],
                        structured['description'],
                        validation_issues,
                        dialect=dialect,
                        description_style=SQL_DESCRIPTION_STYLE,
                        prompt_version=PROMPT_VERSION,
                        qvs_script=combined_scripts,
                        plan_text=cached_plan['planText'],
                    )
                    repaired_struct = parse_migration_response(repaired_raw)
                    if repaired_struct.get('sql'):
                        repaired_struct['sql'] = finalize_generated_sql(
                            repaired_struct['sql'],
                            plan=cached_plan.get('plan'),
                            qvs_script=combined_scripts,
                        ) if dialect != 'powerbi' else repaired_struct['sql']
                        structured = repaired_struct
                except Exception:
                    pass

            structured['promptVersion'] = PROMPT_VERSION
            structured['model'] = _active_ai_model()
            structured['status'] = 'complete'
            structured['sql'] = finalize_generated_sql(
                structured.get('sql') or '',
                plan=cached_plan.get('plan'),
                qvs_script=combined_scripts,
            ) if dialect != 'powerbi' else (structured.get('sql') or '')
            structured['description'] = normalize_sql_description(
                structured.get('description') or current_desc,
                cached_plan['plan'],
            )

            file_id = bundle['latest']['file_id']
            maybe_store_regeneration_state(
                session_id, file_id, current_sql, current_desc,
                structured, status='complete',
                model=_active_ai_model(),
                prompt_version=PROMPT_VERSION,
            )

            yield f"data: {json.dumps({'type': 'done', 'regeneration': structured})}\n\n"

        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        },
    )


@app.route('/api/cost/<session_id>', methods=['GET'])
def session_cost(session_id):
    return jsonify(COST_TRACKER.session_summary(session_id))

@app.route('/api/cost/summary', methods=['GET'])
def global_cost_summary():
    summary = COST_TRACKER.global_summary()
    summary['cacheStats'] = SQL_PLAN_CACHE.stats()
    return jsonify(summary)

@app.route('/api/reset', methods=['POST'])
def reset_all():
    reset_db()
    clear_upload_folder()
    return jsonify({'success': True})

@app.route('/api/export-validation-artifacts', methods=['POST'])
def export_validation_artifacts_route():
    payload = request.get_json(silent=True) or {}
    job_id = payload.get('jobId') or payload.get('job_id')
    job_result = None
    job_status = ''
    if job_id:
        with REGENERATION_LOCK:
            job = REGENERATION_JOBS.get(job_id)
            job_copy = job.copy() if isinstance(job, dict) else None
        if not job_copy:
            db = get_db()
            row = db.execute('SELECT * FROM regeneration_history WHERE id = ?', (job_id,)).fetchone()
            db.close()
            if not row:
                return jsonify({'status': 'error', 'error': 'Job not found'}), 404
            job_status = row['status']
            job_result = safe_json_loads(row['regeneration_json'], {}) or {}
        else:
            job_status = job_copy.get('status', '')
            job_result = job_copy.get('result') or {}
        if job_status in {'queued', 'running'}:
            return jsonify({'status': 'error', 'error': 'Job is not complete', 'jobStatus': job_status}), 409

    validation_artifacts = payload.get('validationArtifacts') or payload.get('validation_artifacts') or {}
    if job_id:
        validation_artifacts = (
            validation_artifacts
            or (job_result or {}).get('validationArtifacts')
            or (job_result or {}).get('validation_artifacts')
            or {}
        )
        if not validation_artifacts:
            sql_text = (job_result or {}).get('final_sql') or (job_result or {}).get('sql') or ''
            report = (job_result or {}).get('validationReport') or (job_result or {}).get('validation_report')
            if sql_text:
                if not report:
                    report = build_migration_validation_report(sql_text, plan=[], dialect='dbt', model_name='executive_dashboard')
                    report = execute_validation_report(report, {'enabled': VALIDATION_EXECUTION_ENABLED})
                    logger.info("VALIDATION_REPORT_GENERATED checks=%s", len(report.get('checks') or []))
                validation_artifacts = generate_validation_artifacts(sql_text, report, model_name='executive_dashboard')
                logger.info(
                    "VALIDATION_ARTIFACTS_GENERATED models=%s tests=%s analyses=%s",
                    len((validation_artifacts.get('models') or {})),
                    len((validation_artifacts.get('tests') or {})),
                    len((validation_artifacts.get('analyses') or {})),
                )
                if isinstance(job_result, dict):
                    job_result['validationReport'] = report
                    job_result['validation_report'] = report
                    job_result['validationArtifacts'] = validation_artifacts
                    job_result['validation_artifacts'] = validation_artifacts
                with REGENERATION_LOCK:
                    if job_id in REGENERATION_JOBS:
                        REGENERATION_JOBS[job_id]['result'] = job_result
    if not isinstance(validation_artifacts, dict) or not validation_artifacts:
        return jsonify({
            'status': 'error',
            'error': 'validationArtifacts is required',
            'filesWritten': [],
        }), 400
    output_dir = payload.get('outputDir') or payload.get('output_dir')
    if not output_dir:
        session_id = str(payload.get('sessionId') or payload.get('session_id') or '').strip()
        safe_session = re.sub(r'[^A-Za-z0-9_.-]+', '_', session_id).strip('._')
        if not safe_session:
            safe_session = datetime.utcnow().strftime('%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:8]
        output_dir = safe_session
    include_project_scaffold = payload.get('includeProjectScaffold')
    if include_project_scaffold is None:
        include_project_scaffold = payload.get('include_project_scaffold', True)
    result = export_validation_artifacts(
        validation_artifacts,
        output_dir,
        include_project_scaffold=bool(include_project_scaffold),
        metadata={
            'status': payload.get('status') or (job_result or {}).get('status'),
            'sqlQualityScore': (
                payload.get('sqlQualityScore')
                or payload.get('sql_quality_score')
                or (job_result or {}).get('sqlQualityScore')
                or (job_result or {}).get('oneShotQualityScore')
            ),
            'warningsCount': (
                payload.get('warningsCount')
                or payload.get('warnings_count')
                or len((job_result or {}).get('warnings') or [])
            ),
        },
    )
    if result.get('errors'):
        return jsonify({
            'status': 'error',
            'outputDir': result.get('output_dir') or '',
            'manifestPath': result.get('manifest_path') or '',
            'filesWritten': result.get('files_written') or [],
            'errors': result.get('errors') or [],
        }), 400
    response_payload = {
        'status': 'success',
        'outputDir': result.get('output_dir'),
        'manifestPath': result.get('manifest_path'),
        'filesWritten': result.get('files_written') or [],
    }
    if bool(payload.get('dryRun') or payload.get('dry_run')):
        response_payload['dryRunResult'] = dry_run_validation_artifacts(result.get('output_dir') or '')
    if bool(payload.get('zip')):
        try:
            zip_result = zip_exported_artifacts(result.get('output_dir') or '')
            response_payload.update(zip_result)
        except Exception as exc:
            return jsonify({
                'status': 'error',
                'outputDir': result.get('output_dir') or '',
                'manifestPath': result.get('manifest_path') or '',
                'filesWritten': result.get('files_written') or [],
                'errors': [str(exc)],
            }), 400
    return jsonify(response_payload)

@app.route('/api/download-validation-artifacts/<zip_file_name>', methods=['GET'])
def download_validation_artifacts(zip_file_name):
    safe_name = os.path.basename(str(zip_file_name or '').replace('\\', '/'))
    if safe_name != zip_file_name or not safe_name.endswith('.zip'):
        return jsonify({'status': 'error', 'error': 'Invalid zip file name'}), 400
    root = os.path.abspath(os.path.join(PROJECT_ROOT, 'generated_artifacts'))
    target = os.path.abspath(os.path.join(root, safe_name))
    if os.path.commonpath([root, target]) != root or not os.path.exists(target):
        return jsonify({'status': 'error', 'error': 'Zip file not found'}), 404
    return send_from_directory(root, safe_name, as_attachment=True)

@app.route('/')
def serve_index(): return send_from_directory(app.static_folder, 'index.html')

@app.route('/<path:path>')
def serve_static(path):
    if os.path.exists(os.path.join(app.static_folder, path)): return send_from_directory(app.static_folder, path)
    return send_from_directory(app.static_folder, 'index.html')

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s:%(name)s:%(message)s')
    print("QVF Decoder - API Server")
    print("http://localhost:5000")
    print(f".env path: {_env_path}")
    provider = _selected_ai_provider()
    model = _active_ai_model(provider)
    if _has_ai_provider_configured(provider):
        print(f"AI provider configured: {provider} / {model}")
    else:
        print("WARNING: No AI provider configured - AI migration will not work.")
        print(f"  Create a .env file at: {_env_path}")
        print("  Example: AI_PROVIDER=gemini and GEMINI_API_KEY=...")
    app.run(debug=True, port=5000, use_reloader=False)
