"""
migration_validator.py
======================
Multi-pass SQL validation for QVF → dbt migrations.

Architectural role
------------------
Sits between the AI response parser and the DB persistence layer.
Each pass is a pure function (sql, plan) → list[Issue].  Passes run in order;
later passes can rely on earlier ones having already fired.

Passes
------
1. Structural    – basic parse-ability (balanced parens, no bare DDL)
2. Plan Coverage – every expected model/CTE in the generation plan has a
                   corresponding SQL block
3. Ref Integrity – {{ ref(...) }} calls resolve to known staging models
4. Dialect       – dialect-specific keyword checks (dbt, powerbi, bigquery…)
5. Security      – blocks dangerous patterns (DROP, TRUNCATE, shell ops)

Usage
-----
    from migration_validator import validate_migration_sql, ValidationIssue

    issues = validate_migration_sql(sql, plan, dialect='dbt')
    for issue in issues:
        print(issue.level, issue.code, issue.message)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional


# ─── Data types ──────────────────────────────────────────────────────────────

@dataclass
class ValidationIssue:
    level: str          # 'error' | 'warning' | 'info'
    code: str           # machine-readable short code, e.g. 'UNBALANCED_PARENS'
    message: str
    line: Optional[int] = None
    suggestion: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            'level': self.level,
            'code': self.code,
            'message': self.message,
            'line': self.line,
            'suggestion': self.suggestion,
        }

    def __str__(self) -> str:
        loc = f" (line {self.line})" if self.line else ""
        return f"[{self.level.upper()}] {self.code}{loc}: {self.message}"


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _lines(sql: str) -> List[str]:
    return sql.splitlines()


def _find_line(sql: str, pattern: re.Pattern) -> Optional[int]:
    for i, line in enumerate(_lines(sql), start=1):
        if pattern.search(line):
            return i
    return None


# ─── Pass 1 — Structural ─────────────────────────────────────────────────────

_BARE_DDL = re.compile(
    r'^\s*(DROP|TRUNCATE|ALTER|CREATE\s+OR\s+REPLACE|DELETE\s+FROM)\s',
    re.IGNORECASE | re.MULTILINE,
)
# Note: | is excluded from the character class because Snowflake uses || for string
# concatenation. We match a lone | only when NOT preceded or followed by another |.
# We also exclude :: which is valid Snowflake cast syntax.
_SHELL_OPS = re.compile(r'[;&`]|(?<!\|)\|(?!\|)|\$\(', re.MULTILINE)


def _pass_structural(sql: str) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []

    if not sql or not sql.strip():
        issues.append(ValidationIssue('error', 'EMPTY_SQL', 'Generated SQL is empty.'))
        return issues

    # Balanced parentheses
    depth = 0
    for i, ch in enumerate(sql):
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        if depth < 0:
            line = sql[:i].count('\n') + 1
            issues.append(ValidationIssue(
                'error', 'UNBALANCED_PARENS',
                'Unmatched closing parenthesis.',
                line=line,
                suggestion='Check CTE definitions and subquery nesting.',
            ))
            break
    if depth > 0:
        issues.append(ValidationIssue(
            'error', 'UNBALANCED_PARENS',
            f'{depth} unclosed parenthesis(es) at end of SQL.',
            suggestion='Ensure every opening "(" has a matching ")".',
        ))

    # Bare DDL
    for m in _BARE_DDL.finditer(sql):
        line = sql[:m.start()].count('\n') + 1
        issues.append(ValidationIssue(
            'error', 'BARE_DDL',
            f'Disallowed DDL statement found: {m.group().strip()!r}',
            line=line,
            suggestion='dbt manages materialisation — remove DDL and use config() instead.',
        ))

    # Shell operators
    if _SHELL_OPS.search(sql):
        line = _find_line(sql, _SHELL_OPS)
        issues.append(ValidationIssue(
            'error', 'SHELL_OPERATOR',
            'Shell operator or subshell syntax detected in SQL.',
            line=line,
            suggestion='Remove all shell metacharacters (;, &, |, `, $(...)).',
        ))

    return issues


# ─── Pass 2 — Plan Coverage ───────────────────────────────────────────────────

def _pass_plan_coverage(sql: str, plan: list) -> List[ValidationIssue]:
    """
    Each item in the generation plan should have a corresponding SELECT/CTE
    block in the generated SQL.
    """
    issues: List[ValidationIssue] = []
    if not plan:
        return issues

    sql_upper = sql.upper()

    for item in plan:
        model_name = (item.get('modelName') or item.get('model') or '').strip()
        if not model_name:
            continue
        # Accept either a CTE name or a dbt ref
        if model_name.upper() not in sql_upper:
            issues.append(ValidationIssue(
                'warning', 'MISSING_PLAN_MODEL',
                f'Generation plan model "{model_name}" has no corresponding block in the SQL.',
                suggestion=f'Add a CTE or model named "{model_name}" or update the plan.',
            ))

    return issues


# ─── Pass 3 — Ref Integrity ───────────────────────────────────────────────────

_REF_CALL = re.compile(r"\{\{\s*ref\s*\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}", re.IGNORECASE)
_SOURCE_CALL = re.compile(r"\{\{\s*source\s*\(\s*['\"][^'\"]+['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}", re.IGNORECASE)


def _pass_ref_integrity(sql: str, known_staging_models: Optional[List[str]] = None) -> List[ValidationIssue]:
    """
    Verify that every {{ ref('...') }} call targets a model that either:
    - Appears as a CTE name in the same SQL, OR
    - Is in the provided known_staging_models list (from dbt_package_agent staging layer)
    """
    issues: List[ValidationIssue] = []

    # Collect CTE names defined in this SQL
    defined_ctes = set(re.findall(r'(\w+)\s+AS\s*\(', sql, re.IGNORECASE))
    staging = set(known_staging_models or [])

    for m in _REF_CALL.finditer(sql):
        ref_name = m.group(1)
        if ref_name not in defined_ctes and ref_name not in staging:
            line = sql[:m.start()].count('\n') + 1
            issues.append(ValidationIssue(
                'warning', 'UNRESOLVED_REF',
                f'{{{{ ref(\'{ref_name}\') }}}} does not resolve to a known CTE or staging model.',
                line=line,
                suggestion=f'Define a CTE named "{ref_name}" or add a staging model stg_{ref_name}.sql.',
            ))

    return issues


# ─── Pass 4 — Dialect ─────────────────────────────────────────────────────────

_DIALECT_RULES: dict[str, List[tuple]] = {
    'dbt': [
        (re.compile(r'\bTOP\s+\d+\b', re.IGNORECASE), 'warning', 'DIALECT_TOP',
         'TOP N syntax is not standard SQL; use LIMIT instead.', 'Replace TOP N with LIMIT N.'),
        (re.compile(r'\bNOLOCK\b', re.IGNORECASE), 'warning', 'DIALECT_NOLOCK',
         'NOLOCK hint is SQL Server-specific and not valid in dbt.', 'Remove the NOLOCK hint.'),
        (re.compile(r'\bIF\s+OBJECT_ID\b', re.IGNORECASE), 'error', 'DIALECT_MSSQL_IDIOM',
         'SQL Server-specific idiom detected.', 'Use dbt macros or Jinja conditionals instead.'),
        (re.compile(r'\bGO\b', re.IGNORECASE | re.MULTILINE), 'warning', 'DIALECT_GO',
         'SQL Server GO batch separator is invalid in dbt.', 'Remove GO statements.'),
    ],
    'bigquery': [
        (re.compile(r'\bLIMIT\s+\d+\s+OFFSET\b', re.IGNORECASE), 'info', 'DIALECT_BQ_OFFSET',
         'OFFSET syntax may behave differently in BigQuery.', 'Use LIMIT N OFFSET M carefully.'),
    ],
    'snowflake': [],
    'redshift': [],
    'powerbi': [
        (re.compile(r'\{\{\s*(ref|source)\s*\(', re.IGNORECASE), 'error', 'DIALECT_DBT_IN_POWERBI',
         'dbt Jinja ref/source calls are not valid in Power BI M/DAX.', 'Remove Jinja templating.'),
    ],
}


def _pass_dialect(sql: str, dialect: str) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    rules = _DIALECT_RULES.get(dialect.lower(), [])
    for pattern, level, code, message, suggestion in rules:
        m = pattern.search(sql)
        if m:
            line = sql[:m.start()].count('\n') + 1
            issues.append(ValidationIssue(level, code, message, line=line, suggestion=suggestion))
    return issues


# ─── Pass 5 — Security ────────────────────────────────────────────────────────

_SECURITY_PATTERNS = [
    (re.compile(r'\bDROP\s+(TABLE|VIEW|SCHEMA|DATABASE)\b', re.IGNORECASE),
     'SECURITY_DROP', 'DROP statement detected — this would destroy data.'),
    (re.compile(r'\bTRUNCATE\s+TABLE\b', re.IGNORECASE),
     'SECURITY_TRUNCATE', 'TRUNCATE TABLE detected — use dbt snapshots or incremental models instead.'),
    (re.compile(r';\s*--\s*injection', re.IGNORECASE),
     'SECURITY_INJECTION_MARKER', 'Possible SQL injection marker found.'),
    (re.compile(r"'\s*OR\s*'1'\s*=\s*'1", re.IGNORECASE),
     'SECURITY_SQLI', 'Classic SQL injection pattern detected.'),
]


def _pass_security(sql: str) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    for pattern, code, message in _SECURITY_PATTERNS:
        m = pattern.search(sql)
        if m:
            line = sql[:m.start()].count('\n') + 1
            issues.append(ValidationIssue('error', code, message, line=line))
    return issues


# ─── Public API ───────────────────────────────────────────────────────────────

def validate_migration_sql(
    sql: str,
    plan: Optional[list] = None,
    dialect: str = 'dbt',
    known_staging_models: Optional[List[str]] = None,
) -> List[ValidationIssue]:
    """
    Run all validation passes against the generated SQL.

    Returns a list of ValidationIssue objects ordered by severity then pass order.
    An empty list means no issues were found.
    """
    all_issues: List[ValidationIssue] = []

    all_issues.extend(_pass_structural(sql))

    # Short-circuit on structural errors — later passes may crash on broken SQL
    if any(i.level == 'error' for i in all_issues):
        return all_issues

    all_issues.extend(_pass_plan_coverage(sql, plan or []))
    all_issues.extend(_pass_ref_integrity(sql, known_staging_models))
    all_issues.extend(_pass_dialect(sql, dialect))
    all_issues.extend(_pass_security(sql))

    # Sort: errors first, then warnings, then info
    _order = {'error': 0, 'warning': 1, 'info': 2}
    all_issues.sort(key=lambda i: _order.get(i.level, 3))

    return all_issues


def needs_repair(issues: List[ValidationIssue]) -> bool:
    """Return True if the issues list contains anything that warrants an AI repair pass."""
    return any(i.level == 'error' for i in issues)


def issues_to_strings(issues: List[ValidationIssue]) -> List[str]:
    """Serialise issues to simple strings for backward compatibility."""
    return [str(i) for i in issues]
