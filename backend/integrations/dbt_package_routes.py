import json
import os
import re
import shutil
import textwrap
import zipfile

from flask import jsonify, send_file


# ─── Schema YAML generation ───────────────────────────────────────────────────

def _safe_yaml_str(value):
    """Wrap a string in double-quotes if it contains YAML-unsafe characters."""
    value = str(value or '')
    if any(c in value for c in (':', '#', '{', '}', '[', ']', ',', '&', '*', '?', '|', '-', '<', '>', '=', '!', '%', '@', '`', '"', "'")):
        return '"' + value.replace('"', '\\"') + '"'
    return value or '""'


def _infer_column_type(field_name, field_type=None):
    """Map a Qlik field name / type hint to a dbt-friendly SQL type."""
    raw_type = (field_type or '').lower()
    if raw_type in ('date', 'timestamp', 'datetime'):
        return 'date'
    if raw_type in ('integer', 'int', 'bigint', 'number', 'numeric', 'float', 'double'):
        return 'number'
    if raw_type in ('boolean', 'bool'):
        return 'boolean'

    name_lower = field_name.lower()
    if re.search(r'(date|_dt|_at|timestamp)$', name_lower):
        return 'date'
    if re.search(r'(amount|qty|quantity|count|total|price|cost|revenue|sales|_id)$', name_lower):
        return 'number'
    if re.search(r'(is_|has_|flag)$', name_lower):
        return 'boolean'
    return 'string'


def _build_column_description(field_name, is_key=False):
    """Generate a readable description from the field name."""
    readable = re.sub(r'([A-Z])', r' \1', field_name).strip()
    readable = re.sub(r'[_\-]+', ' ', readable).strip().title()
    if is_key:
        return f"Primary / foreign key — {readable}"
    return readable


def build_schema_yml(model_name, tables, regenerated_sql='', model_description=''):
    """
    Build a dbt schema.yml with real column definitions extracted from the
    session's table metadata.

    Priority order for columns:
      1. Fields parsed from the generated SQL SELECT list (most accurate)
      2. Fields from the extracted Qlik table metadata
    """
    # --- Try to extract column names from the generated SQL ---
    sql_columns = []
    if regenerated_sql:
        # Grab the outermost SELECT list (handles CTEs by taking the last SELECT)
        select_blocks = re.findall(
            r'(?is)\bSELECT\s+(.*?)(?:\bFROM\b|\bWHERE\b|\bGROUP\s+BY\b|\bORDER\s+BY\b|$)',
            regenerated_sql,
        )
        if select_blocks:
            last_select = select_blocks[-1].strip()
            if last_select and last_select.upper() != '*':
                for raw_col in re.split(r',(?![^()]*\))', last_select):
                    raw_col = raw_col.strip()
                    if not raw_col:
                        continue
                    # AS alias takes priority
                    alias_match = re.search(
                        r'(?i)\bAS\s+([`"\[]?[A-Za-z0-9_]+[`"\]]?)\s*$', raw_col
                    )
                    if alias_match:
                        col = alias_match.group(1).strip('`"[]')
                    else:
                        # Last bare identifier
                        ident = re.search(r'([A-Za-z_][A-Za-z0-9_]*)(?:\s*)$', raw_col)
                        col = ident.group(1) if ident else None
                    if col and col.upper() not in ('SELECT', 'FROM', 'WHERE', 'WITH'):
                        sql_columns.append(col)

    # --- Collect all fields from extracted table metadata ---
    meta_fields = {}  # name_lower -> {name, type, isKey}
    for table in (tables or []):
        for field in table.get('fields', []):
            fname = str(field.get('name') or '').strip()
            if not fname:
                continue
            key = fname.lower()
            if key not in meta_fields:
                meta_fields[key] = field

    # --- Merge: SQL columns first, fill gaps from metadata ---
    final_columns = []
    seen = set()

    for col_name in sql_columns:
        key = col_name.lower()
        if key in seen:
            continue
        seen.add(key)
        meta = meta_fields.get(key, {})
        final_columns.append({
            'name': col_name,
            'type': _infer_column_type(col_name, meta.get('type')),
            'isKey': meta.get('isKey', False),
            'description': _build_column_description(col_name, meta.get('isKey', False)),
        })

    # Fill remaining metadata fields not already covered by SQL columns
    for key, field in meta_fields.items():
        if key in seen:
            continue
        seen.add(key)
        fname = field.get('name', key)
        final_columns.append({
            'name': fname,
            'type': _infer_column_type(fname, field.get('type')),
            'isKey': field.get('isKey', False),
            'description': _build_column_description(fname, field.get('isKey', False)),
        })

    # --- Render YAML manually (no PyYAML dependency) ---
    safe_model = _safe_yaml_str(model_name)
    safe_desc = _safe_yaml_str(
        model_description or f"AI-migrated dbt model from QlikView script — {model_name}"
    )

    lines = [
        'version: 2',
        '',
        'models:',
        f'  - name: {safe_model}',
        f'    description: {safe_desc}',
    ]

    if final_columns:
        lines.append('    columns:')
        for col in final_columns:
            safe_col_name = _safe_yaml_str(col['name'])
            safe_col_desc = _safe_yaml_str(col['description'])
            col_type = col['type']
            lines.append(f'      - name: {safe_col_name}')
            lines.append(f'        description: {safe_col_desc}')
            lines.append(f'        data_type: {col_type}')
            if col['isKey']:
                lines.append('        tests:')
                lines.append('          - not_null')
                lines.append('          - unique')
    else:
        lines.append('    columns: []  # No column metadata available')

    lines.append('')
    return '\n'.join(lines)


# ─── Per-table model files ────────────────────────────────────────────────────

def _rewrite_sources_to_refs(sql, tables):
    """
    Rewrite every {{ source('qvf_source', 'TableName') }} call in the
    generated SQL to {{ ref('stg_<slug>') }} so dbt's lineage graph
    connects the staging layer to the marts layer end-to-end.

    Without this, dbt docs generate shows migration_output as a root
    node with no upstream dependencies — the lineage graph is broken.
    """
    import re as _re
    source_pat = _re.compile(
        r"\{\{\s*source\s*\(\s*['\"]qvf_source['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}",
        _re.IGNORECASE,
    )
    # Build lookup: original table name (lowered) → stg slug
    name_to_slug = {str(t.get('name') or '').strip().lower(): _slugify(str(t.get('name') or ''))
                    for t in (tables or []) if t.get('name')}

    def _replace(m):
        tname = m.group(1).strip()
        slug = name_to_slug.get(tname.lower()) or _slugify(tname)
        return "{{ ref('stg_" + slug + "') }}"

    return source_pat.sub(_replace, sql)


def _build_rowcount_test(tables):
    """
    Generate a dbt singular test (tests/assert_migration_output_rowcount.sql)
    that fails if migration_output is empty.

    Singular tests in dbt fail when the query returns ANY rows.
    We return rows when the model has zero rows — so a non-empty model passes.

    Also embeds the expected minimum row count derived from source table sizes
    so reviewers know what to look for in dbt Cloud run logs.
    """
    total_rows = sum(int(t.get('rows') or 0) for t in (tables or []))
    comment = (
        f"-- Source tables contain {total_rows:,} rows in total.\n"
        "-- After joins and filters the output should be > 0 rows.\n"
        "-- If this test returns rows, migration_output is empty — investigate join conditions.\n"
    )
    return comment + textwrap.dedent("""\
        SELECT 1 AS failure_flag
        FROM {{ ref('migration_output') }}
        HAVING COUNT(*) = 0
    """)


def _extract_cte_blocks(sql):
    """
    Split a WITH … SELECT SQL string into individual CTE blocks.
    Returns list of (cte_name, cte_body_sql) tuples.

    Used by create_dbt_package to enumerate CTEs when generating
    per-CTE documentation in marts/schema.yml.
    """
    cte_pattern = re.compile(
        r'(?is)\bWITH\b\s+(.*?)(?=\bSELECT\b(?!\s*\w+\s*AS\s*\())'
    )
    match = cte_pattern.search(sql)
    if not match:
        return []

    cte_body = match.group(1)
    blocks = []
    depth = 0
    current = []
    for char in cte_body:
        if char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
        if char == ',' and depth == 0:
            blocks.append(''.join(current).strip())
            current = []
        else:
            current.append(char)
    if current:
        blocks.append(''.join(current).strip())

    result = []
    for block in blocks:
        name_match = re.match(r'([A-Za-z_][A-Za-z0-9_]*)\s+AS\s*\(', block, re.IGNORECASE)
        if name_match:
            result.append((name_match.group(1), block))
    return result


def _slugify(name):
    """Convert a table name to a safe dbt model filename."""
    slug = re.sub(r'[^A-Za-z0-9_]', '_', str(name or 'model')).lower()
    slug = re.sub(r'_+', '_', slug).strip('_')
    return slug or 'model'




def create_dbt_package(session_data, upload_folder, session_id):
    """
    Build a proper multi-model dbt project from session data.

    session_data keys:
      - all_rows: list of extracted_data rows (one per uploaded file)
      - file_map: {file_id: filename}
      - regenerated_sql: the latest generated SQL string
      - regenerated_text: the latest generated description
      - tables: merged list of all table dicts from the session
    """
    temp_dir = os.path.join(upload_folder, f"dbt_{session_id}")
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir)

    staging_dir = os.path.join(temp_dir, 'models', 'staging')
    marts_dir = os.path.join(temp_dir, 'models', 'marts')
    os.makedirs(staging_dir, exist_ok=True)
    os.makedirs(marts_dir, exist_ok=True)

    # ── dbt_project.yml ──────────────────────────────────────────────────────
    project_yml = """\
name: 'qvf_migration'
version: '1.0.0'
config-version: 2
profile: 'default'
model-paths: ["models"]
target-path: "target"
clean-targets: ["target", "dbt_packages"]

models:
  qvf_migration:
    staging:
      +materialized: view
    marts:
      +materialized: table
"""
    _write(temp_dir, 'dbt_project.yml', project_yml)

    # ── README.md ────────────────────────────────────────────────────────────
    readme = f"""\
# QVF Migration — dbt Project

Auto-generated by **QVF Decoder** from session `{session_id}`.

## Structure

```
models/
  staging/   — one view per source Qlik table
  marts/     — final migration_output model
```

## Usage

```bash
dbt deps
dbt run
dbt test
dbt docs generate && dbt docs serve
```
"""
    _write(temp_dir, 'README.md', readme)

    all_tables = session_data.get('tables', [])
    regenerated_sql = session_data.get('regenerated_sql', '')
    regenerated_text = session_data.get('regenerated_text', '')

    # ── Staging models — one .sql per extracted table ─────────────────────────
    staging_schema_entries = []
    for table in all_tables:
        tname = str(table.get('name') or '').strip()
        if not tname:
            continue
        slug = _slugify(tname)
        fields = table.get('fields', [])

        col_list = '\n    '.join(
            f"{f['name']}," for f in fields if f.get('name')
        ).rstrip(',')
        if not col_list:
            col_list = '*'

        stg_sql = f"""\
{{{{ config(materialized='view') }}}}

-- Staging model for source table: {tname}
-- Auto-generated by QVF Decoder

SELECT
    {col_list}
FROM {{{{ source('qvf_source', '{tname}') }}}}
"""
        _write(staging_dir, f"stg_{slug}.sql", stg_sql)

        # Collect column entries for staging schema.yml
        col_entries = []
        for f in fields:
            fname = f.get('name', '')
            if not fname:
                continue
            col_entries.append({
                'name': fname,
                'type': _infer_column_type(fname, f.get('type')),
                'isKey': f.get('isKey', False),
                'description': _build_column_description(fname, f.get('isKey', False)),
            })
        staging_schema_entries.append({'model': f"stg_{slug}", 'source_table': tname, 'columns': col_entries})

    # ── Staging schema.yml ────────────────────────────────────────────────────
    stg_schema_lines = ['version: 2', '', 'sources:',
                        "  - name: qvf_source",
                        "    description: Raw source tables from the QlikView application",
                        "    tables:"]
    for entry in staging_schema_entries:
        stg_schema_lines.append(f"      - name: {_safe_yaml_str(entry['source_table'])}")

    stg_schema_lines += ['', 'models:']
    for entry in staging_schema_entries:
        stg_schema_lines.append(f"  - name: {_safe_yaml_str(entry['model'])}")
        stg_schema_lines.append(f"    description: {_safe_yaml_str('Staging view for ' + entry['source_table'])}")
        if entry['columns']:
            stg_schema_lines.append('    columns:')
            for col in entry['columns']:
                stg_schema_lines.append(f"      - name: {_safe_yaml_str(col['name'])}")
                stg_schema_lines.append(f"        description: {_safe_yaml_str(col['description'])}")
                stg_schema_lines.append(f"        data_type: {col['type']}")
                if col['isKey']:
                    stg_schema_lines += ['        tests:', '          - not_null', '          - unique']
    stg_schema_lines.append('')
    _write(staging_dir, 'schema.yml', '\n'.join(stg_schema_lines))

    # ── Marts — migration_output.sql ─────────────────────────────────────────
    # Fix: rewrite {{ source('qvf_source', 'TableName') }} → {{ ref('stg_slug') }}
    # so dbt's lineage graph connects staging → marts end-to-end.
    mart_sql = regenerated_sql or '-- No SQL generated yet. Run "Migrate to DBT" first.\nSELECT 1 AS placeholder'
    mart_sql = _rewrite_sources_to_refs(mart_sql, all_tables)
    _write(marts_dir, 'migration_output.sql', mart_sql)

    # ── Marts tests — row-count assertion ─────────────────────────────────────
    # Fix: generate a singular dbt test that asserts migration_output row count
    # is non-zero (catches empty-result joins and missed WHERE filters).
    tests_dir = os.path.join(temp_dir, 'tests')
    os.makedirs(tests_dir, exist_ok=True)
    rowcount_test = _build_rowcount_test(all_tables)
    _write(tests_dir, 'assert_migration_output_rowcount.sql', rowcount_test)

    # ── Marts schema.yml with real columns ───────────────────────────────────
    marts_schema = build_schema_yml(
        'migration_output',
        all_tables,
        regenerated_sql=regenerated_sql,
        model_description=regenerated_text,
    )
    _write(marts_dir, 'schema.yml', marts_schema)

    # ── ZIP ───────────────────────────────────────────────────────────────────
    zip_path = os.path.join(upload_folder, f"dbt_project_{session_id}.zip")
    with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(temp_dir):
            for fname in files:
                fpath = os.path.join(root, fname)
                zf.write(fpath, os.path.relpath(fpath, temp_dir))

    shutil.rmtree(temp_dir)
    return zip_path


def _write(directory, filename, content):
    with open(os.path.join(directory, filename), 'w', encoding='utf-8') as fh:
        fh.write(content)


# ─── Flask route ─────────────────────────────────────────────────────────────

def register_dbt_package_routes(app, get_db, upload_folder):
    @app.route('/api/download/<session_id>')
    def download_dbt_package(session_id):
        db = get_db()
        rows = db.execute(
            'SELECT * FROM extracted_data WHERE session_id = ? ORDER BY created_at ASC',
            (session_id,),
        ).fetchall()
        db.close()

        if not rows:
            return jsonify({'error': 'No migration results found to package'}), 404

        # Merge all tables from all files in the session
        all_tables = []
        seen_names = set()
        for row in rows:
            for t in json.loads(row['tables_json'] or '[]'):
                name = str(t.get('name') or '').strip().lower()
                if name and name not in seen_names:
                    seen_names.add(name)
                    all_tables.append(t)

        latest = rows[-1]
        session_data = {
            'tables': all_tables,
            'regenerated_sql': latest['regenerated_sql'] or '',
            'regenerated_text': latest['regenerated_text'] or '',
        }

        zip_path = create_dbt_package(session_data, upload_folder, session_id)
        return send_file(zip_path, as_attachment=True, download_name='dbt_project.zip')

