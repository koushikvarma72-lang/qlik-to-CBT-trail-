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
6. Qlik Semantics – text-vs-numeric comparisons, raw YYYYMM in date fns,
                    SELECT * in CTEs, known source-data typos
7. dbt Config    – warns when {{ config(...) }} block is missing
8. Associative   – MONTHNAME() full-name mismatch, missing dimension joins

Usage
-----
    from backend.migration.validator import validate_migration_sql, ValidationIssue

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


def _blank_preserving_lines(text: str) -> str:
    return ''.join('\n' if ch == '\n' else ' ' for ch in text)


def _mask_sql_comments_and_strings(sql: str) -> str:
    """Blank comments and string literals while preserving offsets/line numbers."""
    if not sql:
        return sql

    def blank(match: re.Match) -> str:
        return _blank_preserving_lines(match.group(0))

    masked = re.sub(r'/\*[\s\S]*?\*/', blank, sql)
    masked = re.sub(r'--[^\n\r]*', blank, masked)
    masked = re.sub(r"'(?:''|[^'])*'", blank, masked)
    masked = re.sub(r'"(?:""|[^"])*"', blank, masked)
    return masked


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

    executable_sql = _mask_sql_comments_and_strings(sql)

    # Balanced parentheses
    depth = 0
    for i, ch in enumerate(executable_sql):
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
    for m in _BARE_DDL.finditer(executable_sql):
        line = sql[:m.start()].count('\n') + 1
        issues.append(ValidationIssue(
            'error', 'BARE_DDL',
            f'Disallowed DDL statement found: {m.group().strip()!r}',
            line=line,
            suggestion='dbt manages materialisation — remove DDL and use config() instead.',
        ))

    # Shell operators
    if _SHELL_OPS.search(executable_sql):
        line = _find_line(executable_sql, _SHELL_OPS)
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


def _extract_exact_source_name(value: str) -> str:
    source = str(value or '').strip()
    source = re.sub(r'\s*\([^)]*\)\s*$', '', source, flags=re.IGNORECASE).strip()
    source = source.strip('[]').strip("'\"").strip()
    match = re.search(r'([^/\\]+?)(?:\.[A-Za-z0-9_]+)?$', source)
    return (match.group(1) if match else source).strip()


def _pass_source_name_preservation(sql: str, plan: list) -> List[ValidationIssue]:
    """Preserve exact source table names unless an explicit dbt mapping exists."""
    issues: List[ValidationIssue] = []
    if not plan:
        return issues

    expected = set()
    for item in plan or []:
        for source in item.get('source_tables') or []:
            exact = _extract_exact_source_name(source)
            if exact:
                expected.add(exact)

    actual = set(_SOURCE_CALL.findall(sql or ''))
    for exact in sorted(expected):
        if '-' not in exact:
            continue
        underscored = exact.replace('-', '_')
        if underscored in actual and exact not in actual:
            line = _find_line(sql, re.compile(re.escape(underscored)))
            issues.append(ValidationIssue(
                'error',
                'SOURCE_TABLE_RENAMED',
                f'Source table "{exact}" was referenced as "{underscored}".',
                line=line,
                suggestion=(
                    f"Use source('raw', '{exact}') unless source.yml explicitly maps "
                    f"'{underscored}' to the original Qlik/source table."
                ),
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
    executable_sql = _mask_sql_comments_and_strings(sql)
    for pattern, code, message in _SECURITY_PATTERNS:
        m = pattern.search(executable_sql)
        if m:
            line = sql[:m.start()].count('\n') + 1
            issues.append(ValidationIssue('error', code, message, line=line))
    return issues


# ─── Pass 6 — Qlik-specific semantic checks ───────────────────────────────────

# Text-to-number comparison: a text column compared to a numeric literal
# Matches both quoted ("AccountDesc") and unquoted (AccountDesc) forms.
_TEXT_NUMERIC_CMP = re.compile(
    r'(?:"([A-Za-z_][A-Za-z0-9_]*(?:Desc|Name|Label|Code|Text|Title|Category|Type|Status))"'
    r'|([A-Za-z_][A-Za-z0-9_]*(?:Desc|Name|Label|Code|Text|Title|Category|Type|Status))\b)'
    r'\s*(?:>|<|>=|<=|=|!=|<>)\s*\d+\b',
    re.IGNORECASE,
)

# YYYYMM passed raw to a date function without the required TO_DATE cast.
# Strategy: find date-function calls containing YYYYMM, then filter out
# cases where YYYYMM is already wrapped in TO_DATE(...) or is a format string.
_YYYYMM_DATE_FN_CALL = re.compile(
    r'\b(DATEADD|DATE_TRUNC|DATEDIFF|MONTHS_BETWEEN|ADD_MONTHS)\s*\(([^)]+)\)',
    re.IGNORECASE,
)
_YYYYMM_BARE = re.compile(r'\bYYYYMM\b(?!\s*::)(?!\s*,\s*[\'"])', re.IGNORECASE)

# SELECT * in a non-final position (inside a CTE body, not the last SELECT)
_SELECT_STAR_IN_CTE = re.compile(r'\bAS\s*\(\s*SELECT\s*\*', re.IGNORECASE)

# Common source-data typos that should be flagged for manual review
_KNOWN_TYPOS = [
    ('expeensebudget', 'expensebudget'),
    ('expensebudegt', 'expensebudget'),
    ('calander', 'calendar'),
    ('custommer', 'customer'),
    ('prodcut', 'product'),
]


def _pass_qlik_semantics(sql: str) -> List[ValidationIssue]:
    """Qlik-migration-specific semantic checks."""
    issues: List[ValidationIssue] = []

    # 1. Text column compared to numeric literal
    for m in _TEXT_NUMERIC_CMP.finditer(sql):
        col = m.group(1) or m.group(2)  # group 1 = quoted form, group 2 = unquoted
        line = sql[:m.start()].count('\n') + 1
        issues.append(ValidationIssue(
            'warning', 'TEXT_NUMERIC_COMPARISON',
            f'Column "{col}" appears to be a text field but is compared to a numeric literal: {m.group().strip()!r}',
            line=line,
            suggestion=(
                f'Replace with a NULL/empty check: '
                f'"{col}" IS NOT NULL AND "{col}" != \'\' '
                f'or TRY_CAST("{col}" AS INTEGER) > 0 if numeric content is expected.'
            ),
        ))

    # 2. YYYYMM passed raw to a date function (not wrapped in TO_DATE)
    for m in _YYYYMM_DATE_FN_CALL.finditer(sql):
        fn_args = m.group(2)
        # Check if YYYYMM appears bare (not inside TO_DATE(...) and not as a format string)
        for ym in _YYYYMM_BARE.finditer(fn_args):
            # Walk back in fn_args to see if TO_DATE( precedes this YYYYMM
            preceding = fn_args[:ym.start()].upper()
            if 'TO_DATE(' in preceding or 'TO_DATE (' in preceding:
                continue  # safely wrapped
            line = sql[:m.start()].count('\n') + 1
            issues.append(ValidationIssue(
                'error', 'YYYYMM_RAW_IN_DATE_FN',
                f'YYYYMM passed directly to {m.group(1).upper()}() without TO_DATE cast: '
                f'{m.group().strip()[:80]!r}',
                line=line,
                suggestion="Wrap YYYYMM with TO_DATE(YYYYMM::varchar, 'YYYYMM') before passing to date functions.",
            ))
            break  # one issue per function call is enough

    # 3. SELECT * inside a CTE body (not the final SELECT)
    for m in _SELECT_STAR_IN_CTE.finditer(sql):
        line = sql[:m.start()].count('\n') + 1
        issues.append(ValidationIssue(
            'warning', 'SELECT_STAR_IN_CTE',
            'SELECT * inside a CTE body — downstream consumers may break if source schema changes.',
            line=line,
            suggestion='Enumerate explicit column names in CTE SELECT lists.',
        ))

    # 4. Known source-data typos
    sql_lower = sql.lower()
    for typo, correction in _KNOWN_TYPOS:
        if typo in sql_lower:
            line = _find_line(sql, re.compile(re.escape(typo), re.IGNORECASE))
            issues.append(ValidationIssue(
                'warning', 'LIKELY_TYPO',
                f'Possible typo "{typo}" found — did you mean "{correction}"?',
                line=line,
                suggestion=f'Check source table/column name: "{typo}" → "{correction}".',
            ))

    return issues


# ─── Pass 7 — dbt model config ────────────────────────────────────────────────

_DBT_CONFIG_BLOCK = re.compile(r'\{\{\s*config\s*\(', re.IGNORECASE)
_MALFORMED_DBT_CONFIG_BLOCK = re.compile(r'(?<!\{)\{\s*config\s*\(|\bconfig\s*\([^)]*\)\s*\}(?!\})', re.IGNORECASE)


def _pass_dbt_config(sql: str, dialect: str) -> List[ValidationIssue]:
    """Warn when a dbt model is missing a {{ config(...) }} block."""
    issues: List[ValidationIssue] = []
    if dialect.lower() not in ('dbt', 'snowflake', 'bigquery', 'databricks', 'redshift', 'postgres'):
        return issues
    malformed = _MALFORMED_DBT_CONFIG_BLOCK.search(sql)
    if malformed:
        issues.append(ValidationIssue(
            'error', 'MALFORMED_DBT_CONFIG',
            'dbt config block uses invalid Jinja braces.',
            line=sql[:malformed.start()].count('\n') + 1,
            suggestion="Use exactly: {{ config(materialized='table', tags=['qlik_migration']) }}",
        ))
    if not _DBT_CONFIG_BLOCK.search(sql):
        issues.append(ValidationIssue(
            'error', 'MISSING_DBT_CONFIG',
            'No {{ config(...) }} block found in the dbt model.',
            suggestion=(
                "Add a config block at the top, e.g.:\n"
                "{{ config(materialized='table', tags=['qlik_migration']) }}"
            ),
        ))
    return issues


# ─── Pass 8 — Qlik associative model / missing joins ─────────────────────────

# Detect MONTHNAME() which returns full names, not Qlik-compatible abbreviated names
_MONTHNAME_CALL = re.compile(r'\bMONTHNAME\s*\(', re.IGNORECASE)

# Detect a final SELECT that reads from a single CTE with no JOINs —
# a sign that dimension tables defined earlier are silently dropped.
_FINAL_SELECT_NO_JOIN = re.compile(
    r'SELECT\s+\*\s+FROM\s+(\w+)\s*$',
    re.IGNORECASE,
)


def _pass_associative_model(sql: str) -> List[ValidationIssue]:
    """Warn about patterns that indicate the Qlik associative model wasn't translated."""
    issues: List[ValidationIssue] = []

    # 1. MONTHNAME() produces full names, not Qlik-compatible abbreviated names
    for m in _MONTHNAME_CALL.finditer(sql):
        line = sql[:m.start()].count('\n') + 1
        issues.append(ValidationIssue(
            'warning', 'MONTHNAME_FULL_NAME',
            'MONTHNAME() returns full month names ("January") but Qlik Month() returns '
            'abbreviated names ("Jan"). This will break downstream joins or filters.',
            line=line,
            suggestion="Replace MONTHNAME(expr) with TO_CHAR(expr, 'Mon') to match Qlik behaviour.",
        ))

    # 2. Final SELECT reads from one CTE with no JOINs while multiple CTEs are defined.
    # Only flag when the ENTIRE model has zero JOINs — if any CTE body contains a JOIN
    # the developer has already thought about relationships.
    cte_count = len(re.findall(r'\bAS\s*\(', sql, re.IGNORECASE))
    final_match = _FINAL_SELECT_NO_JOIN.search(sql.rstrip())
    if final_match and cte_count > 2:
        has_any_join = bool(re.search(r'\bJOIN\b', sql, re.IGNORECASE))
        if not has_any_join:
            final_cte = final_match.group(1)
            issues.append(ValidationIssue(
                'warning', 'MISSING_DIMENSION_JOINS',
                f'Final SELECT reads only from `{final_cte}` with no JOINs anywhere in the model, '
                f'but {cte_count} CTEs are defined. In Qlik all tables join automatically via '
                f'shared field names — in SQL every relationship must be written explicitly.',
                suggestion=(
                    f'Add a final_model CTE that LEFT JOINs every dimension to `{final_cte}` '
                    f'on their shared key fields. Use LEFT JOIN so no fact rows are dropped. '
                    f'If a join key is uncertain, add a -- TODO: verify join key comment.'
                ),
            ))


    # 3. Generic one-shot quality gate: multiple CTEs should produce a final_model/final_mart.
    cte_names = re.findall(r'\b([A-Za-z_][A-Za-z0-9_]*)\s+AS\s*\(', sql, re.IGNORECASE)
    cte_set = {c.lower() for c in cte_names}
    if len(cte_set) >= 3 and not (cte_set & {'final_model', 'final_mart'}):
        issues.append(ValidationIssue(
            'error', 'FINAL_MODEL_MISSING',
            'Multiple CTEs were generated but no final_model/final_mart CTE exists.',
            suggestion='Create final_model and end with SELECT * FROM final_model.',
        ))

    # 4. Generic unused CTE check. This catches one-shot outputs that create useful CTEs
    # but never route them into the final mart. It is not tied to any particular Qlik file.
    if len(cte_set) >= 3:
        reference_counts = {c: 0 for c in cte_set}
        for m in re.finditer(r'\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)\b', sql, re.IGNORECASE):
            ref = m.group(1).lower()
            if ref in reference_counts:
                reference_counts[ref] += 1
        unused = sorted(c for c in cte_set if c not in {'final_model', 'final_mart'} and reference_counts.get(c, 0) == 0)
        if unused:
            issues.append(ValidationIssue(
                'warning', 'UNREACHABLE_CTE_CREATED_NOT_USED',
                'CTEs are created but never referenced downstream: ' + ', '.join(unused) + '.',
                suggestion='Join these CTEs into final_model, feed them into another CTE, or remove them.',
            ))

    return issues


# ─── Pass 9 — Schema Contract ────────────────────────────────────────────────

def _pass_schema_contract(sql: str) -> List[ValidationIssue]:
    """Verify that a commented schema contract is present at the top of the SQL."""
    issues: List[ValidationIssue] = []
    # Look for commented headers indicating SOURCE FIELD REGISTRY, DATE FIELD TYPES, etc.
    sql_lines = [line.strip().upper() for line in sql.splitlines()[:25]]
    has_registry = any('SOURCE FIELD REGISTRY' in line or 'SOURCE_FIELD_REGISTRY' in line for line in sql_lines)
    has_date_types = any('DATE FIELD TYPES' in line or 'DATE_FIELD_TYPES' in line for line in sql_lines)
    has_grain = any('ISLAND TABLE GRAINS' in line or 'ISLAND_TABLE_GRAINS' in line for line in sql_lines)
    
    if not (has_registry and has_date_types and has_grain):
        issues.append(ValidationIssue(
            'warning', 'MISSING_SCHEMA_CONTRACT',
            'No Schema Contract block found at the top of the SQL. A contract block must comment SOURCE FIELD REGISTRY, DATE FIELD TYPES, and ISLAND TABLE GRAINS.',
            suggestion=(
                "Add a contract block at the top of your model, e.g.:\n"
                "-- SOURCE FIELD REGISTRY\n"
                "-- DATE FIELD TYPES\n"
                "-- ISLAND TABLE GRAINS"
            )
        ))
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
    all_issues.extend(_pass_source_name_preservation(sql, plan or []))
    all_issues.extend(_pass_dialect(sql, dialect))
    all_issues.extend(_pass_security(sql))
    all_issues.extend(_pass_qlik_semantics(sql))
    all_issues.extend(_pass_dbt_config(sql, dialect))
    all_issues.extend(_pass_associative_model(sql))
    all_issues.extend(_pass_schema_contract(sql))

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
