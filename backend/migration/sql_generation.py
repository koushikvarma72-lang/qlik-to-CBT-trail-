import hashlib
import inspect
import json
import logging
import os
import re
import zipfile
from datetime import datetime
from backend.extraction.qvf_runtime import extract_model_from_script
from backend.extraction.qlik_script_parser import parse_qlik_load_script
from backend.storage_config import ARTIFACT_FOLDER

try:
    from backend.migration.ir import (
        audit_sql_against_ir,
        build_migration_ir,
        format_ir_for_prompt,
        render_ir_contract_comment,
        validate_ir,
    )
except Exception:  # pragma: no cover - keeps legacy installs working.
    audit_sql_against_ir = None
    build_migration_ir = None
    format_ir_for_prompt = None
    render_ir_contract_comment = None
    validate_ir = None

ONE_SHOT_MAX_TOKENS = int(os.environ.get('ONE_SHOT_MAX_TOKENS', '10000'))
LOOP_MAX_TOKENS = int(os.environ.get('LOOP_MAX_TOKENS', '4000'))
REPAIR_MAX_TOKENS = int(os.environ.get('REPAIR_MAX_TOKENS', '2200'))
MIN_REQUIRED_OUTPUT_TOKENS = int(os.environ.get('MIN_REQUIRED_OUTPUT_TOKENS', '1500'))
MIN_FULL_SQL_TOKENS = MIN_REQUIRED_OUTPUT_TOKENS
MIN_REPAIR_SQL_TOKENS = MIN_REQUIRED_OUTPUT_TOKENS

logger = logging.getLogger(__name__)


class MigrationTokenBudgetError(RuntimeError):
    """Raised when the configured output budget is too small for SQL generation."""


def hash_text(text):
    return hashlib.sha256((text or '').encode('utf-8')).hexdigest()


def generate_sql_fingerprint(sql: str) -> str:
    """Return a stable hash for semantically equivalent generated SQL formatting."""
    text = str(sql or '')
    text = re.sub(r'--.*?$', '', text, flags=re.MULTILINE)
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    text = re.sub(r'\s+', ' ', text).strip().lower()
    text = re.sub(r'\s*,\s*', ',', text)
    text = re.sub(r'\s*\(\s*', '(', text)
    text = re.sub(r'\s*\)\s*', ')', text)
    text = re.sub(r'\s*=\s*', '=', text)
    text = re.sub(r'\s+\bas\b\s+', ' as ', text)
    text = re.sub(r',\s*(from|where|group by|order by|having|qualify)\b', r' \1', text)
    return hash_text(text)


def prune_inline_loads(script_text):
    """
    Find huge inline data blocks in Qlik scripts and collapse them to keep the context size
    small while preserving the column schema for the ML model.
    """
    if not script_text:
        return script_text

    def _prune_block(match):
        headers_and_body = match.group(3) or ''
        lines = headers_and_body.split('\n')
        if len(lines) <= 6:
            return match.group(0) # Keep small inlines as-is
        
        # Keep headers (usually line 0) and the first 3 data rows
        preserved_lines = []
        for line in lines:
            line_str = line.strip()
            if not line_str:
                continue
            preserved_lines.append(line)
            if len(preserved_lines) >= 4:
                break
        
        preserved_lines.append(f"    // ... [Pruned {len(lines) - len(preserved_lines)} inline data rows for LLM context optimization] ...")
        return f"{match.group(1) or match.group(2)}:\nLOAD INLINE [\n" + "\n".join(preserved_lines) + "\n];"

    # Match tables named or unlabeled ending in INLINE LOAD blocks
    pattern = re.compile(
        r'(?:\[([^\]]+)\]|([A-Za-z0-9_\$]+)):\s*LOAD[\s\S]*?\bINLINE\s*\[([\s\S]*?)\]\s*;',
        re.IGNORECASE,
    )
    return pattern.sub(_prune_block, script_text)


def optimize_qvs_for_context(qvs_script, max_chars=25_000):
    """
    Intelligently prune large Qlik scripts by collapsing massive inline tables and summarizing
    remaining sections when the script exceeds the model context budget.
    """
    if not qvs_script:
        return qvs_script

    # 1. Collapse large INLINE loads first
    optimized = prune_inline_loads(qvs_script)

    if len(optimized) <= max_chars:
        return optimized

    # 2. Split statements cleanly by semicolon and preserve complete blocks
    statements = [stmt.strip() for stmt in optimized.split(';') if stmt.strip()]
    kept_statements = []
    current_len = 0

    for stmt in statements:
        stmt_str = stmt + ';\n'
        if current_len + len(stmt_str) <= max_chars - 800:
            kept_statements.append(stmt_str)
            current_len += len(stmt_str)
        else:
            break

    remaining = len(statements) - len(kept_statements)
    if remaining > 0:
        summary_lines = [
            '\n\n// ... [Truncated additional Qlik script sections to fit model context limits] ...',
            f'// {remaining} more script sections were omitted in full.',
            '// Summaries of omitted sections follow:',
        ]

        for stmt in statements[len(kept_statements):len(kept_statements) + 5]:
            first_line = stmt.splitlines()[0].strip()
            first_line = re.sub(r'\s+', ' ', first_line)
            summary_lines.append(f'// {first_line[:140]}')

        summary_lines.append(
            '// Please preserve the meaning of the omitted sections using the explicit context already provided.'
        )
        kept_statements.append('\n'.join(summary_lines))

    return ''.join(kept_statements)


def _strip_non_sql_script_fragments(script_text):
    """Remove SET/LET definitions and fenced JSON blocks from Qlik script context."""
    if not script_text:
        return script_text

    script = re.sub(r'```(?:json)?[\s\S]*?```', '', script_text, flags=re.IGNORECASE)
    script = re.sub(r'(?im)^\s*(?:SET|LET)\b.*?;\s*', '', script)
    return script


def _normalize_identifier(name):
    value = str(name or '').strip()
    if not value:
        return None
    return value.strip('[]')


def _split_sql_like_fields(field_text):
    if not field_text:
        return []

    fields = []
    token = []
    depth = 0
    in_single = False
    in_double = False
    in_bracket = False

    for char in field_text:
        if char == "'" and not in_double and not in_bracket:
            in_single = not in_single
        elif char == '"' and not in_single and not in_bracket:
            in_double = not in_double
        elif char == '[' and not in_single and not in_double:
            in_bracket = True
        elif char == ']' and in_bracket and not in_single and not in_double:
            in_bracket = False
        elif not in_single and not in_double and not in_bracket:
            if char == '(':
                depth += 1
            elif char == ')' and depth > 0:
                depth -= 1
            elif char == ',' and depth == 0:
                item = ''.join(token).strip()
                if item:
                    fields.append(item)
                token = []
                continue
        token.append(char)

    tail = ''.join(token).strip()
    if tail:
        fields.append(tail)
    return fields


def split_top_level_csv(field_text):
    """Split a SQL select-list by top-level commas while preserving quoted identifiers."""
    return _split_sql_like_fields(field_text)


def _clean_qlik_field_name(field_name):
    value = str(field_name or '').strip()
    if not value:
        return value
    if value.startswith('[') and value.endswith(']'):
        return value[1:-1]
    return value


def _format_sql_identifier(identifier):
    value = str(identifier or '').strip()
    if not value:
        return value
    if value.startswith('[') and value.endswith(']'):
        return f'"{value[1:-1]}"'
    return value


def _resolve_source_reference(source):
    value = str(source or '').strip()
    if not value:
        return ''

    normalized = value.strip("'\"").strip()
    # Qlik FROM clauses often include bracketed lib paths plus load options, e.g.
    # [lib://Data/Sales.qvd] (qvd) or 'lib://Data/File.xlsx' (ooxml, ...).
    # Strip those wrappers before deriving a stable dbt source name.
    normalized = re.sub(r'\s*\([^)]*\)\s*$', '', normalized, flags=re.IGNORECASE).strip()
    normalized = normalized.strip('[]').strip("'\"").strip()

    match = re.search(r'([^/\\]+?)(?:\.[A-Za-z0-9_]+)?$', normalized)
    if not match:
        return normalized

    source_name = match.group(1).strip()
    if not source_name:
        return normalized

    return "{{ source('raw', '%s') }}" % source_name


def canonical_source_identity(value):
    """Return a logical table identity for Qlik paths and dbt source names."""
    text = str(value or '').strip()
    if not text:
        return ''

    source_match = re.search(
        r"\{\{\s*source\s*\(\s*['\"][^'\"]+['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}",
        text,
        flags=re.IGNORECASE,
    )
    if source_match:
        text = source_match.group(1)

    text = re.sub(r'\s*\([^)]*\)\s*$', '', text, flags=re.IGNORECASE).strip()
    text = text.strip('[]').strip("'\"").strip()
    match = re.search(r'([^/\\]+?)(?:\.[A-Za-z0-9_]+)?$', text)
    if match:
        text = match.group(1)
    return re.sub(r'[^a-z0-9]+', '', text.lower())


def _safe_cte_name(name, fallback='load_block'):
    value = str(name or fallback).strip().strip('[]')
    value = re.sub(r'[^A-Za-z0-9_]+', '_', value).strip('_').lower()
    if not value:
        value = fallback
    if re.match(r'^\d', value):
        value = f'_{value}'
    return value


def generate_dbt_config_block(materialized='table', tags=None, cluster_by=None, dialect='dbt'):
    """Return a {{ config(...) }} Jinja block for a dbt model.

    Args:
        materialized: 'table' | 'view' | 'incremental'
        tags: list of string tags, defaults to ['qlik_migration']
        cluster_by: optional list of columns for Snowflake/BigQuery clustering
        dialect: used to decide whether to include warehouse-specific options
    """
    tags = tags or ['qlik_migration']
    tag_str = ', '.join(f"'{t}'" for t in tags)
    parts = [f"materialized='{materialized}'", f"tags=[{tag_str}]"]
    if cluster_by and dialect.lower() in ('snowflake', 'bigquery', 'databricks'):
        cols = ', '.join(f"'{c}'" for c in cluster_by)
        parts.append(f'cluster_by=[{cols}]')
    return '{{{{ config({}) }}}}'.format(', '.join(parts))


def _infer_sql_type_from_name(field_name: str) -> str:
    """Infer a SQL column type from a field name using the same heuristics as Qlik tag analysis.

    Used to emit typed NULLs in UNION ALL branches so Snowflake/Databricks don't
    fail on untyped NULL inference across branches with different schemas.
    """
    name = str(field_name or '').lower().strip().strip('"').strip('`').strip('[]').replace(' ', '_').replace('-', '_')
    if 'account' in name:
        return 'VARCHAR'
    # Date/time fields
    if re.search(r'(date|_dt|_at|timestamp|yyyymm)$', name) or 'date' in name or name == 'yyyymm' or name.endswith('at'):
        return 'DATE'
    # Boolean-ish fields
    if re.search(r'^(is_|has_)', name) or name.startswith('is') or name.startswith('has') or name.endswith('_flag'):
        return 'BOOLEAN'
    # Numeric/key fields
    if re.search(r'(amount|qty|quantity|count|total|price|cost|revenue|sales|'
                 r'gross|balance|budget|actual|margin|_id|id$|key$|num$|no$|'
                 r'number$|code$|flag$|_flag$|year$|month$|quarter$|day$)$', name) or name.endswith('key') or name.endswith('id'):
        return 'NUMBER'
    # Default: text
    return 'VARCHAR'


def _typed_null(col_name: str) -> str:
    """Return a typed NULL expression for a column, e.g. CAST(NULL AS VARCHAR) AS "col"."""
    sql_type = _infer_sql_type_from_name(col_name)
    return f'CAST(NULL AS {sql_type}) AS "{col_name}"'


def _build_ir_context(plan, qvs_script):
    """Build the Qlik ownership/grain contract used by prompts and validation."""
    if not build_migration_ir:
        return None, [], '', ''
    try:
        ir = build_migration_ir(plan or [], qvs_script or '')
        issues = validate_ir(ir) if validate_ir else []
        contract = render_ir_contract_comment(ir) if render_ir_contract_comment else ''
        prompt_summary = format_ir_for_prompt(ir) if format_ir_for_prompt else ''
        return ir, issues, contract, prompt_summary
    except Exception as exc:
        return None, [f'IR_BUILD_FAILED: {exc}'], '', ''


def _format_ir_issues_for_sql(issues):
    formatted = []
    for issue in issues or []:
        code = getattr(issue, 'code', None)
        level = getattr(issue, 'level', None)
        message = getattr(issue, 'message', None)
        if code or message:
            prefix = f'{code}: ' if code else ''
            formatted.append(f'{prefix}{message or str(issue)}')
        else:
            formatted.append(str(issue))
    return formatted


def _canonical_table_alias(table_name: str) -> str:
    """Return stable aliases for high-risk joins to reduce model ambiguity."""
    name = _safe_cte_name(table_name)
    preferred = {
        'customer_map': 'cmap',
        'customer_master': 'cust',
        'item_branch_master': 'ibm',
        'item_master': 'im',
    }
    if name in preferred:
        return preferred[name]
    parts = [p for p in name.split('_') if p]
    if not parts:
        return 't'
    alias = ''.join(part[0] for part in parts if part[0].isalpha())
    return alias[:4] or parts[0][:1] or 't'


def _normalize_sql_name(name: str) -> str:
    return re.sub(r'[^a-z0-9_]+', '', str(name or '').strip().strip('[]"').lower())


def _plan_table_fields(plan):
    """Best-effort table -> output fields map from the generation plan."""
    table_fields = {}
    for item in plan or []:
        table = _safe_cte_name(item.get('table') or '')
        if not table:
            continue
        fields = table_fields.setdefault(table, [])
        for field in item.get('fields') or []:
            name = _extract_output_column_name(field)
            if name and name not in fields:
                fields.append(name)
    return table_fields


def _fallback_join_candidates_from_plan(plan):
    """Conservative fallback joins from shared-key metadata when IR joins are sparse."""
    safe_keys = {
        'monthlyregionkey', 'custkey', 'custkeyar', 'addressnumber',
        'itembranchkey', 'shortname', 'productgroup', 'sales_rep', 'salesrep',
    }
    forbidden_tables = {'expenses', 'expenses_for_fact', 'int_expenses', 'expenses_aggregated', 'int_expenses_aggregated'}
    table_fields = _plan_table_fields(plan)
    tables = sorted(table_fields.keys())
    lines = []
    warnings = []

    def low_risk_pair(left_table, right_table, key_name):
        pair = {left_table, right_table}
        if pair & forbidden_tables:
            return False
        if key_name == 'monthlyregionkey':
            return 'facttable_with_expenses' in pair and bool(pair & {'budget', 'calendar'})
        if key_name == 'custkey':
            return 'facttable_with_expenses' in pair and 'customer_map' in pair
        if key_name == 'custkeyar':
            return 'customer_map' in pair and bool(pair & {'ar_summary', 'ar_summary_1'})
        if key_name in {'addressnumber'}:
            return 'facttable_with_expenses' in pair and 'customer_master' in pair
        if key_name in {'sales_rep', 'salesrep'}:
            return 'customer_master' in pair and 'sales_rep_master' in pair
        if key_name == 'itembranchkey':
            return 'facttable_with_expenses' in pair and 'item_branch_master' in pair
        if key_name == 'shortname':
            return 'item_branch_master' in pair and 'item_master' in pair
        if key_name == 'productgroup':
            return 'item_master' in pair and 'product_group_master' in pair
        return False

    for i, left in enumerate(tables):
        left_norm = {_normalize_sql_name(f): f for f in table_fields.get(left, [])}
        for right in tables[i + 1:]:
            right_norm = {_normalize_sql_name(f): f for f in table_fields.get(right, [])}
            shared = sorted(set(left_norm.keys()) & set(right_norm.keys()))
            for key in shared:
                if key not in safe_keys:
                    continue
                if not low_risk_pair(left, right, key):
                    continue
                left_key = left_norm[key]
                right_key = right_norm[key]
                la = _canonical_table_alias(left)
                ra = _canonical_table_alias(right)
                lines.append(
                    f"- {left}.{left_key} -> {right}.{right_key} | aliases: {left}={la}, {right}={ra} | source: metadata_fallback"
                )
    if not lines:
        warnings.append('FALLBACK_JOIN_CONTRACT_EMPTY: no low-risk shared-key metadata joins found.')
    return lines, warnings


def build_join_contract(plan, qvs_script=''):
    """Build a deterministic join contract from IR joins only.

    Returns:
        dict with:
          - join_lines: prompt-ready allowed join paths
          - warnings: omitted/unsafe paths and IR caveats
          - required_aliases: mandatory alias map for known risky dimensions
          - forbidden_patterns: hard disallowed join patterns
          - text: compact text block for prompt injection
    """
    ir, ir_issues, _, _ = _build_ir_context(plan or [], qvs_script or '')
    warnings = []
    if ir_issues:
        warnings.extend(_format_ir_issues_for_sql(ir_issues))

    required_aliases = {
        'customer_map': 'cmap',
        'customer_master': 'cust',
        'item_branch_master': 'ibm',
        'item_master': 'im',
        'sales_rep_master': 'srm',
    }
    date_contract_lines = []
    if ir and getattr(ir, 'date_registry', None):
        for entry in ir.date_registry.values():
            field_name = getattr(entry, 'field_name', '')
            storage_type = getattr(entry, 'storage_type', 'unknown')
            conversion_sql = getattr(entry, 'conversion_sql', '')
            if not field_name:
                continue
            if storage_type == 'integer_yyyymm':
                conversion = conversion_sql or "TO_DATE(..., 'YYYYMM')"
                date_contract_lines.append(
                    f"- {field_name}: integer/string YYYYMM -> {conversion}"
                )
            elif storage_type == 'unknown':
                date_contract_lines.append(
                    f"- {field_name}: ambiguous date field -> use TRY_TO_DATE or preserve warning."
                )
    if not date_contract_lines:
        date_contract_lines = [
            "- YYYYMM: integer/string YYYYMM -> TO_DATE(YYYYMM::varchar, 'YYYYMM')",
            "- Ambiguous date fields: use TRY_TO_DATE(...) or preserve warning.",
        ]

    forbidden_patterns = [
        'Never join expenses to facttable_with_expenses by MonthlyRegionKey only.',
        'Never reuse the same alias for different CTEs in final_model.',
        'Never reference alias.column that is not selected by that alias CTE.',
        'Never DATEADD directly on raw integer/date-ambiguous fields.',
    ]

    if not ir or not getattr(ir, 'joins', None):
        fallback_lines, fallback_warnings = _fallback_join_candidates_from_plan(plan or [])
        warnings.extend(fallback_warnings)
        if not fallback_lines:
            fallback_lines = ['- No validated safe joins; omit uncertain lookup joins.']
        text = (
            "JOIN CONTRACT:\n"
            + "\n".join(fallback_lines)
            + "\n"
            + "- No validated join paths were derived from IR.\n"
            "- Omit uncertain lookup joins and continue with verified model logic only."
        )
        if warnings:
            text += "\n\nJOIN CONTRACT WARNINGS:\n" + "\n".join(f"- {w}" for w in warnings[:8])
        text += "\n\nALIAS CONTRACT:\n" + "\n".join(
            f"- {table} = {alias}" for table, alias in required_aliases.items()
        )
        text += "\n\nDATE FIELD CONTRACT:\n" + "\n".join(date_contract_lines[:10])
        text += "\n\nFORBIDDEN PATTERNS:\n" + "\n".join(f"- {rule}" for rule in forbidden_patterns)
        return {
            'join_lines': fallback_lines,
            'lines': fallback_lines,
            'warnings': warnings,
            'required_aliases': required_aliases,
            'forbidden_patterns': forbidden_patterns,
            'date_contract': date_contract_lines,
            'text': text,
        }

    lines = []
    seen = set()
    for join in ir.joins:
        if not getattr(join, 'safe', False):
            warnings.append(
                f"OMITTED_UNSAFE_JOIN: {join.from_table}.{join.left_key} -> "
                f"{join.to_table}.{join.right_key} ({join.cardinality}; {join.required_action or 'unsafe'})"
            )
            continue

        left_table = _safe_cte_name(join.from_table)
        right_table = _safe_cte_name(join.to_table)
        left_key = str(join.left_key or '').strip()
        right_key = str(join.right_key or '').strip()
        if not (left_table and right_table and left_key and right_key):
            warnings.append(
                f"OMITTED_INCOMPLETE_JOIN_SPEC: {join.from_table} -> {join.to_table} (missing key details)"
            )
            continue

        left_alias = _canonical_table_alias(left_table)
        right_alias = _canonical_table_alias(right_table)
        if getattr(join, 'join_chain', None):
            chain = [_safe_cte_name(x) for x in (join.join_chain or []) if str(x).strip()]
            chain_path = " -> ".join([left_table] + chain + [right_table])
            chain_line = (
                f"- CHAIN PATH: {chain_path} | terminal key: "
                f"{left_table}.{left_key} -> {right_table}.{right_key} "
                f"| aliases: {left_table}={left_alias}, {right_table}={right_alias}"
            )
            key = chain_line.lower()
            if key not in seen:
                lines.append(chain_line)
                seen.add(key)
        else:
            line = (
                f"- {left_table}.{left_key} -> {right_table}.{right_key} "
                f"| aliases: {left_table}={left_alias}, {right_table}={right_alias}"
            )
            key = line.lower()
            if key not in seen:
                lines.append(line)
                seen.add(key)

    if not lines:
        fallback_lines, fallback_warnings = _fallback_join_candidates_from_plan(plan or [])
        warnings.extend(fallback_warnings)
        if fallback_lines:
            lines.extend(fallback_lines)
        else:
            lines.append("- No validated safe joins; omit uncertain lookup joins.")

    text = "JOIN CONTRACT:\n" + "\n".join(lines)
    if warnings:
        text += "\n\nJOIN CONTRACT WARNINGS:\n" + "\n".join(f"- {w}" for w in warnings[:10])
    text += "\n\nALIAS CONTRACT:\n" + "\n".join(
        f"- {table} = {alias}" for table, alias in required_aliases.items()
    )
    text += "\n\nDATE FIELD CONTRACT:\n" + "\n".join(date_contract_lines[:10])
    text += "\n\nFORBIDDEN PATTERNS:\n" + "\n".join(f"- {rule}" for rule in forbidden_patterns)

    return {
        'join_lines': lines,
        'lines': lines,  # backward compatibility for existing callers/tests
        'warnings': warnings,
        'required_aliases': required_aliases,
        'forbidden_patterns': forbidden_patterns,
        'date_contract': date_contract_lines,
        'text': text,
    }


def _parse_join_contract_line(line: str):
    match = re.search(
        r'([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_ \-+]*)\s*->\s*'
        r'([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_ \-+]*)',
        str(line or ''),
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return {
        'left_table': match.group(1).strip(),
        'left_key': match.group(2).strip(),
        'right_table': match.group(3).strip(),
        'right_key': match.group(4).strip(),
    }


def _guess_main_fact_cte(sql_text: str):
    names = [n.lower() for n in _cte_names(sql_text or '')]
    for preferred in ('facttable_with_expenses', 'fact_table_with_expenses', 'facttable', 'fact_table'):
        if preferred in names:
            return preferred
    return names[0] if names else ''


def compose_final_model_from_contract(
    sql_text: str,
    join_contract: dict,
    projection_mode: str = 'safe',
    return_metadata: bool = False,
):
    """Deterministically compose final_model using only allowed contract join paths."""
    sql = sql_text or ''
    contract = join_contract or {}
    join_lines = contract.get('join_lines') or []
    required_aliases = contract.get('required_aliases') or {}
    if not sql.strip():
        return sql

    cte_lookup = {name.lower(): name for name in _cte_names(sql)}
    base_lineage = _cte_columns_by_name(sql)
    cte_col_maps = {
        name.lower(): extract_cte_output_column_map(sql, name, lineage_map=base_lineage)
        for name in _cte_names(sql)
    }
    main_fact = _guess_main_fact_cte(sql)
    main_fact_ref = cte_lookup.get(main_fact, main_fact)
    if not main_fact_ref:
        return sql

    fact_alias = 'ft'
    projection = (projection_mode or 'safe').strip().lower()
    if projection not in {'safe', 'rich'}:
        projection = 'safe'
    selected_cols = [f'{fact_alias}.*']
    joins = []
    joined_tables = {main_fact.lower()}
    joined_contract_paths = 0
    total_contract_paths = 0
    omitted_unsafe_joins = [
        w for w in (contract.get('warnings') or [])
        if str(w).startswith('OMITTED_UNSAFE_JOIN:')
    ]

    for raw_line in join_lines:
        parsed = _parse_join_contract_line(raw_line)
        if not parsed:
            continue
        total_contract_paths += 1
        left = parsed['left_table'].lower()
        right = parsed['right_table'].lower()
        if right == 'expenses' and main_fact.lower() in {'facttable_with_expenses', 'fact_table_with_expenses'}:
            continue
        if left not in cte_lookup or right not in cte_lookup:
            continue
        # Build only outward joins reachable from current graph.
        if left in joined_tables and right not in joined_tables:
            left_alias = fact_alias if left == main_fact.lower() else required_aliases.get(left, _canonical_table_alias(left))
            right_alias = required_aliases.get(right, _canonical_table_alias(right))
            left_ref = resolve_cte_column_reference(left_alias, parsed["left_key"], cte_col_maps.get(left, {}))
            right_ref = resolve_cte_column_reference(right_alias, parsed["right_key"], cte_col_maps.get(right, {}))
            joins.append(
                f'LEFT JOIN {cte_lookup[right]} {right_alias} ON {left_ref} = {right_ref}'
            )
            if projection == 'rich':
                selected_cols.append(f'{right_alias}.*')
            else:
                selected_cols.append(right_ref)
            joined_tables.add(right)
            joined_contract_paths += 1
        elif right in joined_tables and left not in joined_tables:
            left_alias = required_aliases.get(left, _canonical_table_alias(left))
            right_alias = fact_alias if right == main_fact.lower() else required_aliases.get(right, _canonical_table_alias(right))
            right_ref = resolve_cte_column_reference(right_alias, parsed["right_key"], cte_col_maps.get(right, {}))
            left_ref = resolve_cte_column_reference(left_alias, parsed["left_key"], cte_col_maps.get(left, {}))
            joins.append(
                f'LEFT JOIN {cte_lookup[left]} {left_alias} ON {right_ref} = {left_ref}'
            )
            if projection == 'rich':
                selected_cols.append(f'{left_alias}.*')
            else:
                selected_cols.append(left_ref)
            joined_tables.add(left)
            joined_contract_paths += 1

    if not joins:
        if return_metadata:
            return sql, {
                'joinContractCoverage': 0.0,
                'joinedContractPaths': 0,
                'totalContractPaths': total_contract_paths,
                'omittedUnsafeJoins': omitted_unsafe_joins,
                'projectionMode': projection,
            }
        return sql

    select_matches = list(re.finditer(r'\bSELECT\b', sql, flags=re.IGNORECASE))
    if select_matches:
        base = sql[:select_matches[-1].start()].rstrip().rstrip(',')
    else:
        base = sql.rstrip().rstrip(',')
    final_model = (
        f"{base},\n"
        "final_model AS (\n"
        f"  SELECT {', '.join(dict.fromkeys(selected_cols))}\n"
        f"  FROM {main_fact_ref} {fact_alias}\n"
        f"  " + "\n  ".join(joins) + "\n"
        ")\n"
        "SELECT *\n"
        "FROM final_model"
    )
    coverage = 1.0 if total_contract_paths == 0 else max(0.0, min(1.0, joined_contract_paths / float(total_contract_paths)))
    metadata = {
        'joinContractCoverage': round(coverage, 4),
        'joinedContractPaths': joined_contract_paths,
        'totalContractPaths': total_contract_paths,
        'omittedUnsafeJoins': omitted_unsafe_joins,
        'projectionMode': projection,
    }
    if return_metadata:
        return final_model, metadata
    return final_model


def compute_join_contract_coverage(sql_text: str, join_contract: dict):
    """Compute coverage diagnostics without mutating SQL."""
    _sql, metrics = compose_final_model_from_contract(
        sql_text or '',
        join_contract or {},
        projection_mode='safe',
        return_metadata=True,
    )
    return metrics


_FINAL_MODEL_ENRICHMENT_FIELDS = {
    'customermap': {'custkeyar', 'customer number', 'customer_number'},
    'customermaster': {'customer number', 'customer_number', 'customer name', 'customer_name', 'address number', 'address_number', 'sales rep', 'sales_rep'},
    'customeraddressmaster': {'address number', 'address_number', 'city', 'state', 'zip', 'postal code', 'country'},
    'arsummary': {'aropen', 'argross', 'ar current', 'ar_current', 'ar1-30', 'ar31-60', 'ar60+', 'custkeyar'},
    'arsummary_1': {'aropen', 'argross', 'ar current', 'ar_current', 'ar1-30', 'ar31-60', 'ar60+', 'custkeyar'},
    'budget': {'budget amount', 'budget_amount', 'budget', 'monthlyregionkey'},
    'historyflag': {'_historyflag', 'historyflag', 'history_flag', 'yyyymm'},
    'calendar': {'yyyymm', 'fiscal year', 'fiscal_year', 'fiscal quarter', 'fiscal_quarter', 'fiscal month', 'fiscal_month'},
    'salesrepmaster': {'sales rep', 'sales_rep', 'sales rep name', 'sales_rep_name', 'rep name', 'rep_name'},
    'itembranchmaster': {'item-branch key', 'item_branch_key', 'short name', 'short_name'},
    'itemmaster': {'short name', 'short_name', 'product group', 'product_group', 'product sub group', 'product_sub_group', 'product type', 'product_type'},
    'productgroupmaster': {'product group', 'product_group', 'product group desc', 'product_group_desc', 'description', 'desc'},
    'productsubgroupmaster': {'product sub group', 'product_sub_group', 'product sub group desc', 'product_sub_group_desc', 'description', 'desc'},
    'producttypemaster': {'product type', 'product_type', 'product type desc', 'product_type_desc', 'description', 'desc'},
}


def _safe_projection_alias(alias: str, raw_col: str) -> str:
    name = re.sub(r'[^A-Za-z0-9_]+', '_', f'{alias}_{_normalize_column_token(raw_col)}').strip('_').lower()
    if not name:
        name = f'{alias}_field'
    if re.match(r'^\d', name):
        name = '_' + name
    return name


def _relation_aliases(alias_map: dict, relation_name: str) -> list[str]:
    target = _normalize_cte_name(relation_name)
    return [alias for alias, relation in (alias_map or {}).items() if _normalize_cte_name(relation) == target]


def _projection_raw_expr_key(expr: str) -> str:
    expr = re.sub(r'\s+\bAS\b\s+.*$', '', str(expr or '').strip(), flags=re.IGNORECASE)
    return re.sub(r'\s+', '', expr).replace('"', '').lower()


def enrich_final_model_projection(sql_text: str) -> str:
    """Add safe enrichment fields from already-joined dimension aliases."""
    sql = sql_text or ''
    target_cte = 'final_model' if _cte_body_for(sql, 'final_model') else ('final_mart' if _cte_body_for(sql, 'final_mart') else '')
    if not target_cte:
        return sql
    body = _cte_body_for(sql, target_cte)
    if not body:
        return sql
    select_list, from_tail = _extract_top_level_select_and_from(body)
    if not select_list or not from_tail:
        return sql

    fields = split_top_level_csv(re.sub(r'^\s*DISTINCT\b\s*', '', select_list, flags=re.IGNORECASE))
    alias_map = extract_alias_to_cte_map(body)
    base_lineage = _cte_columns_by_name(sql)
    cte_cols = {
        _normalize_cte_name(name): extract_cte_output_column_map(sql, name, lineage_map=base_lineage)
        for name in _cte_names(sql)
    }
    fact_aliases = {
        alias for alias, relation in alias_map.items()
        if relation in {'facttable_with_expenses', 'fact_table_with_expenses', 'facttable', 'fact_table'}
    }
    fact_cols = set()
    for alias in fact_aliases:
        relation = alias_map.get(alias)
        fact_cols.update(cte_cols.get(_normalize_cte_name(relation), {}).keys())

    existing_outputs = {_normalize_column_token(extract_output_alias(field)) for field in fields if extract_output_alias(field)}
    existing_exprs = {re.sub(r'\s+', ' ', field.strip()).lower() for field in fields}
    existing_raw_refs = {_projection_raw_expr_key(field) for field in fields}
    additions = []

    for relation_name, desired_cols in _FINAL_MODEL_ENRICHMENT_FIELDS.items():
        desired_norm = {_normalize_column_token(col) for col in desired_cols}
        for alias in _relation_aliases(alias_map, relation_name):
            col_map = cte_cols.get(_normalize_cte_name(relation_name), {})
            for norm_col, raw_col in col_map.items():
                if norm_col not in desired_norm:
                    continue
                if norm_col in fact_cols:
                    continue
                if norm_col in existing_outputs:
                    continue
                projection_alias = _safe_projection_alias(alias, raw_col)
                if projection_alias in existing_outputs:
                    continue
                raw_ref = resolve_cte_column_reference(alias, raw_col, col_map)
                raw_ref_key = _projection_raw_expr_key(raw_ref)
                if raw_ref_key in existing_raw_refs:
                    continue
                expr = f'{raw_ref} AS {projection_alias}'
                expr_key = re.sub(r'\s+', ' ', expr.strip()).lower()
                if expr_key in existing_exprs:
                    continue
                additions.append(expr)
                existing_outputs.add(projection_alias)
                existing_exprs.add(expr_key)
                existing_raw_refs.add(raw_ref_key)

    if not additions:
        return sql
    rebuilt_body = 'SELECT\n    ' + ',\n    '.join(fields + additions) + '\n' + from_tail.strip()
    return _replace_cte_body(sql, target_cte, '\n' + rebuilt_body + '\n')


_JOIN_RELATION_ORDER = {
    'facttable': 0,
    'fact_table': 0,
    'facttable_with_expenses': 0,
    'fact_table_with_expenses': 0,
    'customermap': 10,
    'customermaster': 11,
    'customeraddressmaster': 12,
    'channelmaster': 20,
    'arsummary': 30,
    'arsummary_1': 31,
    'calendar': 40,
    'historyflag': 41,
    'itembranchmaster': 50,
    'itemmaster': 51,
    'productgroupmaster': 52,
    'productsubgroupmaster': 53,
    'producttypemaster': 54,
    'budget': 60,
    'accounts': 70,
    'accountmaster': 71,
    'accountgroupmaster': 72,
}


def _projection_sort_key(expr: str):
    text = str(expr or '').strip()
    if re.search(r'(?is)\b\w+\.\*$', text):
        return (0, text.lower())
    raw = re.sub(r'(?is)\s+\bAS\b\s+.*$', '', text).strip()
    alias_match = re.match(r'(?is)^([A-Za-z_][A-Za-z0-9_]*)\.', raw)
    alias = alias_match.group(1).lower() if alias_match else ''
    return (1, f'{alias}.{_normalize_column_token(extract_output_alias(text) or raw)}')


def _join_sort_key(join_sql: str):
    match = re.search(
        r'(?is)\bJOIN\s+([A-Za-z_][A-Za-z0-9_]*)\s+(?:AS\s+)?([A-Za-z_][A-Za-z0-9_]*)\b',
        join_sql or '',
    )
    relation = _normalize_cte_name(match.group(1)) if match else ''
    alias = match.group(2).lower() if match else ''
    return (_JOIN_RELATION_ORDER.get(relation, 90), relation, alias, re.sub(r'\s+', ' ', join_sql or '').lower())


def _dedupe_projection_items(fields: list[str]) -> list[str]:
    seen_outputs = set()
    seen_raw_refs = set()
    result = []
    for field in fields or []:
        item = str(field or '').strip().rstrip(',')
        if not item:
            continue
        output = _normalize_column_token(extract_output_alias(item))
        raw_key = _projection_raw_expr_key(item)
        if raw_key in seen_raw_refs:
            continue
        if output and output in seen_outputs:
            continue
        seen_raw_refs.add(raw_key)
        if output:
            seen_outputs.add(output)
        result.append(item)
    return result


def _split_from_tail_joins(from_tail: str):
    tail = from_tail or ''
    matches = list(re.finditer(r'(?is)\b(?:LEFT|RIGHT|FULL|INNER|CROSS)?\s*JOIN\b', tail))
    if not matches:
        return tail.strip(), [], ''
    prefix = tail[:matches[0].start()].rstrip()
    joins = []
    stop_suffix = ''
    for idx, match in enumerate(matches):
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(tail)
        chunk = tail[start:end].rstrip()
        stop = re.search(r'(?is)\bWHERE\b|\bGROUP\s+BY\b|\bHAVING\b|\bQUALIFY\b|\bORDER\s+BY\b', chunk)
        if stop:
            joins.append(chunk[:stop.start()].rstrip())
            stop_suffix = chunk[stop.start():].strip()
            break
        joins.append(chunk)
    if not stop_suffix and matches:
        last_end = matches[-1].end()
        # suffix is normally included in final join chunk; leave empty unless a clause split above found it.
    return prefix, [join for join in joins if join.strip()], stop_suffix


def deterministic_finalize_sql_structure(sql_text: str, plan=None) -> str:
    """Stabilize final_model projection and join ordering without changing SQL semantics."""
    sql = sql_text or ''
    target_cte = 'final_model' if _cte_body_for(sql, 'final_model') else ('final_mart' if _cte_body_for(sql, 'final_mart') else '')
    if not target_cte:
        return sql
    body = _cte_body_for(sql, target_cte)
    select_list, from_tail = _extract_top_level_select_and_from(body)
    if not select_list or not from_tail:
        return sql

    fields = split_top_level_csv(re.sub(r'^\s*DISTINCT\b\s*', '', select_list, flags=re.IGNORECASE))
    fields = _dedupe_projection_items(fields)
    fact_fields = [field for field in fields if _projection_sort_key(field)[0] == 0]
    enrichment_fields = sorted(
        [field for field in fields if _projection_sort_key(field)[0] != 0],
        key=_projection_sort_key,
    )
    ordered_fields = fact_fields + enrichment_fields

    from_prefix, joins, suffix = _split_from_tail_joins(from_tail)
    ordered_joins = sorted(_dedupe_projection_items(joins), key=_join_sort_key)
    tail_parts = [from_prefix.strip()]
    tail_parts.extend(join.strip() for join in ordered_joins)
    if suffix:
        tail_parts.append(suffix.strip())
    rebuilt_body = (
        '\nSELECT\n    '
        + ',\n    '.join(ordered_fields)
        + '\n'
        + '\n'.join(part for part in tail_parts if part)
        + '\n'
    )
    return _replace_cte_body(sql, target_cte, rebuilt_body)


def _audit_generated_sql_against_plan(sql_text, plan=None, qvs_script='', dialect='dbt'):
    """Validate source ownership, join keys, grain risks, and UNION shape."""
    issues = validate_generated_sql(sql_text, plan, dialect, qvs_script=qvs_script)
    ir, ir_issues, _, _ = _build_ir_context(plan or [], qvs_script or '')
    issues.extend(_format_ir_issues_for_sql(ir_issues))
    if ir and audit_sql_against_ir:
        try:
            audit_issues = audit_sql_against_ir(sql_text or '', ir)
            issues.extend(_format_ir_issues_for_sql(audit_issues))
        except Exception as exc:
            issues.append(f'IR_AUDIT_FAILED: {exc}')
    return issues


def _estimate_output_tokens(text):
    # Cheap approximation for logging; avoids tokenizer dependency.
    return max(1, len(text or '') // 4) if text else 0


def _require_output_budget(max_tokens, minimum, phase):
    if max_tokens is None:
        return
    if max_tokens < minimum:
        raise MigrationTokenBudgetError(
            "insufficient OpenRouter credits/token budget: "
            f"{phase} requires at least {minimum} output tokens, "
            f"but max_tokens={max_tokens}."
        )


def _supports_stream_kwarg(call_ai):
    try:
        return 'stream' in inspect.signature(call_ai).parameters
    except (TypeError, ValueError):
        return False


def _invoke_ai_text(
    call_ai,
    prompt,
    system_prompt=None,
    max_tokens=LOOP_MAX_TOKENS,
    max_prompt_chars=None,
    phase='generation',
    min_tokens=MIN_FULL_SQL_TOKENS,
    stream_callback=None,
):
    _require_output_budget(max_tokens, min_tokens, phase)
    kwargs = {
        'system_prompt': system_prompt,
        'temperature': 0,
        'top_p': 1,
        'max_tokens': max_tokens,
    }
    if max_prompt_chars is not None:
        kwargs['max_prompt_chars'] = max_prompt_chars

    if stream_callback is not None and _supports_stream_kwarg(call_ai):
        chunks = []
        for token in call_ai(prompt, **kwargs, stream=True):
            chunks.append(token)
            stream_callback(token)
        text = ''.join(chunks)
    else:
        text = call_ai(prompt, **kwargs)
        if stream_callback is not None and text:
            stream_callback(text)

    logger.info(
        "Migration AI phase=%s output_chars=%d output_tokens≈%d",
        phase,
        len(text or ''),
        _estimate_output_tokens(text),
    )
    return text or ''


def _failed_migration_result(message, plan, qvs_script, iterations=0, validation_issues=None):
    return {
        'status': 'failed',
        'iterations': iterations,
        'score': 0.0,
        'final_sql': '',
        'sql': '',
        'qlik_description': describe_qlik_script(qvs_script),
        'sql_description': {},
        'comparison': {'matched': False, 'differences': [], 'score': 0.0},
        'comparison_summary': {'matched': False, 'differences': [], 'score': 0.0},
        'final_description': '',
        'description': '',
        'used_deterministic_fallback': False,
        'validation_issues': validation_issues or [],
        'error': message,
    }


def _translate_qlik_expression_to_sql(expression):
    expr = str(expression or '').strip()
    if not expr:
        return expr

    # Step 1: convert [Field] brackets to "Field" double-quotes
    expr = re.sub(r'\[([^\]]+)\]', r'"\1"', expr)

    # Step 2: translate Addmonths BEFORE Date() so Date(Addmonths(...)) resolves correctly
    expr = re.sub(
        r'Addmonths\s*\(\s*([^,]+?)\s*,\s*([^)]+?)\s*\)',
        r'DATEADD(month, \2, \1)',
        expr,
        flags=re.IGNORECASE,
    )

    # Step 3: Date(expr, 'fmt') or Date(expr, "fmt") → TO_CHAR(expr, 'fmt')
    expr = re.sub(
        r'Date\s*\(\s*(.+?)\s*,\s*[\'"]([^\'"]+)[\'"]\s*\)',
        r"TO_CHAR(\1, '\2')",
        expr,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # Step 4: Date(expr) with no format → CAST(expr AS DATE)
    expr = re.sub(
        r'Date\s*\(\s*(.+?)\s*\)',
        r'CAST(\1 AS DATE)',
        expr,
        flags=re.IGNORECASE | re.DOTALL,
    )

    # Step 5: string concatenation & → ||
    expr = expr.replace(' & ', ' || ')
    expr = re.sub(r'\s*&\s*', ' || ', expr)

    # Step 6: aggregate function name mapping
    for qlik_func, sql_func in {
        'Sum': 'SUM',
        'Count': 'COUNT',
        'Avg': 'AVG',
        'Min': 'MIN',
        'Max': 'MAX',
    }.items():
        expr = re.sub(rf'\b{qlik_func}\s*\(', f'{sql_func}(', expr, flags=re.IGNORECASE)

    # Step 6b: Month(expr) → TO_CHAR(expr, 'Mon')
    # Qlik Month() returns abbreviated names ("Jan", "Feb") — MONTHNAME() returns full
    # names ("January") which breaks downstream joins. Use TO_CHAR with 'Mon' format.
    def _replace_month_fn(m):
        inner = m.group(1).strip()
        return f"TO_CHAR({inner}, 'Mon')"
    expr = re.sub(r'\bMonth\s*\(([^)]+)\)', _replace_month_fn, expr, flags=re.IGNORECASE)

    # Step 7: fix text-column-vs-numeric comparisons that Qlik allows but SQL does not.
    # Columns whose names end in Desc/Name/Label/Code/Text/Title/Category/Type/Status
    # are almost certainly text fields — replace numeric comparisons with a
    # proper NULL/empty check.
    _text_col_eq_zero = re.compile(
        r'"([A-Za-z_][A-Za-z0-9_]*(?:Desc|Name|Label|Code|Text|Title|Category|Type|Status))"\s*'
        r'=\s*0\b',
        re.IGNORECASE,
    )
    _text_col_cmp = re.compile(
        r'"([A-Za-z_][A-Za-z0-9_]*(?:Desc|Name|Label|Code|Text|Title|Category|Type|Status))"\s*'
        r'(?:>|<|>=|<=|!=|<>)\s*\d+\b',
        re.IGNORECASE,
    )
    expr = _text_col_eq_zero.sub(
        lambda m: f'("{m.group(1)}" IS NULL OR "{m.group(1)}" = \'\')',
        expr,
    )
    expr = _text_col_cmp.sub(
        lambda m: f'"{m.group(1)}" IS NOT NULL AND "{m.group(1)}" != \'\'',
        expr,
    )

    return expr


def _split_alias_from_expression(field_expression):
    value = str(field_expression or '').strip()
    if not value:
        return '', None

    match = re.search(
        r'^(?P<expr>.+?)(?:\s+AS\s+(?P<alias>\[[^\]]+\]|"[^"]+"|[A-Za-z0-9_\$][A-Za-z0-9_\$\s-]*))\s*$',
        value,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match:
        expr = match.group('expr').strip()
        alias = match.group('alias').strip()
        return expr, alias
    return value, None


def _extract_trailing_clause(raw, clause_name):
    if not raw:
        return ''
    if clause_name == 'GROUP BY':
        pattern = r'\bGROUP\s+BY\b(.*?)(?=\bORDER\s+BY\b|\bHAVING\b|;)'
    elif clause_name == 'ORDER BY':
        pattern = r'\bORDER\s+BY\b(.*?)(?=\bGROUP\s+BY\b|\bHAVING\b|;)'
    else:
        return ''
    match = re.search(pattern, raw, flags=re.IGNORECASE | re.DOTALL)
    return re.sub(r'\s+', ' ', match.group(1)).strip() if match else ''


def _source_for_plan_item(item, cte_names):
    source_type = (item.get('source_type') or '').lower()
    sources = item.get('source_tables') or []

    if source_type == 'resident' and sources:
        return cte_names.get(sources[0]) or _safe_cte_name(sources[0])

    source = item.get('source') or (sources[0] if sources else '')
    return _resolve_source_reference(source)


def _extract_output_column_name(field_expression):
    """Return the output column name (alias if present, else bare identifier)."""
    _, alias = _split_alias_from_expression(field_expression)
    if alias:
        return _clean_qlik_field_name(alias)
    # No alias — the expression itself is the column name if it's a plain identifier
    expr = field_expression.strip().strip('[]"')
    if re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', expr):
        return expr
    return None


def _render_select_for_plan_item(item, cte_names=None, required_columns=None):
    """Render a SELECT block for a single plan item.

    Args:
        item: plan item dict
        cte_names: mapping of Qlik table names → CTE names
        required_columns: when set (list of column names), pad any missing columns
            with NULL AS "col_name" so UNION ALL branches are column-aligned.
    """
    cte_names = cte_names or {}
    raw_fields = item.get('fields') or []
    rendered_fields = []
    produced_columns = set()

    for field in raw_fields:
        expr, alias = _split_alias_from_expression(field)
        translated_expr = _translate_qlik_expression_to_sql(expr)
        if alias:
            col_name = _clean_qlik_field_name(alias)
            rendered_fields.append(f"{translated_expr} AS {_format_sql_identifier(alias)}")
        else:
            col_name = _extract_output_column_name(field)
            rendered_fields.append(translated_expr)
        if col_name:
            produced_columns.add(col_name.lower())

    # Pad missing columns with typed NULLs so UNION ALL branches stay column-aligned.
    # Typed NULLs (CAST(NULL AS VARCHAR)) prevent Snowflake/Databricks from failing
    # on unresolvable type inference across branches with different schemas.
    if required_columns:
        for col in required_columns:
            if col.lower() not in produced_columns:
                rendered_fields.append(_typed_null(col))

    select_clause = 'SELECT\n    ' + ',\n    '.join(rendered_fields) if rendered_fields else 'SELECT *'
    source = _source_for_plan_item(item, cte_names)

    block_lines = [select_clause]
    if source:
        block_lines.append(f'FROM {source}')
    if item.get('filters'):
        block_lines.append('WHERE ' + ' AND '.join(item.get('filters')))

    group_by = _extract_trailing_clause(item.get('raw', ''), 'GROUP BY')
    if group_by:
        block_lines.append('GROUP BY ' + group_by)

    order_by = _extract_trailing_clause(item.get('raw', ''), 'ORDER BY')
    if order_by:
        block_lines.append('ORDER BY ' + order_by)

    return '\n'.join(block_lines)


def _collect_all_output_columns(items):
    """Return an ordered list of all unique output column names across a set of plan items.

    Used to build the full column list for UNION ALL alignment — every branch must
    produce the same columns in the same order, with NULL for missing ones.
    """
    seen = {}  # col_lower → original_name (preserves first-seen casing)
    for item in items:
        for field in item.get('fields') or []:
            col = _extract_output_column_name(field)
            if col and col.lower() not in seen:
                seen[col.lower()] = col
    return list(seen.values())


def render_sql_from_load_plan(plan):
    """Render deterministic SQL directly from parsed LOAD blocks.

    Handles:
    - CONCATENATE → UNION ALL with NULL-padded column alignment (only the
      columns explicitly loaded by the CONCATENATE branch are selected; all
      other columns from the base table are padded with NULL).
    - DROP FIELDS → split into two CTEs: <table>_pre_drop (full columns,
      used in any UNION ALL) and <table> (post-drop projection).
    - RESIDENT → CTE reference.
    - Picks the best final SELECT (fact table or widest CTE).
    """
    if not plan:
        return ''

    _CONFIG_BLOCK = "{{ config(materialized='table', tags=['qlik_migration']) }}"

    # ── Step 1: assign unique CTE names ──────────────────────────────────────
    # DROP_FIELDS items don't get their own CTE — they modify an existing table's CTE.
    # CONCATENATE items that share a table key with their base also don't get a new name.
    # We assign names only to items that will actually produce a CTE.
    cte_names = {}
    used_names = set()
    for index, item in enumerate(plan, 1):
        if item.get('operation') == 'DROP_FIELDS':
            continue  # no CTE of its own
        table_key = item.get('table') or f'load_{index}'
        if table_key in cte_names:
            continue  # already assigned (e.g. second CONCATENATE into same table)
        base_name = _safe_cte_name(table_key, fallback=f'load_{index}')
        cte_name = base_name
        counter = 2
        while cte_name in used_names:
            cte_name = f'{base_name}_{counter}'
            counter += 1
        used_names.add(cte_name)
        cte_names[table_key] = cte_name

    # ── Step 2: collect DROP FIELDS directives ────────────────────────────────
    # drop_fields_map: table_key → list of column names to drop
    drop_fields_map = {}
    for item in plan:
        if item.get('operation') == 'DROP_FIELDS' and item.get('table'):
            table_key = item['table']
            drop_fields_map.setdefault(table_key, []).extend(item.get('drop_fields') or [])

    # Early-return for a single effective LOAD item with no DROP FIELDS
    non_drop_items = [i for i in plan if i.get('operation') != 'DROP_FIELDS']
    if len(non_drop_items) == 1 and not drop_fields_map:
        return _render_select_for_plan_item(non_drop_items[0], cte_names)

    # ── Step 3: group CONCATENATE blocks into their target CTE ───────────────
    # Build a map: target_table → [base_item, concat_item1, concat_item2, ...]
    concat_groups = {}   # target_table_key → list of items to UNION ALL
    skip_indices = set() # indices that are absorbed into a CONCATENATE group

    for idx, item in enumerate(plan):
        if item.get('is_concatenate') and item.get('concatenate_target'):
            target_key = item['concatenate_target']
            if target_key not in concat_groups:
                # Find the base item for this target
                base_idx = next(
                    (i for i, p in enumerate(plan)
                     if (p.get('table') or '') == target_key and not p.get('is_concatenate')),
                    None
                )
                if base_idx is not None:
                    concat_groups[target_key] = [plan[base_idx]]
                    skip_indices.add(base_idx)
            if target_key in concat_groups:
                concat_groups[target_key].append(item)
                skip_indices.add(idx)

    # ── Step 4: build CTE list ────────────────────────────────────────────────
    # We render in two passes:
    #   A) CONCATENATE groups (keyed by target table) — these absorb all their members
    #   B) Remaining non-absorbed, non-DROP_FIELDS items
    # We need to preserve the original plan order, so we track which table_keys
    # have already been emitted.
    ctes = []
    emitted_table_keys = set()  # table_keys already rendered

    for index, item in enumerate(plan):
        if item.get('operation') == 'DROP_FIELDS':
            continue  # handled via drop_fields_map, not as standalone CTEs

        table_key = item.get('table') or f'load_{index + 1}'
        cte_name = cte_names.get(table_key, f'load_{index + 1}')
        dropped_cols = drop_fields_map.get(table_key, [])

        # Skip items that were absorbed into a concat group AND already rendered
        if index in skip_indices and table_key in emitted_table_keys:
            continue

        if table_key in concat_groups:
            if table_key in emitted_table_keys:
                continue  # already rendered this group
            emitted_table_keys.add(table_key)

            # Collect the full column superset across all UNION ALL branches
            all_members = concat_groups[table_key]
            all_columns = _collect_all_output_columns(all_members)

            if dropped_cols:
                # Need a _pre_drop CTE (full columns) + post-drop CTE (surviving cols)
                pre_drop_name = f'{cte_name}_pre_drop'
                union_parts = [
                    _render_select_for_plan_item(member, cte_names, required_columns=all_columns)
                    for member in all_members
                ]
                body = '\nUNION ALL\n'.join(union_parts)
                ctes.append(f'{pre_drop_name} AS (\n{body}\n)')

                surviving = [c for c in all_columns if c not in dropped_cols]
                if surviving:
                    proj = ',\n    '.join(f'"{c}"' for c in surviving)
                    drop_body = f'SELECT\n    {proj}\nFROM {pre_drop_name}'
                else:
                    drop_body = f'SELECT *\nFROM {pre_drop_name}'
                ctes.append(f'{cte_name} AS (\n{drop_body}\n)')
            else:
                union_parts = [
                    _render_select_for_plan_item(member, cte_names, required_columns=all_columns)
                    for member in all_members
                ]
                body = '\nUNION ALL\n'.join(union_parts)
                ctes.append(f'{cte_name} AS (\n{body}\n)')

        elif index in skip_indices:
            # Absorbed into a concat group that was already rendered — skip
            continue

        else:
            emitted_table_keys.add(table_key)
            if dropped_cols:
                # Simple table with DROP FIELDS — emit pre_drop + post_drop
                pre_drop_name = f'{cte_name}_pre_drop'
                body = _render_select_for_plan_item(item, cte_names)
                ctes.append(f'{pre_drop_name} AS (\n{body}\n)')

                all_columns = _collect_all_output_columns([item])
                surviving = [c for c in all_columns if c not in dropped_cols]
                if surviving:
                    proj = ',\n    '.join(f'"{c}"' for c in surviving)
                    drop_body = f'SELECT\n    {proj}\nFROM {pre_drop_name}'
                else:
                    drop_body = f'SELECT *\nFROM {pre_drop_name}'
                ctes.append(f'{cte_name} AS (\n{drop_body}\n)')
            else:
                body = _render_select_for_plan_item(item, cte_names)
                ctes.append(f'{cte_name} AS (\n{body}\n)')

    # ── Step 5: pick the best final SELECT ────────────────────────────────────
    # Prefer: a CTE whose name contains 'fact', then the largest CTE by field count,
    # then the last non-lookup CTE, then simply the last CTE.
    rendered_cte_names = [c.split(' AS (')[0].strip() for c in ctes]

    fact_cte = next((n for n in rendered_cte_names if 'fact' in n.lower()), None)
    if fact_cte:
        final_cte = fact_cte
    else:
        # Pick the CTE with the most fields (most likely the main model)
        best_idx = 0
        best_field_count = 0
        for i, item in enumerate(plan):
            if item.get('operation') == 'DROP_FIELDS':
                continue
            if (item.get('table') or f'load_{i+1}') in skip_indices:
                continue
            fc = len(item.get('fields') or [])
            if fc > best_field_count:
                best_field_count = fc
                best_idx = i
        best_table = plan[best_idx].get('table') or f'load_{best_idx + 1}'
        final_cte = cte_names.get(best_table, rendered_cte_names[-1] if rendered_cte_names else 'result')

    return f'{_CONFIG_BLOCK}\n\nWITH\n' + ',\n'.join(ctes) + f'\nSELECT *\nFROM {final_cte}'


def extract_load_block_ast(qvs_script):
    """Extract LOAD-centric and DROP FIELDS AST nodes from a decoded Qlik script."""
    parsed = parse_qlik_load_script(qvs_script or '')
    statements = parsed.get('statements', [])
    load_blocks = []
    pending_chain = []

    def _flush_chain():
        nonlocal pending_chain
        if pending_chain:
            load_blocks.extend(pending_chain)
            pending_chain = []

    for stmt in statements:
        stmt_type = stmt.get('type')

        # Capture DROP FIELDS statements so the plan can apply them
        if stmt_type == 'DROP_FIELDS':
            _flush_chain()
            raw_text = stmt.get('rawText') or stmt.get('content') or ''
            # fields here are the columns being dropped; target is the table
            dropped = [str(f).strip() for f in stmt.get('fields', []) if str(f).strip()]
            target = _normalize_identifier(stmt.get('targetTable') or stmt.get('prefixTarget'))
            if target and dropped:
                load_blocks.append({
                    'table': target,
                    'operation': 'DROP_FIELDS',
                    'fields': [],
                    'drop_fields': dropped,
                    'source': target,
                    'sourceType': 'resident',
                    'residentTable': target,
                    'where': [],
                    'joinType': None,
                    'joinTarget': None,
                    'prefix': None,
                    'raw': raw_text,
                    'lineNumber': stmt.get('lineNumber'),
                })
            continue

        if stmt_type != 'LOAD':
            _flush_chain()
            continue

        raw_text = stmt.get('rawText') or stmt.get('content') or ''
        fields = [str(f).strip() for f in stmt.get('fields', []) if str(f).strip()]
        block = {
            'table': _normalize_identifier(stmt.get('label') or stmt.get('prefixTarget') or stmt.get('table') or stmt.get('source') or f"load_{len(load_blocks) + 1}"),
            'operation': 'LOAD',
            'fields': fields,
            'drop_fields': [],
            'source': stmt.get('source'),
            'sourceType': stmt.get('sourceType') or ('resident' if stmt.get('residentTable') else 'from'),
            'residentTable': stmt.get('residentTable'),
            'where': [c.strip() for c in stmt.get('conditions', []) if c and c.strip()],
            'joinType': None,
            'joinTarget': stmt.get('prefixTarget'),
            'prefix': stmt.get('prefix'),
            'raw': raw_text,
            'lineNumber': stmt.get('lineNumber'),
        }

        prefix = str(stmt.get('prefix') or '').upper()
        if 'JOIN' in prefix:
            block['joinType'] = 'JOIN'
        elif 'KEEP' in prefix:
            block['joinType'] = 'KEEP'
        elif 'CONCATENATE' in prefix:
            block['joinType'] = 'CONCATENATE'

        pending_chain.append(block)

    _flush_chain()
    return load_blocks


def _render_load_block_as_sql(block):
    fields = block.get('fields') or []
    if not fields:
        select_clause = 'SELECT *'
    else:
        select_clause = 'SELECT\n    ' + ',\n    '.join(fields)

    source_type = (block.get('sourceType') or '').lower()
    source = block.get('source')
    resident = block.get('residentTable')
    if source_type == 'resident' and resident:
        from_clause = f'FROM {resident}'
    elif source:
        from_clause = f'FROM {source}'
    else:
        from_clause = ''

    sql_parts = [select_clause]
    if from_clause:
        sql_parts.append(from_clause)
    if block.get('where'):
        sql_parts.append('WHERE ' + ' AND '.join(block['where']))

    return '\n'.join(sql_parts)


def clean_mermaid_syntax(mermaid_code):
    """
    Sanitize generated Mermaid diagram syntax to prevent visual browser crashes.
    Ensures all node labels are correctly wrapped in double quotes, standardizes headers,
    and strips dangerous characters.
    """
    if not mermaid_code:
        return ""
        
    lines = []
    has_header = False
    
    for line in mermaid_code.split('\n'):
        line_str = line.strip()
        if not line_str:
            continue
            
        # Standardize the diagram entry
        if any(line_str.lower().startswith(h) for h in ('graph', 'flowchart')):
            has_header = True
            lines.append(line_str)
            continue
            
        # Standard node label wrapping check:
        # e.g., A[Sales Temp] or B("Total Sales (USD)")
        match = re.match(r'^([A-Za-z0-9_-]+)\s*(\[|\[\[|\(|\(\(|\{)\s*(.*?)\s*(\]|\]\]|\)|\)\)|\})\s*(.*)$', line_str)
        if match:
            node_id = match.group(1)
            bracket_open = match.group(2)
            label = match.group(3).strip()
            bracket_close = match.group(4)
            rest = match.group(5) or ''
            
            # Wrap label in quotes if not already quoted and contains spaces/special chars
            if label and not (label.startswith('"') and label.endswith('"')):
                # Remove any existing outer quotes that might be broken
                label = label.strip('"\'')
                label = f'"{label}"'
                
            lines.append(f"{node_id}{bracket_open}{label}{bracket_close}{rest}")
        else:
            lines.append(line_str)
            
    # Prepend header if omitted by LLM
    if not has_header:
        lines.insert(0, "graph TD")
        
    return "\n".join(lines)


def get_dialect_guidance(dialect):
    dialect = (dialect or 'dbt').lower()
    guidance = {
        'snowflake': "- Prefer Snowflake-safe functions like TO_DATE, DATE_TRUNC, and TRY_TO_NUMBER.\n- Use double quotes only when needed for identifiers.",
        'bigquery': "- Prefer BigQuery functions like SAFE_CAST, DATE_TRUNC, and backticked identifiers.\n- Avoid dialects that rely on Snowflake-only syntax.",
        'databricks': "- Prefer Databricks/Spark SQL functions like date_trunc, coalesce, and inline CTEs.\n- Avoid vendor-specific casts unless required.",
        'postgres': "- Prefer Postgres syntax like ::type casts, date_trunc, and standard ANSI joins.\n- Avoid backticks and warehouse-specific functions.",
        'powerbi': (
            "- You are NOT generating SQL. You are generating Power BI artifacts.\n"
            "- Output Power Query M code for data loading/transformation steps.\n"
            "- Output DAX measures for calculated fields, aggregations, and KPIs.\n"
            "- Map Qlik LOAD → Power Query Table.SelectColumns / Table.RenameColumns / Table.TransformColumns.\n"
            "- Map Qlik RESIDENT → Power Query table references (let Source = TableName in ...).\n"
            "- Map Qlik WHERE filters → Table.SelectRows with each [Field] = value.\n"
            "- Map Qlik GROUP BY + aggregations → Table.Group with aggregation list.\n"
            "- Map Qlik JOIN → Table.NestedJoin or Table.Join.\n"
            "- Map Qlik calculated fields → DAX CALCULATE, SUMX, AVERAGEX, COUNTROWS, FILTER, ALL, RELATED.\n"
            "- Map Qlik SET variables → Power Query let ... in parameters or DAX VAR ... RETURN.\n"
            "- Use descriptive step names in M (e.g., #\"Filtered Rows\", #\"Renamed Columns\").\n"
            "- DAX measures must follow the pattern: MeasureName = DAX_EXPRESSION.\n"
            "- Do not output SQL SELECT statements. Output only M Query and DAX."
        ),
    }
    return guidance.get(dialect, "- Use clean ANSI-friendly DBT SQL.\n- Prefer standard casts, explicit aliases, and deterministic CTEs.")


def parse_migration_response(ai_response):
    """Normalize the AI response into structured parts.

    Handles three output formats:
      - Standard DBT:  ### SQL  /  ### DESCRIPTION  /  ### LINEAGE
      - Power BI:      ### M QUERY  /  ### DAX  /  ### DESCRIPTION  /  ### LINEAGE
      - JSON Fallback: Structured JSON with sql/m_query, description, lineage fields.
    """
    result = {
        'sql': '',
        'description': '',
        'lineage': '',
        'warnings': [],
        'raw': ai_response or '',
    }

    if not ai_response:
        result['warnings'].append('Empty AI response')
        return _finalize_migration_parse_result(result)

    text = ai_response.strip()
    
    if len(text) < 500 or not any(kw in text.upper() for kw in ['SQL', 'SELECT', 'WITH', 'M QUERY', 'DAX']):
        logger.debug(
            "Suspicious AI response while parsing migration output: chars=%d has_sql_kw=%s",
            len(text),
            any(kw in text.upper() for kw in ['SQL', 'SELECT', 'WITH']),
        )

    # ── JSON structured fallback ────────────────────────────────────────────────
    if text.startswith('{') or '```json' in text:
        import json as json_lib
        try:
            json_str = text
            if '```json' in json_str:
                json_str = json_str.split('```json')[1].split('```')[0].strip()
            elif '```' in json_str:
                json_str = json_str.split('```')[1].split('```')[0].strip()
            
            parsed = json_lib.loads(json_str)
            if 'sql' in parsed or 'm_query' in parsed:
                sql_body = parsed.get('sql') or parsed.get('m_query', '')
                if 'dax' in parsed and parsed['dax']:
                    sql_body += '\n\n// ── Power BI DAX Measures ──────────────────────────────────\n' + parsed['dax']
                result['sql'] = sql_body
                result['description'] = parsed.get('description', '')
                result['lineage'] = clean_mermaid_syntax(parsed.get('lineage', ''))
                return _finalize_migration_parse_result(result)
        except Exception:
            pass # Fallback to standard parsing

    # ── Power BI format detection ─────────────────────────────────────────────
    if re.search(r'###\s*M\s*QUERY', text, re.IGNORECASE):
        parts = re.split(r'###\s*M\s*QUERY|###\s*DAX|###\s*DESCRIPTION|###\s*LINEAGE', text, flags=re.IGNORECASE)
        # parts[0] = preamble, [1] = M Query, [2] = DAX, [3] = Description, [4] = Lineage
        m_query = parts[1].strip() if len(parts) > 1 else ''
        dax = parts[2].strip() if len(parts) > 2 else ''
        description = parts[3].strip() if len(parts) > 3 else ''
        lineage_block = parts[4].strip() if len(parts) > 4 else ''

        # Strip code fences
        m_query = re.sub(r'^```[a-z]*\s*|```$', '', m_query, flags=re.IGNORECASE | re.MULTILINE).strip()
        dax = re.sub(r'^```[a-z]*\s*|```$', '', dax, flags=re.IGNORECASE | re.MULTILINE).strip()

        # Combine M Query + DAX into the sql field so the existing editor/display
        # pipeline works without changes. Clearly labelled sections.
        combined = ''
        if m_query:
            combined += '// ── Power Query M ──────────────────────────────────────────\n'
            combined += m_query
        if dax:
            if combined:
                combined += '\n\n'
            combined += '// ── DAX Measures ───────────────────────────────────────────\n'
            combined += dax

        result['sql'] = combined
        result['description'] = description

        mermaid_match = re.search(r'```mermaid\s*([\s\S]*?)```', lineage_block, re.IGNORECASE)
        if mermaid_match:
            result['lineage'] = clean_mermaid_syntax(mermaid_match.group(1).strip())
        else:
            result['lineage'] = clean_mermaid_syntax(lineage_block.strip('`').strip())

        return _finalize_migration_parse_result(result)

    # ── Standard DBT format ───────────────────────────────────────────────────
    parts = re.split(r'### SQL|### DESCRIPTION|### LINEAGE', text, flags=re.IGNORECASE)

    if len(parts) >= 4:
        result['sql'] = parts[1].strip()
        result['sql'] = re.sub(r'^```sql\s*|```$', '', result['sql'], flags=re.IGNORECASE | re.MULTILINE).strip()
        result['description'] = parts[2].strip()

        lineage_block = parts[3].strip()
        mermaid_match = re.search(r'```mermaid\s*([\s\S]*?)```', lineage_block, re.IGNORECASE)
        if mermaid_match:
            result['lineage'] = clean_mermaid_syntax(mermaid_match.group(1).strip())
        else:
            result['lineage'] = clean_mermaid_syntax(lineage_block.strip('`').strip('mermaid').strip())
        return _finalize_migration_parse_result(result)

    if len(parts) == 3:
        result['sql'] = parts[1].strip()
        result['sql'] = re.sub(r'^```sql\s*|```$', '', result['sql'], flags=re.IGNORECASE | re.MULTILINE).strip()
        result['description'] = parts[2].strip()
        return _finalize_migration_parse_result(result)

    if len(parts) == 2:
        body = parts[1].strip()
        if re.search(r'\bWITH\b|\bSELECT\b', body, re.IGNORECASE):
            result['sql'] = body
        else:
            result['description'] = body
        return _finalize_migration_parse_result(result)

    if re.search(r'\bWITH\b|\bSELECT\b', text, re.IGNORECASE):
        result['sql'] = text
    else:
        result['description'] = text

    return _finalize_migration_parse_result(result)


def normalize_dbt_config_braces(sql_text: str) -> str:
    """Fix the common single-brace dbt config typo in generated SQL."""
    if not sql_text:
        return sql_text
    return re.sub(
        r'(?<!\{)\{\s*config\s*\(([^)]*)\)\s*\}(?!\})',
        r'{{ config(\1) }}',
        sql_text,
        count=0,
    )


def _finalize_migration_parse_result(result):
    result['sql'] = normalize_dbt_config_braces(result.get('sql') or '')
    return result


def _cte_bounds(sql_text: str, cte_name: str):
    match = re.search(rf'\b{re.escape(cte_name)}\s+AS\s*\(', sql_text or '', flags=re.IGNORECASE)
    if not match:
        return None
    start = match.end()
    depth = 1
    i = start
    while i < len(sql_text):
        if sql_text[i] == '(':
            depth += 1
        elif sql_text[i] == ')':
            depth -= 1
            if depth == 0:
                return match.start(), start, i, i + 1
        i += 1
    return None


def _cte_body_for(sql_text: str, cte_name: str) -> str:
    bounds = _cte_bounds(sql_text, cte_name)
    return sql_text[bounds[1]:bounds[2]] if bounds else ''


def extract_cte_body(sql_text: str, cte_name: str) -> str:
    """Public helper for CTE body extraction."""
    return _cte_body_for(sql_text, cte_name)


def _replace_cte_body(sql_text: str, cte_name: str, new_body: str) -> str:
    bounds = _cte_bounds(sql_text, cte_name)
    if not bounds:
        return sql_text
    _, body_start, body_end, _ = bounds
    return sql_text[:body_start] + new_body + sql_text[body_end:]


def _select_body(body: str) -> str:
    select_list, _ = _extract_top_level_select_and_from(body or '')
    return select_list


def _select_source(body: str) -> str:
    match = re.search(r'\bFROM\s+([A-Za-z_][A-Za-z0-9_]*)\b', body or '', flags=re.IGNORECASE)
    return match.group(1) if match else ''


def _extract_top_level_select_and_from(sql_text: str):
    """Return (select_list, from_tail) for top-level SELECT ... FROM in a SQL fragment."""
    sql = sql_text or ''
    if not sql:
        return '', ''
    depth = 0
    in_single = False
    in_double = False
    select_start = -1
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if ch == '(':
                depth += 1
            elif ch == ')' and depth > 0:
                depth -= 1
            elif depth == 0 and select_start < 0 and sql[i:i + 6].lower() == 'select':
                prev = sql[i - 1] if i > 0 else ' '
                nxt = sql[i + 6] if i + 6 < len(sql) else ' '
                if not prev.isalnum() and prev != '_' and not nxt.isalnum() and nxt != '_':
                    select_start = i + 6
                    i += 6
                    continue
            elif depth == 0 and select_start >= 0 and sql[i:i + 4].lower() == 'from':
                prev = sql[i - 1] if i > 0 else ' '
                nxt = sql[i + 4] if i + 4 < len(sql) else ' '
                if not prev.isalnum() and prev != '_' and not nxt.isalnum() and nxt != '_':
                    return sql[select_start:i], sql[i:]
        i += 1
    return '', ''


def split_top_level_union_all(cte_body: str) -> list[str]:
    sql = cte_body or ''
    parts = []
    depth = 0
    in_single = False
    in_double = False
    start = 0
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if ch == '(':
                depth += 1
            elif ch == ')' and depth > 0:
                depth -= 1
            elif depth == 0 and sql[i:i + 9].lower() == 'union all':
                parts.append(sql[start:i].strip())
                i += 9
                start = i
                continue
        i += 1
    tail = sql[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _select_output_columns(body: str):
    columns = []
    select_body = _select_body(body)
    # DISTINCT should not become part of the first output column token.
    select_body = re.sub(r'^\s*DISTINCT\b\s*', '', select_body, flags=re.IGNORECASE)
    for item in _split_sql_like_fields(select_body):
        output = extract_output_alias(item)
        if output and output != '*':
            columns.append(output)
    return columns


def _unquote_identifier_token(raw_output: str) -> str:
    """Return an identifier token with only its outer quoting removed."""
    output = str(raw_output or '').strip()
    if not output:
        return ''
    if len(output) >= 2 and output[0] == '"' and output[-1] == '"':
        return output[1:-1].strip()
    if len(output) >= 2 and output[0] == '[' and output[-1] == ']':
        return output[1:-1].strip()
    if len(output) >= 2 and output[0] == '`' and output[-1] == '`':
        return output[1:-1].strip()
    return output.strip()


def _output_column_token(raw_output: str) -> str:
    """Compatibility wrapper for output-name extraction."""
    return _unquote_identifier_token(raw_output)


def extract_output_alias(expr: str) -> str:
    """Extract a SELECT item's output column name without blanking quoted identifiers."""
    expression, alias = _split_alias_from_expression(expr)
    if alias:
        return _unquote_identifier_token(alias)
    token = str(expression or '').strip()
    if not token:
        return ''
    simple = re.match(
        r'^(?:[A-Za-z_][A-Za-z0-9_]*\.)?("([^"]+)"|`([^`]+)`|\[([^\]]+)\]|[A-Za-z_][A-Za-z0-9_]*)$',
        token,
    )
    if not simple:
        return ''
    raw = simple.group(1)
    if '.' in raw and not raw.startswith(('"', '`', '[')):
        raw = raw.rsplit('.', 1)[-1]
    return _unquote_identifier_token(raw)


def extract_projection_display_names(select_sql: str) -> list[str]:
    """Return readable output column names from a SELECT statement or branch."""
    select_body = _select_body(select_sql or '')
    select_body = re.sub(r'^\s*DISTINCT\b\s*', '', select_body, flags=re.IGNORECASE)
    fields = split_top_level_csv(select_body)
    outputs = []
    for item in fields:
        out = extract_output_alias(item)
        if out and out != '*' and out != '""':
            outputs.append(out)
    return outputs


def extract_select_projection_columns(select_sql: str) -> list[str]:
    """
    Return normalized output column names from a SELECT statement/branch.

    The parser is top-level comma aware and preserves quoted identifiers until
    after the output alias/name is extracted.
    """
    return [
        _normalize_column_token(column)
        for column in extract_projection_display_names(select_sql or '')
        if _normalize_column_token(column)
    ]


def _projection_parser_integrity_issues(select_sql: str) -> list[str]:
    select_body = _select_body(select_sql or '')
    select_body = re.sub(r'^\s*DISTINCT\b\s*', '', select_body, flags=re.IGNORECASE)
    issues = []
    for item in split_top_level_csv(select_body):
        stripped = str(item or '').strip()
        if not stripped:
            issues.append('INTERNAL_PARSER_ERROR: empty projection token encountered.')
            continue
        if re.search(r'(?<![A-Za-z0-9_])""(?![A-Za-z0-9_])', stripped):
            issues.append('INTERNAL_PARSER_ERROR: empty quoted identifier encountered in projection parser.')
            continue
        if re.match(r'(?is)^(?:[A-Za-z_][A-Za-z0-9_]*\.)?\*$', stripped):
            continue
        try:
            output = extract_output_alias(stripped)
        except Exception as exc:
            issues.append(f'INTERNAL_PARSER_ERROR: projection parser crashed: {exc}')
            continue
        if output == '""' or output == '':
            issues.append(f'INTERNAL_PARSER_ERROR: projection parser returned empty output for {stripped[:80]}.')
    return issues


def count_select_columns_for_branch(branch_sql: str, lineage_map=None) -> int:
    if re.search(r'\bSELECT\s+(?:[A-Za-z_][A-Za-z0-9_]*\.)?\*', branch_sql or '', re.IGNORECASE):
        return None
    return len(extract_select_projection_columns(branch_sql or ''))


def _sql_column_ref(column: str) -> str:
    value = str(column or '').strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value
    return value if re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', value) else f'"{value}"'


def _render_union_branch(source_name: str, columns, available_columns, null_columns=None) -> str:
    null_columns = {c.lower() for c in (null_columns or [])}
    available = {c.lower(): c for c in available_columns or []}
    rendered = []
    for column in columns:
        key = column.lower()
        if key in null_columns or key not in available:
            rendered.append(f'CAST(NULL AS {_infer_sql_type_from_name(column)}) AS {_sql_column_ref(column)}')
        else:
            rendered.append(_sql_column_ref(available[key]))
    return 'SELECT\n    ' + ',\n    '.join(rendered) + f'\nFROM {source_name}'


def _append_unique_columns(columns, extra_columns):
    merged = list(columns or [])
    seen = {column.lower() for column in merged}
    for column in extra_columns or []:
        key = column.lower()
        if key not in seen:
            merged.append(column)
            seen.add(key)
    return merged


def _extract_simple_output_name(expr: str):
    return extract_output_alias(expr)


def _alias_map_for_select_scope(select_sql: str):
    alias_map = {}
    for match in re.finditer(
        r'\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:AS\s+)?([A-Za-z_][A-Za-z0-9_]*)?',
        select_sql or '',
        flags=re.IGNORECASE,
    ):
        relation = match.group(1)
        alias = match.group(2) or relation
        alias_map[alias.lower()] = relation
    return alias_map


def extract_union_branch_columns(select_sql: str, lineage_map=None, alias_map=None) -> list[str]:
    """Extract output columns for a SELECT branch, expanding * / alias.* using lineage."""
    lineage_map = lineage_map or {}
    alias_map = alias_map or _alias_map_for_select_scope(select_sql or '')
    select_body = _select_body(select_sql or '')
    select_body = re.sub(r'^\s*DISTINCT\b\s*', '', select_body, flags=re.IGNORECASE)
    fields = _split_sql_like_fields(select_body)
    outputs = []
    for item in fields:
        expr, alias = _split_alias_from_expression(item)
        expr = str(expr or '').strip()
        alias = str(alias or '').strip()
        if alias:
            out = extract_output_alias(item)
            if out:
                outputs.append(out)
            continue
        star_alias = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\.\*$', expr)
        if star_alias:
            relation = alias_map.get(star_alias.group(1).lower())
            cols = lineage_map.get((relation or '').lower(), [])
            if not cols:
                raise ValueError(f'Unable to expand {expr}: source columns unavailable.')
            outputs.extend(cols)
            continue
        if expr == '*':
            source = _select_source(select_sql or '')
            relation = alias_map.get(source.lower(), source)
            cols = lineage_map.get((relation or '').lower(), [])
            if not cols:
                raise ValueError('Unable to expand *: source columns unavailable.')
            outputs.extend(cols)
            continue
        out = extract_output_alias(expr)
        if out:
            outputs.append(out)
            continue
        raise ValueError(f'Non-deterministic SELECT expression without alias: {expr}')
    return outputs


def extract_cte_output_columns(sql: str, cte_name: str, lineage_map=None) -> list[str]:
    """Extract CTE output columns with star expansion using upstream lineage."""
    lineage_map = dict(lineage_map or {})
    body = extract_cte_body(sql, cte_name)
    if not body:
        return []
    union_branches = split_top_level_union_all(body)
    if len(union_branches) > 1:
        merged = []
        seen = set()
        for branch in union_branches:
            try:
                cols = extract_union_branch_columns(branch, lineage_map=lineage_map)
            except Exception:
                cols = _select_output_columns(branch)
            for col in cols or []:
                norm = _normalize_column_token(col)
                if norm and norm not in seen:
                    seen.add(norm)
                    merged.append(col)
        if merged:
            return merged
    # Seed map with direct columns first for relation lookups.
    direct = _select_output_columns(body)
    if direct:
        lineage_map.setdefault(cte_name.lower(), direct)
    try:
        return extract_union_branch_columns(body, lineage_map=lineage_map)
    except Exception:
        return direct


def extract_cte_output_column_map(sql: str, cte_name: str, lineage_map=None) -> dict:
    """Return normalized->raw output column map for a CTE, including alias-star expansion."""
    body = extract_cte_body(sql, cte_name)
    resolved = cte_name
    if not body:
        target = _normalize_cte_name(cte_name)
        for candidate in _cte_names(sql):
            if _normalize_cte_name(candidate) == target:
                resolved = candidate
                break
    cols = extract_cte_output_columns(sql, resolved, lineage_map=lineage_map)
    result = {}
    for col in cols or []:
        norm = _normalize_column_token(col)
        if norm and norm not in result:
            result[norm] = col
    return result


def _expand_union_select_star_cte(sql_text: str, cte_name: str) -> str:
    """Expand SELECT * UNION branches to explicit, aligned column lists."""
    body = _cte_body_for(sql_text, cte_name)
    if not body or not re.search(r'\bUNION\s+ALL\b', body, re.IGNORECASE):
        return sql_text

    branches = re.split(r'\bUNION\s+ALL\b', body, flags=re.IGNORECASE)
    if len(branches) < 2:
        return sql_text

    branch_info = []
    saw_select_star = False
    schema = []
    seen = set()
    for branch in branches:
        source = _select_source(branch)
        has_select_star = bool(re.search(r'\bSELECT\s+(?:[A-Za-z_][A-Za-z0-9_]*\.)?\*\s+FROM\b', branch, re.IGNORECASE))
        saw_select_star = saw_select_star or has_select_star
        branch_columns = _select_output_columns(branch)
        source_columns = _select_output_columns(_cte_body_for(sql_text, source)) if source else []
        available_columns = source_columns or branch_columns
        for column in available_columns:
            key = column.lower()
            if key not in seen:
                seen.add(key)
                schema.append(column)
        branch_info.append((source, branch_columns, available_columns))

    if not schema or not saw_select_star:
        return sql_text

    rendered = []
    for source, _branch_columns, available_columns in branch_info:
        if not source:
            return sql_text
        rendered.append(_render_union_branch(source, schema, available_columns))

    return _replace_cte_body(sql_text, cte_name, '\n' + '\n\nUNION ALL\n\n'.join(rendered) + '\n')


def enforce_explicit_union_columns(sql_text: str) -> str:
    """Expand common UNION ALL SELECT * patterns into aligned explicit columns."""
    sql = sql_text or ''
    for cte_name in _cte_names(sql):
        sql = _expand_union_select_star_cte(sql, cte_name)
    return sql


def enforce_complete_final_select(sql_text: str) -> str:
    """Ensure generated SQL ends with a complete final SELECT when possible."""
    sql = sql_text or ''
    names = _cte_names(sql)
    lower_names = {name.lower(): name for name in names}
    preferred = (
        lower_names.get('final_model')
        or lower_names.get('final_mart')
        or lower_names.get('facttable_with_expenses')
        or lower_names.get('fact_table_with_expenses')
    )
    if not preferred:
        return sql

    if re.search(r'(?is)\bSELECT\s*$', sql):
        return re.sub(r'(?is)\bSELECT\s*$', f'SELECT *\nFROM {preferred}', sql).rstrip()

    final_source = (_final_select_source(sql) or '').lower()
    if final_source in {'facttable', 'fact_table', 'facttable_with_expenses', 'fact_table_with_expenses'}:
        if lower_names.get('final_model') or lower_names.get('final_mart'):
            return re.sub(
                r'(?is)\bSELECT\s+\*\s+FROM\s+[A-Za-z_][A-Za-z0-9_]*\s*$',
                f'SELECT *\nFROM {preferred}',
                sql,
            ).rstrip()
    return sql


def enforce_final_model_wrapper(sql_text: str) -> str:
    """Wrap a joined final fact query in final_model so validation sees the mart boundary."""
    sql = sql_text or ''
    lower_names = {name.lower() for name in _cte_names(sql)}
    if lower_names & {'final_model', 'final_mart'}:
        return sql

    final_source = (_final_select_source(sql) or '').lower()
    raw_fact_sources = {'facttable', 'fact_table', 'facttable_with_expenses', 'fact_table_with_expenses'}
    if final_source not in raw_fact_sources:
        return sql

    final_tail = _final_select_tail(sql)
    if not final_tail or not re.search(r'\bJOIN\b', final_tail, re.IGNORECASE):
        return sql

    select_matches = list(re.finditer(r'\bSELECT\b', sql, flags=re.IGNORECASE))
    if not select_matches:
        return sql
    final_select_start = select_matches[-1].start()
    prefix = sql[:final_select_start].rstrip()
    if not re.search(r'\bWITH\b', prefix, re.IGNORECASE):
        return sql

    separator = '\n' if prefix.endswith(',') else ',\n'
    return (
        f"{prefix}{separator}"
        "final_model AS (\n"
        f"{final_tail.rstrip()}\n"
        ")\n"
        "SELECT *\n"
        "FROM final_model"
    )


def enforce_facttable_expenses_schema(sql_text: str, plan=None) -> str:
    """Repair the recurring fact/expenses UNION shape without relying on the LLM."""
    sql = sql_text or ''
    fact_body = _cte_body_for(sql, 'facttable') or _cte_body_for(sql, 'fact_table')
    union_cte = 'facttable_with_expenses' if _cte_body_for(sql, 'facttable_with_expenses') else 'fact_table_with_expenses'
    union_body = _cte_body_for(sql, union_cte)
    if not fact_body or not union_body or not re.search(r'\bUNION\s+ALL\b', union_body, re.IGNORECASE):
        return sql

    branches = re.split(r'\bUNION\s+ALL\b', union_body, flags=re.IGNORECASE)
    if len(branches) != 2:
        return sql

    first_source = _select_source(branches[0]) or 'facttable'
    second_source = _select_source(branches[1]) or 'expenses_for_fact'
    if _normalize_cte_name(first_source) not in {'facttable', 'fact_table'}:
        first_source = 'facttable' if _cte_body_for(sql, 'facttable') else 'fact_table'
    if _cte_body_for(sql, 'expenses'):
        second_source = 'expenses'

    new_union_body = _build_dynamic_fact_expenses_union_body(sql, first_source, second_source, plan=plan)
    if not new_union_body:
        return sql
    return _replace_cte_body(sql, union_cte, new_union_body)


def enforce_facttable_region_schema(sql_text: str) -> str:
    """Preserve Region in facttable when it is available upstream."""
    sql = sql_text or ''
    fact_body = _cte_body_for(sql, 'facttable')
    if not fact_body:
        return sql
    fact_columns = _select_output_columns(fact_body)
    if 'region' in {c.lower() for c in fact_columns}:
        return sql

    upstream = _select_source(fact_body)
    upstream_columns = _select_output_columns(_cte_body_for(sql, upstream)) if upstream else []
    if 'region' not in {c.lower() for c in upstream_columns}:
        return sql

    fields = _split_sql_like_fields(_select_body(fact_body))
    if not fields:
        return sql
    insert_at = next((idx + 1 for idx, item in enumerate(fields)
                      if _extract_output_name(*_split_alias_from_expression(item)).lower() == 'monthlyregionkey'), 1)
    fields.insert(insert_at, 'Region')
    match = re.search(r'\bSELECT\b(.*?)\bFROM\b', fact_body, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return sql
    new_select = '\n    ' + ',\n    '.join(fields) + '\n'
    fact_body = fact_body[:match.start(1)] + new_select + fact_body[match.end(1):]
    return _replace_cte_body(sql, 'facttable', fact_body)


def enforce_expenses_yyyymm_coercion(sql_text: str) -> str:
    """Ensure expenses-like CTEs expose shifted YYYYMM for downstream UNION branches."""
    sql = sql_text or ''
    for cte_name in _cte_names(sql):
        lname = cte_name.lower()
        if 'expenses' not in lname:
            continue
        body = _cte_body_for(sql, cte_name)
        if not body:
            continue
        cols = _select_output_columns(body)
        lower_cols = {c.lower() for c in cols}
        if not ({'monthlyregionkey', 'account'} & lower_cols):
            continue
        select_body = _select_body(body)
        fields = _split_sql_like_fields(select_body)
        if not fields:
            continue
        replaced = False
        for idx, field in enumerate(fields):
            expr, alias = _split_alias_from_expression(field)
            out = (alias or expr or '').strip().strip('"').strip('[]').lower()
            if out == 'yyyymm':
                fields[idx] = "DATEADD(month, 12, TO_DATE(YYYYMM::varchar, 'YYYYMM')) AS YYYYMM"
                replaced = True
                break
        if not replaced:
            fields.append("DATEADD(month, 12, TO_DATE(YYYYMM::varchar, 'YYYYMM')) AS YYYYMM")
        match = re.search(r'\bSELECT\b(.*?)\bFROM\b', body, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        new_select = '\n    ' + ',\n    '.join(fields) + '\n'
        body = body[:match.start(1)] + new_select + body[match.end(1):]
        sql = _replace_cte_body(sql, cte_name, body)
    return sql


def enforce_expenses_account_join(sql_text: str) -> str:
    """Keep Expenses joins at MonthlyRegionKey + Account grain."""
    sql = sql_text or ''
    fact_alias_match = re.search(
        r'\bFROM\s+facttable_with_expenses\s+(?:AS\s+)?([A-Za-z_][A-Za-z0-9_]*)\b',
        sql,
        flags=re.IGNORECASE,
    )
    fact_alias = fact_alias_match.group(1) if fact_alias_match else 'f'

    join_pattern = re.compile(
        r'(\b(?:LEFT|RIGHT|FULL|INNER)?\s*JOIN\s+'
        r'(?:expenses|expenses_for_fact|int_expenses|expenses_aggregated|int_expenses_aggregated)\s+'
        r'(?:AS\s+)?(?P<alias>[A-Za-z_][A-Za-z0-9_]*)\s+ON\s+)'
        r'(?P<condition>.*?)(?=\b(?:LEFT|RIGHT|FULL|INNER|CROSS)?\s*JOIN\b|\bWHERE\b|\bGROUP\s+BY\b|\bORDER\s+BY\b|\)|$)',
        flags=re.IGNORECASE | re.DOTALL,
    )

    def replace_join(match):
        prefix = match.group(1)
        exp_alias = match.group('alias')
        condition = match.group('condition')
        if 'monthlyregionkey' not in condition.lower():
            return match.group(0)
        condition = re.sub(
            r'\s+AND\s+\(?\s*[A-Za-z_][A-Za-z0-9_]*\."?[A-Za-z_][A-Za-z0-9_ ]*"?\s+IS\s+NOT\s+NULL\s+OR\s+'
            r'[A-Za-z_][A-Za-z0-9_]*\."?[A-Za-z_][A-Za-z0-9_ ]*"?\s+IS\s+NOT\s+NULL\s*\)?',
            '',
            condition,
            flags=re.IGNORECASE,
        )
        condition = re.sub(
            r'\s+AND\s+[A-Za-z_][A-Za-z0-9_]*\."?Account"?\s+IS\s+NOT\s+NULL\b',
            '',
            condition,
            flags=re.IGNORECASE,
        )
        if re.search(rf'\b{re.escape(fact_alias)}\."?Account"?\s*=\s*{re.escape(exp_alias)}\."?Account"?', condition, re.IGNORECASE):
            return prefix + condition
        return prefix + condition.rstrip() + f'\n   AND {fact_alias}.Account = {exp_alias}.Account'

    return join_pattern.sub(replace_join, sql)


def has_union_star_branch(sql: str) -> bool:
    if not sql:
        return False
    return bool(re.search(
        r'(?is)\bSELECT\s+(?:[A-Za-z_][A-Za-z0-9_]*\.)?\*\s*(?:,|\bFROM\b)[\s\S]*?\bUNION\s+ALL\b',
        sql
    ))


def has_fact_expenses_join_cte(sql: str) -> bool:
    return bool(_cte_body_for(sql or '', 'facttable_with_expenses') or _cte_body_for(sql or '', 'fact_table_with_expenses'))


def _fact_expenses_cte_name(sql: str) -> str:
    if _cte_body_for(sql or '', 'facttable_with_expenses'):
        return 'facttable_with_expenses'
    if _cte_body_for(sql or '', 'fact_table_with_expenses'):
        return 'fact_table_with_expenses'
    return ''


def _needs_fact_expenses_dynamic_rebuild(sql: str) -> bool:
    cte_name = _fact_expenses_cte_name(sql or '')
    if not cte_name:
        return False
    body = _cte_body_for(sql or '', cte_name)
    if not body:
        return False
    if re.search(r'\bJOIN\s+(?:expenses|expenses_for_fact|int_expenses|expenses_aggregated|int_expenses_aggregated)\b', body, re.IGNORECASE):
        return True
    if has_union_star_branch(body):
        return True
    if re.search(r'\bUNION\s+ALL\b', body, re.IGNORECASE):
        return True
    return False


def fact_expenses_cte_has_alias_star(sql: str) -> bool:
    body = extract_cte_body(sql, 'facttable_with_expenses') or extract_cte_body(sql, 'fact_table_with_expenses')
    if not body:
        return False
    return bool(re.search(r'(?is)\bSELECT\s+[A-Za-z_][A-Za-z0-9_]*\.\*', body))


def expand_fact_expenses_alias_star(sql: str) -> str:
    """
    Expand first UNION branch alias-star in facttable_with_expenses to explicit fact columns.
    Returns original SQL if deterministic expansion cannot be proven.
    """
    cte_name = _fact_expenses_cte_name(sql)
    if not cte_name:
        return sql
    body = _cte_body_for(sql, cte_name)
    if not body or not re.search(r'\bUNION\s+ALL\b', body, re.IGNORECASE):
        return sql
    if not re.search(r'(?is)\bSELECT\s+[A-Za-z_][A-Za-z0-9_]*\.\*', body):
        return sql

    branches = split_top_level_union_all(body)
    if len(branches) < 2:
        return sql

    first_branch = branches[0]
    select_match = re.search(
        r'(?is)^\s*SELECT\s+(?P<select_body>.*?)\bFROM\b\s+(?P<source>[A-Za-z_][A-Za-z0-9_]*)\s+(?:AS\s+)?(?P<alias>[A-Za-z_][A-Za-z0-9_]*)\b(?P<tail>[\s\S]*)$',
        first_branch.strip(),
    )
    if not select_match:
        return sql

    source_cte = select_match.group('source')
    alias = select_match.group('alias')
    select_body = select_match.group('select_body')
    source_cols = extract_cte_output_columns(sql, source_cte, lineage_map=_cte_columns_by_name(sql))
    if not source_cols:
        return sql

    fields = _split_sql_like_fields(select_body)
    expanded_fields = []
    replaced_star = False
    star_pattern = re.compile(rf'^\s*{re.escape(alias)}\.\*\s*$', re.IGNORECASE)
    for field in fields:
        if star_pattern.match(field):
            replaced_star = True
            expanded_fields.extend(_sql_column_ref(col) for col in source_cols)
        else:
            expanded_fields.append(field.strip())
    if not replaced_star:
        return sql

    rebuilt_first = (
        'SELECT\n    '
        + ',\n    '.join(expanded_fields)
        + f"\nFROM {source_cte}{select_match.group('tail')}"
    )
    branches[0] = rebuilt_first
    new_body = '\n' + '\n\nUNION ALL\n\n'.join(branches) + '\n'
    return _replace_cte_body(sql, cte_name, new_body)


def _mark_dynamic_union_rebuild_failed(sql: str) -> str:
    marker = '-- DYNAMIC_UNION_REBUILD_FAILED'
    if marker in (sql or ''):
        return sql
    return (sql or '').rstrip() + '\n' + marker + '\n'


def plan_has_expenses_concatenate(plan) -> bool:
    """
    Return true when parsed plan indicates expenses is concatenated into a fact table.
    """
    for item in plan or []:
        text = ' '.join(str(item.get(k, '')) for k in ['table', 'source', 'concatenate_target', 'operation']).lower()
        if item.get('is_concatenate') and 'expense' in text:
            return True
        if 'expense' in text and 'concat' in text:
            return True
    return False


def final_model_has_bad_expenses_join(sql: str) -> bool:
    """
    Return true when final_model/final_mart joins expenses directly to a fact/base CTE.
    """
    body = extract_cte_body(sql, 'final_model') or extract_cte_body(sql, 'final_mart')
    if not body:
        return False
    return bool(re.search(
        r'(?is)\bFROM\s+(facttable_with_expenses|fact_table_with_expenses|facttable|fact_table|[A-Za-z_][A-Za-z0-9_]*fact[A-Za-z0-9_]*)\s+\w+[\s\S]*?\bJOIN\s+expenses\s+\w+\s+ON\b',
        body
    ))


def remove_join_clause(body: str, join_alias: str) -> str:
    """Remove a direct expenses join and its full ON predicate from a SELECT body."""
    alias = re.escape(str(join_alias or '').strip())
    if not alias:
        return body
    pattern = re.compile(
        rf'(?is)\s+(?:LEFT|RIGHT|INNER|FULL|OUTER)?\s*JOIN\s+expenses\s+(?:AS\s+)?{alias}\s+ON\s+'
        rf'.*?(?=\s+(?:LEFT|RIGHT|INNER|FULL|OUTER)?\s*JOIN\b|\s+WHERE\b|\s+GROUP\s+BY\b|'
        rf'\s+ORDER\s+BY\b|\s+HAVING\b|\s+QUALIFY\b|\s*\)|$)'
    )
    cleaned = pattern.sub('\n', body or '')
    cleaned = re.sub(r'(?i)([A-Za-z_][A-Za-z0-9_]*\.Account)(LEFT\s+JOIN)', r'\1 \2', cleaned)
    cleaned = re.sub(
        rf'(?im)^\s*AND\s+[A-Za-z_][A-Za-z0-9_]*\."?[A-Za-z_][A-Za-z0-9_ ]*"?\s*=\s*{alias}\."?[A-Za-z_][A-Za-z0-9_ ]*"?\s*$\n?',
        '',
        cleaned,
    )
    return cleaned


def _remove_select_items_for_alias(body: str, alias: str) -> str:
    select_body = _select_body(body)
    fields = _split_sql_like_fields(select_body)
    if not fields:
        return body
    kept = [
        item for item in fields
        if not re.search(rf'^\s*{re.escape(alias)}\.(?:\*|"?[A-Za-z_][A-Za-z0-9_ ]*"?)\b', item, flags=re.IGNORECASE)
    ]
    if len(kept) == len(fields):
        return body
    return re.sub(
        r'(\bSELECT\b)(.*?)(\bFROM\b)',
        lambda m: f"{m.group(1)}\n    " + ',\n    '.join(kept) + f"\n{m.group(3)}",
        body,
        flags=re.IGNORECASE | re.DOTALL,
        count=1,
    )


def _cleanup_leftover_expenses_alias_refs(sql: str, body: str, alias: str = 'e') -> str:
    """
    After expenses are merged into facttable_with_expenses, retarget safe account
    predicates from the old expenses alias to the fact/union alias and remove any
    remaining orphaned expenses alias references.
    """
    body = body or ''
    alias = str(alias or 'e').strip()
    if not alias:
        return body

    from_match = re.search(
        r'\bFROM\s+(facttable_with_expenses|fact_table_with_expenses)\s+(?:AS\s+)?(?P<fact_alias>[A-Za-z_][A-Za-z0-9_]*)\b',
        body,
        flags=re.IGNORECASE,
    )
    if not from_match:
        return body

    union_cte = from_match.group(1)
    fact_alias = from_match.group('fact_alias')
    union_cols = extract_cte_output_column_map(sql or '', union_cte)
    if 'account' in union_cols:
        fact_account_ref = f'{fact_alias}.{union_cols.get("account") or "account"}'
        alias_re = re.escape(alias)
        body = re.sub(
            rf'(?is)(?<![A-Za-z0-9_]){alias_re}\s*\.\s*"?account"?\s*=\s*'
            rf'(?P<right>[A-Za-z_][A-Za-z0-9_]*\s*\.\s*"?account"?)',
            lambda m: f'{fact_account_ref} = {m.group("right")}',
            body,
        )
        body = re.sub(
            rf'(?is)(?P<left>[A-Za-z_][A-Za-z0-9_]*\s*\.\s*"?account"?)\s*=\s*'
            rf'(?<![A-Za-z0-9_]){alias_re}\s*\.\s*"?account"?',
            lambda m: f'{m.group("left")} = {fact_account_ref}',
            body,
        )

    body = _remove_select_items_for_alias(body, alias)
    if not re.search(rf'(?is)\bJOIN\s+expenses\s+(?:AS\s+)?{re.escape(alias)}\b', body):
        body = re.sub(
            rf'(?im)^\s*(?:AND|OR)\s+.*(?<![A-Za-z0-9_]){re.escape(alias)}\s*\.\s*'
            rf'(?:\*|"?[A-Za-z_][A-Za-z0-9_ ]*"?)\b.*$\n?',
            '',
            body,
        )
    return body


def _explicit_concatenate_fields_for_target(plan, target_table: str, source_table: str = ''):
    target_norm = _normalize_cte_name(target_table)
    source_norm = _normalize_cte_name(source_table)
    for item in plan or []:
        if not item.get('is_concatenate'):
            continue
        if _normalize_cte_name(item.get('concatenate_target') or '') != target_norm:
            continue
        item_names = {
            _normalize_cte_name(item.get('table') or ''),
            _normalize_cte_name(item.get('source') or ''),
            *{_normalize_cte_name(src) for src in (item.get('source_tables') or [])},
        }
        source_matches = (
            not source_norm
            or source_norm in item_names
            or any(source_norm in name or name in source_norm for name in item_names if name)
        )
        # Executive Dashboard emits Expenses under a few deterministic CTE names;
        # once Qlik explicitly targets FactTable, the listed LOAD fields are the
        # authoritative source branch shape even if the generated CTE was renamed.
        if not source_matches and target_norm == 'facttable' and 'expense' in source_norm:
            source_matches = True
        if not source_matches:
            continue
        fields = [_normalize_column_token(_extract_output_column_name(field)) for field in (item.get('fields') or [])]
        return {field for field in fields if field}
    return None


def _has_explicit_facttable_concatenate(plan, qvs_script: str = '') -> bool:
    if any(
        item.get('is_concatenate')
        and _normalize_cte_name(item.get('concatenate_target') or '') == 'facttable'
        for item in (plan or [])
    ):
        return True
    return bool(re.search(
        r'(?is)\bCONCATENATE\s*\(\s*FactTable\s*\)\s*LOAD\b.*?\bRESIDENT\s+Expenses\b',
        qvs_script or '',
    ))


def _explicit_facttable_concat_fields_from_qvs(qvs_script: str) -> set:
    match = re.search(
        r'(?is)\bCONCATENATE\s*\(\s*FactTable\s*\)\s*LOAD\s+(?P<fields>.*?)\bRESIDENT\s+Expenses\b',
        qvs_script or '',
    )
    if not match:
        return set()
    fields = []
    for item in split_top_level_csv(match.group('fields')):
        cleaned = re.sub(r'(?is)\s+AS\s+.*$', '', item).strip()
        cleaned = re.sub(r'(?is)\bDISTINCT\b', '', cleaned).strip()
        if cleaned:
            fields.append(_normalize_column_token(_extract_output_column_name(cleaned)))
    return {field for field in fields if field}


def _target_table_for_fact_cte(fact_cte: str) -> str:
    norm = _normalize_cte_name(fact_cte)
    if norm in {'facttable', 'fact_table'}:
        return 'FactTable'
    return fact_cte


def _cte_for_plan_table(sql: str, table_name: str) -> str:
    target = _normalize_cte_name(table_name)
    aliases = {
        'facttable': ['facttable', 'fact_table'],
        'expenses': ['expenses', 'expenses_for_fact', 'int_expenses', 'expenses_aggregated', 'int_expenses_aggregated'],
    }
    for candidate in aliases.get(target, [target]):
        if _cte_body_for(sql or '', candidate):
            return candidate
    for name in _cte_names(sql or ''):
        if _normalize_cte_name(name) == target:
            return name
    return ''


def _explicit_concat_target_source_ctes(sql: str, plan=None):
    for item in plan or []:
        if not item.get('is_concatenate'):
            continue
        target = item.get('concatenate_target') or ''
        target_cte = _cte_for_plan_table(sql, target)
        source_cte = _cte_for_plan_table(sql, item.get('table') or '')
        if not source_cte:
            for src in item.get('source_tables') or []:
                source_cte = _cte_for_plan_table(sql, src)
                if source_cte:
                    break
        if target_cte and source_cte:
            return target_cte, source_cte
    return '', ''


def enforce_explicit_facttable_concatenate_schema(sql_text: str, plan=None, qvs_script: str = '') -> str:
    """Force explicit CONCATENATE (FactTable) unions to the target FactTable schema only."""
    return enforce_explicit_concat_target_schema(sql_text, plan, qvs_script=qvs_script)


def enforce_explicit_concat_target_schema(sql_text: str, plan=None, qvs_script: str = '') -> str:
    """Rebuild explicit Qlik CONCATENATE target unions using only the target table schema."""
    sql = sql_text or ''
    concat_blocks = sum(
        1 for item in (plan or [])
        if item.get('is_concatenate') and _normalize_cte_name(item.get('concatenate_target') or '') == 'facttable'
    )
    has_explicit_concat = _has_explicit_facttable_concatenate(plan, qvs_script=qvs_script)
    logger.info(
        "EXPLICIT_CONCAT_ENFORCER_ENTER plan_present=%s concat_blocks=%s",
        bool(plan),
        concat_blocks,
    )
    if not has_explicit_concat:
        logger.info("EXPLICIT_CONCAT_ENFORCER_EXIT changed=%s", False)
        return sql
    union_cte = _fact_expenses_cte_name(sql)
    if not union_cte:
        logger.info("EXPLICIT_CONCAT_ENFORCER_EXIT changed=%s", False)
        return sql
    fact_cte, source_cte = _explicit_concat_target_source_ctes(sql, plan)
    if not fact_cte:
        fact_cte = 'facttable' if _cte_body_for(sql, 'facttable') else ('fact_table' if _cte_body_for(sql, 'fact_table') else '')
    if not source_cte:
        source_cte = 'expenses' if _cte_body_for(sql, 'expenses') else ''
    logger.info("EXPLICIT_CONCAT_ENFORCER_TARGET target=%s source=%s", fact_cte, source_cte)
    if not fact_cte or not source_cte:
        logger.info("EXPLICIT_CONCAT_ENFORCER_EXIT changed=%s", False)
        return _mark_dynamic_union_rebuild_failed(sql)
    if not _cte_body_for(sql, fact_cte) or not _cte_body_for(sql, source_cte):
        logger.info("EXPLICIT_CONCAT_ENFORCER_EXIT changed=%s", False)
        return _mark_dynamic_union_rebuild_failed(sql)

    lineage_map = _cte_columns_by_name(sql)
    fact_cols = extract_cte_output_column_map(sql, fact_cte, lineage_map=lineage_map)
    union_cols_before = extract_cte_output_column_map(sql, union_cte, lineage_map=lineage_map)
    source_cols = extract_cte_output_column_map(sql, source_cte, lineage_map=lineage_map)
    removed_source_only = sorted(
        raw for norm, raw in source_cols.items()
        if norm in union_cols_before and norm not in fact_cols
    )
    logger.info("EXPLICIT_CONCAT_ENFORCER_TARGET_COLS cols=%s", list(fact_cols.values()))
    logger.info("EXPLICIT_CONCAT_ENFORCER_SOURCE_ONLY removed=%s", removed_source_only)

    effective_plan = plan
    if _explicit_concatenate_fields_for_target(plan, 'FactTable', source_cte) is None and qvs_script:
        qvs_fields = _explicit_facttable_concat_fields_from_qvs(qvs_script)
        if qvs_fields:
            effective_plan = [{
                'table': source_cte,
                'source': source_cte,
                'source_tables': [source_cte],
                'fields': list(qvs_fields),
                'is_concatenate': True,
                'concatenate_target': 'FactTable',
            }]
    union_body = _build_dynamic_fact_expenses_union_body(sql, fact_cte, source_cte, plan=effective_plan)
    if not union_body:
        logger.info("EXPLICIT_CONCAT_ENFORCER_EXIT changed=%s", False)
        return _mark_dynamic_union_rebuild_failed(sql)
    before_body = _cte_body_for(sql, union_cte)
    changed = before_body.strip() != union_body.strip()
    logger.info(
        "EXPLICIT_CONCAT_SCHEMA_ENFORCED target=%s source=%s target_cols=%s removed_source_only=%s",
        fact_cte,
        source_cte,
        len(fact_cols),
        removed_source_only,
    )
    logger.info("EXPLICIT_CONCAT_ENFORCER_EXIT changed=%s", changed)
    enforced = _replace_cte_body(sql, union_cte, union_body)
    marker = '-- EXPLICIT_CONCAT_SCHEMA_ENFORCED'
    if marker not in enforced:
        enforced = enforced.rstrip() + '\n' + marker + '\n'
    return enforced


def force_replace_explicit_facttable_concat_cte(sql, plan=None, qvs_script=None) -> tuple[str, bool]:
    """Hard-replace facttable_with_expenses for explicit Qlik CONCATENATE (FactTable)."""
    text = sql or ''
    detected = _has_explicit_facttable_concatenate(plan, qvs_script=qvs_script or '')
    logger.info("FORCE_EXPLICIT_CONCAT_REPLACE_ENTER detected=%s", detected)
    if not detected:
        return text, False

    union_cte = _fact_expenses_cte_name(text)
    if not union_cte:
        logger.info("FORCE_EXPLICIT_CONCAT_REPLACE_DONE applied=%s fact_cols=%s leaked_cols=%s", False, 0, [])
        return text, False

    fact_cte = 'facttable' if _cte_body_for(text, 'facttable') else ('fact_table' if _cte_body_for(text, 'fact_table') else '')
    expenses_cte = 'expenses' if _cte_body_for(text, 'expenses') else ''
    if not fact_cte or not expenses_cte:
        logger.info("FORCE_EXPLICIT_CONCAT_REPLACE_DONE applied=%s fact_cols=%s leaked_cols=%s", False, 0, [])
        return _mark_dynamic_union_rebuild_failed(text) + '-- DYNAMIC_UNION_REBUILD_FAILED: explicit concat replacement failed\n', False

    lineage_map = _cte_columns_by_name(text)
    fact_cols_map = extract_cte_output_column_map(text, fact_cte, lineage_map=lineage_map)
    expense_cols_map = extract_cte_output_column_map(text, expenses_cte, lineage_map=lineage_map)
    if not fact_cols_map or not expense_cols_map:
        logger.info("FORCE_EXPLICIT_CONCAT_REPLACE_DONE applied=%s fact_cols=%s leaked_cols=%s", False, len(fact_cols_map), [])
        return _mark_dynamic_union_rebuild_failed(text) + '-- DYNAMIC_UNION_REBUILD_FAILED: explicit concat replacement failed\n', False

    explicit_fields = _explicit_concatenate_fields_for_target(plan, 'FactTable', expenses_cte)
    if explicit_fields is None:
        explicit_fields = _explicit_facttable_concat_fields_from_qvs(qvs_script or '')
    explicit_fields = explicit_fields or set()

    fact_rendered = [_sql_column_ref(raw) for _norm, raw in fact_cols_map.items()]
    expense_rendered = []
    for norm, raw in fact_cols_map.items():
        expense_raw = expense_cols_map.get(norm) if norm in explicit_fields else None
        if expense_raw:
            expense_rendered.append(_sql_column_ref(expense_raw))
        else:
            expense_rendered.append(f'CAST(NULL AS {_infer_sql_type_from_name(raw)}) AS {_sql_column_ref(raw)}')

    new_body = (
        '\nSELECT\n    '
        + ',\n    '.join(fact_rendered)
        + f'\nFROM {fact_cte}\n\nUNION ALL\n\nSELECT\n    '
        + ',\n    '.join(expense_rendered)
        + f'\nFROM {expenses_cte}\n'
    )
    replaced = _replace_cte_body(text, union_cte, new_body)

    fact_cols = extract_cte_output_columns(replaced, fact_cte)
    union_cols = extract_cte_output_columns(replaced, union_cte)
    leaked_cols = [
        raw for norm, raw in expense_cols_map.items()
        if norm in {_normalize_column_token(col) for col in union_cols}
        and norm not in {_normalize_column_token(col) for col in fact_cols}
    ]
    applied = fact_cols == union_cols and not leaked_cols
    logger.info(
        "FORCE_EXPLICIT_CONCAT_REPLACE_DONE applied=%s fact_cols=%s leaked_cols=%s",
        applied,
        len(fact_cols),
        leaked_cols,
    )
    if not applied:
        return _mark_dynamic_union_rebuild_failed(replaced) + '-- DYNAMIC_UNION_REBUILD_FAILED: explicit concat replacement failed\n', False
    marker = '-- EXPLICIT_CONCAT_SCHEMA_ENFORCED'
    if marker not in replaced:
        replaced = replaced.rstrip() + '\n' + marker + '\n'
    return replaced, True


def _build_dynamic_fact_expenses_union_body(sql: str, fact_cte: str, expenses_cte: str, plan=None) -> str:
    if not fact_cte or not expenses_cte:
        return ''
    lineage_map = _cte_columns_by_name(sql)
    fact_cols_map = extract_cte_output_column_map(sql, fact_cte, lineage_map=lineage_map)
    expense_cols_map = extract_cte_output_column_map(sql, expenses_cte, lineage_map=lineage_map)
    if not fact_cols_map or not expense_cols_map:
        return ''

    explicit_concat_fields = _explicit_concatenate_fields_for_target(
        plan,
        _target_table_for_fact_cte(fact_cte),
        expenses_cte,
    )
    union_cols = list(fact_cols_map.items())
    if explicit_concat_fields is None:
        for norm, raw in expense_cols_map.items():
            if norm not in fact_cols_map:
                union_cols.append((norm, raw))

    def typed_null(raw_column: str) -> str:
        return f'CAST(NULL AS {_infer_sql_type_from_name(raw_column)}) AS {_sql_column_ref(raw_column)}'

    first_rendered = []
    for norm, raw in union_cols:
        fact_col = fact_cols_map.get(norm)
        first_rendered.append(_sql_column_ref(fact_col) if fact_col else typed_null(raw))

    second_rendered = []
    for norm, raw in union_cols:
        expense_col = expense_cols_map.get(norm)
        if explicit_concat_fields is not None and norm not in explicit_concat_fields:
            expense_col = None
        second_rendered.append(_sql_column_ref(expense_col) if expense_col else typed_null(raw))

    return (
        '\nSELECT\n    '
        + ',\n    '.join(first_rendered)
        + f'\nFROM {fact_cte}\n\nUNION ALL\n\nSELECT\n    '
        + ',\n    '.join(second_rendered)
        + f'\nFROM {expenses_cte}\n'
    )


def _ensure_facttable_with_expenses_cte(sql: str) -> str:
    if _fact_expenses_cte_name(sql):
        return sql
    fact_cte = 'facttable' if _cte_body_for(sql, 'facttable') else ('fact_table' if _cte_body_for(sql, 'fact_table') else '')
    expenses_cte = 'expenses' if _cte_body_for(sql, 'expenses') else ''
    if not fact_cte or not expenses_cte:
        return _mark_dynamic_union_rebuild_failed(sql)

    union_body = _build_dynamic_fact_expenses_union_body(sql, fact_cte, expenses_cte)
    if not union_body:
        return _mark_dynamic_union_rebuild_failed(sql)

    final_match = re.search(r'\b(final_model|final_mart)\s+AS\s*\(', sql, flags=re.IGNORECASE)
    cte_def = f'facttable_with_expenses AS ({union_body})'
    if final_match:
        insert_at = final_match.start()
        prefix = sql[:insert_at].rstrip()
        suffix = sql[insert_at:]
        sep = ',\n' if not prefix.endswith(',') else '\n'
        return prefix + sep + cte_def + ',\n' + suffix
    # fallback: append at end of WITH chain
    select_matches = list(re.finditer(r'\bSELECT\b', sql, flags=re.IGNORECASE))
    if not select_matches:
        return _mark_dynamic_union_rebuild_failed(sql)
    insert_at = select_matches[-1].start()
    prefix = sql[:insert_at].rstrip()
    suffix = sql[insert_at:]
    sep = ',\n' if not prefix.endswith(',') else '\n'
    return prefix + sep + cte_def + '\n' + suffix


def rewrite_final_model_to_use_fact_expenses(sql: str) -> str:
    """
    Rewrite final_model to use facttable_with_expenses and remove direct expenses join/projections.
    """
    target_cte = 'final_model' if _cte_body_for(sql, 'final_model') else ('final_mart' if _cte_body_for(sql, 'final_mart') else '')
    if not target_cte:
        return sql
    body = _cte_body_for(sql, target_cte)
    if not body:
        return sql

    from_match = re.search(
        r'\bFROM\s+(facttable_with_expenses|fact_table_with_expenses|facttable|fact_table)\s+(?:AS\s+)?(?P<fact_alias>[A-Za-z_][A-Za-z0-9_]*)\b',
        body,
        flags=re.IGNORECASE,
    )
    join_match = re.search(
        r'\b(?:LEFT|RIGHT|FULL|INNER)?\s*JOIN\s+expenses\s+(?:AS\s+)?(?P<exp_alias>[A-Za-z_][A-Za-z0-9_]*)\s+ON\b',
        body,
        flags=re.IGNORECASE,
    )
    if not from_match or not join_match:
        return sql

    exp_alias = join_match.group('exp_alias')

    # Replace FROM source to facttable_with_expenses preserving alias.
    body = re.sub(
        r'\bFROM\s+(facttable|fact_table)\s+(?:AS\s+)?([A-Za-z_][A-Za-z0-9_]*)\b',
        r'FROM facttable_with_expenses \2',
        body,
        flags=re.IGNORECASE,
        count=1,
    )

    body = remove_join_clause(body, exp_alias)
    body = _remove_select_items_for_alias(body, exp_alias)
    body = _cleanup_leftover_expenses_alias_refs(sql, body, exp_alias)

    return _replace_cte_body(sql, target_cte, body)


def repair_concat_union_from_plan(sql_text: str, plan=None) -> str:
    """Deterministically enforce CONCATENATE/UNION invariants from parsed plan metadata."""
    sql = sql_text or ''
    if not plan:
        sql = enforce_expenses_yyyymm_coercion(sql)
        return enforce_facttable_expenses_schema(sql)

    has_concat = any(item.get('is_concatenate') for item in (plan or []))
    has_fact_expenses_union = bool(
        re.search(
            r'(?is)\bfacttable_with_expenses\s+AS\s*\(.*?\bUNION\s+ALL\b',
            sql,
        )
    )
    if not has_concat and not has_fact_expenses_union:
        return sql

    sql = enforce_expenses_yyyymm_coercion(sql)
    sql = enforce_facttable_expenses_schema(sql, plan=plan)
    sql = enforce_explicit_union_columns(sql)
    if has_union_star_branch(sql):
        sql = rebuild_facttable_with_expenses_union(sql, plan)
    return sql


def rebuild_facttable_with_expenses_union(sql_text: str, plan=None) -> str:
    """Dynamically rebuild facttable_with_expenses UNION from parsed CTE schemas."""
    sql = sql_text or ''
    union_cte = _fact_expenses_cte_name(sql)
    if not union_cte:
        return sql
    union_body = _cte_body_for(sql, union_cte)
    if not union_body:
        return sql

    branches = re.split(r'\bUNION\s+ALL\b', union_body, flags=re.IGNORECASE)
    explicit_target_cte, explicit_source_cte = _explicit_concat_target_source_ctes(sql, plan)
    if explicit_target_cte and explicit_source_cte:
        fact_cte = explicit_target_cte
        expenses_cte = explicit_source_cte
    else:
        first_source = _select_source(branches[0]) if len(branches) >= 1 else ''
        second_source = _select_source(branches[1]) if len(branches) >= 2 else ''

        if first_source:
            fact_cte = first_source
        elif _cte_body_for(sql, 'facttable'):
            fact_cte = 'facttable'
        elif _cte_body_for(sql, 'fact_table'):
            fact_cte = 'fact_table'
        else:
            fact_cte = ''

        if _cte_body_for(sql, 'expenses'):
            expenses_cte = 'expenses'
        else:
            expenses_cte = second_source

    if not fact_cte or not expenses_cte:
        return _mark_dynamic_union_rebuild_failed(sql)
    if not _cte_body_for(sql, fact_cte) or not _cte_body_for(sql, expenses_cte):
        return _mark_dynamic_union_rebuild_failed(sql)

    # Keep Executive Dashboard semantics: explicit CONCATENATE (FactTable)
    # must use the target fact CTE as the base branch, never a nearby budget or
    # unrelated CTE selected from a malformed existing union.
    if explicit_target_cte and _normalize_cte_name(fact_cte) not in {'facttable', 'fact_table'}:
        return _mark_dynamic_union_rebuild_failed(sql)

    union_body_new = _build_dynamic_fact_expenses_union_body(sql, fact_cte, expenses_cte, plan=plan)
    if not union_body_new:
        return _mark_dynamic_union_rebuild_failed(sql)
    sql = _replace_cte_body(sql, union_cte, union_body_new)
    return sql


def preserve_hyphenated_source_names(sql_text: str, plan=None) -> str:
    """Restore exact hyphenated Qlik source names in dbt source() calls when plan metadata has them."""
    sql = sql_text or ''
    replacements = {}
    for item in plan or []:
        for source in item.get('source_tables') or []:
            exact_ref = _resolve_source_reference(source)
            match = re.search(r"source\s*\(\s*'raw'\s*,\s*'([^']+)'\s*\)", exact_ref)
            exact = match.group(1) if match else ''
            if '-' not in exact:
                continue
            replacements[exact.replace('-', '_').lower()] = exact
    if not replacements:
        return sql

    def repl(match):
        source_name = match.group(2)
        exact = replacements.get(source_name.lower())
        if not exact:
            return match.group(0)
        return "{{ source('" + match.group(1) + "', '" + exact + "') }}"

    return re.sub(
        r"\{\{\s*source\s*\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}",
        repl,
        sql,
        flags=re.IGNORECASE,
    )


def enforce_clean_historyflag_logic(sql_text: str) -> str:
    """Use the direct Qlik monthstart comparison for generated HistoryFlag CTEs."""
    sql = sql_text or ''
    replacement = (
        "CASE\n"
        "    WHEN yyyymm <= DATE_TRUNC('month', TO_DATE('2013-05-31'))\n"
        "    THEN 1 ELSE 0\n"
        "END AS _historyflag"
    )
    return re.sub(
        r'(?is)CASE\s+WHEN\s+[^;]*?(?:2013-05-31|DATEADD\s*\(|monthstart|makedate)[^;]*?END\s+AS\s+"?_?historyflag"?',
        replacement,
        sql,
    )


def finalize_generated_sql(sql_text: str, plan=None, qvs_script='') -> str:
    """Apply deterministic post-repair invariants before validation/return."""
    logger.info(
        "FINALIZE_CONTEXT plan_size=%s has_expenses_concat=%s",
        len(plan or []),
        plan_has_expenses_concatenate(plan),
    )
    sql = normalize_dbt_config_braces(sql_text or '')
    sql = preserve_hyphenated_source_names(sql, plan=plan)
    sql = enforce_global_yyyymm_dateadd_coercion(sql)
    sql = enforce_clean_historyflag_logic(sql)
    sql = enforce_expenses_yyyymm_coercion(sql)
    if _needs_fact_expenses_dynamic_rebuild(sql):
        sql = rebuild_facttable_with_expenses_union(sql, plan=plan)
    if fact_expenses_cte_has_alias_star(sql):
        sql = expand_fact_expenses_alias_star(sql)
    if plan_has_expenses_concatenate(plan) and final_model_has_bad_expenses_join(sql):
        sql = _ensure_facttable_with_expenses_cte(sql)
        sql = rebuild_facttable_with_expenses_union(sql, plan=plan)
        if fact_expenses_cte_has_alias_star(sql):
            sql = expand_fact_expenses_alias_star(sql)
        sql = rewrite_final_model_to_use_fact_expenses(sql)
    sql = repair_concat_union_from_plan(sql, plan=plan)
    sql = enforce_explicit_facttable_concatenate_schema(sql, plan=plan, qvs_script=qvs_script)
    if fact_expenses_cte_has_alias_star(sql):
        sql = expand_fact_expenses_alias_star(sql)
    if _fact_expenses_cte_name(sql) and final_model_has_bad_expenses_join(sql):
        sql = rewrite_final_model_to_use_fact_expenses(sql)
    sql = enforce_facttable_region_schema(sql)
    if not plan:
        sql = enforce_explicit_union_columns(sql)
    sql = enforce_expenses_account_join(sql)
    sql = remove_bad_expenses_monthly_join(sql)
    sql = enforce_global_yyyymm_dateadd_coercion(sql)
    sql = enforce_clean_historyflag_logic(sql)
    sql = preserve_hyphenated_source_names(sql, plan=plan)
    sql = enforce_explicit_facttable_concatenate_schema(sql, plan=plan, qvs_script=qvs_script)
    if plan:
        join_contract = build_join_contract(plan, qvs_script or '')
        sql = compose_final_model_from_contract(sql, join_contract)
    sql = enforce_final_model_wrapper(sql)
    sql = enrich_final_model_projection(sql)
    sql = deterministic_finalize_sql_structure(sql, plan=plan)
    sql = enforce_complete_final_select(sql)
    before_last = sql
    sql, forced_explicit_concat = force_replace_explicit_facttable_concat_cte(sql, plan=plan, qvs_script=qvs_script)
    union_body = _cte_body_for(sql, _fact_expenses_cte_name(sql)) if _fact_expenses_cte_name(sql) else ''
    logger.info(
        "FINALIZE_LAST_STEP_EXPLICIT_CONCAT applied=%s has_account=%s has_expenseactual=%s has_expensebudget=%s",
        forced_explicit_concat or before_last != sql or '-- EXPLICIT_CONCAT_SCHEMA_ENFORCED' in sql,
        bool(re.search(r'(?i)\baccount\b', union_body)),
        bool(re.search(r'(?i)\bexpenseactual\b', union_body)),
        bool(re.search(r'(?i)\bexpensebudget\b', union_body)),
    )
    return normalize_dbt_config_braces(sql)


def _cte_names(sql_text: str):
    return [
        match.group(1)
        for match in re.finditer(
            r'\b([A-Za-z_][A-Za-z0-9_]*)\s+AS\s*\(',
            sql_text or '',
            flags=re.IGNORECASE,
        )
    ]


def _final_select_source(sql_text: str):
    tail = _final_select_tail(sql_text)
    matches = list(re.finditer(
        r'\bSELECT\b[\s\S]*?\bFROM\s+([A-Za-z_][A-Za-z0-9_]*)\b',
        tail,
        flags=re.IGNORECASE,
    ))
    return matches[-1].group(1) if matches else ''


def _final_select_tail(sql_text: str):
    matches = list(re.finditer(r'\bSELECT\b', sql_text or '', flags=re.IGNORECASE))
    return (sql_text or '')[matches[-1].start():] if matches else ''



def _sql_identifier_variants(name: str):
    """Return normalized variants for comparing SQL/Qlik identifiers."""
    raw = str(name or '').strip().strip('"').strip('[]')
    compact = re.sub(r'[^a-z0-9]+', '', raw.lower())
    snake = re.sub(r'[^a-z0-9]+', '_', raw.lower()).strip('_')
    return {raw.lower(), compact, snake}


def resolve_cte_column_reference(alias, column_name, cte_columns) -> str:
    """Resolve alias.column to the display identifier exposed by a CTE."""
    target_variants = _sql_identifier_variants(column_name)
    if isinstance(cte_columns, dict):
        iterable = cte_columns.values()
    else:
        iterable = cte_columns or []
    for raw_col in iterable:
        if target_variants & _sql_identifier_variants(raw_col):
            return f'{alias}.{_sql_column_ref(raw_col)}'
    return f'{alias}.{_sql_column_ref(column_name)}'


def _same_identifier(left: str, right: str) -> bool:
    return bool(_sql_identifier_variants(left) & _sql_identifier_variants(right))


def _alias_relation_map(select_tail: str):
    """Map aliases used in a SELECT tail to CTE/relation names from FROM/JOIN clauses."""
    alias_map = {}
    pattern = re.compile(
        r'\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:AS\s+)?([A-Za-z_][A-Za-z0-9_]*)?',
        re.IGNORECASE,
    )
    reserved = {'on', 'where', 'left', 'right', 'full', 'inner', 'join', 'group', 'order', 'qualify'}
    for match in pattern.finditer(select_tail or ''):
        relation = match.group(1)
        alias = match.group(2) or relation
        if alias.lower() in reserved:
            alias = relation
        alias_map[alias.lower()] = relation
    return alias_map


def _validate_duplicate_aliases(sql_text: str):
    """Flag repeated aliases in the final_model/final SELECT join scope."""
    issues = []
    select_scope = _cte_body_for(sql_text, 'final_model') or _cte_body_for(sql_text, 'final_mart') or _final_select_tail(sql_text)
    pattern = re.compile(
        r'\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:AS\s+)?([A-Za-z_][A-Za-z0-9_]*)?',
        re.IGNORECASE,
    )
    reserved = {'on', 'where', 'left', 'right', 'full', 'inner', 'join', 'group', 'order', 'qualify'}
    aliases = {}
    for match in pattern.finditer(select_scope or ''):
        relation = match.group(1)
        alias = match.group(2) or relation
        if alias.lower() in reserved:
            alias = relation
        alias_key = alias.lower()
        previous = aliases.get(alias_key)
        if previous and previous.lower() != relation.lower():
            issues.append(
                f'DUPLICATE_ALIAS: alias {alias} is reused for both {previous} and {relation}. '
                'Use a unique alias for each CTE/relation.'
            )
        aliases[alias_key] = relation
    return issues


def _cte_columns_by_name(sql_text: str, names=None):
    names = names or _cte_names(sql_text)
    result = {}
    for name in names:
        cols = _select_output_columns(_cte_body_for(sql_text, name))
        result[_normalize_cte_name(name)] = cols
    return result


def _build_column_lineage_map(sql_text: str, plan=None):
    """Return cte -> columns lineage used by alias.column integrity checks."""
    lineage = {}
    names = _cte_names(sql_text)
    base_lineage = _cte_columns_by_name(sql_text, names=names)
    for name in names:
        col_map = extract_cte_output_column_map(sql_text, name, lineage_map=base_lineage)
        lineage[_normalize_cte_name(name)] = list(col_map.keys())
    for item in plan or []:
        table = _safe_cte_name(item.get('table') or '')
        if not table:
            continue
        cols = lineage.setdefault(_normalize_cte_name(table), [])
        for field in item.get('fields') or []:
            col = _normalize_column_token(_extract_output_column_name(field))
            if col and col not in {c for c in cols}:
                cols.append(col)
    return lineage


def _normalize_column_token(value: str) -> str:
    value = str(value or '')
    value = value.strip()
    if len(value) >= 2 and ((value[0] == '"' and value[-1] == '"') or (value[0] == "'" and value[-1] == "'") or (value[0] == '`' and value[-1] == '`') or (value[0] == '[' and value[-1] == ']')):
        value = value[1:-1]
    value = value.strip()
    value = re.sub(r'\s+', ' ', value)
    return value.lower()


def _normalize_cte_name(name: str) -> str:
    return re.sub(r'\s+', '', str(name or '').strip().strip('"').strip('`').strip('[]')).lower()


_ALIAS_COL_RE = re.compile(
    r'(?<![A-Za-z0-9_])'
    r'(?P<alias>[A-Za-z_][A-Za-z0-9_]*)'
    r'\s*\.\s*'
    r'(?P<col>"[^"]+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][A-Za-z0-9_]*)',
    re.IGNORECASE,
)


def iter_alias_column_refs(sql: str):
    for m in _ALIAS_COL_RE.finditer(sql or ''):
        alias = _normalize_column_token(m.group('alias'))
        col = _normalize_column_token(m.group('col'))
        yield alias, col, m.group(0)


def extract_alias_to_cte_map(select_body: str) -> dict:
    alias_map = {}
    pattern = re.compile(
        r'\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:AS\s+)?([A-Za-z_][A-Za-z0-9_]*)?',
        re.IGNORECASE,
    )
    reserved = {'on', 'where', 'left', 'right', 'full', 'inner', 'join', 'group', 'order', 'qualify'}
    for match in pattern.finditer(select_body or ''):
        relation = _normalize_cte_name(match.group(1))
        alias = _normalize_column_token(match.group(2) or match.group(1))
        if alias in reserved:
            alias = _normalize_column_token(match.group(1))
        alias_map[alias] = relation
    return alias_map


def _column_exists(columns, column_name: str) -> bool:
    norm = _normalize_column_token(column_name)
    for col in columns or []:
        c_norm = _normalize_column_token(col)
        if norm == c_norm:
            return True
    return False


def _cte_reference_counts(sql_text: str, names):
    """Count FROM/JOIN references to each CTE, excluding its own definition."""
    counts = {name.lower(): 0 for name in names or []}
    for match in re.finditer(r'\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)\b', sql_text or '', re.IGNORECASE):
        ref = match.group(1).lower()
        if ref in counts:
            counts[ref] += 1
    return counts


def _validate_alias_column_references(sql_text: str, names, plan=None):
    """Ensure alias.column references in the final SELECT/JOIN tail exist on the alias relation."""
    issues = []
    select_scope = _cte_body_for(sql_text, 'final_model') or _cte_body_for(sql_text, 'final_mart') or _final_select_tail(sql_text)
    alias_map = extract_alias_to_cte_map(select_scope)
    cte_cols = _build_column_lineage_map(sql_text, plan=plan)
    has_expenses_alias_e = bool(re.search(r'(?is)\bJOIN\s+expenses\s+(?:AS\s+)?e\b', select_scope or ''))
    if not has_expenses_alias_e and 'e' not in alias_map and re.search(r'(?is)\be\.(?:\*|"?[A-Za-z_][A-Za-z0-9_ ]*"?)', select_scope or ''):
        issues.append('SQL_CLEANUP_LEFTOVER_ALIAS_E: final_model references e.* after expenses join cleanup removed JOIN expenses e.')
    # Match alias.column safely:
    # - quoted identifiers: alias."Column Name"
    # - unquoted identifiers: alias.column_name
    # Avoid swallowing trailing "AS alias_name" into the column token.
    for alias, column, raw_ref in iter_alias_column_refs(select_scope or ''):
        relation = alias_map.get(alias)
        if not relation:
            issues.append(f'ALIAS_COLUMN_NOT_FOUND: alias {alias} is not defined in FROM/JOIN scope.')
            continue
        columns = cte_cols.get(_normalize_cte_name(relation))
        if columns is None:
            continue
        # SELECT alias.* is valid if alias exists.
        if column == '*':
            continue
        if not _column_exists(columns, column):
            issues.append(
                f'JOIN_KEY_MISSING: alias {alias} references column "{column}" '
                f'but CTE {relation} does not expose that column.'
            )
            issues.append(
                f'ALIAS_COLUMN_NOT_FOUND: alias {alias} references column "{column}" '
                f'but CTE {relation} does not expose that column.'
            )
    return issues


def remove_bad_expenses_monthly_join(sql_text: str) -> str:
    """
    If facttable_with_expenses/fact_table_with_expenses exists, remove any LEFT JOIN expenses
    that joins only by MonthlyRegionKey/monthly_region_key.
    """
    sql = sql_text or ''
    if not re.search(r'\bfact_?table_with_expenses\b', sql, re.IGNORECASE):
        return sql
    target_cte = 'final_model' if _cte_body_for(sql, 'final_model') else ('final_mart' if _cte_body_for(sql, 'final_mart') else '')
    if not target_cte:
        return sql
    body = _cte_body_for(sql, target_cte)
    if not body:
        return sql
    aliases = [
        match.group(1)
        for match in re.finditer(r'(?is)\bJOIN\s+expenses\s+(?:AS\s+)?([A-Za-z_][A-Za-z0-9_]*)\s+ON\b', body)
    ]
    for alias in aliases:
        body = remove_join_clause(body, alias)
        body = _remove_select_items_for_alias(body, alias)
        body = _cleanup_leftover_expenses_alias_refs(sql, body, alias)
    body = _cleanup_leftover_expenses_alias_refs(sql, body, 'e')
    return _replace_cte_body(sql, target_cte, body)


def _projection_items_by_output(select_sql: str) -> dict:
    select_body = _select_body(select_sql or '')
    result = {}
    for item in split_top_level_csv(re.sub(r'^\s*DISTINCT\b\s*', '', select_body, flags=re.IGNORECASE)):
        out = extract_output_alias(item)
        norm = _normalize_column_token(out)
        if norm and norm not in result:
            result[norm] = item.strip()
    return result


def _is_null_projection(expr: str) -> bool:
    return bool(re.search(r'(?is)\bCAST\s*\(\s*NULL\s+AS\s+[A-Za-z0-9_()]+\s*\)|(?<![A-Za-z0-9_])NULL(?![A-Za-z0-9_])', expr or ''))


def validate_fact_expenses_union_semantics(sql: str) -> list[str]:
    issues = []
    union_cte = _fact_expenses_cte_name(sql or '')
    union_body = _cte_body_for(sql or '', union_cte) if union_cte else ''
    if not union_body or not re.search(r'\bUNION\s+ALL\b', union_body, re.IGNORECASE):
        return issues

    branches = split_top_level_union_all(union_body)
    if len(branches) < 2:
        issues.append('INVALID_FACT_EXPENSES_UNION: facttable_with_expenses must have fact and expenses UNION ALL branches.')
        return issues

    fact_cte = 'facttable' if _cte_body_for(sql, 'facttable') else ('fact_table' if _cte_body_for(sql, 'fact_table') else '')
    expenses_cte = 'expenses' if _cte_body_for(sql, 'expenses') else ''
    if not fact_cte or not expenses_cte:
        return issues

    fact_map = extract_cte_output_column_map(sql, fact_cte, lineage_map=_cte_columns_by_name(sql))
    expense_map = extract_cte_output_column_map(sql, expenses_cte, lineage_map=_cte_columns_by_name(sql))
    fact_only = set(fact_map) - set(expense_map)
    expense_only = set(expense_map) - set(fact_map)

    fact_branch = branches[0]
    expense_branch = next((branch for branch in branches[1:] if re.search(r'\bFROM\s+expenses\b', branch, re.IGNORECASE)), branches[-1])
    fact_items = _projection_items_by_output(fact_branch)
    expense_items = _projection_items_by_output(expense_branch)

    bad_fact = sorted(col for col in expense_only if col in fact_items and not _is_null_projection(fact_items[col]))
    if bad_fact:
        issues.append(
            'INVALID_FACT_BRANCH_OWNERSHIP: fact branch must emit typed NULLs for expense-owned columns: '
            + ', '.join(bad_fact)
        )
    bad_expense = sorted(col for col in fact_only if col in expense_items and not _is_null_projection(expense_items[col]))
    if bad_expense:
        issues.append(
            'INVALID_EXPENSES_BRANCH_OWNERSHIP: expenses branch must emit typed NULLs for fact-only columns: '
            + ', '.join(bad_expense)
        )

    final_body = extract_cte_body(sql, 'final_model') or extract_cte_body(sql, 'final_mart')
    if final_body and re.search(r'(?is)\bJOIN\s+expenses\s+(?:AS\s+)?[A-Za-z_][A-Za-z0-9_]*\b', final_body):
        issues.append('LEFTOVER_EXPENSES_REJOIN: final_model must not rejoin expenses after facttable_with_expenses exists.')
    if final_body and re.search(r'(?is)\be\.(?:\*|"?[A-Za-z_][A-Za-z0-9_ ]*"?)', final_body) and not re.search(r'(?is)\bJOIN\s+expenses\s+(?:AS\s+)?e\b', final_body):
        issues.append('LEFTOVER_EXPENSES_REJOIN: final_model contains leftover expenses alias e references.')
    return issues


def validate_explicit_concatenate_field_parity(sql: str, plan, qvs_script: str = '') -> list[str]:
    """Ensure explicit Qlik CONCATENATE LOAD fields do not widen the target table schema."""
    issues = []
    explicit_fields = _explicit_concatenate_fields_for_target(plan, 'FactTable', 'expenses')
    if explicit_fields is None and _has_explicit_facttable_concatenate(plan, qvs_script=qvs_script):
        explicit_fields = _explicit_facttable_concat_fields_from_qvs(qvs_script)
    if explicit_fields is None:
        return issues
    union_cte = _fact_expenses_cte_name(sql or '')
    if not union_cte:
        return issues
    fact_cte = 'facttable' if _cte_body_for(sql, 'facttable') else ('fact_table' if _cte_body_for(sql, 'fact_table') else '')
    if not fact_cte:
        return issues
    union_cols = extract_cte_output_column_map(sql, union_cte, lineage_map=_cte_columns_by_name(sql))
    fact_cols = extract_cte_output_column_map(sql, fact_cte, lineage_map=_cte_columns_by_name(sql))
    expenses_cte = 'expenses' if _cte_body_for(sql, 'expenses') else ''
    expense_cols = extract_cte_output_column_map(sql, expenses_cte, lineage_map=_cte_columns_by_name(sql)) if expenses_cte else {}
    extra_union_cols = sorted(set(union_cols) - set(fact_cols))
    if extra_union_cols:
        issues.append(
            'CONCATENATE_FIELD_PARITY_MISMATCH: facttable_with_expenses includes non-FactTable columns not loaded by explicit Qlik CONCATENATE: '
            + ', '.join(extra_union_cols)
        )
        leaked_source_only = sorted(col for col in extra_union_cols if col in expense_cols and col not in explicit_fields)
        if leaked_source_only:
            issues.append(
                'CONCATENATE_SOURCE_ONLY_FIELD_LEAK: explicit CONCATENATE (FactTable) leaked source-only Expenses fields into facttable_with_expenses: '
                + ', '.join(leaked_source_only)
            )
    body = _cte_body_for(sql, union_cte)
    branches = split_top_level_union_all(body)
    expense_branch = next((branch for branch in branches[1:] if re.search(r'\bFROM\s+expenses\b', branch, re.IGNORECASE)), branches[-1] if len(branches) > 1 else '')
    expense_items = _projection_items_by_output(expense_branch)
    bad_direct = sorted(
        col for col, expr in expense_items.items()
        if col in union_cols and col not in explicit_fields and not _is_null_projection(expr)
    )
    if bad_direct:
        issues.append(
            'CONCATENATE_FIELD_PARITY_MISMATCH: expenses branch directly maps columns not present in explicit Qlik CONCATENATE field list: '
            + ', '.join(bad_direct)
        )
        leaked_direct = sorted(col for col in bad_direct if col in expense_cols and col not in fact_cols)
        if leaked_direct:
            issues.append(
                'CONCATENATE_SOURCE_ONLY_FIELD_LEAK: expenses branch directly maps source-only fields not present in the explicit Qlik CONCATENATE field list: '
                + ', '.join(leaked_direct)
            )
    return issues


def _plan_table_names(plan) -> set:
    names = set()
    for item in plan or []:
        for value in (item.get('table'), item.get('source'), *(item.get('source_tables') or [])):
            safe = _safe_cte_name(value or '')
            if safe:
                names.add(safe)
    return names


def validate_qlik_semantic_parity(sql: str, plan) -> list[str]:
    issues = []
    if not plan:
        return issues
    plan_names = _plan_table_names(plan)
    lower_sql = (sql or '').lower()
    final_body = (extract_cte_body(sql, 'final_model') or extract_cte_body(sql, 'final_mart') or _final_select_tail(sql)).lower()

    if 'calendar' in plan_names:
        if not ('calendar' in final_body and 'yyyymm' in final_body and re.search(r'fiscal\s*(year|quarter|month)|fiscal(year|quarter|month)', lower_sql)):
            issues.append('MISSING_CALENDAR_ENRICHMENT: Calendar exists in Qlik plan but fiscal/yyyymm enrichment is incomplete.')

    if {'itemmaster', 'productgroupmaster', 'producttypemaster'} & plan_names:
        required = ('itembranchmaster', 'itemmaster')
        product_dim_present = any(name in final_body for name in ('productgroupmaster', 'productsubgroupmaster', 'producttypemaster'))
        if not (all(name in final_body for name in required) and product_dim_present):
            issues.append('INCOMPLETE_PRODUCT_HIERARCHY: Product hierarchy exists in Qlik plan but final_model does not join item/product dimensions through the expected path.')

    if any(name.startswith('arsummary') for name in plan_names):
        if not ('arsummary' in final_body and 'custkeyar' in final_body):
            issues.append('MISSING_AR_ENRICHMENT: ARSummary exists in Qlik plan but arsummary/custkeyar enrichment is incomplete.')

    if 'customermaster' in plan_names:
        if not ('customermaster' in final_body and ('address number' in final_body or 'address_number' in final_body)):
            issues.append('MISSING_CUSTOMER_ENRICHMENT: CustomerMaster exists in Qlik plan but Address Number customer enrichment is incomplete.')

    if {'regionalsales', 'budget'} & plan_names:
        if not ('budget' in final_body and 'monthlyregionkey' in final_body):
            issues.append('MISSING_BUDGET_ENRICHMENT: Budget/RegionalSales exists in Qlik plan but MonthlyRegionKey budget enrichment is incomplete.')
    return issues


def validate_execution_safety(sql: str) -> list[str]:
    issues = []
    content = sql or ''
    if content.count('(') != content.count(')'):
        issues.append('UNBALANCED_PARENS: parentheses look unbalanced.')
    names = [_normalize_cte_name(name) for name in _cte_names(content)]
    duplicates = sorted(name for name in set(names) if names.count(name) > 1)
    if duplicates:
        issues.append(f'DUPLICATE_CTE_NAME: duplicate CTE definitions detected: {", ".join(duplicates)}.')
    if re.search(r'(?is)\bSELECT\s+(?:[A-Za-z_][A-Za-z0-9_]*\.)?\*', content):
        issues.append('SELECT_STAR_EXECUTION_RISK: generated SQL should enumerate projections explicitly before execution.')
    if re.search(r'(?is)\bJOIN\s+[A-Za-z_][A-Za-z0-9_]*(?:\s+(?:AS\s+)?[A-Za-z_][A-Za-z0-9_]*)?(?=\s*(?:LEFT|RIGHT|INNER|FULL|OUTER)?\s*JOIN\b|\s+WHERE\b|\s+GROUP\s+BY\b|\s+ORDER\s+BY\b|\s*$)', content):
        issues.append('JOIN_WITHOUT_ON: JOIN appears without an ON clause.')
    if re.search(r'(?is)\bUNION\s+ALL\s*(?:\)|$)', content):
        issues.append('MALFORMED_UNION: UNION ALL is missing a following SELECT branch.')
    if re.search(r'(?i)AccountLEFT\s+JOIN', content):
        issues.append('SQL_CLEANUP_CORRUPTION: AccountLEFT JOIN corruption detected.')
    if re.search(r'(?im)^\s*AND\s+[A-Za-z_][A-Za-z0-9_]*\.[^\n]+JOIN\b', content):
        issues.append('SQL_CLEANUP_CORRUPTION: dangling AND predicate appears before JOIN.')
    issues.extend(_validate_alias_column_references(content, _cte_names(content)))
    return list(dict.fromkeys(issues))


def _score_add(passed, label, score, points):
    passed.append(label)
    return score + points


def _score_warn(warnings, label, score, penalty):
    warnings.append(label)
    return max(0, score - penalty)


def _score_fail(failures, label, score, penalty):
    failures.append(label)
    return max(0, score - penalty)


def score_generated_sql_quality(sql, plan=None) -> dict:
    """Score finalized SQL quality and completeness on a 0-100 scale."""
    text = sql or ''
    plan = plan or []
    passed = []
    warnings = []
    failures = []
    score = 0

    cte_names = {_normalize_cte_name(name) for name in _cte_names(text)}
    final_body = extract_cte_body(text, 'final_model') or extract_cte_body(text, 'final_mart')
    final_lower = final_body.lower()
    union_name = _fact_expenses_cte_name(text)
    union_body = _cte_body_for(text, union_name) if union_name else ''

    if final_body:
        score = _score_add(passed, 'final_model exists', score, 12)
    else:
        score = _score_fail(failures, 'final_model missing', score, 18)

    if plan_has_expenses_concatenate(plan):
        if union_body:
            score = _score_add(passed, 'facttable_with_expenses exists for expenses concatenate', score, 12)
        else:
            score = _score_fail(failures, 'facttable_with_expenses missing for expenses concatenate', score, 18)
    elif union_body:
        score = _score_add(passed, 'facttable_with_expenses exists', score, 8)

    if re.search(r'(?is)\bJOIN\s+expenses\b', final_body):
        score = _score_fail(failures, 'direct expenses join remains in final_model', score, 15)
    else:
        score = _score_add(passed, 'no direct expenses join in final_model', score, 10)

    if union_body:
        if re.search(r'(?is)\bSELECT\s+(?:[A-Za-z_][A-Za-z0-9_]*\.)?\*', union_body):
            score = _score_fail(failures, 'SELECT * appears inside fact/expenses union', score, 15)
        else:
            score = _score_add(passed, 'fact/expenses union uses explicit columns', score, 10)
        counts = _union_branch_column_counts(union_body)
        if counts and len(set(counts)) == 1:
            score = _score_add(passed, 'fact/expenses union branches aligned', score, 10)
        else:
            score = _score_fail(failures, f'fact/expenses union branches not aligned: {counts}', score, 15)

    alias_issues = _validate_alias_column_references(text, _cte_names(text), plan=plan)
    if alias_issues:
        score = _score_fail(failures, 'undefined alias or alias column references found', score, 15)
    else:
        score = _score_add(passed, 'no undefined alias references', score, 10)

    core_join_relations = {
        'customermap': 'customer map joined',
        'customermaster': 'customer master joined',
        'arsummary': 'AR summary joined',
    }
    for relation, label in core_join_relations.items():
        if relation in cte_names:
            if re.search(rf'(?is)\bJOIN\s+{re.escape(relation)}\b', final_body):
                score = _score_add(passed, label, score, 5)
            else:
                score = _score_warn(warnings, f'missing {label}', score, 3)

    optional_join_relations = {
        'historyflag': 'historyflag joined if present',
        'budget': 'budget joined if present',
        'calendar': 'calendar joined if present',
        'salesrepmaster': 'sales rep master joined if present',
        'itembranchmaster': 'item branch master joined if present',
        'itemmaster': 'item master joined if present',
        'productgroupmaster': 'product group master joined if present',
        'productsubgroupmaster': 'product subgroup master joined if present',
        'producttypemaster': 'product type master joined if present',
    }
    for relation, label in optional_join_relations.items():
        if relation in cte_names:
            if re.search(rf'(?is)\bJOIN\s+{re.escape(relation)}\b', final_body):
                score = _score_add(passed, label, score, 3)
            else:
                score = _score_warn(warnings, f'missing {label}', score, 2)

    semantic_issues = validate_fact_expenses_union_semantics(text)
    if semantic_issues:
        score = _score_fail(failures, 'fact/expenses union semantic issues: ' + '; '.join(semantic_issues), score, 12)
    else:
        score = _score_add(passed, 'fact/expenses union semantic checks passed', score, 8)

    return {
        'score': max(0, min(100, score)),
        'passed': list(dict.fromkeys(passed)),
        'warnings': list(dict.fromkeys(warnings)),
        'failures': list(dict.fromkeys(failures)),
    }


def _plan_item_for_table(plan, table_name: str):
    target = _normalize_cte_name(table_name)
    for item in plan or []:
        if _normalize_cte_name(item.get('table') or '') == target:
            return item
    return None


def _plan_source_relation(plan, table_name: str) -> str:
    item = _plan_item_for_table(plan, table_name)
    if not item:
        return table_name
    source = item.get('source') or ''
    if not source and item.get('source_tables'):
        source = (item.get('source_tables') or [''])[0]
    source_name = _resolve_source_reference(source or table_name)
    return "{{ source('raw', '" + source_name + "') }}"


def _validation_check(check_id: str, check_type: str, description: str, sql: str, severity: str = 'warning') -> dict:
    return {
        'id': check_id,
        'type': check_type,
        'description': description,
        'severity': severity,
        'status': 'not_run',
        'sql': sql.strip(),
    }


def _sum_check_sql(model_name: str, field_name: str, source_relation: str = '') -> str:
    field_ref = _sql_column_ref(field_name)
    migrated = (
        f"SELECT '{field_name}' AS metric_name, SUM({field_ref}) AS metric_total\n"
        f"FROM {{{{ ref('{model_name}') }}}}"
    )
    if not source_relation:
        return migrated
    return (
        "WITH source_total AS (\n"
        f"    SELECT SUM({field_ref}) AS total_value FROM {source_relation}\n"
        "), migrated_total AS (\n"
        f"    SELECT SUM({field_ref}) AS total_value FROM {{{{ ref('{model_name}') }}}}\n"
        ")\n"
        f"SELECT '{field_name}' AS metric_name,\n"
        "       source_total.total_value AS source_total,\n"
        "       migrated_total.total_value AS migrated_total,\n"
        "       migrated_total.total_value - source_total.total_value AS variance\n"
        "FROM source_total CROSS JOIN migrated_total"
    )


def _breakdown_check_sql(model_name: str, dimension_name: str) -> str:
    dim_ref = _sql_column_ref(dimension_name)
    return (
        f"SELECT {dim_ref} AS dimension_value,\n"
        "       COUNT(*) AS row_count,\n"
        "       SUM(COALESCE(\"Sales Amount\", 0)) AS sales_amount,\n"
        "       SUM(COALESCE(ExpenseActual, 0)) AS expenseactual,\n"
        "       SUM(COALESCE(ExpenseBudget, 0)) AS expensebudget\n"
        f"FROM {{{{ ref('{model_name}') }}}}\n"
        f"GROUP BY {dim_ref}\n"
        f"ORDER BY {dim_ref}"
    )


def build_migration_validation_report(sql: str, plan=None, dialect: str = 'dbt', model_name: str = 'migration_output') -> dict:
    """Build dbt compile and parity validation SQL without executing it."""
    plan = plan or []
    checks = []
    fact_relation = _plan_source_relation(plan, 'FactTable')
    expenses_relation = _plan_source_relation(plan, 'Expenses')

    checks.append(_validation_check(
        'facttable_count',
        'row_count',
        'Count rows in the Qlik FactTable source.',
        f"SELECT COUNT(*) AS facttable_count\nFROM {fact_relation}",
    ))
    checks.append(_validation_check(
        'expenses_count',
        'row_count',
        'Count rows in the Qlik Expenses source.',
        f"SELECT COUNT(*) AS expenses_count\nFROM {expenses_relation}",
    ))
    checks.append(_validation_check(
        'facttable_with_expenses_count',
        'row_count',
        'Validate migrated facttable_with_expenses row count equals FactTable plus Expenses.',
        (
            "WITH source_counts AS (\n"
            f"    SELECT COUNT(*) AS facttable_count FROM {fact_relation}\n"
            "), expense_counts AS (\n"
            f"    SELECT COUNT(*) AS expenses_count FROM {expenses_relation}\n"
            "), migrated_counts AS (\n"
            f"    SELECT COUNT(*) AS migrated_count FROM {{{{ ref('{model_name}') }}}}\n"
            ")\n"
            "SELECT source_counts.facttable_count,\n"
            "       expense_counts.expenses_count,\n"
            "       migrated_counts.migrated_count,\n"
            "       migrated_counts.migrated_count - (source_counts.facttable_count + expense_counts.expenses_count) AS variance\n"
            "FROM source_counts CROSS JOIN expense_counts CROSS JOIN migrated_counts"
        ),
    ))

    metric_sources = {
        'Sales Amount': fact_relation,
        'Sales Cost Amount': fact_relation,
        'Sales Margin Amount': fact_relation,
        'ExpenseActual': expenses_relation,
        'ExpenseBudget': expenses_relation,
        'Budget Amount': _plan_source_relation(plan, 'Budget'),
    }
    for metric_name, source_relation in metric_sources.items():
        checks.append(_validation_check(
            'sum_' + re.sub(r'[^a-z0-9]+', '_', metric_name.lower()).strip('_'),
            'metric_total',
            f'Compare total {metric_name} between Qlik source and migrated dbt model.',
            _sum_check_sql(model_name, metric_name, source_relation),
        ))

    for dimension_name in ['Region', 'YYYYMM', 'Customer Number', 'Sales Rep', 'Product Group']:
        checks.append(_validation_check(
            'breakdown_by_' + re.sub(r'[^a-z0-9]+', '_', dimension_name.lower()).strip('_'),
            'dimension_breakdown',
            f'Validate migrated measures by {dimension_name}.',
            _breakdown_check_sql(model_name, dimension_name),
        ))

    return {
        'version': '2026-05-27.validation.v1',
        'status': 'not_run',
        'dialect': dialect or 'dbt',
        'modelName': model_name,
        'dbtCompile': {
            'status': 'not_run',
            'command': f'dbt compile --select {model_name}',
            'description': 'Run after packaging the generated model in a dbt project to verify SQL compilation.',
        },
        'checks': checks,
        'summary': {
            'totalChecks': len(checks),
            'rowCountChecks': len([c for c in checks if c['type'] == 'row_count']),
            'metricTotalChecks': len([c for c in checks if c['type'] == 'metric_total']),
            'dimensionBreakdownChecks': len([c for c in checks if c['type'] == 'dimension_breakdown']),
        },
    }


def _strip_trailing_semicolon(sql: str) -> str:
    return re.sub(r';+\s*$', '', str(sql or '').strip())


def _artifact_column_names(sql: str) -> list[str]:
    columns = extract_cte_output_columns(sql or '', 'final_model') or extract_cte_output_columns(sql or '', 'final_mart')
    if columns:
        return columns
    tail = _final_select_tail(sql or '')
    if tail:
        return extract_projection_display_names(tail)
    return []


def _artifact_has_column(columns, name: str) -> bool:
    target = _normalize_column_token(name)
    return any(_normalize_column_token(column) == target for column in columns or [])


def _artifact_model_ref(model_name: str) -> str:
    safe_name = re.sub(r'[^A-Za-z0-9_]+', '_', str(model_name or 'executive_dashboard')).strip('_') or 'executive_dashboard'
    return "{{ ref('" + safe_name + "') }}"


def _artifact_noop_test(reason: str) -> str:
    escaped = str(reason or 'validation skipped').replace("'", "''")
    return f"SELECT '{escaped}' AS skipped_reason\nWHERE 1 = 0"


def sanitize_test_sql_projection(sql: str) -> str:
    """Keep dbt singular tests explicit: no SELECT * failure projections."""
    text = str(sql or '').strip()
    text = re.sub(
        r'(?is)\bSELECT\s+\*\s+FROM\s*\(\s*(?P<inner>.*?)\s*\)\s+(?P<alias>[A-Za-z_][A-Za-z0-9_]*)\s+WHERE\s+COALESCE\s*\(\s*variance\s*,\s*0\s*\)\s*!=\s*0\s*$',
        (
            "SELECT\n"
            "    migrated_count,\n"
            "    facttable_count + expenses_count AS expected_count,\n"
            "    variance AS difference\n"
            "FROM (\n"
            "\\g<inner>\n"
            ") \\g<alias>\n"
            "WHERE COALESCE(variance, 0) != 0"
        ),
        text,
    )
    text = re.sub(
        r'(?is)\bSELECT\s+\*\s+FROM\s+keyed\s+WHERE\s+row_count\s*>\s*1\s*$',
        (
            "SELECT\n"
            "    1 AS validation_failure,\n"
            "    row_count\n"
            "FROM keyed\n"
            "WHERE row_count > 1"
        ),
        text,
    )
    return text


def _artifact_not_null_test(model_name: str, column: str, test_name: str, predicate: str = '') -> str:
    col_ref = _sql_column_ref(column)
    where = f"{col_ref} IS NULL"
    if predicate:
        where = f"({predicate}) AND {where}"
    return (
        f"SELECT '{test_name}' AS validation_name, COUNT(*) AS failing_rows\n"
        f"FROM {_artifact_model_ref(model_name)}\n"
        f"WHERE {where}\n"
        "HAVING COUNT(*) > 0"
    )


def _artifact_metric_totals_sql(model_name: str, columns) -> str:
    metrics = [
        'Sales Amount',
        'Sales Cost Amount',
        'Sales Margin Amount',
        'ExpenseActual',
        'ExpenseBudget',
        'Budget Amount',
        'AROpen',
        'ARGross',
    ]
    selects = []
    for metric in metrics:
        if _artifact_has_column(columns, metric):
            metric_ref = _sql_column_ref(metric)
            selects.append(f"SELECT '{metric}' AS metric_name, SUM({metric_ref}) AS metric_total FROM {_artifact_model_ref(model_name)}")
    return '\nUNION ALL\n'.join(selects) if selects else _artifact_noop_test('No metric columns detected for totals analysis')


def _artifact_breakdown_sql(model_name: str, columns, dimension: str) -> str:
    if not _artifact_has_column(columns, dimension):
        return _artifact_noop_test(f'{dimension} column not detected for breakdown analysis')
    dim_ref = _sql_column_ref(dimension)
    measure_exprs = ["COUNT(*) AS row_count"]
    for metric in ['Sales Amount', 'ExpenseActual', 'ExpenseBudget', 'Budget Amount', 'AROpen', 'ARGross']:
        if _artifact_has_column(columns, metric):
            metric_ref = _sql_column_ref(metric)
            alias = re.sub(r'[^a-z0-9]+', '_', metric.lower()).strip('_')
            measure_exprs.append(f"SUM(COALESCE({metric_ref}, 0)) AS {alias}")
    return (
        f"SELECT {dim_ref} AS dimension_value,\n"
        "       " + ',\n       '.join(measure_exprs) + "\n"
        f"FROM {_artifact_model_ref(model_name)}\n"
        f"GROUP BY {dim_ref}\n"
        f"ORDER BY {dim_ref}"
    )


def _validation_report_check_sql(validation_report: dict, check_id: str) -> str:
    for check in (validation_report or {}).get('checks') or []:
        if check.get('id') == check_id:
            return _strip_trailing_semicolon(check.get('sql') or '')
    return ''


def _source_names_for_artifacts(sql: str) -> list[str]:
    names = []
    seen = set()
    for match in re.finditer(
        r"\{\{\s*source\s*\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}",
        sql or '',
        flags=re.IGNORECASE,
    ):
        source_name = match.group(2)
        key = source_name.lower()
        if key not in seen:
            seen.add(key)
            names.append(source_name)
    return names


def _schema_yml_for_validation_artifacts(sql: str, model_name: str, columns) -> str:
    sources = sorted(_source_names_for_artifacts(sql), key=lambda value: str(value).lower())
    lines = ["version: 2", "", "sources:", "  - name: raw", "    tables:"]
    if sources:
        for source in sources:
            lines.append(f"      - name: {source}")
    else:
        lines.append("      - name: source_table")
    lines.extend(["", "models:", f"  - name: {model_name}", "    columns:"])
    if columns:
        for column in sorted(columns, key=lambda value: _normalize_column_token(value)):
            col_name = str(column or '').strip().strip('"')
            if not col_name:
                continue
            lines.append(f"      - name: {col_name}")
            tests = []
            if _normalize_column_token(col_name) in {'monthlyregionkey', 'yyyymm', 'custkey', 'account'}:
                tests.append('not_null')
            if _normalize_column_token(col_name) == '_historyflag':
                tests.append('accepted_values')
            if tests:
                lines.append("        data_tests:")
                for test in tests:
                    if test == 'accepted_values':
                        lines.append("          - accepted_values:")
                        lines.append("              values: [0, 1]")
                    else:
                        lines.append(f"          - {test}")
    else:
        lines.append("      - name: generated_row")
    return '\n'.join(lines)


def generate_validation_artifacts(sql: str, validation_report: dict, model_name='executive_dashboard') -> dict:
    """Generate dbt model, singular tests, analyses, and schema YAML for SQL validation."""
    safe_model = re.sub(r'[^A-Za-z0-9_]+', '_', str(model_name or 'executive_dashboard')).strip('_') or 'executive_dashboard'
    sql_text = _strip_trailing_semicolon(sql or '')
    columns = sorted(_artifact_column_names(sql_text), key=lambda value: _normalize_column_token(value))
    lower_sql = (sql_text or '').lower()

    tests = {}
    row_count_sql = _validation_report_check_sql(validation_report, 'facttable_with_expenses_count')
    if row_count_sql:
        tests['assert_fact_expenses_row_count.sql'] = (
            "SELECT\n"
            "    migrated_count,\n"
            "    facttable_count + expenses_count AS expected_count,\n"
            "    variance AS difference\n"
            "FROM (\n"
            f"{row_count_sql}\n"
            ") counts\n"
            "WHERE COALESCE(variance, 0) != 0"
        )
    else:
        tests['assert_fact_expenses_row_count.sql'] = _artifact_noop_test('Row count validation SQL was not generated')

    fact_predicate = ''
    expense_predicate = ''
    if _artifact_has_column(columns, 'ExpenseActual'):
        fact_predicate = 'ExpenseActual IS NULL'
        expense_predicate = 'ExpenseActual IS NOT NULL'
    elif _artifact_has_column(columns, 'ExpenseBudget'):
        fact_predicate = 'ExpenseBudget IS NULL'
        expense_predicate = 'ExpenseBudget IS NOT NULL'

    tests['assert_no_null_monthlyregionkey.sql'] = (
        _artifact_not_null_test(safe_model, 'MonthlyRegionKey', 'assert_no_null_monthlyregionkey', fact_predicate)
        if _artifact_has_column(columns, 'MonthlyRegionKey')
        else _artifact_noop_test('MonthlyRegionKey column not detected')
    )
    tests['assert_no_null_yyyymm.sql'] = (
        _artifact_not_null_test(safe_model, 'YYYYMM', 'assert_no_null_yyyymm', fact_predicate)
        if _artifact_has_column(columns, 'YYYYMM')
        else _artifact_noop_test('YYYYMM column not detected')
    )
    tests['assert_sales_amount_not_null.sql'] = (
        _artifact_not_null_test(safe_model, 'Sales Amount', 'assert_sales_amount_not_null', fact_predicate)
        if _artifact_has_column(columns, 'Sales Amount')
        else _artifact_noop_test('Sales Amount column not detected')
    )
    tests['assert_expenseactual_not_null.sql'] = (
        _artifact_not_null_test(safe_model, 'ExpenseActual', 'assert_expenseactual_not_null', expense_predicate)
        if _artifact_has_column(columns, 'ExpenseActual')
        else _artifact_noop_test('ExpenseActual column not detected')
    )
    key_candidates = ['MonthlyRegionKey', 'CustKey', 'YYYYMM', 'Account']
    available_keys = [column for column in key_candidates if _artifact_has_column(columns, column)]
    if len(available_keys) >= 2:
        partition = ', '.join(_sql_column_ref(column) for column in available_keys)
        tests['assert_no_duplicate_impossible_key.sql'] = (
            "WITH keyed AS (\n"
            f"    SELECT {partition}, COUNT(*) AS row_count\n"
            f"    FROM {_artifact_model_ref(safe_model)}\n"
            f"    GROUP BY {partition}\n"
            ")\n"
            "SELECT\n"
            "    1 AS validation_failure,\n"
            "    row_count\n"
            "FROM keyed\n"
            "WHERE row_count > 1"
        )
    else:
        tests['assert_no_duplicate_impossible_key.sql'] = _artifact_noop_test('Not enough key columns detected for duplicate-key assertion')
    tests['assert_no_dangling_null_region.sql'] = (
        _artifact_not_null_test(safe_model, 'Region', 'assert_no_dangling_null_region', fact_predicate)
        if _artifact_has_column(columns, 'Region')
        else _artifact_noop_test('Region column not detected')
    )
    tests['assert_no_direct_expenses_rejoin.sql'] = (
        "SELECT 'direct expenses rejoin detected in generated SQL' AS validation_error\nWHERE 1 = 1"
        if re.search(r'(?is)\bJOIN\s+expenses\b', sql_text)
        else "SELECT 'no direct expenses rejoin detected' AS validation_name\nWHERE 1 = 0"
    )

    analyses = {
        'metric_totals.sql': _artifact_metric_totals_sql(safe_model, columns),
        'region_breakdown.sql': _artifact_breakdown_sql(safe_model, columns, 'Region'),
        'month_breakdown.sql': _artifact_breakdown_sql(safe_model, columns, 'YYYYMM'),
        'customer_breakdown.sql': _artifact_breakdown_sql(safe_model, columns, 'Customer Number'),
        'sales_rep_breakdown.sql': _artifact_breakdown_sql(safe_model, columns, 'Sales Rep'),
        'product_breakdown.sql': _artifact_breakdown_sql(safe_model, columns, 'Product Group'),
    }

    tests = {name: _strip_trailing_semicolon(sanitize_test_sql_projection(value)) for name, value in tests.items()}
    analyses = {name: _strip_trailing_semicolon(value) for name, value in analyses.items()}
    tests = dict(sorted(tests.items()))
    analyses = dict(sorted(analyses.items()))
    return {
        'models': {
            f'{safe_model}.sql': sql_text,
        },
        'tests': tests,
        'analyses': analyses,
        'schema_yml': _schema_yml_for_validation_artifacts(sql_text, safe_model, columns),
    }


def generate_dbt_project_scaffold(project_name='qlik_migration_validation') -> dict:
    """Return minimal dbt project files for exported validation artifacts."""
    safe_project = re.sub(r'[^A-Za-z0-9_]+', '_', str(project_name or 'qlik_migration_validation')).strip('_')
    safe_project = safe_project or 'qlik_migration_validation'
    dbt_project = (
        f"name: {safe_project}\n"
        "version: '1.0.0'\n"
        "config-version: 2\n"
        f"profile: {safe_project}\n"
        "\n"
        'model-paths: ["models"]\n'
        'test-paths: ["tests"]\n'
        'analysis-paths: ["analyses"]\n'
        'target-path: "target"\n'
        'clean-targets: ["target", "dbt_packages"]\n'
        "\n"
        "models:\n"
        f"  {safe_project}:\n"
        "    +materialized: table\n"
    )
    readme = (
        f"# {safe_project}\n\n"
        "This folder contains generated dbt validation artifacts for a Qlik migration.\n\n"
        "You can copy this folder into an existing dbt project, or run it as a standalone dbt project after configuring a warehouse connection.\n\n"
        "Before running, configure `profiles.yml` locally or set up the equivalent dbt Cloud connection for this project/profile.\n\n"
        "Recommended validation flow:\n\n"
        "```bash\n"
        "dbt compile\n"
        "dbt run --select executive_dashboard\n"
        "dbt test\n"
        "dbt compile --select analyses\n"
        "```\n\n"
        "The singular tests return failing rows. A test passes when it returns zero rows.\n"
    )
    return {
        'dbt_project.yml': dbt_project,
        'packages.yml': '',
        'README.md': readme,
        '.gitignore': "target/\ndbt_packages/\nlogs/\n.env\n",
    }


def _validation_artifacts_root() -> str:
    return os.path.abspath(ARTIFACT_FOLDER)


def _sanitize_artifact_filename(name: str, default_name: str = 'artifact.sql') -> str:
    basename = os.path.basename(str(name or '').replace('\\', '/'))
    if basename == '.gitignore':
        return basename
    basename = re.sub(r'[^A-Za-z0-9_.-]+', '_', basename).strip('._')
    return basename or default_name


def _resolve_validation_output_dir(output_dir: str) -> str:
    root = _validation_artifacts_root()
    raw = str(output_dir or '').strip()
    if not raw:
        raw = 'validation_export'
    if os.path.isabs(raw):
        resolved = os.path.abspath(raw)
    else:
        resolved = os.path.abspath(os.path.join(root, raw))
    if os.path.commonpath([root, resolved]) != root:
        raise ValueError('output_dir must be inside generated_artifacts')
    return resolved


def _write_validation_artifact_file(base_dir: str, relative_dir: str, filename: str, content, files_written: list, errors: list):
    safe_name = _sanitize_artifact_filename(filename)
    target_dir = os.path.join(base_dir, relative_dir)
    os.makedirs(target_dir, exist_ok=True)
    target_path = os.path.abspath(os.path.join(target_dir, safe_name))
    if os.path.commonpath([base_dir, target_path]) != base_dir:
        errors.append(f'Blocked unsafe artifact path: {filename}')
        return
    try:
        with open(target_path, 'w', encoding='utf-8') as handle:
            handle.write(str(content or '').rstrip() + '\n')
        files_written.append(os.path.relpath(target_path, base_dir))
    except Exception as exc:
        errors.append(f'Failed to write {safe_name}: {exc}')


def generate_export_manifest(validation_artifacts, export_result, metadata=None) -> dict:
    """Build a self-describing manifest for exported dbt validation artifacts."""
    artifacts = validation_artifacts or {}
    result = export_result or {}
    metadata = metadata or {}
    model_files = list((artifacts.get('models') or {}).keys())
    model_name = metadata.get('modelName') or metadata.get('model_name')
    if not model_name and model_files:
        model_name = re.sub(r'\.sql$', '', os.path.basename(model_files[0]), flags=re.IGNORECASE)
    model_name = model_name or 'executive_dashboard'
    project_name = metadata.get('projectName') or metadata.get('project_name') or 'qlik_migration_validation'
    warnings = metadata.get('warnings') or metadata.get('validationIssues') or metadata.get('validation_issues') or []
    quality = {
        'sqlQualityScore': metadata.get('sqlQualityScore', metadata.get('sql_quality_score')),
        'status': metadata.get('status'),
        'warningsCount': metadata.get('warningsCount', metadata.get('warnings_count', len(warnings or []))),
    }
    quality = {key: value for key, value in quality.items() if value is not None}
    files = list(result.get('files_written') or result.get('filesWritten') or [])
    return {
        'exportedAt': datetime.utcnow().replace(microsecond=0).isoformat() + 'Z',
        'projectName': project_name,
        'modelName': model_name,
        'artifactCounts': {
            'models': len(artifacts.get('models') or {}),
            'tests': len(artifacts.get('tests') or {}),
            'analyses': len(artifacts.get('analyses') or {}),
        },
        'files': files,
        'hasSchemaYml': bool(artifacts.get('schema_yml')),
        'hasDbtProject': any(str(file_name).endswith('dbt_project.yml') for file_name in files),
        'quality': quality,
        'instructions': [
            'Configure dbt profile or dbt Cloud connection',
            'Run dbt compile',
            'Run dbt run --select executive_dashboard',
            'Run dbt test',
            'Review analyses SQL outputs',
        ],
    }


def _summary_list(items, empty_text='None'):
    items = list(items or [])
    if not items:
        return f"- {empty_text}"
    return '\n'.join(f"- {item}" for item in items)


def generate_export_summary_report(
    validation_artifacts: dict,
    manifest: dict,
    dry_run_result=None,
    quality=None,
) -> str:
    """Generate a human-readable summary for a validation artifact export."""
    artifacts = validation_artifacts or {}
    manifest = manifest or {}
    quality = quality or manifest.get('quality') or {}
    dry_run_status = (dry_run_result or {}).get('status') if dry_run_result else 'not run'
    models = sorted((artifacts.get('models') or {}).keys())
    tests = sorted((artifacts.get('tests') or {}).keys())
    analyses = sorted((artifacts.get('analyses') or {}).keys())
    score = quality.get('score', quality.get('sqlQualityScore'))
    passed = quality.get('passed') or []
    warnings = quality.get('warnings') or []
    failures = quality.get('failures') or []
    if quality.get('warningsCount') is not None and not warnings:
        warnings = [f"{quality.get('warningsCount')} warning(s) reported"]
    lines = [
        '# Qlik Migration Validation Package',
        '',
        '## Package Overview',
        f"- Model name: {manifest.get('modelName', 'executive_dashboard')}",
        f"- Exported timestamp: {manifest.get('exportedAt', 'unknown')}",
        f"- Models: {(manifest.get('artifactCounts') or {}).get('models', len(models))}",
        f"- Tests: {(manifest.get('artifactCounts') or {}).get('tests', len(tests))}",
        f"- Analyses: {(manifest.get('artifactCounts') or {}).get('analyses', len(analyses))}",
        f"- Dry-run status: {dry_run_status}",
        '',
        '## SQL Quality Summary',
        f"- Score: {score if score is not None else 'not available'}",
        f"- Status: {quality.get('status', 'not available')}",
        '',
        'Passed checks:',
        _summary_list(passed),
        '',
        'Warnings:',
        _summary_list(warnings),
        '',
        'Failures:',
        _summary_list(failures),
        '',
        '## Validation Assets',
        '',
        'Model SQL files:',
        _summary_list(models),
        '',
        'Singular dbt tests:',
        _summary_list(tests),
        '',
        'Analyses SQL files:',
        _summary_list(analyses),
        '',
        '## What This Package Validates',
        '- Row count validation between source fact/expenses data and migrated output',
        '- Null and key checks for required migrated fields',
        '- Fact/expense concat structure through `facttable_with_expenses` validation',
        '- Metric totals for sales, expense, budget, and AR fields when present',
        '- Breakdown analyses by region, month, customer, sales rep, and product group',
        '',
        '## What Still Requires Execution',
        '- Run `dbt compile` to validate dbt/Jinja compilation',
        '- Run `dbt run` to materialize the generated model',
        '- Run `dbt test` to execute singular validation tests',
        '- Compare analysis outputs to Qlik baseline extracts',
        '- Confirm warehouse raw source availability and source mappings',
        '',
        '## Recommended Commands',
        '',
        '```bash',
        'dbt compile',
        'dbt run --select executive_dashboard',
        'dbt test',
        'dbt compile --select analyses',
        '```',
        '',
        '## Known Limitations',
        '- Qlik associative behavior is not fully proven until execution comparison',
        '- Date fields can remain ambiguous when source types differ from Qlik dual values',
        '- Source type differences between QVD/Qlik and warehouse tables may affect parity',
        '- Metric parity requires a real Qlik baseline for row counts, totals, and grouped outputs',
    ]
    return '\n'.join(lines).rstrip() + '\n'


def export_validation_artifacts(validation_artifacts: dict, output_dir: str, include_project_scaffold: bool = True, metadata=None) -> dict:
    """Write validation artifacts to a dbt-compatible folder under generated_artifacts."""
    files_written = []
    errors = []
    try:
        resolved_dir = _resolve_validation_output_dir(output_dir)
    except Exception as exc:
        return {
            'output_dir': '',
            'files_written': [],
            'errors': [str(exc)],
        }

    artifacts = validation_artifacts or {}
    os.makedirs(resolved_dir, exist_ok=True)
    if include_project_scaffold:
        for filename, content in generate_dbt_project_scaffold().items():
            if filename == 'packages.yml' and not str(content or '').strip():
                continue
            _write_validation_artifact_file(resolved_dir, '', filename, content, files_written, errors)
    for filename, content in (artifacts.get('models') or {}).items():
        _write_validation_artifact_file(resolved_dir, 'models', filename, content, files_written, errors)
    if 'schema_yml' in artifacts:
        _write_validation_artifact_file(resolved_dir, 'models', 'schema.yml', artifacts.get('schema_yml'), files_written, errors)
    for filename, content in (artifacts.get('tests') or {}).items():
        _write_validation_artifact_file(resolved_dir, 'tests', filename, content, files_written, errors)
    for filename, content in (artifacts.get('analyses') or {}).items():
        _write_validation_artifact_file(resolved_dir, 'analyses', filename, content, files_written, errors)
    export_result = {
        'output_dir': resolved_dir,
        'files_written': files_written,
        'errors': errors,
    }
    provisional_manifest = generate_export_manifest(artifacts, export_result, metadata=metadata)
    summary_report = generate_export_summary_report(
        artifacts,
        provisional_manifest,
        dry_run_result=None,
        quality=(metadata or {}).get('quality') if isinstance(metadata, dict) else None,
    )
    _write_validation_artifact_file(
        resolved_dir,
        '',
        'summary_report.md',
        summary_report,
        files_written,
        errors,
    )
    export_result['files_written'] = files_written
    manifest = generate_export_manifest(artifacts, export_result, metadata=metadata)
    _write_validation_artifact_file(
        resolved_dir,
        '',
        'manifest.json',
        json.dumps(manifest, indent=2, sort_keys=True),
        files_written,
        errors,
    )
    export_result['files_written'] = files_written
    export_result['errors'] = errors
    export_result['manifest_path'] = os.path.join(resolved_dir, 'manifest.json')
    return export_result


def zip_exported_artifacts(output_dir: str) -> dict:
    """Create a zip archive for an exported validation artifact folder."""
    root = _validation_artifacts_root()
    resolved_dir = os.path.abspath(str(output_dir or '').strip())
    if os.path.commonpath([root, resolved_dir]) != root:
        raise ValueError('output_dir must be inside generated_artifacts')
    if not os.path.isdir(resolved_dir):
        raise ValueError('output_dir does not exist')

    zip_file_name = _sanitize_artifact_filename(os.path.basename(resolved_dir) + '.zip', 'validation_artifacts.zip')
    zip_path = os.path.abspath(os.path.join(root, zip_file_name))
    if os.path.commonpath([root, zip_path]) != root:
        raise ValueError('zip path must be inside generated_artifacts')

    excluded_dirs = {'__pycache__', 'target', 'logs', 'dbt_packages'}
    excluded_files = {'.DS_Store'}
    files_zipped = []
    with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for current_dir, dirnames, filenames in os.walk(resolved_dir):
            dirnames[:] = [dirname for dirname in dirnames if dirname not in excluded_dirs]
            for filename in filenames:
                if filename in excluded_files:
                    continue
                abs_path = os.path.abspath(os.path.join(current_dir, filename))
                if os.path.commonpath([resolved_dir, abs_path]) != resolved_dir:
                    continue
                rel_path = os.path.relpath(abs_path, resolved_dir)
                if rel_path.startswith('..') or os.path.isabs(rel_path):
                    continue
                archive.write(abs_path, rel_path)
                files_zipped.append(rel_path)

    return {
        'zipPath': zip_path,
        'zipFileName': zip_file_name,
        'filesZipped': files_zipped,
    }


def _dry_run_check(checks: list, name: str, passed: bool, message: str):
    checks.append({
        'name': name,
        'status': 'passed' if passed else 'failed',
        'message': message,
    })


def _actual_exported_files(output_dir: str) -> list[str]:
    files = []
    for current_dir, dirnames, filenames in os.walk(output_dir):
        dirnames[:] = [dirname for dirname in dirnames if dirname not in {'__pycache__'}]
        for filename in filenames:
            if filename == '.DS_Store':
                continue
            abs_path = os.path.abspath(os.path.join(current_dir, filename))
            if os.path.commonpath([output_dir, abs_path]) != output_dir:
                continue
            files.append(os.path.relpath(abs_path, output_dir))
    return sorted(files)


def dry_run_validation_artifacts(output_dir: str) -> dict:
    """Validate exported dbt artifact package structure and SQL text without running dbt."""
    checks = []
    errors = []
    root = _validation_artifacts_root()
    resolved_dir = os.path.abspath(str(output_dir or '').strip())
    if os.path.commonpath([root, resolved_dir]) != root:
        return {
            'status': 'failed',
            'checks': [{'name': 'output_dir_scope', 'status': 'failed', 'message': 'output_dir must be inside generated_artifacts'}],
            'errors': ['output_dir must be inside generated_artifacts'],
        }
    if not os.path.isdir(resolved_dir):
        return {
            'status': 'failed',
            'checks': [{'name': 'output_dir_exists', 'status': 'failed', 'message': 'output_dir does not exist'}],
            'errors': ['output_dir does not exist'],
        }

    required_files = [
        'dbt_project.yml',
        'manifest.json',
        'models/executive_dashboard.sql',
        'models/schema.yml',
    ]
    for rel_path in required_files:
        exists = os.path.isfile(os.path.join(resolved_dir, rel_path))
        _dry_run_check(checks, f'required_file:{rel_path}', exists, f'{rel_path} exists' if exists else f'{rel_path} is missing')
        if not exists:
            errors.append(f'Missing required file: {rel_path}')

    for rel_dir in ['models', 'tests', 'analyses']:
        exists = os.path.isdir(os.path.join(resolved_dir, rel_dir))
        _dry_run_check(checks, f'required_folder:{rel_dir}', exists, f'{rel_dir}/ exists' if exists else f'{rel_dir}/ is missing')
        if not exists:
            errors.append(f'Missing required folder: {rel_dir}/')

    sql_files = [
        rel_path for rel_path in _actual_exported_files(resolved_dir)
        if rel_path.lower().endswith('.sql')
    ]
    for rel_path in sql_files:
        abs_path = os.path.join(resolved_dir, rel_path)
        try:
            with open(abs_path, encoding='utf-8') as handle:
                content = handle.read()
        except Exception as exc:
            content = ''
            errors.append(f'Could not read {rel_path}: {exc}')
        non_empty = bool(content.strip())
        _dry_run_check(checks, f'sql_non_empty:{rel_path}', non_empty, f'{rel_path} is non-empty' if non_empty else f'{rel_path} is empty')
        if not non_empty:
            errors.append(f'Empty SQL file: {rel_path}')
        no_placeholder = not re.search(r'<[^>\n]+>', content or '')
        _dry_run_check(checks, f'no_placeholder:{rel_path}', no_placeholder, f'{rel_path} has no unresolved placeholders' if no_placeholder else f'{rel_path} contains unresolved placeholder text')
        if not no_placeholder:
            errors.append(f'Unresolved placeholder in {rel_path}')
        no_join_corruption = 'AccountLEFT JOIN' not in (content or '')
        _dry_run_check(checks, f'no_account_left_join:{rel_path}', no_join_corruption, f'{rel_path} has no AccountLEFT JOIN corruption' if no_join_corruption else f'{rel_path} contains AccountLEFT JOIN corruption')
        if not no_join_corruption:
            errors.append(f'AccountLEFT JOIN corruption in {rel_path}')
        if rel_path.startswith('tests/'):
            no_trailing_semicolon = not content.rstrip().endswith(';')
            _dry_run_check(checks, f'test_no_trailing_semicolon:{rel_path}', no_trailing_semicolon, f'{rel_path} has no trailing semicolon' if no_trailing_semicolon else f'{rel_path} has trailing semicolon')
            if not no_trailing_semicolon:
                errors.append(f'Trailing semicolon in test SQL: {rel_path}')
            no_select_star = not re.search(r'(?is)\bSELECT\s+\*', content or '')
            _dry_run_check(checks, f'test_no_select_star:{rel_path}', no_select_star, f'{rel_path} has no SELECT *' if no_select_star else f'{rel_path} contains SELECT *')
            if not no_select_star:
                errors.append(f'SELECT * in test SQL: {rel_path}')

    model_path = os.path.join(resolved_dir, 'models', 'executive_dashboard.sql')
    model_sql = ''
    if os.path.exists(model_path):
        with open(model_path, encoding='utf-8') as handle:
            model_sql = handle.read()
    has_final_model = bool(re.search(r'(?is)\bfinal_model\b', model_sql))
    _dry_run_check(checks, 'model_contains_final_model', has_final_model, 'model SQL contains final_model' if has_final_model else 'model SQL does not contain final_model')
    if not has_final_model:
        errors.append('Model SQL does not contain final_model')
    has_final_select = bool(re.search(r'(?is)\bSELECT\s+\*\s+FROM\s+final_model\b', model_sql))
    _dry_run_check(checks, 'model_final_select_from_final_model', has_final_select, 'model SQL ends with SELECT * FROM final_model' if has_final_select else 'model SQL does not contain final SELECT * FROM final_model')
    if not has_final_select:
        errors.append('Model SQL does not contain final SELECT * FROM final_model')

    manifest_path = os.path.join(resolved_dir, 'manifest.json')
    manifest = {}
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, encoding='utf-8') as handle:
                manifest = json.load(handle)
        except Exception as exc:
            errors.append(f'Could not parse manifest.json: {exc}')
    actual_files = _actual_exported_files(resolved_dir)
    manifest_files = sorted(set(manifest.get('files') or []))
    actual_non_manifest_files = sorted(file_name for file_name in actual_files if file_name != 'manifest.json')
    file_match = bool(manifest) and manifest_files == actual_non_manifest_files
    _dry_run_check(checks, 'manifest_files_match_actual', file_match, 'manifest file list matches exported files' if file_match else 'manifest file list does not match exported files')
    if not file_match:
        errors.append('Manifest file list does not match actual exported files')

    counts = manifest.get('artifactCounts') or {}
    actual_counts = {
        'models': len([file_name for file_name in actual_files if file_name.startswith('models/') and file_name.endswith('.sql')]),
        'tests': len([file_name for file_name in actual_files if file_name.startswith('tests/') and file_name.endswith('.sql')]),
        'analyses': len([file_name for file_name in actual_files if file_name.startswith('analyses/') and file_name.endswith('.sql')]),
    }
    counts_match = bool(manifest) and all(int(counts.get(key, -1)) == value for key, value in actual_counts.items())
    _dry_run_check(checks, 'manifest_artifact_counts_match', counts_match, 'manifest artifact counts match files' if counts_match else 'manifest artifact counts do not match files')
    if not counts_match:
        errors.append('Manifest artifact counts do not match actual files')

    return {
        'status': 'failed' if errors else 'passed',
        'checks': checks,
        'errors': list(dict.fromkeys(errors)),
    }


def _pending_validation_check(check: dict, reason: str) -> dict:
    result = dict(check or {})
    result.setdefault('name', result.get('id') or result.get('type') or 'validation_check')
    result.setdefault('id', result.get('name'))
    result['status'] = 'pending'
    result['result'] = None
    result['expected'] = result.get('expected')
    result['difference'] = None
    result['message'] = reason
    return result


def _first_result_row(rows):
    if rows is None:
        return {}
    if isinstance(rows, dict):
        return rows
    if isinstance(rows, (list, tuple)) and rows:
        first = rows[0]
        return first if isinstance(first, dict) else {}
    return {}


def _number_or_none(value):
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _evaluate_executed_check(check: dict, rows, execution_context: dict) -> dict:
    result = dict(check or {})
    check_id = result.get('id') or result.get('name') or ''
    check_type = result.get('type') or ''
    result.setdefault('name', check_id)
    result['result'] = rows
    result.setdefault('expected', None)
    result['difference'] = None

    row = _first_result_row(rows)
    if check_id == 'facttable_with_expenses_count':
        variance = _number_or_none(row.get('variance'))
        if variance is None:
            fact = _number_or_none(row.get('facttable_count'))
            expenses = _number_or_none(row.get('expenses_count'))
            actual = _number_or_none(row.get('migrated_count') or row.get('actual'))
            if fact is not None and expenses is not None and actual is not None:
                variance = actual - (fact + expenses)
        result['expected'] = 'migrated_count = facttable_count + expenses_count'
        result['difference'] = variance
        if variance is not None and abs(variance) < 1e-9:
            result['status'] = 'passed'
            result['message'] = 'Row count parity check passed.'
        else:
            result['status'] = 'failed'
            result['message'] = 'Row count parity check failed.'
        return result

    expected_metrics = execution_context.get('expectedMetrics') or execution_context.get('expected_metrics') or {}
    expected_breakdowns = execution_context.get('expectedBreakdowns') or execution_context.get('expected_breakdowns') or {}
    if check_type == 'metric_total':
        expected = expected_metrics.get(check_id) or expected_metrics.get(result.get('name'))
        if expected is None:
            result['status'] = 'pending'
            result['message'] = 'Metric check executed, but no expected Qlik total is available.'
            return result
        actual = _number_or_none(row.get('migrated_total') or row.get('metric_total') or row.get('actual'))
        expected_num = _number_or_none(expected)
        diff = None if actual is None or expected_num is None else actual - expected_num
        result['expected'] = expected
        result['difference'] = diff
        result['status'] = 'passed' if diff is not None and abs(diff) < 1e-9 else 'failed'
        result['message'] = 'Metric total parity check passed.' if result['status'] == 'passed' else 'Metric total parity check failed.'
        return result

    if check_type == 'dimension_breakdown':
        expected = expected_breakdowns.get(check_id) or expected_breakdowns.get(result.get('name'))
        if expected is None:
            result['status'] = 'pending'
            result['message'] = 'Breakdown check executed, but no expected Qlik breakdown is available.'
            return result
        result['expected'] = expected
        result['status'] = 'pending'
        result['message'] = 'Breakdown comparison requires a configured comparison adapter.'
        return result

    result['status'] = 'passed'
    result['message'] = 'Validation SQL executed.'
    return result


def execute_validation_report(validation_report, execution_context=None) -> dict:
    """Execute generated validation checks when a dbt/warehouse runner is available."""
    report = dict(validation_report or {})
    checks = list(report.get('checks') or [])
    context = execution_context or {}
    enabled = bool(context.get('enabled')) if 'enabled' in context else False
    reason = 'No execution context configured' if not execution_context else 'Validation execution disabled'

    compile_check = dict(report.get('dbtCompile') or {})
    compile_check.setdefault('name', 'dbt_compile')
    compile_check.setdefault('type', 'dbt_compile')
    compile_check.setdefault('sql', compile_check.get('command', ''))

    if not enabled:
        compile_check.update({
            'status': 'pending',
            'result': None,
            'expected': 'dbt compile succeeds',
            'difference': None,
            'message': reason,
        })
        report['dbtCompile'] = compile_check
        report['checks'] = [_pending_validation_check(check, reason) for check in checks]
        report['status'] = 'pending'
        return report

    run_sql = context.get('run_sql') or context.get('execute_sql')
    run_command = context.get('run_command') or context.get('run_dbt_command')
    if not callable(run_sql):
        reason = 'No execution context configured'
        compile_check.update({
            'status': 'pending',
            'result': None,
            'expected': 'dbt compile succeeds',
            'difference': None,
            'message': reason,
        })
        report['dbtCompile'] = compile_check
        report['checks'] = [_pending_validation_check(check, reason) for check in checks]
        report['status'] = 'pending'
        return report

    if callable(run_command):
        try:
            compile_result = run_command(compile_check.get('command') or '')
            compile_check.update({
                'status': 'passed',
                'result': compile_result,
                'expected': 'dbt compile succeeds',
                'difference': None,
                'message': 'dbt compile succeeded.',
            })
        except Exception as exc:
            compile_check.update({
                'status': 'failed',
                'result': None,
                'expected': 'dbt compile succeeds',
                'difference': None,
                'message': f'dbt compile failed: {exc}',
            })
    else:
        compile_check.update({
            'status': 'pending',
            'result': None,
            'expected': 'dbt compile succeeds',
            'difference': None,
            'message': 'No dbt compile runner configured.',
        })

    executed_checks = []
    for check in checks:
        try:
            rows = run_sql(check.get('sql') or '')
            executed_checks.append(_evaluate_executed_check(check, rows, context))
        except Exception as exc:
            failed = dict(check or {})
            failed.setdefault('name', failed.get('id') or failed.get('type') or 'validation_check')
            failed.setdefault('id', failed.get('name'))
            failed.update({
                'status': 'error',
                'result': None,
                'expected': failed.get('expected'),
                'difference': None,
                'message': f'Validation SQL execution failed: {exc}',
            })
            executed_checks.append(failed)

    report['dbtCompile'] = compile_check
    report['checks'] = executed_checks
    statuses = [compile_check.get('status')] + [check.get('status') for check in executed_checks]
    if any(status in {'failed', 'error'} for status in statuses):
        report['status'] = 'failed'
    elif all(status == 'passed' for status in statuses):
        report['status'] = 'passed'
    else:
        report['status'] = 'pending'
    return report


def enforce_global_yyyymm_dateadd_coercion(sql_text: str) -> str:
    """
    Replace DATEADD(month, N, YYYYMM) or DATEADD(month, N, table.YYYYMM)
    with DATEADD(month, N, TO_DATE(YYYYMM::varchar, 'YYYYMM')).
    """
    sql = sql_text or ''
    pattern = re.compile(
        r"""
        DATEADD\s*\(\s*month\s*,\s*
        (?P<n>-?\d+)\s*,\s*
        (?P<expr>(?:[A-Za-z_][A-Za-z0-9_]*\.)?YYYYMM|(?:[A-Za-z_][A-Za-z0-9_]*\.)?yyyymm)
        \s*\)
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    def repl(match):
        n = match.group('n')
        expr = match.group('expr')
        if expr.lower() == 'yyyymm':
            expr = 'YYYYMM'
        return f"DATEADD(month, {n}, TO_DATE({expr}::varchar, 'YYYYMM'))"

    return pattern.sub(repl, sql)


def _validate_join_key_name_compatibility(sql_text: str):
    """Flag invented equality joins where both sides use unrelated column names."""
    issues = []
    final_tail = _final_select_tail(sql_text)
    join_conditions = re.findall(
        r'\bJOIN\s+[A-Za-z_][A-Za-z0-9_]*\s+(?:AS\s+)?[A-Za-z_][A-Za-z0-9_]*\s+ON\s+(.*?)(?=\b(?:LEFT|RIGHT|FULL|INNER|CROSS)?\s*JOIN\b|\bWHERE\b|\bGROUP\s+BY\b|\bORDER\s+BY\b|\)|$)',
        final_tail or '',
        flags=re.IGNORECASE | re.DOTALL,
    )
    for condition in join_conditions:
        for left, right in re.findall(
            r'[A-Za-z_][A-Za-z0-9_]*\."?([A-Za-z_][A-Za-z0-9_ \-+]*)"?\s*=\s*'
            r'[A-Za-z_][A-Za-z0-9_]*\."?([A-Za-z_][A-Za-z0-9_ \-+]*)"?',
            condition,
        ):
            if _same_identifier(left, right):
                continue
            issues.append(
                f'JOIN_KEY_NAME_MISMATCH: suspicious join uses different key names "{left}" = "{right}". '
                'Use a verified bridge/path from the generation plan or add a TODO instead of inventing a join.'
            )
    return issues

def _union_branch_column_counts(cte_body: str, lineage_map=None):
    branches = split_top_level_union_all(cte_body or '')
    if len(branches) <= 1:
        return []
    return [count_select_columns_for_branch(branch, lineage_map=lineage_map) for branch in branches]


def _union_branch_parser_integrity_issues(cte_name: str, cte_body: str) -> list[str]:
    branches = split_top_level_union_all(cte_body or '')
    if len(branches) <= 1:
        return []
    issues = []
    for idx, branch in enumerate(branches, start=1):
        for issue in _projection_parser_integrity_issues(branch):
            issues.append(f'{issue} CTE {cte_name} branch {idx}.')
    return issues


def _strip_sql_comments_and_strings(sql_text: str):
    """Remove comments and string literals before scanning for executable syntax."""
    sql = sql_text or ''
    sql = re.sub(r'/\*[\s\S]*?\*/', ' ', sql)
    sql = re.sub(r'--[^\n\r]*', ' ', sql)
    sql = re.sub(r"'(?:''|[^'])*'", "''", sql)
    return sql


def validate_candidate_integrity(sql_text: str, plan=None):
    """Detect candidate assembly corruption before accepting generated SQL."""
    issues = []
    sql = sql_text or ''
    executable_sql = _strip_sql_comments_and_strings(sql)
    if not sql.strip():
        return ['EMPTY_SQL: generated SQL is empty.']
    if plan and len(plan) >= 5 and len(sql) < 1000:
        issues.append(
            f'LOW_COVERAGE_SQL: Generated SQL is too small for generation plan size {len(plan)}.'
        )

    config_count = len(re.findall(r'\{\{\s*config\s*\(', executable_sql, flags=re.IGNORECASE))
    if config_count > 1:
        issues.append('DUPLICATE_MODEL_COPY: multiple dbt config blocks detected; repair output appears concatenated.')

    names = _cte_names(executable_sql)
    seen = {}
    for name in names:
        key = name.lower()
        seen[key] = seen.get(key, 0) + 1
    duplicates = sorted(name for name, count in seen.items() if count > 1)
    if duplicates:
        issues.append(f'DUPLICATE_CTE_NAME: duplicate CTE definitions detected: {", ".join(duplicates)}.')

    repair_suffixes = sorted({name for name in names if re.search(r'_v\d+$', name, re.IGNORECASE)})
    if repair_suffixes:
        issues.append(f'REPAIR_CTE_SUFFIX_LEAK: _vN repair CTE names detected: {", ".join(repair_suffixes)}.')

    unresolved_functions = sorted({
        match.group(1)
        for match in re.finditer(
            r'\b(if|num|monthstart|makedate|makecast|addmonths)\s*\(',
            executable_sql,
            flags=re.IGNORECASE,
        )
    }, key=str.lower)
    if unresolved_functions:
        issues.append(f'UNRESOLVED_QLIK_FUNCTION: Qlik functions remain in SQL: {", ".join(unresolved_functions)}.')

    if re.search(r'\bCAST\s*\(\s*DATEADD\s*\([^)]*\bAS\s+DATE\s*\)', executable_sql, flags=re.IGNORECASE | re.DOTALL):
        issues.append('INVALID_CAST_DATEADD_SYNTAX: DATEADD was placed inside malformed CAST(... AS DATE) syntax.')

    lineage_map = _cte_columns_by_name(executable_sql, names=names)
    for name in names:
        body = _cte_body_for(executable_sql, name)
        parser_issues = _union_branch_parser_integrity_issues(name, body)
        issues.extend(parser_issues)
        counts = _union_branch_column_counts(body, lineage_map=lineage_map)
        has_star_branch = bool(re.search(r'\bSELECT\s+(?:\w+\.)?\*\s*(?:,|\bFROM\b)', body or '', re.IGNORECASE))
        if parser_issues:
            continue
        if counts and (any(count is None for count in counts) or any(count < 0 for count in counts) or has_star_branch):
            # Star branches are handled by UNION_SELECT_STAR_BRANCH and should not
            # produce misleading branch count mismatches.
            pass
        elif counts and len(set(counts)) > 1:
            snippets = []
            for idx, branch in enumerate(split_top_level_union_all(body)[:3], start=1):
                snippet = re.sub(r'\s+', ' ', branch.strip())[:120]
                snippets.append(f'b{idx}={snippet}')
            snippet_text = ' | '.join(snippets)
            if '""' in snippet_text:
                issues.append(
                    f'INTERNAL_PARSER_ERROR_QUOTED_IDENTIFIER_LOSS: CTE {name} union parser lost quoted identifiers. '
                    f'snippets: {snippet_text}'
                )
            else:
                issues.append(
                    f'UNION_COLUMN_COUNT_MISMATCH: CTE {name} has UNION ALL branch column counts {counts}. '
                    f'snippets: {snippet_text}'
                )
        if re.search(r'SELECT\s+(?:\w+\.)?\*\s*,?[\s\S]*?\bUNION\s+ALL\b', body or '', re.IGNORECASE):
            issues.append(
                f'UNION_SELECT_STAR_BRANCH: CTE {name} uses SELECT * in a UNION ALL branch; '
                'expand all UNION branches to explicit columns with aligned schema.'
            )

    fact_expenses_body = (
        _cte_body_for(executable_sql, 'facttable_with_expenses')
        or _cte_body_for(executable_sql, 'fact_table_with_expenses')
    )
    if fact_expenses_body and re.search(r'\bUNION\s+ALL\b', fact_expenses_body, re.IGNORECASE):
        fact_expenses_columns = {column.lower() for column in _select_output_columns(fact_expenses_body)}
        expenses_columns = []
        for cte_name in names:
            if 'expenses' in cte_name.lower() and cte_name.lower() not in {
                'facttable_with_expenses',
                'fact_table_with_expenses',
            }:
                expenses_columns = _append_unique_columns(expenses_columns, _select_output_columns(_cte_body_for(executable_sql, cte_name)))
        required_expense_columns = [
            column for column in expenses_columns
            if column.lower() in {'account', 'expenseactual', 'expensebudget', 'expeensebudget'}
        ]
        missing_expense_columns = [
            'ExpenseBudget' if column.lower() == 'expeensebudget' else column
            for column in required_expense_columns
            if ('expensebudget' if column.lower() == 'expeensebudget' else column.lower()) not in fact_expenses_columns
        ]
        if missing_expense_columns:
            issues.append(
                'FACT_EXPENSES_FIELDS_MISSING: facttable_with_expenses drops expenses fields: '
                + ', '.join(sorted(set(missing_expense_columns), key=str.lower))
                + '.'
            )

    final_source = (_final_select_source(executable_sql) or '').lower()
    final_tail = _final_select_tail(executable_sql)
    final_select_has_joins = bool(re.search(r'\bJOIN\b', final_tail, re.IGNORECASE))
    cte_set = {name.lower() for name in names}
    if final_source in {'facttable', 'fact_table', 'facttable_with_expenses', 'fact_table_with_expenses'}:
        if cte_set & {'final_model', 'final_mart'}:
            issues.append('WRONG_FINAL_SELECT_SOURCE: final SELECT reads raw fact CTE instead of final_model/final_mart.')
        dimension_markers = {
            'itembranchmaster',
            'itemmaster',
            'productgroupmaster',
            'productsubgroupmaster',
            'producttypemaster',
            'calendar',
            'customermaster',
            'customeraddressmaster',
            'arsummary',
            'arsummary_1',
        }
        if cte_set & dimension_markers and not final_select_has_joins:
            issues.append('WRONG_FINAL_SELECT_SOURCE: final SELECT reads raw fact CTE while dimension CTEs are present.')

    if len(names) >= 3:
        lower_names = {name.lower() for name in names}
        if not (lower_names & {'final_model', 'final_mart'}):
            issues.append('FINAL_MODEL_MISSING: multiple CTEs were generated but no final_model/final_mart mart boundary exists.')

        reference_counts = _cte_reference_counts(executable_sql, names)
        unreachable = sorted(
            name for name in lower_names
            if name not in {'final_model', 'final_mart'} and reference_counts.get(name, 0) == 0
        )
        if unreachable:
            issues.append(
                'UNREACHABLE_CTE_CREATED_NOT_USED: CTEs are created but never referenced downstream: '
                + ', '.join(unreachable)
                + '. Join them into final_model, feed them into another CTE, or omit them.'
            )

    issues.extend(_validate_duplicate_aliases(executable_sql))
    issues.extend(_validate_alias_column_references(executable_sql, names, plan=plan))
    issues.extend(_validate_join_key_name_compatibility(executable_sql))
    if 'DYNAMIC_UNION_REBUILD_FAILED' in sql or (
        has_fact_expenses_join_cte(executable_sql) and has_union_star_branch(executable_sql)
    ):
        issues.append(
            'DYNAMIC_UNION_REBUILD_FAILED: Could not dynamically rebuild facttable_with_expenses union '
            'from facttable and expenses CTE schemas.'
        )
    has_expenses_join = bool(re.search(
        r'\bJOIN\s+(?:expenses|expenses_for_fact|int_expenses|expenses_aggregated|int_expenses_aggregated)\b',
        executable_sql,
        re.IGNORECASE,
    ))
    if plan_has_expenses_concatenate(plan) and final_model_has_bad_expenses_join(sql):
        issues.append(
            'INVALID_EXPENSES_JOIN_WITH_CONCAT_PLAN: final_model joins expenses directly even though plan requires CONCATENATE/UNION semantics.'
        )
    if has_expenses_join and not _has_expenses_account_join(executable_sql):
        issues.append(
            'INVALID_EXPENSES_JOIN_MONTHLY_ONLY: expenses join is missing Account equality; '
            'never join expenses to facttable_with_expenses by MonthlyRegionKey alone.'
        )

    return issues


def _has_join_to(sql_text: str, relation: str) -> bool:
    return bool(re.search(rf'\bJOIN\s+{re.escape(relation)}\b', sql_text or '', re.IGNORECASE))


def _has_expenses_account_join(sql_text: str) -> bool:
    return bool(re.search(
        r'\bJOIN\s+(?:expenses|expenses_for_fact|int_expenses|expenses_aggregated|int_expenses_aggregated)\b'
        r'[\s\S]*?\b[A-Za-z_][A-Za-z0-9_]*\."?Account"?\s*=\s*[A-Za-z_][A-Za-z0-9_]*\."?Account"?',
        sql_text or '',
        re.IGNORECASE,
    ))


def detect_repair_regressions(previous_sql: str, candidate_sql: str):
    """Return repair-lock violations where candidate removed previously valid structure."""
    previous = previous_sql or ''
    candidate = candidate_sql or ''
    regressions = []

    for relation in (
        'itembranchmaster',
        'itemmaster',
        'productgroupmaster',
        'productsubgroupmaster',
        'producttypemaster',
        'arsummary_1',
    ):
        if _has_join_to(previous, relation) and not _has_join_to(candidate, relation):
            regressions.append(f'REPAIR_REGRESSION_REMOVED_JOIN: {relation}')

    if _has_expenses_account_join(previous) and not _has_expenses_account_join(candidate):
        regressions.append('REPAIR_REGRESSION_WEAKENED_EXPENSES_JOIN: Account equality was removed.')

    previous_fact = _cte_body_for(previous, 'facttable')
    candidate_fact = _cte_body_for(candidate, 'facttable')
    if previous_fact and candidate_fact:
        prev_cols = {c.lower() for c in _select_output_columns(previous_fact)}
        cand_cols = {c.lower() for c in _select_output_columns(candidate_fact)}
        for column in ('region', 'account'):
            if column in prev_cols and column not in cand_cols:
                regressions.append(f'REPAIR_REGRESSION_DROPPED_FACT_COLUMN: {column}')

    return regressions


def validate_generated_sql(sql_text, plan=None, dialect='dbt', qvs_script=''):
    """Lightweight sanity checks for generated output.

    For Power BI dialect the checks are M Query / DAX aware.
    For all other dialects the standard SQL checks apply.
    """
    issues = []
    content = (sql_text or '').strip()
    if not content:
        issues.append('Output is empty.')
        return issues

    if (dialect or '').lower() == 'powerbi':
        # Power BI: expect either M Query (let...in) or DAX (Name = expr)
        has_m = bool(re.search(r'\blet\b', content, re.IGNORECASE) and re.search(r'\bin\b', content, re.IGNORECASE))
        has_dax = bool(re.search(r'[A-Za-z][A-Za-z0-9 _]*\s*=\s*\S', content))
        if not has_m and not has_dax:
            issues.append('Power BI output does not appear to contain M Query (let...in) or DAX (Name = Expression).')
        return issues

    # Standard SQL checks
    upper = content.upper()
    if 'SELECT' not in upper and 'WITH' not in upper:
        issues.append('SQL does not appear to contain a SELECT or WITH clause.')

    if upper.count('(') != upper.count(')'):
        issues.append('Parentheses look unbalanced.')

    if re.search(r',\s*(FROM|WHERE|GROUP BY|ORDER BY)\b', content, re.IGNORECASE):
        issues.append('A trailing comma appears before a clause boundary.')

    if re.search(r'(?is)\bWITH\b\s*$', content):
        issues.append('WITH appears without a following CTE body.')

    if re.search(r'select\s+\*\s+from\s+(source_table|staging|stg_[A-Za-z0-9_]+|temp|table|data)\b', content, re.IGNORECASE):
        issues.append('Generated SQL appears to be a generic placeholder query rather than a real migration.')

    issues.extend(validate_candidate_integrity(content, plan=plan))

    if re.search(r'(?<!\{)\{\s*config\s*\(|\bconfig\s*\([^)]*\)\s*\}(?!\})', content, re.IGNORECASE):
        issues.append(
            "MALFORMED_DBT_CONFIG: dbt config block must use double Jinja braces: "
            "{{ config(materialized='table', tags=['qlik_migration']) }}"
        )

    for cte_match in re.finditer(r'\b([A-Za-z_][A-Za-z0-9_]*)\s+AS\s*\(', content, flags=re.IGNORECASE):
        bounds = _cte_bounds(content, cte_match.group(1))
        if not bounds:
            continue
        cte_body = content[bounds[1]:bounds[2]]
        if re.search(r'\bUNION\s+ALL\b', cte_body, re.IGNORECASE) and re.search(
            r'\bSELECT\s+\*\s+FROM\b',
            cte_body,
            re.IGNORECASE,
        ):
            issues.append('UNION_SELECT_STAR: UNION ALL branches must enumerate columns explicitly.')
            break

    if re.search(
        r'\(\s*[A-Za-z_][A-Za-z0-9_]*\."?[A-Za-z_][A-Za-z0-9_ ]*"?\s+IS\s+NOT\s+NULL\s+OR\s+'
        r'[A-Za-z_][A-Za-z0-9_]*\."?[A-Za-z_][A-Za-z0-9_ ]*"?\s+IS\s+NOT\s+NULL\s*\)',
        content,
        re.IGNORECASE,
    ):
        issues.append(
            'INVALID_NULLABLE_OR_JOIN_PREDICATE: nullable OR predicates cannot substitute for equality grain joins.'
        )
    if re.search(
        r'\bJOIN\s+(?:expenses|expenses_for_fact|int_expenses|expenses_aggregated|int_expenses_aggregated)\b'
        r'[\s\S]*?\bON\b[\s\S]*?\bMonthlyRegionKey\b[\s\S]*?\bAND\s+'
        r'[A-Za-z_][A-Za-z0-9_]*\."?Account"?\s+IS\s+NOT\s+NULL\b',
        content,
        re.IGNORECASE,
    ):
        issues.append(
            'INVALID_NULLABLE_ACCOUNT_JOIN_PREDICATE: Account IS NOT NULL cannot substitute for Account equality.'
        )

    # Qlik variable syntax $(varName) must never appear in generated SQL
    if re.search(r'\$\([A-Za-z_][A-Za-z0-9_]*\)', content):
        issues.append('Qlik variable syntax $(variable) detected in SQL — replace with literal SQL values.')

    if plan:
        referenced_sources = set()
        for item in plan:
            referenced_sources.update({canonical_source_identity(s) for s in item.get('source_tables', []) if s})
        content_sources = {
            canonical_source_identity(match.group(1))
            for match in re.finditer(
                r"\{\{\s*source\s*\(\s*['\"][^'\"]+['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}",
                content,
                flags=re.IGNORECASE,
            )
        }
        if referenced_sources and content_sources and not (referenced_sources & content_sources):
            issues.append('Generated SQL does not reference any extracted source tables.')

    issues.extend(validate_fact_expenses_union_semantics(content))
    issues.extend(validate_explicit_concatenate_field_parity(content, plan, qvs_script=qvs_script))
    issues.extend(validate_qlik_semantic_parity(content, plan))
    issues.extend(validate_execution_safety(content))
    return list(dict.fromkeys(issues))


def needs_sql_repair(issues):
    return any(validation_issue_category(issue) in {'compile_error', 'semantic_error'} for issue in issues or [])


def validation_issue_category(issue):
    """Classify validator/audit strings for repair and scoring decisions."""
    text = str(issue or '')
    upper = text.upper()

    compile_markers = (
        'EMPTY_SQL',
        'LOW_COVERAGE_SQL',
        'UNBALANCED_PARENS',
        'BARE_DDL',
        'SHELL_OPERATOR',
        'MALFORMED_DBT_CONFIG',
        'MISSING_DBT_CONFIG',
        'DIALECT_DBT_IN_POWERBI',
        'UNION_',
        'DUPLICATE_ALIAS',
        'DUPLICATE_MODEL_COPY',
        'DUPLICATE_CTE_NAME',
        'REPAIR_CTE_SUFFIX_LEAK',
        'SQL_CLEANUP_CORRUPTION',
        'UNRESOLVED_QLIK_FUNCTION',
        'INVALID_CAST_DATEADD_SYNTAX',
        'WRONG_FINAL_SELECT_SOURCE',
        'FINAL_MODEL_MISSING',
        'JOIN_KEY_MISSING',
        'JOIN_KEY_NAME_MISMATCH',
        'COLUMN_OWNERSHIP_MISMATCH',
        'QUOTED_CASE_MISMATCH',
        'CONCATENATE_SOURCE_ONLY_FIELD_LEAK',
        'INVALID_NULLABLE_OR_JOIN_PREDICATE',
        'INVALID_NULLABLE_ACCOUNT_JOIN_PREDICATE',
        'INVALID_EXPENSES_JOIN_MONTHLY_ONLY',
        'ALIAS_COLUMN_NOT_FOUND',
        'WITH APPEARS WITHOUT A FOLLOWING CTE BODY',
        'DOES NOT APPEAR TO CONTAIN A SELECT OR WITH CLAUSE',
        'TRAILING COMMA',
        'PARENTHESES LOOK UNBALANCED',
    )
    semantic_markers = (
        'MANY_TO_MANY_NO_ACTION',
        'MISSING_AGGREGATION_CTE',
        'MISSING_PIVOT_CTE',
        'WRONG_PRODUCT_JOIN_PATH',
        'EXPENSES_GRAIN_JOIN_INCOMPLETE',
        'INVALID_KEY_TO_TEXT_JOIN',
        'FACT_EXPENSES_ACCOUNT_MISSING',
        'FACT_EXPENSES_FIELDS_MISSING',
        'MISSING_PRODUCT_BRIDGE_JOIN',
        'MISSING_PRODUCT_MASTER_JOIN',
        'MISSING_ARSUMMARY_1_JOIN',
        'UNUSED_ACCOUNT_MASTER',
        'UNUSED_ACCOUNT_GROUP_MASTER',
        'REPAIR_REGRESSION_',
        'INVALID_FACT_EXPENSES_UNION',
        'INVALID_EXPENSES_BRANCH_OWNERSHIP',
        'INVALID_FACT_BRANCH_OWNERSHIP',
        'LEFTOVER_EXPENSES_REJOIN',
        'CONCATENATE_FIELD_PARITY_MISMATCH',
        'QLIK VARIABLE SYNTAX',
    )
    metadata_markers = (
        'INTERNAL_PARSER_ERROR',
        'IR_AMBIGUITY',
        'ISLAND_TABLE',
        'SOURCE_TABLE_MISMATCH',
        'SOURCE_TABLE_RENAMED',
        'MISSING_PLAN_MODEL',
        'UNRESOLVED_REF',
        'UNREACHABLE_CTE_CREATED_NOT_USED',
        'LIKELY_TYPO',
        'SQL_CLEANUP_LEFTOVER_ALIAS_E',
        'SAFE_UNION_OVERRIDE',
        'SELECT_STAR_EXECUTION_RISK',
        'MISSING_CALENDAR_ENRICHMENT',
        'INCOMPLETE_PRODUCT_HIERARCHY',
        'MISSING_AR_ENRICHMENT',
        'MISSING_CUSTOMER_ENRICHMENT',
        'MISSING_BUDGET_ENRICHMENT',
    )

    if any(marker in upper for marker in compile_markers):
        return 'compile_error'
    if any(marker in upper for marker in semantic_markers):
        return 'semantic_error'
    if any(marker in upper for marker in metadata_markers) or '[WARNING]' in upper:
        return 'metadata_warning'
    if '[INFO]' in upper:
        return 'informational'
    return 'informational'


def extract_sql_generation_plan(qvs_script):
    """Create a compact, deterministic plan from actual Qlik LOAD blocks only."""
    load_blocks = extract_load_block_ast(qvs_script or '')
    plan = []

    for block in load_blocks:
        source_tables = []
        source = _normalize_identifier(block.get('source'))
        resident = _normalize_identifier(block.get('residentTable'))
        join_target = _normalize_identifier(block.get('joinTarget'))

        if source:
            source_tables.append(source)
        if resident and resident not in source_tables:
            source_tables.append(resident)
        if join_target and join_target not in source_tables:
            source_tables.append(join_target)

        plan.append({
            'table': block.get('table') or 'generated_sql',
            'operation': block.get('operation', 'LOAD'),
            'source': block.get('source'),
            'source_tables': source_tables,
            'fields': block.get('fields', []),
            'filters': block.get('where', []),
            'joins': [join_target] if join_target else [],
            'joinType': block.get('joinType'),
            'source_type': block.get('sourceType'),
            'raw': block.get('raw', ''),
            'is_concatenate': (block.get('joinType') or '').upper() == 'CONCATENATE',
            'concatenate_target': join_target if (block.get('joinType') or '').upper() == 'CONCATENATE' else None,
            'drop_fields': block.get('drop_fields', []),
        })

    return plan


def format_sql_generation_plan(plan):
    """Render a stable, deduplicated text representation of the generation plan."""
    if not plan:
        return ''

    seen_tables = set()
    lines = []
    for item in plan:
        table = item.get('table', 'generated_sql')
        # Deduplicate — skip entries with no real table name or already seen
        table_key = re.sub(r'\s+', ' ', table.strip()).lower()
        if table_key in seen_tables:
            continue
        # Skip raw path entries (table name starts with quote or lib://)
        if table.startswith("'") or table.startswith('"') or 'lib://' in table.lower():
            continue
        seen_tables.add(table_key)

        sources = item.get('source_tables') or []
        join_type = (item.get('joinType') or '').upper()
        concat_target = item.get('concatenate_target')

        # Clean up source names — strip lib:// paths to just the filename
        def clean_source(s):
            s = re.sub(r'\s+', ' ', (s or '').strip())
            # Extract just the filename without path or load options
            m = re.search(r'/([^/\'\"]+?)(?:\.[a-zA-Z0-9]+)?\s*(?:\'|\")?\s*(?:\([^)]*\))?\s*$', s, re.IGNORECASE)
            if m:
                return m.group(1).strip(" '\"")
            # Fallback: strip lib:// prefix
            s = re.sub(r"lib://[^/]*/", '', s, flags=re.IGNORECASE)
            return s.strip(" '\"")

        clean_sources = [clean_source(s) for s in sources if s]

        if join_type == 'CONCATENATE' and concat_target:
            lines.append(
                f"- {table}: CONCATENATE (UNION ALL) into {concat_target} "
                f"from {', '.join(clean_sources)}"
            )
        elif clean_sources:
            lines.append(f"- {table}: reads from {', '.join(clean_sources)}")
        else:
            lines.append(f"- {table}: no explicit source detected")
    return "\n".join(lines)


def _normalize_logic_text(value):
    return re.sub(r'\s+', ' ', (value or '').strip().lower())


def _normalize_identifier_for_compare(name):
    if not name:
        return ''
    return re.sub(r'[\[\]"`\s]', '', str(name).strip()).lower()


def _detect_aggregation_functions(text):
    if not text:
        return []
    pattern = re.compile(
        r'\b(Sum|Count|Avg|Min|Max|StDev|Variance|Aggr|Concat|StringConcat|Median|FirstSortedValue|LastSortedValue)\s*\(',
        re.IGNORECASE,
    )
    return sorted({m.group(1).upper() for m in pattern.finditer(text)})


def _split_alias_from_expression(field_expression):
    value = str(field_expression or '').strip()
    if not value:
        return '', None

    match = re.search(
        r'^(?P<expr>.+?)(?:\s+AS\s+(?P<alias>\[[^\]]+\]|"[^"]+"|[A-Za-z0-9_\$][A-Za-z0-9_\$\s-]*))\s*$',
        value,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match:
        expr = match.group('expr').strip()
        alias = match.group('alias').strip()
        return expr, alias
    return value, None


def _extract_output_name(expression, alias):
    if alias:
        return _normalize_identifier_for_compare(alias)
    if not expression:
        return ''
    if expression.startswith('[') and expression.endswith(']'):
        return _normalize_identifier_for_compare(expression[1:-1])
    return _normalize_identifier_for_compare(expression)


def _parse_qlik_block_fields(fields):
    results = []
    for field in fields or []:
        expr, alias = _split_alias_from_expression(field)
        results.append({
            'raw': field,
            'expression': expr.strip(),
            'alias': alias.strip() if alias else None,
            'output': _extract_output_name(expr, alias),
            'aggregations': _detect_aggregation_functions(expr),
            'is_calculated': bool(alias or re.search(r'\W', expr.strip()) and expr.strip().lower() not in {alias.lower() if alias else ''}),
        })
    return results


def _parse_sql_select_fields(sql_text):
    match = re.search(r'\bSELECT\b(.*?)\bFROM\b', sql_text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return []
    body = match.group(1)
    items = _split_sql_like_fields(body)
    return [item.strip() for item in items if item.strip()]


def describe_qlik_script(qvs_script):
    plan = extract_sql_generation_plan(qvs_script or '')
    source_tables = set()
    joins = []
    filters = set()
    aggregations = set()
    calculated_fields = set()
    output_columns = set()
    transformations = []

    for item in plan:
        for source in item.get('source_tables', []) or []:
            if source:
                source_tables.add(_normalize_identifier_for_compare(source))

        for target in item.get('joins', []) or []:
            if target:
                joins.append({
                    'type': (item.get('joinType') or 'JOIN').upper(),
                    'target': _normalize_identifier_for_compare(target),
                })

        for filter_expr in item.get('filters', []) or []:
            normalized = _normalize_logic_text(filter_expr)
            if normalized:
                filters.add(normalized)

        for field_info in _parse_qlik_block_fields(item.get('fields', [])):
            output = field_info.get('output')
            if output:
                output_columns.add(output)
            if field_info.get('is_calculated'):
                calculated_fields.add(output or _normalize_identifier_for_compare(field_info['expression']))
            aggregations.update({a.upper() for a in field_info.get('aggregations', [])})

        operation = item.get('operation') or 'LOAD'
        join_type = item.get('joinType') or ''
        transformations.append(f"{operation}{'/' + join_type if join_type else ''}".strip('/'))

    return {
        'source_tables': sorted(source_tables),
        'joins': joins,
        'filters': sorted(filters),
        'aggregations': sorted(aggregations),
        'calculated_fields': sorted(calculated_fields),
        'output_columns': sorted(output_columns),
        'transformations': sorted(set(transformations)),
        'summary': {
            'blockCount': len(plan),
            'tables': sorted(source_tables),
            'joinCount': len(joins),
            'filterCount': len(filters),
            'aggregationCount': len(aggregations),
            'outputColumnsCount': len(output_columns),
        },
    }


def _normalize_join_type(join_type):
    if not join_type:
        return 'INNER'
    normalized = re.sub(r'\s+', ' ', str(join_type).strip().upper())
    if 'LEFT' in normalized:
        return 'LEFT'
    if 'RIGHT' in normalized:
        return 'RIGHT'
    if 'FULL' in normalized:
        return 'FULL'
    if 'CROSS' in normalized:
        return 'CROSS'
    return 'INNER'


def _extract_sql_sources(sql_text):
    sources = set()
    for match in re.finditer(r'\bFROM\s+([A-Za-z0-9_`"\[\]\.\{\}]+)', sql_text, flags=re.IGNORECASE):
        sources.add(_normalize_identifier_for_compare(match.group(1)))
    for match in re.finditer(r'\b(?:LEFT|RIGHT|FULL|INNER|CROSS|OUTER)?\s*JOIN\s+([A-Za-z0-9_`"\[\]\.\{\}]+)', sql_text, flags=re.IGNORECASE):
        sources.add(_normalize_identifier_for_compare(match.group(1)))
    for match in re.finditer(r"\{\{\s*source\s*\(\s*['\"][^'\"]+['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}", sql_text, flags=re.IGNORECASE):
        sources.add(_normalize_identifier_for_compare(match.group(1)))
    for match in re.finditer(r"\{\{\s*ref\s*\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}", sql_text, flags=re.IGNORECASE):
        sources.add(_normalize_identifier_for_compare(match.group(1)))
    return sorted({s for s in sources if s})


def _extract_sql_joins(sql_text):
    join_clauses = []
    join_pattern = re.compile(
        r'\b(LEFT|RIGHT|FULL|INNER|CROSS|OUTER)?\s*JOIN\s+([A-Za-z0-9_`"\[\]\.\{\}]+)\s+ON\s+(.*?)(?=\b(LEFT|RIGHT|FULL|INNER|CROSS|OUTER)?\s*JOIN\b|\bWHERE\b|\bGROUP\s+BY\b|\bORDER\s+BY\b|\bHAVING\b|$)',
        flags=re.IGNORECASE | re.DOTALL,
    )
    for match in join_pattern.finditer(sql_text):
        join_type = _normalize_join_type(match.group(1))
        target = _normalize_identifier_for_compare(match.group(2))
        condition = _normalize_logic_text(match.group(3))
        join_clauses.append({'type': join_type, 'target': target, 'condition': condition})
    return join_clauses


def _extract_sql_filters(sql_text):
    filters = set()
    where_match = re.search(r'\bWHERE\b(.*?)(?=\bGROUP\s+BY\b|\bORDER\s+BY\b|\bHAVING\b|\bLIMIT\b|$)', sql_text, flags=re.IGNORECASE | re.DOTALL)
    if where_match:
        expressions = _split_sql_like_fields(where_match.group(1))
        for expr in expressions:
            normalized = _normalize_logic_text(expr)
            if normalized:
                filters.add(normalized)
    return sorted(filters)


def _extract_sql_aggregations(sql_text):
    return _detect_aggregation_functions(sql_text)


def _parse_sql_fields(fields):
    results = []
    for field in fields or []:
        expr, alias = _split_alias_from_expression(field)
        results.append({
            'raw': field,
            'expression': expr.strip(),
            'alias': alias.strip() if alias else None,
            'output': _extract_output_name(expr, alias),
            'aggregations': _detect_aggregation_functions(expr),
            'is_calculated': bool(alias or re.search(r'\W', expr.strip()) and expr.strip().lower() not in {alias.lower() if alias else ''}),
        })
    return results


def describe_sql(sql_text):
    sql_text = sql_text or ''

    # ── Extract CTE names so we can exclude them from "source tables" ────────
    # In a WITH ... AS (...) model, CTE names appear as FROM/JOIN targets but
    # are not real source tables — only {{ source(...) }} and {{ ref(...) }}
    # references are real external sources.
    cte_names = set()
    for m in re.finditer(r'\b(\w+)\s+AS\s*\(', sql_text, flags=re.IGNORECASE):
        cte_names.add(m.group(1).lower())

    # Real external sources: {{ source('schema', 'table') }} and {{ ref('model') }}
    real_sources = set()
    for m in re.finditer(
        r"\{\{\s*source\s*\(\s*['\"][^'\"]+['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}",
        sql_text, flags=re.IGNORECASE
    ):
        real_sources.add(_normalize_identifier_for_compare(m.group(1)))
    for m in re.finditer(
        r"\{\{\s*ref\s*\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}",
        sql_text, flags=re.IGNORECASE
    ):
        real_sources.add(_normalize_identifier_for_compare(m.group(1)))

    # Fall back to all FROM/JOIN targets minus CTE names when no source() refs found
    if not real_sources:
        all_sources = _extract_sql_sources(sql_text)
        real_sources = {s for s in all_sources if s.lower() not in cte_names}

    # ── Collect fields, aggregations, filters across ALL CTEs ────────────────
    # Walk every SELECT ... FROM block in the SQL (one per CTE + final select)
    all_select_fields = []
    for m in re.finditer(r'\bSELECT\b(.*?)\bFROM\b', sql_text, flags=re.IGNORECASE | re.DOTALL):
        body = m.group(1)
        # Skip SELECT * — it carries no field information
        if body.strip() == '*':
            continue
        items = _split_sql_like_fields(body)
        all_select_fields.extend([item.strip() for item in items if item.strip()])

    select_fields = _parse_sql_fields(all_select_fields)
    aggregations = sorted({a for field in select_fields for a in field.get('aggregations', [])})
    # Exclude placeholder names like '*' from calculated/output columns
    calculated_fields = sorted({
        field['output'] for field in select_fields
        if field.get('is_calculated') and field.get('output') and field['output'] != '*'
    })
    output_columns = sorted({
        field['output'] for field in select_fields
        if field.get('output') and field['output'] != '*'
    })

    joins = _extract_sql_joins(sql_text)
    filters = _extract_sql_filters(sql_text)

    transformations = []
    if re.search(r'\bWITH\b', sql_text, flags=re.IGNORECASE):
        transformations.append('WITH_CTES')
    if re.search(r'\bGROUP\s+BY\b', sql_text, flags=re.IGNORECASE):
        transformations.append('GROUP_BY')
    if re.search(r'\bHAVING\b', sql_text, flags=re.IGNORECASE):
        transformations.append('HAVING')

    return {
        'source_tables': sorted(real_sources),
        'joins': joins,
        'filters': filters,
        'aggregations': aggregations,
        'calculated_fields': calculated_fields,
        'output_columns': output_columns,
        'transformations': sorted(set(transformations)),
        'summary': {
            'joinCount': len(joins),
            'filterCount': len(filters),
            'aggregationCount': len(aggregations),
            'outputColumnsCount': len(output_columns),
        },
    }


def compare_descriptions(qlik_description, sql_description):
    differences = []

    # ── Source tables ─────────────────────────────────────────────────────────
    q_tables_raw = set(qlik_description.get('source_tables', []))
    s_tables_raw = set(sql_description.get('source_tables', []))
    q_tables = {canonical_source_identity(t) for t in q_tables_raw if canonical_source_identity(t)}
    s_tables = {canonical_source_identity(t) for t in s_tables_raw if canonical_source_identity(t)}
    if q_tables and s_tables and q_tables != s_tables:
        # Only flag if both sides have data — avoids false positives when
        # the SQL uses {{ source() }} refs that don't match raw Qlik file names
        missing_in_sql = q_tables - s_tables
        if missing_in_sql:
            differences.append({
                'type': 'SOURCE_TABLE_MISMATCH',
                'qlik': sorted(q_tables_raw),
                'sql': sorted(s_tables_raw),
            })

    # ── Joins ─────────────────────────────────────────────────────────────────
    q_joins = {(j['target'], _normalize_join_type(j.get('type'))) for j in qlik_description.get('joins', [])}
    s_joins = {(j['target'], _normalize_join_type(j.get('type'))) for j in sql_description.get('joins', [])}
    if q_joins != s_joins:
        missing = q_joins - s_joins
        extra = s_joins - q_joins
        if missing:
            differences.append({
                'type': 'JOIN_MISMATCH',
                'qlik': [f"{jt} {target}" for target, jt in sorted(missing)],
                'sql': [f"{jt} {target}" for target, jt in sorted(s_joins if not missing else [])],
            })
        if extra:
            differences.append({
                'type': 'EXTRA_SQL_JOIN',
                'qlik': [f"{jt} {target}" for target, jt in sorted(q_joins)],
                'sql': [f"{jt} {target}" for target, jt in sorted(extra)],
            })

    # ── Filters ───────────────────────────────────────────────────────────────
    q_filters = set(qlik_description.get('filters', []))
    s_filters = set(sql_description.get('filters', []))
    if q_filters and q_filters != s_filters:
        differences.append({
            'type': 'FILTER_MISMATCH',
            'qlik': sorted(q_filters),
            'sql': sorted(s_filters),
        })

    # ── Aggregations ──────────────────────────────────────────────────────────
    q_aggs = set(qlik_description.get('aggregations', []))
    s_aggs = set(sql_description.get('aggregations', []))
    if q_aggs != s_aggs:
        differences.append({
            'type': 'AGGREGATION_MISMATCH',
            'qlik': sorted(q_aggs),
            'sql': sorted(s_aggs),
        })

    # ── Output columns ────────────────────────────────────────────────────────
    # Only compare output columns when both sides have a meaningful list.
    # CTE models end with SELECT * so the SQL side is often empty — skip in
    # that case to avoid false positives that waste an iteration.
    q_outputs = set(qlik_description.get('output_columns', []))
    s_outputs = set(sql_description.get('output_columns', []))
    if q_outputs and s_outputs and q_outputs != s_outputs:
        differences.append({
            'type': 'OUTPUT_COLUMN_MISMATCH',
            'qlik': sorted(q_outputs),
            'sql': sorted(s_outputs),
        })

    # ── Calculated fields ─────────────────────────────────────────────────────
    q_calcs = set(qlik_description.get('calculated_fields', []))
    s_calcs = set(sql_description.get('calculated_fields', []))
    if q_calcs and s_calcs and q_calcs != s_calcs:
        differences.append({
            'type': 'CALCULATION_MISMATCH',
            'qlik': sorted(q_calcs),
            'sql': sorted(s_calcs),
        })

    score = 1.0
    if differences:
        score = max(0.0, 1.0 - len(differences) * 0.15)

    return {
        'matched': not differences,
        'differences': differences,
        'score': round(score, 2),
    }


def build_semantic_validation_prompt(
    qvs_script,
    previous_sql,
    qlik_description,
    sql_description,
    comparison,
    current_desc=None,
    dialect='dbt',
    plan_text='',
    prompt_version='',
    description_style='',
):
    system_prompt = f"""You are an expert SQL migration assistant.
Prompt version: {prompt_version}
Your task is to compare the original Qlik script logic against the generated SQL logic and produce a corrected SQL migration.
Use the structured Qlik and SQL descriptions to identify semantic mismatches.
Do not change anything that already matches. Fix only the detected mismatches.
Keep the same field names, join logic, filters, aggregations, and output schema wherever possible.
Target dialect: {dialect.upper()}.

For the ### DESCRIPTION section: write expert-level technical Markdown.
Start with 1–2 sentences explaining what the model does and what business question it answers.
Then one ## Block: <cte_name> section per CTE. For each block explain: what it does and why,
source tables/CTEs, key transformations (renames, casts, date arithmetic, CASE logic, aggregations),
filters and their business meaning, and how it feeds downstream blocks.
Use `inline code` for field names. Use **bold** for important terms. Be specific — no boilerplate.
"""

    diff_lines = []
    for item in comparison.get('differences', []):
        diff_lines.append(f"- {item['type']}: Qlik={item['qlik']}; SQL={item['sql']}")

    previous_sql_summary = describe_sql(previous_sql or '')

    prompt_parts = [
        "### Source Qlik Script",
        f"```sql\n{qvs_script}\n```",
        "### Extracted Qlik Description",
        json.dumps(qlik_description, indent=2),
        "### Previous Attempt Summary",
        json.dumps(previous_sql_summary, indent=2),
        "### Difference Report",
        "\n".join(diff_lines) if diff_lines else 'No semantic differences detected.',
    ]

    if current_desc:
        prompt_parts.extend([
            "### Previous SQL Description",
            current_desc,
        ])

    if plan_text:
        prompt_parts.extend([
            "### Extraction Plan",
            plan_text,
        ])

    prompt_parts.append(
        "### Instructions\n" +
        "Please regenerate only the SQL and description sections. Keep the same structure where possible. Fix the mismatches above. " +
        "Do not introduce unrelated tables, columns, or logic. Output exactly in the same format as the original migration prompt."
    )

    return system_prompt, "\n\n".join(prompt_parts)


def _apply_semantic_validation_loop(
    call_ai,
    qvs_script,
    session_context=None,
    current_sql=None,
    current_desc=None,
    dialect='dbt',
    plan=None,
    plan_text=None,
    prompt_version='',
    description_style='',
    max_iterations=8,
    progress_callback=None,
    stream_callback=None,
):
    """
    Run the semantic validation loop until the generated SQL matches the Qlik
    script description, or until max_iterations is exhausted.

    The loop exits early as soon as compare_descriptions() returns matched=True
    (score=1.0).  On the final AI call (whether matched or ceiling hit), tokens
    are streamed to the frontend via stream_callback so the user sees live output.
    """
    if progress_callback:
        progress_callback("Analyzing Qlik script...")
    plan = plan if plan is not None else extract_sql_generation_plan(qvs_script or '')
    plan_text = plan_text if plan_text is not None else format_sql_generation_plan(plan)
    if progress_callback:
        progress_callback(f"Parsed {len(plan)} LOAD blocks from Qlik script...")
    qlik_description = describe_qlik_script(qvs_script)
    deterministic_sql = render_sql_from_load_plan(plan)
    best_result = None
    best_score = 0.0

    for iteration in range(1, max_iterations + 1):
        is_last_possible = (iteration == max_iterations)

        if progress_callback:
            if iteration == 1:
                progress_callback(f"Iteration {iteration}/{max_iterations}: generating SQL...")
            else:
                progress_callback(f"Iteration {iteration}/{max_iterations}: semantic repair pass (score so far: {best_score:.2f})...")

        if iteration == 1:
            system_prompt, prompt = build_sql_generation_prompt(
                qvs_script,
                session_context=session_context,
                current_sql=current_sql,
                current_desc=current_desc,
                dialect=dialect,
                plan=plan,
                plan_text=plan_text,
                prompt_version=prompt_version,
                description_style=description_style,
            )
            if deterministic_sql:
                prompt += (
                    "\n\n### Deterministic SQL Draft From Parsed Qlik LOAD Blocks\n"
                    "Use this draft as the structural anchor. You may improve dialect-specific syntax, "
                    "but do not remove parsed fields, source tables, filters, GROUP BY clauses, or block order.\n"
                    f"```sql\n{deterministic_sql}\n```"
                )
        else:
            system_prompt, prompt = build_semantic_validation_prompt(
                qvs_script,
                current_sql or '',
                qlik_description,
                describe_sql(current_sql or ''),
                best_result['comparison'] if best_result else {'differences': []},
                current_desc=current_desc,
                dialect=dialect,
                plan_text=plan_text,
                prompt_version=prompt_version,
                description_style=description_style,
            )

        logger.info("Migration loop iteration %d/%d: generating SQL", iteration, max_iterations)
        if progress_callback:
            progress_callback(f"Iteration {iteration}/{max_iterations}: calling AI model...")

        iter_max_prompt_chars = 25_000 if iteration == 1 else 30_000
        iter_max_tokens = LOOP_MAX_TOKENS

        # Stream tokens on the very last AI call (either ceiling hit or we'll
        # check match after and it might be the final one).
        # We stream on the last possible iteration; if we match earlier we also
        # stream that call so the user always sees live output on the final pass.
        should_stream = (stream_callback is not None) and is_last_possible

        try:
            if should_stream and progress_callback:
                progress_callback(f"Iteration {iteration}/{max_iterations}: streaming final SQL from AI...")
            ai_response = _invoke_ai_text(
                call_ai,
                prompt,
                system_prompt=system_prompt,
                max_tokens=iter_max_tokens,
                max_prompt_chars=iter_max_prompt_chars,
                phase='full_generation',
                min_tokens=MIN_FULL_SQL_TOKENS,
                stream_callback=stream_callback if should_stream else None,
            )
        except Exception as exc:
            message = str(exc)
            logger.warning("Migration loop iteration %d failed during AI call: %s", iteration, message)
            if progress_callback:
                progress_callback(f"Migration stopped: {message}")
            return _failed_migration_result(
                message,
                plan,
                qvs_script,
                iterations=iteration - 1,
            )

        if progress_callback:
            progress_callback(f"Iteration {iteration}/{max_iterations}: AI responded, validating structure...")

        structured_output = parse_migration_response(ai_response)
        if structured_output.get('sql'):
            structured_output['sql'] = finalize_generated_sql(
                structured_output['sql'],
                plan=plan,
                qvs_script=qvs_script,
            )

        # Reject stub/empty responses. Do not score deterministic fallback as AI.
        sql_candidate = (structured_output.get('sql') or '').strip()
        is_stub = (
            not sql_candidate
            or sql_candidate.startswith('--')
            or len(sql_candidate) < 80
            or not re.search(r'\bSELECT\b|\bWITH\b', sql_candidate, re.IGNORECASE)
        )
        if is_stub:
            message = (
                f"AI returned empty/stub SQL on iteration {iteration} "
                f"(chars={len(sql_candidate)}, output_tokens≈{_estimate_output_tokens(ai_response)})."
            )
            logger.info("Migration loop returned stub SQL: %s", message)
            if progress_callback:
                progress_callback(f"Migration stopped: {message}")
            return _failed_migration_result(
                message,
                plan,
                qvs_script,
                iterations=iteration,
                validation_issues=['AI_STUB_OUTPUT'],
            )

        # Structural repair pass
        validation_issues = _audit_generated_sql_against_plan(
            structured_output.get('sql', ''),
            plan=plan,
            qvs_script=qvs_script,
            dialect=dialect,
        )
        if validation_issues and needs_sql_repair(validation_issues):
            logger.info("Structural validation failed; starting repair pass with %d issue(s)", len(validation_issues))
            if progress_callback:
                progress_callback(f"Iteration {iteration}/{max_iterations}: repairing {len(validation_issues)} structural issue(s)...")
            try:
                repaired_response = request_sql_repair(
                    call_ai,
                    structured_output.get('sql', ''),
                    structured_output.get('description', ''),
                    validation_issues,
                    dialect=dialect,
                    description_style=description_style,
                    prompt_version=prompt_version,
                    qvs_script=qvs_script,
                    plan_text=plan_text,
                )
                repaired_structured = parse_migration_response(repaired_response)
                if repaired_structured.get('sql'):
                    repaired_structured['sql'] = finalize_generated_sql(
                        repaired_structured['sql'],
                        plan=plan,
                        qvs_script=qvs_script,
                    )
                    regressions = detect_repair_regressions(structured_output.get('sql', ''), repaired_structured['sql'])
                    if regressions:
                        logger.warning("Repair candidate rejected due to regressions: %s", regressions)
                        validation_issues.extend(regressions)
                    else:
                        structured_output = repaired_structured
                        if progress_callback:
                            progress_callback(f"Iteration {iteration}/{max_iterations}: repair complete, re-validating...")
            except Exception as repair_err:
                logger.warning("Structural self-repair failed: %s", repair_err)

        validation_issues = _audit_generated_sql_against_plan(
            structured_output.get('sql', ''),
            plan=plan,
            qvs_script=qvs_script,
            dialect=dialect,
        )

        # Semantic comparison
        sql_description = describe_sql(structured_output.get('sql', ''))
        comparison = compare_descriptions(qlik_description, sql_description)
        score = comparison.get('score', 0.0)
        diffs = comparison.get('differences', [])
        issue_categories = [validation_issue_category(issue) for issue in validation_issues or []]
        has_blocking_issues = any(category in {'compile_error', 'semantic_error'} for category in issue_categories)
        matched = comparison.get('matched', False) and not has_blocking_issues
        if has_blocking_issues:
            logger.info(
                "Migration validation capped score due to blocking issues: categories=%s issues=%s",
                issue_categories[:5],
                validation_issues[:5],
            )
            score = min(score, 0.25)
        logger.info(
            "Migration validation: score=%.2f diffs=%d issues=%d categories=%s",
            score,
            len(diffs),
            len(validation_issues or []),
            issue_categories[:5],
        )

        # Track best result by score
        if best_result is None or score >= best_score:
            best_score = score
            best_result = {
                'iteration': iteration,
                'ai_response': ai_response,
                'final_sql': structured_output.get('sql', ''),
                'final_description': structured_output.get('description', ''),
                'qlik_description': qlik_description,
                'sql_description': sql_description,
                'comparison': comparison,
                'score': score,
                'validation_issues': validation_issues,
            }

        if progress_callback:
            if matched:
                progress_callback(f"✅ Iteration {iteration}/{max_iterations}: semantic match confirmed! Score: 1.00")
            else:
                diff_types = ', '.join(d['type'] for d in diffs[:3])
                progress_callback(
                    f"Iteration {iteration}/{max_iterations}: score={score:.2f} "
                    f"({len(diffs)} mismatch(es): {diff_types})"
                )

        logger.info(
            "Migration loop iteration %d/%d complete: score=%.2f matched=%s diffs=%d",
            iteration,
            max_iterations,
            score,
            matched,
            len(diffs),
        )

        if matched:
            best_result['status'] = 'matched'
            best_result['iterations'] = iteration
            return best_result

        if is_last_possible:
            best_result['status'] = 'retry'
            best_result['iterations'] = iteration
            if progress_callback:
                progress_callback(
                    f"⚠️ Reached max iterations ({max_iterations}). "
                    f"Best AI score: {best_result['score']:.2f}. No deterministic fallback was scored as AI."
                )
            return best_result

        # Feed this iteration's output into the next
        current_sql = structured_output.get('sql', '')
        current_desc = structured_output.get('description', '')

    # Should never reach here, but safety net
    if best_result:
        best_result['status'] = 'retry'
        best_result['iterations'] = max_iterations
    return best_result


def request_migration_with_validation(
    call_ai,
    qvs_script,
    session_context=None,
    current_sql=None,
    current_desc=None,
    dialect='dbt',
    plan=None,
    plan_text=None,
    prompt_version='',
    description_style='',
    max_iterations=8,
    progress_callback=None,
    stream_callback=None,
):
    """Migrate Qlik to SQL with a semantic validation loop and structured result."""
    result = _apply_semantic_validation_loop(
        call_ai,
        qvs_script,
        session_context=session_context,
        current_sql=current_sql,
        current_desc=current_desc,
        dialect=dialect,
        plan=plan,
        plan_text=plan_text,
        prompt_version=prompt_version,
        description_style=description_style,
        max_iterations=max_iterations,
        progress_callback=progress_callback,
        stream_callback=stream_callback,
    )
    return {
        'status': result.get('status', 'retry'),
        'iterations': result.get('iterations', 0),
        'score': result.get('score', 0.0),
        'final_sql': result.get('final_sql', ''),
        'sql': result.get('final_sql', ''),
        'qlik_description': result.get('qlik_description', {}),
        'sql_description': result.get('sql_description', {}),
        'comparison_summary': result.get('comparison', {}),
        'final_description': result.get('final_description', ''),
        'description': result.get('final_description', ''),
        'used_deterministic_fallback': result.get('used_deterministic_fallback', False),
        'validation_issues': result.get('validation_issues', []),
        'error': result.get('error', ''),
    }


def request_migration_one_shot(
    call_ai,
    qvs_script,
    session_context=None,
    current_sql=None,
    current_desc=None,
    dialect='dbt',
    plan=None,
    plan_text=None,
    prompt_version='',
    description_style='',
    progress_callback=None,
    stream_callback=None,
):
    """Call AI once and return the raw generated SQL/description without self-correction."""
    if progress_callback:
        progress_callback('Analyzing Qlik script for one-shot migration...')
    plan = plan if plan is not None else extract_sql_generation_plan(qvs_script or '')
    plan_text = plan_text if plan_text is not None else format_sql_generation_plan(plan)
    if not plan:
        return {
            'status': 'failed',
            'iterations': 0,
            'final_sql': '',
            'sql': '',
            'description': '',
            'qlik_description': {},
            'sql_description': {},
            'comparison_summary': {},
        }

    # Note: build_fast_sql_generation_prompt handles its own context optimization.
    # Do NOT pre-truncate here — let the prompt builder decide the right limit.
    if progress_callback:
        progress_callback('Calling AI for one-shot SQL generation...')
    system_prompt, prompt = build_fast_sql_generation_prompt(
        qvs_script,
        dialect=dialect,
        plan=plan,
        plan_text=plan_text,
        prompt_version=prompt_version,
        description_style=description_style,
    )

    try:
        ai_response = _invoke_ai_text(
            call_ai,
            prompt,
            system_prompt=system_prompt,
            max_tokens=ONE_SHOT_MAX_TOKENS,
            phase='one_shot_generation',
            min_tokens=MIN_FULL_SQL_TOKENS,
            stream_callback=stream_callback,
        )
    except Exception as exc:
        message = str(exc)
        if progress_callback:
            progress_callback(f"Migration stopped: {message}")
        return _failed_migration_result(message, plan, qvs_script, iterations=0)
    structured_output = parse_migration_response(ai_response)
    final_sql = (structured_output.get('sql') or '').strip()
    final_desc = (structured_output.get('description') or '').strip()

    # Treat comment-only / stub responses as failed AI output. Do not score fallback.
    is_stub = (
        not final_sql
        or final_sql.startswith('--')
        or len(final_sql) < 80
        or not re.search(r'\bSELECT\b|\bWITH\b', final_sql, re.IGNORECASE)
    )
    if is_stub:
        message = (
            "AI returned empty/stub SQL in one-shot generation "
            f"(chars={len(final_sql)}, output_tokens≈{_estimate_output_tokens(ai_response)})."
        )
        if progress_callback:
            progress_callback(f"Migration stopped: {message}")
        logger.info("One-shot migration returned stub SQL: %s", message)
        return _failed_migration_result(
            message,
            plan,
            qvs_script,
            iterations=1,
            validation_issues=['AI_STUB_OUTPUT'],
        )

    final_sql = finalize_generated_sql(final_sql, plan=plan, qvs_script=qvs_script)
    validation_issues = _audit_generated_sql_against_plan(
        final_sql,
        plan=plan,
        qvs_script=qvs_script,
        dialect=dialect,
    )
    if needs_sql_repair(validation_issues):
        issue_categories = [validation_issue_category(issue) for issue in validation_issues or []]
        logger.info(
            "One-shot migration has blocking validation issues: categories=%s issues=%s",
            issue_categories[:5],
            validation_issues[:5],
        )
    return {
        'status': 'complete' if final_sql else 'failed',
        'iterations': 1,
        'final_sql': final_sql,
        'sql': final_sql,
        'description': final_desc,
        'qlik_description': describe_qlik_script(qvs_script),
        'sql_description': describe_sql(final_sql),
        'comparison_summary': {},
        'validation_issues': validation_issues,
    }


def request_migration(
    call_ai,
    qvs_script,
    session_context=None,
    current_sql=None,
    current_desc=None,
    dialect='dbt',
    plan=None,
    plan_text=None,
    prompt_version='',
    description_style='',
):
    """Call AI to migrate Qlik to DBT, giving top priority to user instructions and DBT best practices.
    
    Uses an agentic Self-Correction loop to automatically test and repair the generated code before return.
    """
    plan = plan if plan is not None else extract_sql_generation_plan(qvs_script or '')
    if not plan:
        return ''

    result = _apply_semantic_validation_loop(
        call_ai,
        qvs_script,
        session_context=session_context,
        current_sql=current_sql,
        current_desc=current_desc,
        dialect=dialect,
        plan=plan,
        plan_text=plan_text,
        prompt_version=prompt_version,
        description_style=description_style,
        max_iterations=5,
    )

    return result.get('final_sql', '')


def summarize_plan_for_description(plan):
    """Create a compact phrase for deterministic SQL description fallbacks."""
    if not plan:
        return "recreate the source Qlik transformation in DBT SQL"

    table_names = [item.get('table') for item in plan if item.get('table')]
    source_names = []
    field_names = []
    filters = []
    joins = []

    for item in plan:
        source_names.extend(item.get('source_tables') or [])
        field_names.extend(item.get('fields') or [])
        filters.extend(item.get('filters') or [])
        joins.extend(item.get('joins') or [])

    def unique_limited(values, limit):
        seen = []
        for value in values:
            value = str(value).strip()
            if value and value not in seen:
                seen.append(value)
            if len(seen) >= limit:
                break
        return seen

    tables = unique_limited(table_names, 3)
    sources = unique_limited(source_names, 3)
    fields = unique_limited(field_names, 5)

    target_phrase = ", ".join(tables) if tables else "the target model"
    purpose = f"build {target_phrase}"
    if sources:
        purpose += f" from {', '.join(sources)}"

    details = []
    if fields:
        details.append(f"selecting and renaming fields such as {', '.join(fields)}")
    if joins:
        details.append("preserving the source joins")
    if filters:
        details.append("applying the source filters")

    if details:
        return f"{purpose}. It does this by {', '.join(details)}"
    return purpose


def build_block_description_from_plan(plan, existing_description=''):
    """
    Build a Markdown description from the generation plan.

    If the AI already produced a rich description (existing_description), use it
    directly — only fall back to the plan-based template when the AI gave nothing.
    """
    ai_desc = (existing_description or '').strip()

    # If the AI gave us a real description (not just whitespace), trust it.
    # Only use the template when there is genuinely nothing to show.
    if ai_desc:
        # If it already has ## Block: structure, return as-is
        if re.search(r'(?im)^##\s+Block:', ai_desc):
            return ai_desc
        # If it's a multi-sentence paragraph, wrap it cleanly
        if len(ai_desc) > 120:
            return ai_desc

    # ── Deterministic fallback when AI returned nothing ──────────────────────
    if not plan:
        if ai_desc:
            return ai_desc
        return (
            "This model implements the Qlik-to-dbt migration.\n\n"
            "## Block: Generated SQL\n"
            "Review the SELECT list, joins, filters, and output columns to confirm the transformation details."
        )

    # Build a concise but informative plan-based description
    table_names = [item.get('table') for item in plan if item.get('table')]
    overview_tables = ', '.join(f'`{t}`' for t in table_names[:4])
    if len(table_names) > 4:
        overview_tables += f' and {len(table_names) - 4} more'
    overview = (
        f"This model migrates {len(plan)} Qlik LOAD block(s) "
        f"({overview_tables}) into dbt CTEs."
    )
    sections = [overview]

    for item in plan:
        table = item.get('table') or 'Generated SQL'
        sources = item.get('source_tables') or []
        fields = item.get('fields') or []
        joins = item.get('joins') or []
        filters = [f.strip() for f in (item.get('filters') or []) if f and f.strip()]
        is_concat = item.get('is_concatenate', False)

        lines = []
        if sources:
            src_list = ', '.join(f'`{s}`' for s in sources)
            lines.append(f"**Source:** {src_list}")
        if is_concat:
            lines.append("**Pattern:** CONCATENATE (UNION ALL append into previous CTE)")
        if fields:
            shown = fields[:10]
            field_list = ', '.join(f'`{f}`' for f in shown)
            suffix = f' *(+{len(fields) - 10} more)*' if len(fields) > 10 else ''
            lines.append(f"**Fields:** {field_list}{suffix}")
        if joins:
            lines.append(f"**Joins:** {', '.join(f'`{j}`' for j in joins)}")
        if filters:
            lines.append(f"**Filter:** `{filters[0]}`")

        body = '\n'.join(lines) if lines else '_No additional metadata extracted._'
        sections.append(f"## Block: {table}\n{body}")

    return "\n\n".join(sections)


def normalize_sql_description(description_text, plan=None):
    """
    Normalise the AI-generated description into the canonical ## Block: style.

    Strategy:
    - If the AI returned a rich description (>200 chars), trust it completely —
      do NOT overwrite it with the template. The AI knows what it generated.
    - If the AI returned something short or empty, use the plan-based fallback.
    - If the description already has ## Block: headers, pass it through unchanged.
    """
    raw = (description_text or '').strip()
    if not raw:
        return build_block_description_from_plan(plan)

    # Already structured — pass through
    if re.search(r'(?im)^##\s+Block:', raw):
        return raw

    # Rich AI description without block headers — trust it, just return it
    # (the AI wrote a proper narrative; don't replace it with boilerplate)
    if len(raw) > 200:
        return raw

    # Short/thin description — augment with plan structure
    return build_block_description_from_plan(plan, raw)


def _uses_gemini_prompt(prompt_version=''):
    return 'gemini' in str(prompt_version or '').lower()


def build_fast_sql_generation_prompt(
    qvs_script,
    dialect='dbt',
    plan=None,
    plan_text=None,
    prompt_version='',
    description_style='',
):
    """Build a compact SQL-first prompt for the fast one-shot migration call."""
    plan = plan if plan is not None else extract_sql_generation_plan(qvs_script)
    plan_text = plan_text if plan_text is not None else format_sql_generation_plan(plan)
    ir, ir_issues, _ir_contract, ir_prompt_summary = _build_ir_context(plan, qvs_script)
    join_contract = build_join_contract(plan, qvs_script)
    qvs_script = optimize_qvs_for_context(qvs_script, max_chars=12_000)
    target_dialect = (dialect or 'dbt').upper()

    system_prompt = f"""You are a Qlik LOAD to dbt SQL converter. Target dialect: {target_dialect}.

Return executable output immediately. Do not use Markdown fences. Do not begin with analysis, schema contracts, checklists, or questions.

Output exactly:
### SQL
{{{{ config(materialized='table', tags=['qlik_migration']) }}}}
WITH ...
SELECT ...

### DESCRIPTION
One concise technical paragraph. Do not write per-CTE documentation in one-shot mode.

Core migration rules:
- Convert every provided Qlik LOAD block into dbt SQL CTEs.
- Use lowercase_with_underscores CTE names.
- Use {{{{ source('raw', 'TableName') }}}} for raw sources.
- Convert Addmonths(d,n) to DATEADD(month, n, d).
- Convert Date(Addmonths(YYYYMM,n),'YYYYMM') to TO_CHAR(DATEADD(month, n, TO_DATE(YYYYMM::varchar, 'YYYYMM')), 'YYYYMM').
- Never pass raw YYYYMM to DATEADD; wrap raw YYYYMM with TO_DATE(YYYYMM::varchar, 'YYYYMM') when reading from a raw source.
- Convert Month(d) to TO_CHAR(d, 'Mon'), not MONTHNAME().
- Convert Qlik if(cond,a,b) to CASE WHEN cond THEN a ELSE b END.
- Convert text concat & to ||.
- Preserve source typos exactly, e.g. ExpeenseBudget AS "ExpenseBudget".
- Translate CONCATENATE loads as UNION ALL with identical columns in identical order.
- If UNION branches differ, add CAST(NULL AS appropriate_type) AS "missing_column".
- Keep Expenses as its own CTE/model with Account, ExpenseActual, and ExpenseBudget available for joins/output.
- facttable_with_expenses must include expense-owned columns in the UNION schema when Expenses is appended: Account, ExpenseActual, ExpenseBudget. Fact rows should emit typed NULLs for those columns.
- DROP FIELDS means dropped columns must not appear in that table's final SELECT list.
- Build final_model for dashboard output; do not leave the final SELECT reading only the raw fact CTE.
- final_model must be the only final SELECT source: end exactly with SELECT * FROM final_model.
- Every CTE you create must either feed another CTE/final_model or be omitted. Do not create unused CTEs.
- If a lookup/dimension CTE cannot be safely joined due to unclear keys, do not create that CTE. Prefer omitting unsafe lookup CTEs over creating unused CTEs.
- If no safe join contract exists for a lookup CTE, do not create that CTE.
- Do not reuse the same table alias for multiple CTEs.
- Before joining, verify every referenced alias.column exists in that CTE.
- For customer_map use alias cmap.
- For customer_master use alias cust.
- For item_branch_master use alias ibm.
- For item_master use alias im.
- For sales_rep_master use alias srm.
- Never join to a column that is not selected by the upstream CTE.
- Use ONLY the Required Join Contract paths provided in the prompt. Do not invent joins.
- Join dimensions explicitly in final_model only when the join key exists in BOTH aliases being joined; use LEFT JOIN.
- Before writing each JOIN, verify the selected alias actually exposes every column used in the ON condition.
- Never join columns only because their data types look compatible. Prefer exact shared field names or bridge paths from the generation plan / ownership notes.
- If a join key is unclear, do not invent a join. Leave a TODO SQL comment and do not reference unavailable fields.
- If ProductGroupMaster/ProductSubGroupMaster/ProductTypeMaster CTEs exist, join them through ItemBranchMaster -> ItemMaster -> product master keys. Never join FactTable.Item-Branch Key directly to ItemMaster.Short Name.
- If CustomerMap plus ARSummary/ARSummary_1 CTEs exist, join Fact/CustKey -> CustomerMap/CustKey -> ARSummary/CustKeyAR so AR measures are present.
- Never join Expenses to FactTable/FactTable_With_Expenses by MonthlyRegionKey only; Account equality is mandatory when joining expenses.
- End with a complete final SELECT. Never stop after a bare SELECT keyword.
- If a detail is ambiguous, add a SQL comment and continue. Never stop early.
"""

    prompt_parts = [
        f"### Qlik Script\n{qvs_script.strip()}",
        f"### Generation Plan\n{plan_text}",
        f"### Ownership / Grain Notes\n{ir_prompt_summary or 'Use the generation plan and Qlik script to infer source ownership.'}",
        "### Required Join Contract\n"
        + (join_contract.get('text') or "JOIN CONTRACT:\n- No validated join paths were derived."),
        "Use ONLY these join paths. Do not invent joins.",
        "Generate the complete dbt SQL now. Start with ### SQL on the first line.",
    ]
    if ir_issues:
        prompt_parts.insert(
            -1,
            "### Non-blocking Validation Notes\n"
            + "\n".join(f"- {issue}" for issue in _format_ir_issues_for_sql(ir_issues)),
        )

    return system_prompt, "\n\n".join(prompt_parts)


def build_sql_generation_prompt(
    qvs_script,
    session_context=None,
    current_sql=None,
    current_desc=None,
    dialect='dbt',
    plan=None,
    plan_text=None,
    prompt_version='',
    description_style='',
):
    """Construct a stricter two-pass prompt for DBT SQL generation (or Power BI M/DAX)."""
    original_script = qvs_script
    # Intelligently optimize context before extracting the plan or building the prompt.
    # Use a generous limit — the system prompt is now lean so we have budget for script.
    qvs_script = optimize_qvs_for_context(qvs_script, max_chars=20_000)

    def extract_qlik_script_blocks(script, max_blocks=6, max_chars=9_000):
        if not script:
            return ''

        blocks = extract_load_block_ast(script)
        if not blocks:
            return ''

        header = [
            '// Compact migration context generated from actual Qlik LOAD blocks only.',
            '// JSON metadata, SET/LET, and UI objects were excluded.',
            '',
        ]

        selected = []
        total = 0
        for block in blocks[:max_blocks]:
            raw_block = (block.get('raw') or '').strip()
            if not raw_block:
                raw_block = _render_load_block_as_sql(block)
            if len(raw_block) < 10:
                continue
            if total + len(raw_block) > max_chars and selected:
                break
            selected.append(raw_block)
            total += len(raw_block)

        return '\n\n'.join(header + selected)

    qvs_for_prompt = qvs_script
    compact_script = extract_qlik_script_blocks(original_script, max_blocks=30, max_chars=18_000)
    if compact_script:
        qvs_for_prompt = compact_script

    plan = plan if plan is not None else extract_sql_generation_plan(original_script)
    plan_text = plan_text if plan_text is not None else format_sql_generation_plan(plan)
    ir, ir_issues, ir_contract, ir_prompt_summary = _build_ir_context(plan, original_script)

    if not plan:
        return ("", "")

    single_block_mode = len(plan) == 1

    if (dialect or '').lower() == 'powerbi':
        return _build_powerbi_prompt(
            qvs_script,
            plan_text=plan_text,
            current_sql=current_sql,
            current_desc=current_desc,
            session_context=session_context,
            prompt_version=prompt_version,
            description_style=description_style,
        )

    target_dialect = dialect.upper()

    system_prompt = f"""You are a Qlik to dbt SQL migration expert.
Prompt version: {prompt_version}
Target dialect: {target_dialect}.

Your ONLY job: convert the exact Qlik LOAD blocks provided into a valid dbt SQL model using CTEs.
Ignore completely: JSON metadata, qMetaDef, dimensions, measures, visualizations, SET/LET variables, and any non-LOAD objects.

═══════════════════════════════════════════════════════
PRE-GENERATION SCHEMA CONTRACT (MUST BE EMITTED FIRST)
═══════════════════════════════════════════════════════
Before outputting any CTEs or SQL statements, write a compact commented contract block at the very top of your ### SQL output.
Keep it under 20 comment lines total. Do not exhaustively list every field.
1. SOURCE FIELD REGISTRY: Summarize only important shared/derived fields. Identify the source field for MonthlyRegionKey on both sides (e.g. Budget source is 'Month', FactTable is 'YYYYMM').
2. DATE FIELD TYPES: Enumerate YYYYMM tags ($date, integer/string stored) vs OrderDate ($date, date stored).
3. INTENTIONAL SOURCE TYPOS: Flag and document typos (e.g. Expenses.ExpeenseBudget aliased as ExpenseBudget) with a comment, and do not correct them.
4. ISLAND TABLE GRAINS: Record granularities for Budget (Region + Month), Expenses (Region + Account + Month), ARSummary (CustKeyAR snapshot).
If any contract detail cannot be resolved, add a concise -- CONTRACT ASSUMPTION: comment and continue generating the complete SQL. Never stop after the contract.

═══════════════════════════════════════════════════════
STRICT MIGRATION RULES
═══════════════════════════════════════════════════════
RULE 1 — NO DUPLICATE CTE NAMES
Every CTE name must be globally unique. If the same source is loaded twice, merge the logic into one CTE.
Do NOT append _v2/_v3 CTEs during repair; that indicates candidate corruption.

RULE 2 — FUNCTION MAPPING (replace ALL Qlik functions, zero allowed in output)
  Makedate(Y,M,D)    → MAKE_DATE(Y,M,D)
  Addmonths(d,n)     → DATEADD(month, n, d)
  monthstart(d)      → DATE_TRUNC('month', d)
  Month(d)           → TO_CHAR(d, 'Mon')   ← abbreviated "Jan"/"Feb", NOT MONTHNAME()
  num(x)             → CAST(x AS INTEGER)
  num(year(d))       → EXTRACT(YEAR FROM d)
  Date(d,'YYYYMM')   → TO_CHAR(d, 'YYYYMM')
  Date(d,'YYYY-MM-DD') → TO_CHAR(d, 'YYYY-MM-DD')
  if(cond,a,b)       → CASE WHEN cond THEN a ELSE b END
  len(x)             → LENGTH(x)
  left(x,n)          → LEFT(x, n)
  right(x,n)         → RIGHT(x, n)
  upper(x)           → UPPER(x)
  lower(x)           → LOWER(x)
  trim(x)            → TRIM(x)
  mid(x,s,n)         → SUBSTRING(x, s, n)
  floor(x)           → FLOOR(x)
  round(x,n)         → ROUND(x, n)
  isnull(x)          → x IS NULL
  text concat &      → ||

RULE 3 — YYYYMM CAST (NO EXCEPTIONS — applies in EVERY CTE)
YYYYMM is stored as integer/string (e.g. 201305). NEVER pass it raw to date functions.
  ✗ WRONG: DATEADD(month, 12, YYYYMM)
  ✗ WRONG: DATEADD(month, 12, YYYYMM::date)
  ✓ CORRECT: DATEADD(month, 12, TO_DATE(YYYYMM::varchar, 'YYYYMM'))
  ✓ CORRECT: TO_CHAR(DATEADD(month, 12, TO_DATE(YYYYMM::varchar, 'YYYYMM')), 'YYYYMM')
This applies in fact_table, expenses, calendar, budget — everywhere, no exceptions.

RULE 4 — MONTHLYREGIONKEY PATTERN
  Qlik: Region & '_' & Date(Addmonths(YYYYMM, 12), 'YYYYMM')
  SQL:  Region || '_' || TO_CHAR(DATEADD(month, 12, TO_DATE(YYYYMM::varchar, 'YYYYMM')), 'YYYYMM') AS MonthlyRegionKey

RULE 5 — CONCATENATE → UNION ALL INSIDE SAME CTE
  CONCATENATE (TableA) LOAD x,y RESIDENT TableB
  means append rows into TableA. Translate as UNION ALL inside the SAME CTE.
  Missing columns in appended branch = NULL AS "col_name".
  The source CTE referenced in UNION ALL must be defined BEFORE the CTE that uses it.

RULE 6 — DROP FIELDS → SPLIT INTO TWO CTEs
  DROP FIELDS col1, col2 FROM TableA means those columns must NOT appear in the final TableA CTE SELECT list.
  A column used to COMPUTE another column is not the same as selecting it — do not include it.
  BUT if TableA is used in a UNION ALL before the DROP, split into:
    table_a_for_fact → full columns including dropped ones (used in UNION ALL)
    table_a          → columns after drop (final version, no dropped cols)
  Concrete example — DROP FIELDS Region, YYYYMM FROM Expenses:
    ✗ WRONG:
      expenses AS (SELECT MonthlyRegionKey, Region, Account, ExpenseActual ...)
      -- Region appears in SELECT even though it was dropped
    ✓ CORRECT:
      expenses AS (SELECT MonthlyRegionKey, Account, ExpenseActual ...)
      -- Region was used to build MonthlyRegionKey but is NOT selected itself
      -- YYYYMM was used in DATEADD but is NOT selected itself

RULE 7 — CTE DEPENDENCY ORDER
  CTEs must be defined in dependency order.
  If CTE_B is used inside CTE_A (via UNION ALL or RESIDENT), CTE_B must appear BEFORE CTE_A.
  Always check all UNION ALL and RESIDENT references before ordering CTEs.

RULE 8 — CAST SYNTAX
  CORRECT: CAST(expression AS TYPE)
  WRONG:   CAST(MAKE_DATE(...) AS DATE) ← MAKE_DATE already returns DATE, no outer cast needed

RULE 9 — ARITHMETIC ON QUOTED COLUMNS
  WRONG:   "Fiscal Year" + 1
  CORRECT: CAST("Fiscal Year" AS INTEGER) + 1

RULE 10 — RESIDENT → CTE REFERENCE
  LOAD ... RESIDENT TableA → reference the already-defined CTE named table_a

RULE 11 — UNNAMED LOADS → skip or merge into the named CTE below

RULE 12 — FINAL SELECT
  Pick the fact table CTE (name contains "fact") or widest CTE. NOT the last CTE blindly.
  End with: SELECT * FROM <chosen_cte>
  Do NOT add joins in the final SELECT — joins happen in downstream mart models.

RULE 13 — SOURCE NAMING
  All raw file sources: {{{{ source('raw', 'TableName') }}}}

RULE 14 — CTE NAMES: lowercase_with_underscores

RULE 15 — EXACT FIELD ALIASES: keep exactly as authored in the Qlik script

RULE 16 — NO PLACEHOLDER TABLES
  NEVER use source_table, staging, temp, raw_data as table names.

RULE 17 — NO $(variable) SYNTAX
  Replace $(vTodaysDate) → DATE '2013-05-31'
  Replace $(vCurrentMonthNum) → 5
  Replace $(vCurrentYear) → 2013

RULE 18 — HISTORY FLAG DATE COMPARISON
  After YYYYMM is cast to DATE, comparisons must match types:
  CASE WHEN TO_DATE(YYYYMM::varchar,'YYYYMM') <= DATE_TRUNC('month', DATE '2013-05-31') THEN 1 ELSE 0 END

RULE 19 — COMPLETENESS — NEVER STOP EARLY
  Generate ALL CTEs from the script without stopping.
  Before writing ### DESCRIPTION, count your CTEs and verify every table in the
  Required CTEs list (provided in the user prompt) is present.
  Do not stop, truncate, or add comments like "-- remaining tables follow same pattern".
  If any CTEs are missing, add them before finishing.

RULE 20 — MONTH() FUNCTION → ABBREVIATED MONTH NAME
  Qlik's Month() returns abbreviated month names: "Jan", "Feb", "Mar", etc.
  MONTHNAME() in Snowflake returns full names: "January", "February" — this BREAKS
  downstream joins and filters that expect abbreviated values.
  ✗ WRONG: MONTHNAME(DATEADD(month, 12, TO_DATE(YYYYMM::varchar, 'YYYYMM'))) AS FiscalMonth
  ✓ CORRECT: TO_CHAR(DATEADD(month, 12, TO_DATE(YYYYMM::varchar, 'YYYYMM')), 'Mon') AS FiscalMonth
  Rule: ALWAYS use TO_CHAR(..., 'Mon') for Month() — never MONTHNAME().

RULE 21 — YYYYMM TYPE PROPAGATION ACROSS CTEs
  Once a CTE outputs YYYYMM as a DATE (via DATEADD or TO_DATE), any downstream CTE
  referencing that column already has a DATE — do NOT wrap it in TO_DATE() again.
  ✗ WRONG (history_flag reading from fact_table_with_expenses where YYYYMM is already DATE):
    CASE WHEN TO_DATE(YYYYMM::varchar, 'YYYYMM') <= DATE_TRUNC('month', DATE '2013-05-31') THEN 1 ELSE 0 END
  ✓ CORRECT:
    CASE WHEN YYYYMM <= DATE_TRUNC('month', DATE '2013-05-31')::DATE THEN 1 ELSE 0 END
  Rule of thumb: only apply TO_DATE(YYYYMM::varchar,'YYYYMM') when reading from a raw source.
  When reading from another CTE, use YYYYMM directly.

RULE 22 — TEXT COLUMN COMPARED TO NUMERIC LITERAL
  Qlik dual-types everything so "AccountDesc > 0" means "has a non-null, non-empty value".
  SQL databases do NOT dual-type — this comparison will error or silently return wrong results.
  ✗ WRONG: WHERE "AccountDesc" > 0
  ✗ WRONG: WHERE TRY_CAST("AccountDesc" AS INTEGER) > 0  ← drops valid text rows
  ✓ CORRECT: WHERE "AccountDesc" IS NOT NULL AND "AccountDesc" != ''
  Apply this to ANY column whose name ends in Desc, Name, Label, Code, Text,
  Title, Category, Type, or Status when compared to a numeric literal.
  The safe translation is ALWAYS the IS NOT NULL / != '' form unless the column
  is explicitly documented as containing only numeric strings.

RULE 23 — dbt CONFIG BLOCK
  Every generated dbt model MUST start with a {{ config(...) }} block.
  Minimum required:
    {{ config(materialized='table', tags=['qlik_migration']) }}
  Place it as the very first line before the WITH clause.

RULE 24 — QLIK ASSOCIATIVE MODEL → EXPLICIT SQL JOINS
  In Qlik, all loaded tables exist in memory and join automatically via shared field names.
  In SQL/dbt there is NO automatic joining — every relationship must be written explicitly.
  The final SELECT must JOIN all dimension CTEs to the fact CTE on their shared keys.
  ✗ WRONG: SELECT * FROM fact_table_with_expenses
    (silently drops all customer, product, calendar, AR data)
  ✓ CORRECT: Build a final mart CTE that LEFT JOINs every dimension:
    final_model AS (
      SELECT
        f.*,
        cal."Year", cal."FiscalMonth",
        cust."CustomerName",
        prod."ProductDesc"
      FROM fact_table_with_expenses f
      LEFT JOIN calendar cal ON f."YYYYMM" = cal."YYYYMM"
      LEFT JOIN customer_master cust ON f."CustomerKey" = cust."CustomerKey"
      LEFT JOIN item_master prod ON f."ItemKey" = prod."ItemKey"
      -- add all other dimension joins here
    )
  Identify the join keys by finding field names that appear in BOTH the fact CTE
  and a dimension CTE. Use LEFT JOIN so fact rows are never dropped.
  If a dimension has no obvious key match, add a comment: -- TODO: verify join key.

RULE 25 — SOURCE OWNERSHIP / JOIN / UNION VALIDATION
  The main bug to avoid: creating SQL from visible final fields without validating
  source ownership, join path, grain, or UNION compatibility first.
  Validate before returning:
  - every referenced column exists in the CTE/source alias that uses it
  - every JOIN key exists on both sides
  - every UNION ALL branch emits the same columns in the same order
  - grain-specific fields such as Account survive in the fact/union CTE before f.Account is referenced
  - unused CTEs are either removed or joined intentionally
  - row-multiplying grain mismatches are aggregated/pivoted before joining

RULE 26 — FIELD REGISTRY AND SOURCE NAME MAPPING
  Treat the Qlik ownership/grain contract as ground truth.
  Use exact raw source names inside source('raw', '<name>'), including names like ARSummary-1.
  Use clean stg_/int_/fct_/dim_ aliases only after the source() call. Preserve intentional
  source typos at the stg_* layer and alias them cleanly downstream.

RULE 27 — LAYERED DBT MODEL SHAPE
  Generate layered dbt SQL:
  - stg_* CTEs clean raw sources and preserve source ownership
  - int_* CTEs perform bridge/master joins, CONCATENATE/UNION, pivots, and grain alignment
  - fct_* and dim_* CTEs expose business-ready fact/dimension shapes
  - final mart CTE only after relationships are validated

RULE 28 — KNOWN ASSOCIATIVE JOIN PATHS AND GRAINS
  - facttable_with_expenses MUST include Account. In the UNION ALL:
    facttable branch emits CAST(NULL AS VARCHAR) AS "Account"; expenses branch emits "Account".
    UNION columns must be identical and in identical order.
  - Do not invent joins between unrelated fields. NEVER join f."CustKey" to cust."Customer";
    Customer is descriptive text, not a key. Use Customermap/CustomerMaster only via validated keys.
  - Product path is bridged, never direct:
    FactTable."Item-Branch Key" = ItemBranchMaster."Item-Branch Key"
    ItemBranchMaster."Short Name" = ItemMaster."Short Name"
    WRONG: FactTable."Item-Branch Key" = ItemMaster."Short Name"
  - Expenses joins at MonthlyRegionKey + Account, not MonthlyRegionKey alone.
  - Required associative joins: budget by MonthlyRegionKey; expenses by MonthlyRegionKey + Account;
    accounts by Account; customermap by CustKey; arsummary and ARSummary-1 by CustKeyAR;
    customeraddressmaster by Address Number; channelmaster by Segment; salesrepmaster by Sales Rep;
    itembranchmaster by Item-Branch Key; itemmaster by Short Name through itembranchmaster;
    product master tables by product keys.
  - ARSummary keeps source('raw', 'ARSummary-1') unless source.yml explicitly maps another name.
  - accountmaster/accountgroupmaster must be joined on validated keys or omitted.

{get_dialect_guidance(dialect)}

OUTPUT FORMAT — use EXACTLY these two headers, nothing else:

### SQL
[the complete dbt SQL model — no fences, no preamble]

### DESCRIPTION
Write expert-level technical Markdown.
Start with 1–2 sentences: what does this model do and what business question does it answer?
Then one ## Block: <cte_name> section per CTE, in order. For each block:
- What it does and WHY it exists (not just "it loads data")
- Source table(s) or upstream CTEs it reads from
- Key transformations: field renames, type casts, date arithmetic, CASE logic, aggregations
- Any filters and their business meaning
- How it feeds the next block or the final SELECT
Use `inline code` for field names and SQL expressions. Use **bold** for important terms.
Be specific — name the actual fields, expressions, and data flow. No generic boilerplate.
"""

    if single_block_mode:
        system_prompt += (
            "\n\nSingle-block mode: produce one SELECT statement only. "
            "Do not introduce extra CTEs, unions, or unrelated tables."
        )

    prompt_parts = [
        "### Source Qlik Scripts",
        f"```sql\n{qvs_for_prompt}\n```",
        "### Extracted Generation Plan",
        plan_text,
        "### Qlik Ownership / Grain Contract",
        ir_prompt_summary or ir_contract or "No IR contract available.",
    ]

    if ir_issues:
        prompt_parts.extend([
            "### Pre-generation Validation Issues",
            "\n".join(f"- {issue}" for issue in _format_ir_issues_for_sql(ir_issues)),
        ])

    # Inject expected CTE checklist so the model can self-verify completeness
    if plan and not single_block_mode:
        expected_ctes = [
            _safe_cte_name(item.get('table', ''))
            for item in plan
            if item.get('table')
            and not item.get('table', '').startswith("'")
            and 'lib://' not in item.get('table', '').lower()
            and not item.get('is_concatenate')
        ]
        # Deduplicate while preserving order
        seen = set()
        unique_ctes = []
        for c in expected_ctes:
            if c and c not in seen:
                seen.add(c)
                unique_ctes.append(c)
        if unique_ctes:
            prompt_parts.append(
                f"### Required CTEs — ALL {len(unique_ctes)} must appear in your output\n"
                + ", ".join(unique_ctes)
            )

    if current_desc and not single_block_mode:
        prompt_parts.extend([
            "### User Instructions / Description",
            current_desc,
        ])

    if current_sql and not single_block_mode:
        prompt_parts.extend([
            "### Current SQL Draft",
            f"```sql\n{current_sql}\n```",
        ])

    if session_context and not single_block_mode:
        prompt_parts.extend([
            "### Project Context",
            session_context,
        ])

    return system_prompt, "\n\n".join(prompt_parts)


def _build_powerbi_prompt(
    qvs_script,
    plan_text='',
    current_sql=None,
    current_desc=None,
    session_context=None,
    prompt_version='',
    description_style='',
):
    """
    Build a Power BI-specific prompt that produces:
      - Power Query M code  (data loading / transformation)
      - DAX measures        (calculations / KPIs)
      - A description

    Output sections use ### M QUERY, ### DAX, ### DESCRIPTION
    so the parser can split them correctly.
    """
    # Optimize Power BI script block to keep M transformation prompts light
    qvs_script = optimize_qvs_for_context(qvs_script, max_chars=35_000)
    system_prompt = f"""You are an expert Power BI developer and Analytics Engineer.
Prompt version: {prompt_version}

Your task is to migrate a legacy QlikView/QlikSense script into Power BI artifacts:
  1. Power Query M code  — for data loading and transformation
  2. DAX measures        — for calculated fields, aggregations, and KPIs

Qlik → Power BI mapping rules (follow strictly):
- Qlik LOAD field1, field2 FROM [lib://...] → Power Query: let Source = ..., #"Selected Columns" = Table.SelectColumns(Source, {{"field1","field2"}}) in #"Selected Columns"
- Qlik LOAD * RESIDENT TableName → Power Query: let Source = TableName in Source  (reference existing query)
- Qlik WHERE condition → Table.SelectRows(Source, each [Field] = value)
- Qlik AS alias → Table.RenameColumns(Source, {{{{"OldName","NewName"}}}})
- Qlik calculated field: Expr AS Name → DAX measure: Name = DAX_EQUIVALENT(Expr)
- Qlik GROUP BY + Sum(x) → Table.Group(Source, {{"GroupField"}}, {{{{"Total", each List.Sum([x]), type number}}}})
- Qlik JOIN → Table.NestedJoin(Left, {{"KeyField"}}, Right, {{"KeyField"}}, "JoinedData", JoinKind.Inner) then Table.ExpandTableColumn(...)
- Qlik LEFT JOIN → JoinKind.LeftOuter
- Qlik CONCATENATE → Table.Combine({{Table1, Table2}})
- Qlik SET vVar = 'value' → Power Query parameter or DAX VAR vVar = "value"
- Qlik IF(cond, a, b) → DAX IF(cond, a, b) or Power Query if cond then a else b
- Qlik Date functions (Today(), Now()) → DAX TODAY(), NOW() or Power Query DateTime.LocalNow()
- Qlik Num(x, '#,##0') → DAX FORMAT(x, "#,##0") or Power Query Number.ToText(x, "#,##0")
- Qlik Aggr(Sum(Sales), CustomerID) → DAX SUMX(VALUES(Table[CustomerID]), CALCULATE(SUM(Table[Sales])))
- Qlik Count(DISTINCT field) → DAX DISTINCTCOUNT(Table[field])

Hard rules:
- Do NOT output SQL SELECT statements.
- Every M query must be a complete, valid let...in block.
- Every DAX measure must be a standalone Name = Expression definition.
- Use descriptive step names in M (e.g., #"Filtered Active Rows", #"Renamed ID Column").
- If a Qlik expression has no direct Power BI equivalent, add a comment explaining the gap.
- Description style: {description_style}

Output format (use EXACTLY these section headers):
### M QUERY
(one complete M let...in block per Qlik table, separated by // --- TABLE: name ---)

### DAX
(one DAX measure definition per line, grouped by table)

### DESCRIPTION
(one-sentence overview, then ## Block: sections per table)
"""

    prompt_parts = [
        "### Source Qlik Script",
        f"```\n{qvs_script}\n```",
        "### Extracted Table Plan",
        plan_text,
    ]

    if current_desc:
        prompt_parts.extend(["### User Instructions", current_desc])

    if current_sql:
        prompt_parts.extend([
            "### Existing Draft (M Query / DAX)",
            f"```\n{current_sql}\n```",
        ])

    if session_context:
        prompt_parts.extend(["### Project Context", session_context])

    return system_prompt, "\n\n".join(prompt_parts)


def deduplicate_ctes(sql: str) -> str:
    """Deprecated compatibility hook.

    Duplicate CTEs are candidate-corruption bugs, not something to silently
    repair by appending _v2 names. Keep the SQL unchanged and let validation
    reject the candidate with DUPLICATE_CTE_NAME / REPAIR_CTE_SUFFIX_LEAK.
    """
    return sql

    # Legacy implementation kept below for reference during the transition.
    match = re.search(r'\bwith\b', sql, re.IGNORECASE)
    if not match:
        return sql
    
    with_start = match.start()
    pos = match.end()
    ctes = []
    
    cte_pattern = re.compile(r'\b(\w+)\s+as\s*\(', re.IGNORECASE)
    
    while True:
        m = cte_pattern.search(sql, pos)
        if not m:
            break
        
        cte_name = m.group(1)
        body_start = m.end()
        
        depth = 1
        i = body_start
        while i < len(sql) and depth > 0:
            if sql[i] == '(':
                depth += 1
            elif sql[i] == ')':
                depth -= 1
            i += 1
        
        if depth == 0:
            body_end = i - 1
            cte_body = sql[body_start:body_end]
            ctes.append({
                'name': cte_name,
                'def_start': m.start(),
                'def_end': i,
                'body': cte_body
            })
            pos = i
        else:
            break

    if not ctes:
        return sql

    seen_names = {}
    duplicates_exist = False
    for cte in ctes:
        name = cte['name'].lower()
        seen_names[name] = seen_names.get(name, 0) + 1
        if seen_names[name] > 1:
            duplicates_exist = True

    if not duplicates_exist:
        return sql

    rename_map = {}
    current_counts = {}
    total_counts = {}
    for cte in ctes:
        name_lower = cte['name'].lower()
        total_counts[name_lower] = total_counts.get(name_lower, 0) + 1

    for idx, cte in enumerate(ctes):
        name = cte['name']
        name_lower = name.lower()
        if total_counts[name_lower] > 1:
            count = current_counts.get(name_lower, 0) + 1
            current_counts[name_lower] = count
            if count == 1:
                new_name = name
            else:
                new_name = f"{name}_v{count}"
            rename_map[idx] = new_name
        else:
            rename_map[idx] = name

    active_mapping = {}
    new_ctes_code = []
    
    for idx, cte in enumerate(ctes):
        old_name = cte['name']
        old_name_lower = old_name.lower()
        new_name = rename_map[idx]
        
        body = cte['body']
        for ref_old_lower, active_ref in active_mapping.items():
            if ref_old_lower != active_ref.lower():
                body = re.sub(rf'\b{ref_old_lower}\b', active_ref, body, flags=re.IGNORECASE)
            
        active_mapping[old_name_lower] = new_name
        new_ctes_code.append((new_name, body))
        
    ctes_str = "WITH " + ",\n".join(f"{name} AS ({body})" for name, body in new_ctes_code)
    
    last_cte = ctes[-1]
    final_query = sql[last_cte['def_end']:]
    
    for ref_old_lower, active_ref in active_mapping.items():
        if ref_old_lower != active_ref.lower():
            final_query = re.sub(rf'\b{ref_old_lower}\b', active_ref, final_query, flags=re.IGNORECASE)
        
    prefix = sql[:ctes[0]['def_start']]
    with_match = re.search(r'\bwith\b', prefix, re.IGNORECASE)
    if with_match:
        prefix = prefix[:with_match.start()]
        
    return prefix + ctes_str + final_query


def request_sql_repair(
    call_ai,
    sql_text,
    description_text,
    issues,
    dialect='dbt',
    description_style='',
    prompt_version='',
    qvs_script=None,
    plan_text=None,
):
    """Ask the model for a minimal repair pass when validation flags problems."""
    system_prompt = f"""You are a Qlik to dbt SQL migration repair expert.
Your ONLY job: take the broken/incorrect SQL and fix it using the original Qlik script, the generation plan, and the strict rules below.

Fix the SQL with the smallest possible change to address the validation issues, while maintaining all strict rules.
Do not rewrite business logic that is already correct.
Do not invent new tables or joins.
Target dialect: {dialect.upper()}.
Prompt version: {prompt_version}

═══════════════════════════════════════════════════════
STRICT RULES — violating any rule is unacceptable
═══════════════════════════════════════════════════════
RULE 1 — NO DUPLICATE CTE NAMES.

RULE 2 — FUNCTION MAPPING: Replace ALL Qlik functions.
   - Makedate(Y,M,D)    → MAKE_DATE(Y,M,D)
   - monthstart(d)      → DATE_TRUNC('month', d)
   - Addmonths(d,n)     → DATEADD(month, n, d)
   - Month(d)           → TO_CHAR(d, 'Mon')   ← abbreviated name, NOT MONTHNAME()
   - Num(x)             → CAST(x AS INTEGER)
   - num(year(d))       → EXTRACT(YEAR FROM d)
   - Date(d,'YYYYMM')   → TO_CHAR(d, 'YYYYMM')
   - if(cond,a,b)       → CASE WHEN cond THEN a ELSE b END
   - len(x)             → LENGTH(x)
   - text concat &      → ||

RULE 3 — YYYYMM CAST (NO EXCEPTIONS — applies in EVERY CTE)
YYYYMM is stored as integer/string (e.g. 201305). NEVER pass it raw to date functions.
   ✗ WRONG: DATEADD(month, 12, YYYYMM)
   ✗ WRONG: DATEADD(month, 12, YYYYMM::date)
   ✓ CORRECT: DATEADD(month, 12, TO_DATE(YYYYMM::varchar, 'YYYYMM'))
   ✓ CORRECT: TO_CHAR(DATEADD(month, 12, TO_DATE(YYYYMM::varchar, 'YYYYMM')), 'YYYYMM')
This applies in fact_table, expenses, calendar, budget — everywhere, no exceptions.

RULE 4 — MONTHLYREGIONKEY PATTERN
   Qlik: Region & '_' & Date(Addmonths(YYYYMM, 12), 'YYYYMM')
   SQL:  Region || '_' || TO_CHAR(DATEADD(month, 12, TO_DATE(YYYYMM::varchar, 'YYYYMM')), 'YYYYMM') AS MonthlyRegionKey

RULE 5 — CONCATENATE → UNION ALL inside the SAME CTE. Missing columns in the appended branch = NULL AS "col_name". The source CTE must be defined BEFORE the target CTE in the WITH block.

RULE 6 — DROP FIELDS → SPLIT INTO TWO CTEs
   DROP FIELDS col FROM T → exclude col from T's standalone SELECT only; keep in UNION ALL branches that reference T.
   A column used to COMPUTE another column is not the same as selecting it — do not include it.
   If T is used in a UNION ALL before the DROP, split into:
     t_for_fact → full columns including dropped ones (used in UNION ALL)
     t          → columns after drop (final version)
   Concrete example — DROP FIELDS Region, YYYYMM FROM Expenses:
     ✗ WRONG:
       expenses AS (SELECT MonthlyRegionKey, Region, Account, ExpenseActual ...)
     ✓ CORRECT:
       expenses AS (SELECT MonthlyRegionKey, Account, ExpenseActual ...)
       -- Region built MonthlyRegionKey but is NOT selected; YYYYMM used in DATEADD but NOT selected

RULE 7 — CTE DEPENDENCY ORDER
   CTEs must be defined in dependency order.
   If CTE_B is used inside CTE_A (via UNION ALL or RESIDENT), CTE_B must appear BEFORE CTE_A.

RULE 8 — CAST SYNTAX: CAST(col AS TYPE). MAKE_DATE() already returns DATE — no outer CAST needed.

RULE 9 — ARITHMETIC ON QUOTED COLUMNS: CAST("Col" AS INTEGER) + 1, not "Col"+1.

RULE 10 — RESIDENT loads must reference previously defined CTEs.

RULE 11 — FINAL SELECT: pick the fact table CTE (name contains 'fact') or widest CTE — NOT the last CTE blindly.

RULE 12 — NO $(variable) SYNTAX: Replace all Qlik variables with their literal SQL values.
   vTodaysDate → DATE '2013-05-31', vCurrentMonthNum → 5, vCurrentYear → 2013

RULE 13 — HISTORY FLAG DATE COMPARISON
   CASE WHEN TO_DATE(YYYYMM::varchar,'YYYYMM') <= DATE_TRUNC('month', DATE '2013-05-31') THEN 1 ELSE 0 END

RULE 14 — MONTH() FUNCTION → ABBREVIATED MONTH NAME
   Qlik's Month() returns abbreviated names: "Jan", "Feb", etc.
   MONTHNAME() returns full names ("January") — this breaks downstream joins.
   ✗ WRONG: MONTHNAME(DATEADD(month, 12, TO_DATE(YYYYMM::varchar, 'YYYYMM'))) AS FiscalMonth
   ✓ CORRECT: TO_CHAR(DATEADD(month, 12, TO_DATE(YYYYMM::varchar, 'YYYYMM')), 'Mon') AS FiscalMonth

RULE 14b — TEXT COLUMN COMPARED TO NUMERIC LITERAL
   Qlik dual-types everything so "AccountDesc > 0" means "has a non-null, non-empty value".
   ✗ WRONG: WHERE "AccountDesc" > 0
   ✗ WRONG: WHERE TRY_CAST("AccountDesc" AS INTEGER) > 0  ← drops valid text rows
   ✓ CORRECT: WHERE "AccountDesc" IS NOT NULL AND "AccountDesc" != ''

RULE 14c — QLIK ASSOCIATIVE MODEL → EXPLICIT SQL JOINS
   In Qlik all tables join automatically via shared field names. In SQL they do not.
   The final SELECT must LEFT JOIN every dimension CTE to the fact CTE on shared keys.
   ✗ WRONG: SELECT * FROM fact_table_with_expenses  (drops all dimension data)
   ✓ CORRECT: Build a final_model CTE that LEFT JOINs calendar, customers, products, etc.

RULE 15 — DO NOT REMOVE CTEs DURING REPAIR
   Only fix the specific errors reported in the validation issues.
   Do NOT remove, truncate, collapse, or simplify any CTEs that are not mentioned in the issues.
   The repaired SQL must contain AT LEAST as many CTEs as the input SQL.
   If the input has ar_summary, ar_summary_1, history_flag, or any other CTE — they must all appear in the output.
   Keep valid business joins while fixing invalid joins. Do not drop budget, expenses,
   account, customermap, ARSummary, customeraddressmaster, channelmaster, salesrepmaster,
   itembranchmaster, itemmaster, or product-master joins just to pass validation.

RULE 16 — YYYYMM TYPE PROPAGATION ACROSS CTEs
   Once a CTE outputs YYYYMM as a DATE (via DATEADD or TO_DATE), any downstream CTE
   referencing that column already has a DATE — do NOT wrap it in TO_DATE() again.
   ✗ WRONG (history_flag reading from fact_table_with_expenses where YYYYMM is already DATE):
     CASE WHEN TO_DATE(YYYYMM::varchar, 'YYYYMM') <= DATE_TRUNC('month', DATE '2013-05-31') THEN 1 ELSE 0 END
   ✓ CORRECT:
     CASE WHEN YYYYMM <= DATE_TRUNC('month', DATE '2013-05-31')::DATE THEN 1 ELSE 0 END
   Rule of thumb: only apply TO_DATE(YYYYMM::varchar,'YYYYMM') when reading from a raw source.
   When reading from another CTE, use YYYYMM directly.

RULE 17 — REPAIR KNOWN JOIN/UNION BUGS WITHOUT REMOVING VALID RELATIONSHIPS
   - dbt config must be exactly: {{ config(materialized='table', tags=['qlik_migration']) }}
   - Keep Expenses as its own CTE/model with Account, ExpenseActual, and ExpenseBudget available.
   - facttable_with_expenses UNION must include Account, ExpenseActual, and ExpenseBudget when Expenses is appended; fact rows use typed NULLs and every branch keeps identical column order.
   - Never join CustKey to Customer or any key field to descriptive text.
   - Product joins must route through ItemBranchMaster before ItemMaster.
   - Product master descriptions must join from ItemMaster keys when ProductGroupMaster/ProductSubGroupMaster/ProductTypeMaster CTEs exist.
   - Expenses joins must include MonthlyRegionKey + Account.
   - ARSummary and ARSummary_1 must be joined through CustomerMap/CustKeyAR when those CTEs exist.
   - Preserve source('raw', 'ARSummary-1') exactly unless source.yml maps another name.

Output format (use EXACTLY these headers):
### SQL
```sql
[repaired SQL model]
```

### Description
Write expert-level technical Markdown.
Start with 1–2 sentences: what does this model do and what business question does it answer?
Then one ## Block: <cte_name> section per CTE, in order. For each block:
- What it does and WHY it exists
- Source table(s) or upstream CTEs it reads from
- Key transformations: field renames, type casts, date arithmetic, CASE logic, aggregations
- Any filters and their business meaning
- How it feeds the next block or the final SELECT
Use `inline code` for field names and SQL expressions. Use **bold** for important terms.
Be specific — name the actual fields, expressions, and data flow. No generic boilerplate.
"""
    prompt_parts = []
    if qvs_script:
        prompt_parts.append(f"### Original Source Qlik Scripts\n```sql\n{qvs_script}\n```")
    if plan_text:
        prompt_parts.append(f"### Target Generation Plan\n{plan_text}")

    prompt_parts.extend([
        f"### Broken SQL\n```sql\n{sql_text}\n```",
        f"### Existing Description\n{description_text or ''}",
        f"### Validation Issues to Fix\n" + "\n".join(f"- {issue}" for issue in issues),
        "### Instructions\nProvide the corrected SQL and Description following the output format. Ensure the final SQL does not contain any Qlik functions, duplicate CTEs, or broken CAST syntax."
    ])
    prompt = "\n\n".join(prompt_parts)
    return _invoke_ai_text(
        call_ai,
        prompt,
        system_prompt=system_prompt,
        max_tokens=REPAIR_MAX_TOKENS,
        max_prompt_chars=12_000,
        phase='repair',
        min_tokens=MIN_REPAIR_SQL_TOKENS,
    )
