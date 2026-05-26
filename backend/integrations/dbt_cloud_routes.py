import json
import logging
import re
import time

import requests
from flask import jsonify, request

from backend.integrations.openrouter_client import AIClientError

logger = logging.getLogger(__name__)

DBT_CLOUD_DEFAULT_BASE_URL = 'https://cloud.getdbt.com/api/v2'
DBT_RUN_STATUS = {
    1: 'Queued',
    2: 'Starting',
    3: 'Running',
    10: 'Success',
    20: 'Error',
    30: 'Cancelled',
}

# Commands that must never appear in dbt steps, regardless of context.
_BLOCKED_COMMAND_PATTERNS = [
    r'\brm\b', r'\brmdir\b', r'\bdrop\b', r'\bdelete\b',
    r'\btruncate\b', r'\bchmod\b', r'\bchown\b', r'\bsudo\b',
    r'[;&|`$]',          # shell operators / subshell
    r'\.\./',            # path traversal
]
_BLOCKED_RE = re.compile('|'.join(_BLOCKED_COMMAND_PATTERNS), re.IGNORECASE)


# ─── URL / auth helpers ───────────────────────────────────────────────────────

def normalize_dbt_cloud_base_url(base_url):
    base = (base_url or DBT_CLOUD_DEFAULT_BASE_URL).strip().rstrip('/')
    if not base.startswith('https://'):
        raise ValueError('Use an HTTPS dbt Cloud API URL.')
    if not base.endswith('/api/v2'):
        base = f"{base}/api/v2"
    return base


def dbt_cloud_headers(token):
    token = (token or '').strip()
    if not token:
        raise ValueError('dbt Cloud service token is required.')
    return {
        'Authorization': f'Token {token}',
        'Content-Type': 'application/json',
    }


def require_dbt_cloud_config(data, require_job=False):
    account_id = str(data.get('accountId') or '').strip()
    if not account_id:
        raise ValueError('dbt Cloud account ID is required.')
    job_id = str(data.get('jobId') or '').strip()
    if require_job and not job_id:
        raise ValueError('dbt Cloud job ID is required.')
    return {
        'base_url': normalize_dbt_cloud_base_url(data.get('baseUrl')),
        'headers': dbt_cloud_headers(data.get('token')),
        'account_id': account_id,
        'project_id': str(data.get('projectId') or '').strip(),
        'job_id': job_id,
    }


# ─── HTTP helper with retry ───────────────────────────────────────────────────

def dbt_cloud_request(method, path, config, retries=2, backoff=1.0, **kwargs):
    """
    Make an authenticated request to the dbt Cloud API.

    Retries on transient server errors (5xx) with exponential back-off.
    Raises RuntimeError with a structured message on permanent failures.
    """
    url = f"{config['base_url']}{path}"
    last_exc = None
    for attempt in range(retries + 1):
        try:
            response = requests.request(
                method, url, headers=config['headers'], timeout=30, **kwargs
            )
        except requests.exceptions.Timeout as exc:
            last_exc = exc
            logger.warning("dbt Cloud API timeout on attempt %d/%d: %s", attempt + 1, retries + 1, url)
            if attempt < retries:
                time.sleep(backoff * (2 ** attempt))
            continue
        except requests.exceptions.ConnectionError as exc:
            raise RuntimeError(f"Could not connect to dbt Cloud API: {exc}") from exc

        if response.status_code >= 500 and attempt < retries:
            logger.warning(
                "dbt Cloud API returned %s on attempt %d/%d; retrying…",
                response.status_code, attempt + 1, retries + 1,
            )
            time.sleep(backoff * (2 ** attempt))
            continue

        if response.status_code >= 400:
            try:
                detail = response.json()
            except Exception:
                detail = response.text
            raise RuntimeError(f"dbt Cloud API returned {response.status_code}: {detail}")

        return response.json()

    raise RuntimeError(f"dbt Cloud API request failed after {retries + 1} attempts: {last_exc}")


# ─── Queue-depth check ────────────────────────────────────────────────────────

def check_dbt_cloud_queue(config, job_id, warn_threshold=3):
    """
    Fetch running/queued runs for the given job and return a warning string
    if the queue is backed up beyond warn_threshold active runs.

    dbt Cloud returns HTTP 200 for a trigger even when the queue is full,
    so without this check a user has no feedback and the run waits silently.

    Returns None when the queue is healthy, or a warning string when it isn't.
    """
    try:
        runs_payload = dbt_cloud_request(
            'GET',
            f"/accounts/{config['account_id']}/runs/",
            config,
            params={
                'job_definition_id': job_id,
                'status': '1,2,3',   # 1=Queued 2=Starting 3=Running
                'limit': 10,
            },
            retries=1,
        )
        active = runs_payload.get('data', [])
        if len(active) >= warn_threshold:
            run_ids = [str(r.get('id', '?')) for r in active[:5]]
            return (
                f"dbt Cloud job {job_id} already has {len(active)} active/queued run(s) "
                f"(IDs: {', '.join(run_ids)}). Your run will queue behind them."
            )
    except Exception as exc:
        # Non-fatal — queue check failure must never block the deployment
        logger.warning("Queue depth check failed (non-fatal): %s", exc)
    return None


# ─── Serialisation ────────────────────────────────────────────────────────────

def serialize_dbt_run(run):
    status_code = run.get('status')
    status_humanized = (
        run.get('status_humanized')
        or DBT_RUN_STATUS.get(status_code)
        or str(status_code or '')
    )
    return {
        'runId': run.get('id'),
        'jobId': run.get('job_id'),
        'status': status_code,
        'statusHumanized': status_humanized,
        'href': run.get('href'),
        'createdAt': run.get('created_at'),
        'startedAt': run.get('started_at'),
        'finishedAt': run.get('finished_at'),
    }


# ─── Command sanitisation ─────────────────────────────────────────────────────

def sanitize_dbt_commands(commands):
    """
    Validate that every command is a safe dbt CLI invocation.

    Raises ValueError with a descriptive message on the first violation.
    """
    if not isinstance(commands, list):
        raise ValueError('Commands must be a list of dbt command strings.')

    cleaned = [str(command).strip() for command in commands if str(command).strip()]
    if not cleaned:
        raise ValueError('At least one dbt command is required.')

    for command in cleaned:
        # Must start with "dbt "
        if not re.match(r'^dbt\s+[A-Za-z0-9:_\-\s\+@.,/="\'*]+$', command):
            raise ValueError(
                f"Only dbt CLI commands are allowed. Invalid command: {command!r}"
            )
        # Secondary blocklist check
        if _BLOCKED_RE.search(command):
            raise ValueError(
                f"Command contains disallowed pattern: {command!r}"
            )

    return cleaned


# ─── AI response parsing ──────────────────────────────────────────────────────

def parse_agent_response(response_text):
    """
    Extract a structured agent plan from the AI response text.

    Returns a dict with keys ``summary``, ``commands``, ``checks``,
    ``warnings``, or ``None`` if the response cannot be parsed.
    Raises ValueError if the commands fail sanitisation.
    """
    if not response_text:
        return None

    text = response_text.strip()
    # Strip fenced code blocks (```json … ``` or ``` … ```)
    match = re.search(r'```(?:json)?\s*([\s\S]*?)```', text, re.IGNORECASE)
    if match:
        text = match.group(1).strip()

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        logger.debug("parse_agent_response: JSON decode failed for: %s", text[:200])
        return None

    if not isinstance(payload, dict):
        return None
    commands = payload.get('commands')
    if not isinstance(commands, list):
        return None

    # Let sanitize_dbt_commands raise ValueError to the caller
    return {
        'summary': str(payload.get('summary') or 'AI agent prepared a dbt Cloud run plan.'),
        'commands': sanitize_dbt_commands(commands),
        'checks': [str(item) for item in payload.get('checks', []) if str(item).strip()],
        'warnings': [str(item) for item in payload.get('warnings', []) if str(item).strip()],
    }


# ─── Agent prompt ─────────────────────────────────────────────────────────────

def build_dbt_agent_prompt(session_id, commands, bundle, job_steps=None):
    latest = bundle.get('latest') if bundle else None
    sql = latest['regenerated_sql'] if latest else ''
    description = latest['regenerated_text'] if latest else ''
    plan_text = (bundle.get('cached_plan') or {}).get('planText', '') if bundle else ''

    job_steps_block = ''
    if job_steps:
        job_steps_block = (
            "\nExisting dbt Cloud job steps (already configured — "
            "do NOT duplicate or conflict):\n"
            + json.dumps(job_steps, indent=2) + "\n"
        )

    return f"""You are a dbt Cloud deployment agent for a Qlik-to-dbt migration.
Review the generated model and decide the safest dbt Cloud commands to run.

Rules:
- Return JSON only.
- Keep commands as dbt CLI commands only.
- Do not add shell syntax, pipes, redirects, environment variable exports, package installs, or destructive filesystem commands.
- Prefer dbt build when it safely covers run and tests; keep explicit user commands if they are already safe and specific.
- Include a short summary, commands, checks, and warnings.

Session ID: {session_id or 'manual'}

Requested commands:
{json.dumps(commands, indent=2)}
{job_steps_block}
Generated SQL:
```sql
{sql or ''}
```

Generated description:
{description or ''}

Generation plan:
{plan_text or ''}

Expected JSON shape:
{{
  "summary": "what the agent decided",
  "commands": ["dbt ..."],
  "checks": ["what should be watched in dbt Cloud"],
  "warnings": ["risks or assumptions"]
}}
"""


# ─── Agent orchestration ──────────────────────────────────────────────────────

def plan_dbt_agent_run(session_id, commands, bundle, call_ai=None, job_steps=None):
    """
    Return a validated agent plan for the given dbt commands.

    Falls back to the original commands whenever AI planning is unavailable
    or produces an unparseable / unsafe response.
    """
    fallback = {
        'summary': 'Deterministic dbt agent plan prepared from the requested commands.',
        'commands': commands,
        'checks': ['Review dbt Cloud logs for model build and test failures.'],
        'warnings': [],
        'usedAi': False,
    }

    if not call_ai:
        fallback['warnings'].append('AI planning was unavailable; using the requested dbt commands.')
        return fallback

    prompt = build_dbt_agent_prompt(session_id, commands, bundle, job_steps=job_steps)
    try:
        response = call_ai(
            prompt,
            system_prompt='You are a careful dbt Cloud deployment agent. Return strict JSON only.',
            temperature=0,
            top_p=1,
            max_tokens=1200,
        )
    except AIClientError as exc:
        logger.warning("AI planning failed: %s", exc)
        fallback['warnings'].append(f'AI planning failed ({exc}); using the requested dbt commands.')
        return fallback

    try:
        planned = parse_agent_response(response)
    except ValueError as exc:
        logger.warning("AI plan sanitisation rejected: %s", exc)
        fallback['warnings'].append(
            f'AI plan was rejected ({exc}); using the requested dbt commands.'
        )
        return fallback

    if not planned:
        fallback['warnings'].append('AI planning response could not be parsed; using the requested dbt commands.')
        return fallback

    planned['usedAi'] = True
    return planned


# ─── Flask routes ─────────────────────────────────────────────────────────────

def register_dbt_cloud_routes(app, build_session_bundle, call_ai=None):

    @app.route('/api/dbt-cloud/test', methods=['POST'])
    def test_dbt_cloud_connection():
        data = request.get_json() or {}
        try:
            config = require_dbt_cloud_config(data)
            account_payload = dbt_cloud_request('GET', f"/accounts/{config['account_id']}/", config)
            projects_payload = dbt_cloud_request('GET', f"/accounts/{config['account_id']}/projects/", config)
            jobs_payload = dbt_cloud_request('GET', f"/accounts/{config['account_id']}/jobs/", config)

            projects = projects_payload.get('data', [])
            jobs = jobs_payload.get('data', [])
            if config['project_id']:
                jobs = [j for j in jobs if str(j.get('project_id')) == config['project_id']]

            return jsonify({
                'success': True,
                'account': account_payload.get('data', account_payload),
                'projects': [{'id': p.get('id'), 'name': p.get('name')} for p in projects],
                'jobs': [
                    {'id': j.get('id'), 'name': j.get('name'), 'projectId': j.get('project_id')}
                    for j in jobs
                ],
            })
        except Exception as err:
            # 401/403/validation errors are expected user mistakes — log as warning, no traceback
            is_user_error = isinstance(err, (ValueError, RuntimeError))
            if is_user_error:
                logger.warning("test_dbt_cloud_connection: %s", err)
            else:
                logger.error("test_dbt_cloud_connection unexpected error: %s", err, exc_info=True)
            return jsonify({'error': str(err)}), 400

    @app.route('/api/dbt-cloud/run', methods=['POST'])
    def run_dbt_cloud_job():
        data = request.get_json() or {}
        try:
            config = require_dbt_cloud_config(data, require_job=True)
            session_id = data.get('sessionId')

            # Only look up bundle when a session is provided
            bundle = None
            if session_id:
                bundle = build_session_bundle(session_id)
                if not bundle or not bundle.get('latest') or not bundle['latest'].get('regenerated_sql'):
                    return jsonify({'error': 'No generated dbt SQL is available for this session.'}), 404
            commands = sanitize_dbt_commands(data.get('commands') or [])

            # Fetch the job definition to get its existing steps (Fix 3: inject
            # into agent prompt so the LLM won't duplicate or conflict with them).
            job_steps = None
            try:
                job_payload = dbt_cloud_request(
                    'GET',
                    f"/accounts/{config['account_id']}/jobs/{config['job_id']}/",
                    config,
                    retries=1,
                )
                job_steps = job_payload.get('data', {}).get('execute_steps', [])
            except Exception as exc:
                logger.warning("Could not fetch job steps (non-fatal): %s", exc)

            # Queue depth check (Fix 2): warn user if the job queue is backed up.
            queue_warning = check_dbt_cloud_queue(config, config['job_id'])

            agent_plan = plan_dbt_agent_run(
                session_id, commands, bundle,
                call_ai=call_ai, job_steps=job_steps,
            )
            if queue_warning:
                agent_plan.setdefault('warnings', []).insert(0, queue_warning)
            commands = agent_plan['commands']

            payload = {
                'cause': f"QVF Decoder AI dbt agent deploy for session {session_id or 'manual'}",
                'steps_override': commands,
            }
            run_payload = dbt_cloud_request(
                'POST',
                f"/accounts/{config['account_id']}/jobs/{config['job_id']}/run/",
                config,
                json=payload,
            )
            run = run_payload.get('data', run_payload)
            return jsonify({
                'success': True,
                **serialize_dbt_run(run),
                'commands': commands,
                'agent': agent_plan,
            })
        except Exception as err:
            is_user_error = isinstance(err, (ValueError, RuntimeError))
            if is_user_error:
                logger.warning("run_dbt_cloud_job: %s", err)
            else:
                logger.error("run_dbt_cloud_job unexpected error: %s", err, exc_info=True)
            return jsonify({'error': str(err)}), 400

    @app.route('/api/dbt-cloud/status', methods=['POST'])
    def dbt_cloud_run_status():
        data = request.get_json() or {}
        try:
            config = require_dbt_cloud_config(data)
            run_id = str(data.get('runId') or '').strip()
            if not run_id:
                return jsonify({'error': 'dbt Cloud run ID is required.'}), 400
            run_payload = dbt_cloud_request(
                'GET', f"/accounts/{config['account_id']}/runs/{run_id}/", config
            )
            run = run_payload.get('data', run_payload)
            return jsonify({'success': True, **serialize_dbt_run(run)})
        except Exception as err:
            is_user_error = isinstance(err, (ValueError, RuntimeError))
            if is_user_error:
                logger.warning("dbt_cloud_run_status: %s", err)
            else:
                logger.error("dbt_cloud_run_status unexpected error: %s", err, exc_info=True)
            return jsonify({'error': str(err)}), 400


