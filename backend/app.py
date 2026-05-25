
import os
import json
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
from backend.migration_validator import validate_migration_sql, needs_repair, issues_to_strings
# pyrefly: ignore [missing-import]
from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context
from flask_cors import CORS
# pyrefly: ignore [missing-import]
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
from backend.openrouter_client import call_openrouter_chat, call_openrouter_chat_stream
from backend.dbt_routes import register_dbt_agent_routes
from backend.qvf_runtime import attach_inline_samples_to_tables, build_graph_json, extract_model_from_script, extract_qvf, generate_description_rule_based, parse_sql_sections, prepare_script_for_migration
from backend.comprehensive_qvf_extractor import enhance_metadata_with_comprehensive_extraction
from backend.qlik_script_parser import parse_qlik_load_script
from backend.advanced_qvf_extractor import extract_advanced_metadata
from backend.sql_migration import (
    extract_sql_generation_plan,
    format_sql_generation_plan,
    hash_text,
    needs_sql_repair,
    normalize_sql_description,
    parse_migration_response,
    request_migration,
    request_migration_with_validation,
    request_migration_one_shot,
    request_sql_repair,
    validate_generated_sql,
    optimize_qvs_for_context,
)

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
):
    if not OPENROUTER_API_KEY:
        raise RuntimeError('OPENROUTER_API_KEY is not configured.')

    model = model or OPENROUTER_DEFAULT_MODEL
    max_tokens = max_tokens if max_tokens is not None else OPENROUTER_MAX_TOKENS
    max_prompt_chars = max_prompt_chars if max_prompt_chars is not None else OPENROUTER_MAX_PROMPT_CHARS
    model_candidates = [m for m in OPENROUTER_FALLBACK_MODELS if m]
    if model not in model_candidates:
        model_candidates.insert(0, model)

    last_error = None
    tried_lower_tokens = False
    tried_lower_prompt = False

    for candidate_model in model_candidates:
        while True:
            print(
                f"OPENROUTER REQUEST model={candidate_model} prompt_chars={len(prompt)} "
                f"max_tokens={max_tokens} max_prompt_chars={max_prompt_chars}"
            )
            try:
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
                if _is_openrouter_credit_error(exc) and not tried_lower_prompt and max_prompt_chars > 3500:
                    tried_lower_prompt = True
                    max_prompt_chars = 3500
                    print(
                        "OPENROUTER CREDIT WARNING: retrying with lower max_prompt_chars=3500"
                    )
                    continue
                if _is_openrouter_credit_error(exc) and not tried_lower_tokens and max_tokens > 200:
                    tried_lower_tokens = True
                    max_tokens = 200
                    print(
                        "OPENROUTER CREDIT WARNING: retrying with lower max_tokens=200"
                    )
                    continue
                if _is_openrouter_credit_error(exc) and candidate_model != model_candidates[-1]:
                    print(
                        f"OPENROUTER CREDIT WARNING: model {candidate_model} failed, trying next fallback model."
                    )
                    break
                if _is_openrouter_credit_error(exc):
                    raise RuntimeError(
                        "OpenRouter prompt token limit exceeded or credits exhausted. "
                        "Please top up credits, lower OPENROUTER_MAX_TOKENS / OPENROUTER_MAX_PROMPT_CHARS, "
                        "or use a cheaper model."
                    ) from exc
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
    """Fast direct OpenRouter request with no fallback and no retry overhead.
    
    When stream=True, returns a generator yielding text chunks.
    """
    if not OPENROUTER_API_KEY:
        raise RuntimeError('OPENROUTER_API_KEY is not configured.')

    model = model or OPENROUTER_DEFAULT_MODEL
    effective_max_tokens = min(max_tokens if max_tokens is not None else OPENROUTER_MAX_TOKENS, 8000)
    effective_max_prompt_chars = min(max_prompt_chars if max_prompt_chars is not None else OPENROUTER_MAX_PROMPT_CHARS, 35000)

    print(
        f"OPENROUTER FAST REQUEST model={model} prompt_chars={len(prompt)} "
        f"max_tokens={effective_max_tokens} max_prompt_chars={effective_max_prompt_chars} stream={stream}"
    )
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
        stream=stream,
    )


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

def migrate_qvs_to_dbt(qvs_script, session_context=None, current_sql=None, current_desc=None, dialect='dbt', plan=None, plan_text=None, progress_callback=None, stream_callback=None):
    if (dialect or '').lower() == 'powerbi':
        return request_migration_one_shot(
            call_openrouter_fast,
            qvs_script,
            session_context=session_context,
            current_sql=current_sql,
            current_desc=current_desc,
            dialect=dialect,
            plan=plan,
            plan_text=plan_text,
            prompt_version=PROMPT_VERSION,
            description_style=SQL_DESCRIPTION_STYLE,
            progress_callback=progress_callback,
            stream_callback=stream_callback,
        )

    # Try a fast one-shot migration first. This is often much faster than the
    # validation loop and still produces valid SQL in the majority of cases.
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
            prompt_version=PROMPT_VERSION,
            description_style=SQL_DESCRIPTION_STYLE,
            progress_callback=progress_callback,
            stream_callback=stream_callback,
        )
        quick_issues = validate_generated_sql(quick_result.get('sql', ''), plan, dialect)
        # Also reject shallow output: if every CTE is just SELECT * with no field list,
        # the one-shot prompt didn't do real work — escalate to the full validation loop.
        quick_sql = quick_result.get('sql', '')
        cte_count = quick_sql.upper().count(' AS (')
        select_star_count = len(re.findall(r'SELECT\s+\*\s+FROM', quick_sql, re.IGNORECASE))
        is_shallow = cte_count > 0 and select_star_count >= cte_count
        if (not quick_issues or not needs_sql_repair(quick_issues)) and not is_shallow:
            if progress_callback:
                progress_callback('Fast one-shot migration succeeded.')
            return quick_result
        reason = 'shallow SELECT * output' if is_shallow else 'validation issues'
        if progress_callback:
            progress_callback(f'Fast one-shot migration returned {reason}; switching to the validation loop...')
    except Exception as e:
        print(f"WARNING: Fast one-shot migration failed: {str(e)}")
        if progress_callback:
            progress_callback('Fast one-shot migration failed; switching to the validation loop...')

    try:
        return request_migration_with_validation(
            call_openrouter,
            qvs_script,
            session_context=session_context,
            current_sql=current_sql,
            current_desc=current_desc,
            dialect=dialect,
            plan=plan,
            plan_text=plan_text,
            prompt_version=PROMPT_VERSION,
            description_style=SQL_DESCRIPTION_STYLE,
            max_iterations=8,
            progress_callback=progress_callback,
            stream_callback=stream_callback,
        )
    except Exception as e:
        print(f"WARNING: Validation loop failed ({str(e)}). Falling back to one-shot migration.")
        return request_migration_one_shot(
            call_openrouter_fast,
            qvs_script,
            session_context=session_context,
            current_sql=current_sql,
            current_desc=current_desc,
            dialect=dialect,
            plan=plan,
            plan_text=plan_text,
            prompt_version=PROMPT_VERSION,
            description_style=SQL_DESCRIPTION_STYLE,
            progress_callback=progress_callback,
            stream_callback=stream_callback,
        )

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


def run_regeneration_job(job_id, session_id, file_id, edited_sql, edited_text, regenerated_sql, regenerated_text, dialect, combined_scripts, cached_plan, input_hash, trigger_migration):
    model = OPENROUTER_DEFAULT_MODEL
    prompt_version = PROMPT_VERSION
    status = 'complete'
    error_text = ''
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
        print(f"RUN_JOB STATUS job_id={job_id} status=running session={session_id}")

        if trigger_migration and OPENROUTER_API_KEY:
            target_label = 'Power BI (M + DAX)' if is_powerbi else f'DBT [{dialect}]'
            print(f"AI Migration Triggered → {target_label} | session={session_id}")
            print(f"RUN_JOB AI CALL job_id={job_id} plan_size={len(cached_plan.get('plan', []))} combined_scripts_len={len(combined_scripts or '')} plan_text_preview={repr(cached_plan.get('planText','')[:200])}")
            
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

            def stream_callback(token):
                if _sql_done[0]:
                    return  # description section — don't stream to editor
                try:
                    _stream_buf.append(token)
                    # Check the last ~30 chars of the accumulated buffer for the
                    # description header.  We use a sliding window so we catch
                    # the header even when it arrives split across multiple tokens.
                    tail = ''.join(_stream_buf[-30:])
                    if '### DESCRIPTION' in tail or '###DESCRIPTION' in tail:
                        _sql_done[0] = True
                        # Don't push this token — the description comes via "done"
                        return
                    q = _get_or_create_stream_queue(job_id)
                    q.put({'type': 'token', 'content': token})
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
            )
            print(f"RUN_JOB AI RETURN job_id={job_id} migration_result_type={type(migration_result).__name__} len={len(str(migration_result)) if migration_result is not None else 0}")
            if isinstance(migration_result, dict):
                print(f"  Migration result final_sql length: {len(migration_result.get('final_sql', ''))}")
                print(f"  Migration result sql length: {len(migration_result.get('sql', ''))}")
                print(f"  Migration result keys: {list(migration_result.keys())}")
            if migration_result:
                print(f"AI Migration result received for session {session_id}")
                if isinstance(migration_result, dict):
                    migration_sql = migration_result.get('sql') or ''
                    migration_final_sql = migration_result.get('final_sql') or ''
                    chosen_sql = migration_final_sql if len(migration_final_sql) > len(migration_sql) else migration_sql
                    migration_desc = migration_result.get('description') or ''
                    migration_final_desc = migration_result.get('final_description') or ''
                    chosen_desc = migration_final_desc if len(migration_final_desc) > len(migration_desc) else migration_desc

                    structured = {
                        'sql': chosen_sql,
                        'description': chosen_desc,
                        'lineage': migration_result.get('lineage', ''),
                        'warnings': migration_result.get('warnings', []),
                        'promptVersion': prompt_version,
                        'model': model,
                        'status': 'complete',
                        'comparisonSummary': migration_result.get('comparison_summary', {}),
                        'validationStatus': migration_result.get('status', 'complete'),
                        'semanticScore': round(float(migration_result.get('score', 0.0)), 2),
                        'iterations': migration_result.get('iterations', 1),
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

                validation_issues = issues_to_strings(validate_migration_sql(regenerated_sql, cached_plan['plan'], dialect=dialect))
                structured.setdefault('warnings', [])
                structured['warnings'].extend(validation_issues)

                # Run SQL repair if validation issues are detected
                if not is_powerbi and validation_issues and needs_sql_repair(validation_issues):
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
                            # Apply CTE name deduplication post-processing in python
                            from backend.sql_migration import deduplicate_ctes
                            repaired_result['sql'] = deduplicate_ctes(repaired_result['sql'])
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

                structured['sql'] = regenerated_sql or edited_sql or ''

                # Power BI: keep the description as-is (it's already M/DAX aware)
                # DBT: normalize into ## Block: sections
                if is_powerbi:
                    structured['description'] = regenerated_text or edited_text or ''
                else:
                    structured['description'] = normalize_sql_description(regenerated_text or edited_text, cached_plan['plan'])
            else:
                print(f"WARN: AI Response empty for session {session_id}")
                structured['warnings'].append('AI response was empty; using the current draft state.')
        else:
            if trigger_migration:
                print(f"WARN: OpenRouter API key not set â€” skipping AI migration for session {session_id}")
                structured['warnings'].append('OpenRouter API key is not configured. Set OPENROUTER_API_KEY in your .env file and restart the server.')

        # Final description normalisation (DBT only)
        if not is_powerbi:
            structured['description'] = normalize_sql_description(structured.get('description'), cached_plan['plan'])

        structured['status'] = status
        print(f"RUN_JOB FINALIZE job_id={job_id} status={status} sql_len={len(structured.get('sql', ''))} warnings_count={len(structured.get('warnings', []))}")
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
        print(f"RUN_JOB DONE job_id={job_id} - history entry finalized")

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
        print(f"Job {job_id} completed with status={status} | sql_len={len(structured.get('sql',''))} | warnings={structured.get('warnings',[])}")

        # Push final result into SSE stream queue (if a client is listening)
        try:
            q = _get_or_create_stream_queue(job_id)
            q.put({'type': 'done', 'sql': structured.get('sql', ''), 'description': structured.get('description', ''), 'warnings': structured.get('warnings', [])})
        except Exception:
            pass
        threading.Timer(60, _cleanup_stream_queue, args=[job_id]).start()

    except Exception as exc:
        import traceback
        error_text = str(exc)
        status = 'failed'
        structured['status'] = status
        structured['warnings'].append(error_text)
        print(f"RUN_JOB EXCEPTION job_id={job_id} error_text={error_text}")
        print(f"ERROR: Job {job_id} failed: {error_text}")
        traceback.print_exc()
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
        model=OPENROUTER_DEFAULT_MODEL,
        prompt_version=prompt_version,
    )

    structured = {
        'sql': regenerated_sql or edited_sql or '',
        'description': normalize_sql_description(regenerated_text or edited_text, cached_plan['plan']),
        'lineage': '',
        'warnings': [],
        'promptVersion': prompt_version,
        'model': OPENROUTER_DEFAULT_MODEL,
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
                        'regenerationHistory': load_regeneration_history(session_id),
                    }), 202

        with REGENERATION_LOCK:
            REGENERATION_JOBS[history_id] = {
                'status': 'queued',
                'sessionId': session_id,
                'updatedAt': datetime.utcnow().isoformat(),
                'promptVersion': prompt_version,
                'model': OPENROUTER_DEFAULT_MODEL,
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
        )
        return jsonify({
            'success': True,
            'queued': True,
            'jobId': history_id,
            'promptVersion': prompt_version,
            'generationPlan': cached_plan['plan'],
            'generationPlanText': cached_plan['planText'],
            'regeneration': structured,
            'regenerationHistory': load_regeneration_history(session_id),
        }), 202

    maybe_store_regeneration_state(
        session_id,
        file_id,
        edited_sql,
        edited_text,
        structured,
        status='complete',
        model=OPENROUTER_DEFAULT_MODEL,
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

@app.route('/api/explain', methods=['POST'])
def explain_code():
    data = request.get_json()
    code_snippet = data.get('code')
    session_id = data.get('sessionId')
    
    if not code_snippet or not OPENROUTER_API_KEY:
        return jsonify({'error': 'No code provided or API key missing'}), 400
        
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
    if not OPENROUTER_API_KEY:
        return jsonify({'error': 'OpenRouter API key not configured'}), 503

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
                print(f"✅ [Senior AI/ML Agent] Chat Refinement Self-Repair completed successfully!")
                structured = repaired_struct
                structured['warnings'] = structured.get('warnings', [])
                structured['warnings'].extend([f"auto-repaired: {issue}" for issue in validation_issues])
        except Exception as repair_err:
            print(f"❌ [Senior AI/ML Agent] Chat Refinement Self-Repair failed: {repair_err}. Falling back to proposer draft.")
    structured['promptVersion'] = PROMPT_VERSION
    structured['model'] = OPENROUTER_DEFAULT_MODEL
    structured['status'] = 'complete'

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
        model=OPENROUTER_DEFAULT_MODEL,
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
    if not OPENROUTER_API_KEY:
        return jsonify({'error': 'OpenRouter API key not configured'}), 503

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
            for token in call_openrouter_chat_stream(
                api_key=OPENROUTER_API_KEY,
                model=OPENROUTER_DEFAULT_MODEL,
                prompt=full_prompt,
                system_prompt=system_prompt,
                max_tokens=4096,
                temperature=0,
                top_p=1,
            ):
                full_content.append(token)
                yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"

            raw = ''.join(full_content)
            structured = parse_migration_response(raw)

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
                        structured = repaired_struct
                except Exception:
                    pass

            structured['promptVersion'] = PROMPT_VERSION
            structured['model'] = OPENROUTER_DEFAULT_MODEL
            structured['status'] = 'complete'
            structured['description'] = normalize_sql_description(
                structured.get('description') or current_desc,
                cached_plan['plan'],
            )

            file_id = bundle['latest']['file_id']
            maybe_store_regeneration_state(
                session_id, file_id, current_sql, current_desc,
                structured, status='complete',
                model=OPENROUTER_DEFAULT_MODEL,
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

@app.route('/')
def serve_index(): return send_from_directory(app.static_folder, 'index.html')

@app.route('/<path:path>')
def serve_static(path):
    if os.path.exists(os.path.join(app.static_folder, path)): return send_from_directory(app.static_folder, path)
    return send_from_directory(app.static_folder, 'index.html')

if __name__ == '__main__':
    print("QVF Decoder - API Server")
    print("http://localhost:5000")
    print(f".env path: {_env_path}")
    if OPENROUTER_API_KEY:
        print(f"OpenRouter API key detected (starts with: {OPENROUTER_API_KEY[:12]}...)")
    else:
        print("WARNING: No OpenRouter API key detected â€” AI migration will not work.")
        print(f"  Create a .env file at: {_env_path}")
        print("  Add: OPENROUTER_API_KEY=sk-or-v1-...")
    app.run(debug=True, port=5000)
