import hashlib
import json
import re
from backend.qvf_runtime import extract_model_from_script
from backend.qlik_script_parser import parse_qlik_load_script


def hash_text(text):
    return hashlib.sha256((text or '').encode('utf-8')).hexdigest()


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

    for char in field_text:
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
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

    # Pad missing columns with NULL so UNION ALL branches stay column-aligned
    if required_columns:
        for col in required_columns:
            if col.lower() not in produced_columns:
                rendered_fields.append(f'NULL AS "{col}"')

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
        return result

    text = ai_response.strip()

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
                return result
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

        return result

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
        return result

    if len(parts) == 3:
        result['sql'] = parts[1].strip()
        result['sql'] = re.sub(r'^```sql\s*|```$', '', result['sql'], flags=re.IGNORECASE | re.MULTILINE).strip()
        result['description'] = parts[2].strip()
        return result

    if len(parts) == 2:
        body = parts[1].strip()
        if re.search(r'\bWITH\b|\bSELECT\b', body, re.IGNORECASE):
            result['sql'] = body
        else:
            result['description'] = body
        return result

    if re.search(r'\bWITH\b|\bSELECT\b', text, re.IGNORECASE):
        result['sql'] = text
    else:
        result['description'] = text

    return result


def validate_generated_sql(sql_text, plan=None, dialect='dbt'):
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

    # Qlik variable syntax $(varName) must never appear in generated SQL
    if re.search(r'\$\([A-Za-z_][A-Za-z0-9_]*\)', content):
        issues.append('Qlik variable syntax $(variable) detected in SQL — replace with literal SQL values.')

    if plan:
        referenced_sources = set()
        for item in plan:
            referenced_sources.update({s.lower() for s in item.get('source_tables', []) if s})
        if referenced_sources and not any(source in content.lower() for source in referenced_sources):
            issues.append('Generated SQL does not reference any extracted source tables.')

    return issues


def needs_sql_repair(issues):
    repair_markers = (
        'empty',
        'unbalanced',
        'trailing comma',
        'WITH appears without a following CTE body',
        'does not appear to contain a SELECT or WITH clause',
        'Qlik variable syntax $(variable)',
        'SHELL_OPERATOR',
    )
    return any(any(marker.lower() in issue.lower() for marker in repair_markers) for issue in issues)


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
    q_tables = set(qlik_description.get('source_tables', []))
    s_tables = set(sql_description.get('source_tables', []))
    if q_tables and s_tables and q_tables != s_tables:
        # Only flag if both sides have data — avoids false positives when
        # the SQL uses {{ source() }} refs that don't match raw Qlik file names
        missing_in_sql = q_tables - s_tables
        if missing_in_sql:
            differences.append({
                'type': 'SOURCE_TABLE_MISMATCH',
                'qlik': sorted(q_tables),
                'sql': sorted(s_tables),
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

    prompt_parts = [
        "### Source Qlik Script",
        f"```sql\n{qvs_script}\n```",
        "### Extracted Qlik Description",
        json.dumps(qlik_description, indent=2),
        "### Generated SQL Draft",
        f"```sql\n{previous_sql or ''}\n```",
        "### Generated SQL Description",
        json.dumps(sql_description, indent=2),
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

        print(f"[Senior AI/ML Agent] Iteration {iteration}/{max_iterations} generating SQL...")
        if progress_callback:
            progress_callback(f"Iteration {iteration}/{max_iterations}: calling AI model...")

        iter_max_prompt_chars = 25_000 if iteration == 1 else 30_000
        iter_max_tokens = 8000

        # Stream tokens on the very last AI call (either ceiling hit or we'll
        # check match after and it might be the final one).
        # We stream on the last possible iteration; if we match earlier we also
        # stream that call so the user always sees live output on the final pass.
        should_stream = (stream_callback is not None) and is_last_possible

        if should_stream:
            if progress_callback:
                progress_callback(f"Iteration {iteration}/{max_iterations}: streaming final SQL from AI...")
            chunks = []
            for token in call_ai(
                prompt,
                system_prompt=system_prompt,
                temperature=0,
                top_p=1,
                max_tokens=iter_max_tokens,
                max_prompt_chars=iter_max_prompt_chars,
                stream=True,
            ):
                chunks.append(token)
                stream_callback(token)
            ai_response = ''.join(chunks)
        else:
            ai_response = call_ai(
                prompt,
                system_prompt=system_prompt,
                temperature=0,
                top_p=1,
                max_tokens=iter_max_tokens,
                max_prompt_chars=iter_max_prompt_chars,
            )

        if progress_callback:
            progress_callback(f"Iteration {iteration}/{max_iterations}: AI responded, validating structure...")

        structured_output = parse_migration_response(ai_response)
        if structured_output.get('sql'):
            structured_output['sql'] = deduplicate_ctes(structured_output['sql'])

        # Reject stub/empty responses — fall back to deterministic SQL
        sql_candidate = (structured_output.get('sql') or '').strip()
        is_stub = (
            not sql_candidate
            or sql_candidate.startswith('--')
            or len(sql_candidate) < 80
            or not re.search(r'\bSELECT\b|\bWITH\b', sql_candidate, re.IGNORECASE)
        )
        if is_stub:
            print(f"[Senior AI/ML Agent] Iteration {iteration} returned stub ({len(sql_candidate)} chars) — using deterministic fallback")
            if deterministic_sql:
                structured_output['sql'] = deterministic_sql
                structured_output['description'] = build_block_description_from_plan(plan, current_desc or '')
            if progress_callback:
                progress_callback(f"Iteration {iteration}/{max_iterations}: AI returned empty output, using deterministic SQL fallback...")

        # Structural repair pass
        validation_issues = validate_generated_sql(structured_output.get('sql', ''), plan, dialect)
        if validation_issues and needs_sql_repair(validation_issues):
            print(f"[Senior AI/ML Agent] Structural validation failed. Performing repair pass...")
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
                    repaired_structured['sql'] = deduplicate_ctes(repaired_structured['sql'])
                    structured_output = repaired_structured
                    if progress_callback:
                        progress_callback(f"Iteration {iteration}/{max_iterations}: repair complete, re-validating...")
            except Exception as repair_err:
                print(f"[Senior AI/ML Agent] Structural self-repair failed: {repair_err}")

        # Semantic comparison
        sql_description = describe_sql(structured_output.get('sql', ''))
        comparison = compare_descriptions(qlik_description, sql_description)
        score = comparison.get('score', 0.0)
        diffs = comparison.get('differences', [])
        matched = comparison.get('matched', False)

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

        print(f"[Senior AI/ML Agent] Iteration {iteration}/{max_iterations} score={score:.2f} matched={matched} diffs={len(diffs)}")

        if matched:
            best_result['status'] = 'matched'
            best_result['iterations'] = iteration
            return best_result

        if is_last_possible:
            # Use deterministic SQL if it scores better than the AI output
            fallback_description = describe_sql(deterministic_sql)
            fallback_comparison = compare_descriptions(qlik_description, fallback_description)
            if deterministic_sql and fallback_comparison.get('score', 0) >= score:
                best_result.update({
                    'final_sql': deterministic_sql,
                    'final_description': build_block_description_from_plan(plan, current_desc or structured_output.get('description', '')),
                    'sql_description': fallback_description,
                    'comparison': fallback_comparison,
                    'score': fallback_comparison.get('score', 0),
                    'used_deterministic_fallback': True,
                })
            best_result['status'] = 'retry'
            best_result['iterations'] = iteration
            if progress_callback:
                progress_callback(
                    f"⚠️ Reached max iterations ({max_iterations}). "
                    f"Best score: {best_result['score']:.2f}. Using best result."
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

    if stream_callback is not None:
        # Stream tokens to the frontend in real time
        chunks = []
        for token in call_ai(
            prompt,
            system_prompt=system_prompt,
            temperature=0,
            top_p=1,
            max_tokens=8000,
            stream=True,
        ):
            chunks.append(token)
            stream_callback(token)
        ai_response = ''.join(chunks)
    else:
        ai_response = call_ai(
            prompt,
            system_prompt=system_prompt,
            temperature=0,
            top_p=1,
            max_tokens=8000,
        )
    structured_output = parse_migration_response(ai_response)
    final_sql = (structured_output.get('sql') or '').strip()
    final_desc = (structured_output.get('description') or '').strip()

    # Treat comment-only / stub responses as empty — fall back to deterministic SQL
    is_stub = (
        not final_sql
        or final_sql.startswith('--')
        or len(final_sql) < 80
        or not re.search(r'\bSELECT\b|\bWITH\b', final_sql, re.IGNORECASE)
    )
    if is_stub and plan:
        if progress_callback:
            progress_callback('AI returned stub/empty SQL; using deterministic SQL fallback.')
        print(f"[one-shot] AI returned stub ({len(final_sql)} chars) — using deterministic fallback")
        final_sql = deduplicate_ctes(render_sql_from_load_plan(plan))
        if not final_desc:
            final_desc = build_block_description_from_plan(plan, current_desc or '')
    elif not final_sql and plan:
        if progress_callback:
            progress_callback('AI did not return a valid SQL block; using deterministic SQL fallback.')
        final_sql = deduplicate_ctes(render_sql_from_load_plan(plan))
        if not final_desc:
            final_desc = build_block_description_from_plan(plan, current_desc or '')

    final_sql = deduplicate_ctes(final_sql)
    return {
        'status': 'complete' if final_sql else 'failed',
        'iterations': 1,
        'final_sql': final_sql,
        'sql': final_sql,
        'description': final_desc,
        'qlik_description': describe_qlik_script(qvs_script),
        'sql_description': describe_sql(final_sql),
        'comparison_summary': {},
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


def build_fast_sql_generation_prompt(
    qvs_script,
    dialect='dbt',
    plan=None,
    plan_text=None,
    prompt_version='',
    description_style='',
):
    """Build a focused prompt for a single fast LLM call that still produces real SQL."""
    # Extract plan from full script BEFORE truncating for context
    plan = plan if plan is not None else extract_sql_generation_plan(qvs_script)
    plan_text = plan_text if plan_text is not None else format_sql_generation_plan(plan)
    qvs_script = optimize_qvs_for_context(qvs_script, max_chars=12_000)
    target_dialect = (dialect or 'dbt').upper()

    system_prompt = f"""You are a Qlik to dbt SQL migration expert. Target dialect: {target_dialect}.

Your ONLY job: convert the exact Qlik LOAD blocks provided into a valid dbt SQL model using CTEs.
Ignore completely: JSON metadata, qMetaDef, dimensions, measures, visualizations, SET/LET variables, and any non-LOAD objects.

RULE 1 — NO DUPLICATE CTE NAMES
Every CTE name must be globally unique. If same source loaded twice, merge or suffix _v2.

RULE 2 — FUNCTION MAPPING (replace ALL Qlik functions, zero allowed in output)
  Makedate(Y,M,D)    → MAKE_DATE(Y,M,D)
  Addmonths(d,n)     → DATEADD(month, n, d)
  monthstart(d)      → DATE_TRUNC('month', d)
  Month(d)           → MONTHNAME(d)
  num(x)             → CAST(x AS INTEGER)
  num(year(d))       → EXTRACT(YEAR FROM d)
  Date(d,'YYYYMM')   → TO_CHAR(d, 'YYYYMM')
  if(cond,a,b)       → CASE WHEN cond THEN a ELSE b END
  len(x)             → LENGTH(x)
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
  Do NOT add joins in the final SELECT.

RULE 13 — SOURCE NAMING
  All raw file sources: {{{{ source('raw', 'TableName') }}}}

RULE 14 — CTE NAMES: lowercase_with_underscores

RULE 15 — EXACT FIELD ALIASES: keep exactly as authored in the Qlik script

RULE 16 — NO PLACEHOLDER TABLES

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

RULE 20 — FISCALMONTH FROM MONTH() FUNCTION
  Qlik: Month(addmonths(YYYYMM, 12)) as FiscalMonth
  SQL:  MONTHNAME(DATEADD(month, 12, TO_DATE(YYYYMM::varchar, 'YYYYMM'))) AS FiscalMonth

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
  Qlik allows comparing text fields to numbers (e.g. AccountDesc > 0) because it
  dual-types everything. SQL databases do NOT — this will either error or silently
  return wrong results.
  ✗ WRONG: WHERE "AccountDesc" > 0
  ✓ CORRECT (null/empty check): WHERE "AccountDesc" IS NOT NULL AND "AccountDesc" != ''
  ✓ CORRECT (numeric content): WHERE TRY_CAST("AccountDesc" AS INTEGER) > 0
  Apply this fix to ANY column whose name ends in Desc, Name, Label, Code, Text,
  Title, Category, Type, or Status when compared to a numeric literal.

RULE 23 — dbt CONFIG BLOCK
  Every generated dbt model MUST start with a {{ config(...) }} block.
  Minimum required:
    {{ config(materialized='table', tags=['qlik_migration']) }}
  Place it as the very first line before the WITH clause.

Output format — use EXACTLY these two headers:
### SQL
[complete dbt SQL — no fences]

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

    prompt_parts = [
        f"### Qlik Script\n{qvs_script.strip()}",
        f"### Generation Plan\n{plan_text}",
        "Generate the dbt SQL model now.",
    ]

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

RULE 1 — NO DUPLICATE CTE NAMES
Every CTE name must be globally unique. If same source loaded twice, merge or suffix _v2.

RULE 2 — FUNCTION MAPPING (replace ALL Qlik functions, zero allowed in output)
  Makedate(Y,M,D)    → MAKE_DATE(Y,M,D)
  Addmonths(d,n)     → DATEADD(month, n, d)
  monthstart(d)      → DATE_TRUNC('month', d)
  Month(d)           → MONTHNAME(d)
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

RULE 20 — FISCALMONTH FROM MONTH() FUNCTION
  Qlik: Month(addmonths(YYYYMM, 12)) as FiscalMonth
  SQL:  MONTHNAME(DATEADD(month, 12, TO_DATE(YYYYMM::varchar, 'YYYYMM'))) AS FiscalMonth

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
  Qlik allows comparing text fields to numbers (e.g. AccountDesc > 0) because it
  dual-types everything. SQL databases do NOT — this will either error or silently
  return wrong results.
  ✗ WRONG: WHERE "AccountDesc" > 0
  ✓ CORRECT (null/empty check): WHERE "AccountDesc" IS NOT NULL AND "AccountDesc" != ''
  ✓ CORRECT (numeric content): WHERE TRY_CAST("AccountDesc" AS INTEGER) > 0
  Apply this fix to ANY column whose name ends in Desc, Name, Label, Code, Text,
  Title, Category, Type, or Status when compared to a numeric literal.

RULE 23 — dbt CONFIG BLOCK
  Every generated dbt model MUST start with a {{ config(...) }} block.
  Minimum required:
    {{ config(materialized='table', tags=['qlik_migration']) }}
  Place it as the very first line before the WITH clause.

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
    ]

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
    """Automatically deduplicate duplicate CTE definitions in the generated SQL.
    Ensures each CTE name is unique and updates references in subsequent CTEs and final SELECT.
    """
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
   - Month(d)           → MONTHNAME(d)
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

RULE 14 — FISCALMONTH FROM MONTH() FUNCTION
   Qlik: Month(addmonths(YYYYMM, 12)) as FiscalMonth
   SQL:  MONTHNAME(DATEADD(month, 12, TO_DATE(YYYYMM::varchar, 'YYYYMM'))) AS FiscalMonth

RULE 15 — DO NOT REMOVE CTEs DURING REPAIR
   Only fix the specific errors reported in the validation issues.
   Do NOT remove, truncate, collapse, or simplify any CTEs that are not mentioned in the issues.
   The repaired SQL must contain AT LEAST as many CTEs as the input SQL.
   If the input has ar_summary, ar_summary_1, history_flag, or any other CTE — they must all appear in the output.

RULE 16 — YYYYMM TYPE PROPAGATION ACROSS CTEs
   Once a CTE outputs YYYYMM as a DATE (via DATEADD or TO_DATE), any downstream CTE
   referencing that column already has a DATE — do NOT wrap it in TO_DATE() again.
   ✗ WRONG (history_flag reading from fact_table_with_expenses where YYYYMM is already DATE):
     CASE WHEN TO_DATE(YYYYMM::varchar, 'YYYYMM') <= DATE_TRUNC('month', DATE '2013-05-31') THEN 1 ELSE 0 END
   ✓ CORRECT:
     CASE WHEN YYYYMM <= DATE_TRUNC('month', DATE '2013-05-31')::DATE THEN 1 ELSE 0 END
   Rule of thumb: only apply TO_DATE(YYYYMM::varchar,'YYYYMM') when reading from a raw source.
   When reading from another CTE, use YYYYMM directly.

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
    return call_ai(prompt, system_prompt=system_prompt, temperature=0, top_p=1, max_tokens=8000, max_prompt_chars=12_000)
