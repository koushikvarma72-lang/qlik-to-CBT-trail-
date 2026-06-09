
"""Generate Databricks load artifacts for validated QVD Parquet outputs."""

from __future__ import annotations

import csv
import json
import os
from datetime import datetime
from urllib.parse import urlparse

from qvd_to_databricks.ddl_generator import (
    AUDIT_COLUMNS,
    read_approved_mapping_csv,
    safe_sql_filename,
)


DEFAULT_CATALOG = "main"
DEFAULT_SCHEMA = "qvd_raw"

# Audit column names (lowercase) that may be present in the Parquet file
_AUDIT_COLUMN_NAMES = {col.lower() for col, _ in AUDIT_COLUMNS}

# Map from approved Databricks target_type → CAST expression template
_CAST_MAP: dict[str, str] = {
    "STRING": "CAST({col} AS STRING)",
    "DATE": "CAST({col} AS DATE)",
    "BOOLEAN": "CAST({col} AS BOOLEAN)",
    "BIGINT": "CAST({col} AS BIGINT)",
    "DECIMAL(18,2)": "CAST({col} AS DECIMAL(18,2))",
    "TIMESTAMP": "CAST({col} AS TIMESTAMP)",
    "DOUBLE": "CAST({col} AS DOUBLE)",
    "FLOAT": "CAST({col} AS FLOAT)",
    "INT": "CAST({col} AS INT)",
    "INTEGER": "CAST({col} AS INTEGER)",
    "LONG": "CAST({col} AS BIGINT)",
    "BINARY": "CAST({col} AS BINARY)",
    "TINYINT": "CAST({col} AS TINYINT)",
    "SMALLINT": "CAST({col} AS SMALLINT)",
}


def load_validation_report(validation_report_path: str) -> dict:
    if not os.path.exists(validation_report_path):
        raise FileNotFoundError("Parquet validation report not found.")
    with open(validation_report_path, encoding="utf-8") as handle:
        return json.load(handle)


def validation_report_passed(validation_report: dict) -> bool:
    return bool(validation_report.get("success") or validation_report.get("passed"))


def plain_qualified_table_name(target_table: str, catalog: str = DEFAULT_CATALOG, schema: str = DEFAULT_SCHEMA) -> str:
    parts = [catalog, schema, target_table]
    return ".".join(str(part).strip() for part in parts if str(part).strip())


def is_local_parquet_path(parquet_path: str) -> bool:
    parsed = urlparse(str(parquet_path or ""))
    if parsed.scheme in {"s3", "abfss", "wasbs", "gs", "dbfs"}:
        return False
    return bool(str(parquet_path or "").startswith("/") or parsed.scheme in {"", "file"})


def render_copy_into_sql(target_table: str, parquet_path: str, catalog: str = DEFAULT_CATALOG, schema: str = DEFAULT_SCHEMA) -> str:
    qualified = plain_qualified_table_name(target_table, catalog, schema)
    escaped_path = str(parquet_path).replace("'", "''")
    return (
        f"COPY INTO {qualified}\n"
        f"FROM '{escaped_path}'\n"
        "FILEFORMAT = PARQUET;\n"
    )


def _quote_backtick(name: str) -> str:
    return f"`{str(name).replace('`', '``')}`"


def render_cast_expression(col: str, target_type: str) -> str:
    """Return a CAST expression for *col* → *target_type* (backtick-quoted column)."""
    type_upper = str(target_type or "STRING").strip().upper()
    template = _CAST_MAP.get(type_upper, "CAST({col} AS STRING)")
    return template.format(col=_quote_backtick(col))


# Internal alias used within this module
_cast_expression = render_cast_expression


def render_create_temp_view_sql(target_table: str, parquet_path: str) -> str:
    """Return SQL that creates a temporary PARQUET view over the volume path."""
    safe_name = f"qvd_stage_{safe_sql_filename(target_table)}"
    escaped_path = str(parquet_path or "").replace("'", "''")
    return (
        f"CREATE OR REPLACE TEMPORARY VIEW {safe_name}\n"
        f"USING PARQUET\n"
        f"OPTIONS (\n"
        f"  path = '{escaped_path}'\n"
        f");\n"
    )


def render_insert_select_cast_sql(
    target_table: str,
    mapping_rows: list[dict],
    parquet_columns: list[str] | None,
    catalog: str = DEFAULT_CATALOG,
    schema: str = DEFAULT_SCHEMA,
    parquet_path: str | None = None,
) -> str:
    """Return a typed INSERT INTO … SELECT CAST(…) statement.

    - *mapping_rows*: rows from approved_databricks_mapping.csv filtered to this table.
    - *parquet_columns*: column names actually present in the Parquet file (may be None
      when unknown; audit columns are generated in that case regardless).
    """
    view_name = f"qvd_stage_{safe_sql_filename(target_table)}"
    qualified = plain_qualified_table_name(target_table, catalog, schema)

    parquet_col_set = {c.lower() for c in (parquet_columns or [])}

    cast_lines = []
    for row in mapping_rows:
        col = str(row.get("target_column") or "").strip()
        col_type = str(row.get("target_type") or "STRING").strip().upper()
        if not col:
            continue
        expr = _cast_expression(col, col_type)
        cast_lines.append(f"  {expr} AS {_quote_backtick(col)}")

    # Audit columns
    for audit_col, audit_type in AUDIT_COLUMNS:
        if parquet_columns is None or audit_col.lower() in parquet_col_set:
            # Present in Parquet — cast normally
            expr = _cast_expression(audit_col, audit_type)
        else:
            # Missing from Parquet — generate a value at INSERT time
            if audit_col == "_source_file_name":
                expr = "input_file_name()"
            elif audit_col == "_source_file_path":
                expr = "input_file_name()"
            elif audit_col == "_ingestion_timestamp":
                expr = "CURRENT_TIMESTAMP()"
            elif audit_col == "_batch_id":
                expr = "CAST(NULL AS STRING)"
            elif audit_col == "_record_hash":
                expr = "CAST(NULL AS STRING)"
            else:
                expr = "CAST(NULL AS STRING)"
        cast_lines.append(f"  {expr} AS {_quote_backtick(audit_col)}")

    select_body = ",\n".join(cast_lines)
    # If a parquet path is provided, query directly from the Parquet files
    if parquet_path:
        escaped_path = str(parquet_path or "").replace("'", "''").replace('`', '``')
        from_clause = f"FROM parquet.`{escaped_path}`"
    else:
        from_clause = f"FROM {view_name}"

    return (
        f"-- Recommended typed load: INSERT SELECT CAST\n"
        f"INSERT INTO {qualified}\n"
        f"SELECT\n"
        f"{select_body}\n"
        f"{from_clause};\n"
    )


def render_validation_sql(target_table: str, catalog: str = DEFAULT_CATALOG, schema: str = DEFAULT_SCHEMA) -> str:
    """Return a row-count validation query for the target Delta table."""
    parts = [p for p in (catalog, schema, target_table) if p]
    qualified = ".".join(f"`{p.replace('`', '``')}`" for p in parts)
    return f"SELECT COUNT(*) AS loaded_rows FROM {qualified};\n"


def render_pyspark_snippet(target_table: str, parquet_path: str, catalog: str = DEFAULT_CATALOG, schema: str = DEFAULT_SCHEMA) -> str:
    qualified = plain_qualified_table_name(target_table, catalog, schema)
    escaped_path = str(parquet_path).replace("\\", "\\\\").replace('"', '\\"')
    escaped_table = qualified.replace('"', '\\"')
    return (
        f'df = spark.read.parquet("{escaped_path}")\n'
        f'df.write.format("delta").mode("append").saveAsTable("{escaped_table}")\n'
    )


def direct_databricks_execution_placeholder() -> dict:
    return {
        "enabled": False,
        "success": False,
        "message": "Direct Databricks execution is not configured in this step. Use generated script artifacts instead.",
    }


def render_load_sql(create_table_sql: str, copy_into_sql: str, pyspark_snippet: str, local_path_warning: str = "") -> str:
    warning_block = f"-- WARNING: {local_path_warning}\n\n" if local_path_warning else ""
    commented_pyspark = "\n".join(f"-- {line}" if line else "--" for line in pyspark_snippet.splitlines())
    return (
        "-- Databricks Delta load script generated from validated QVD Parquet output.\n"
        f"{warning_block}"
        "-- 1. Create Delta table\n"
        f"{create_table_sql.rstrip()}\n\n"
        "-- 2A. SQL load option (reference only — use insert_select_cast.sql for typed loads)\n"
        f"{copy_into_sql.rstrip()}\n\n"
        "-- 2B. PySpark notebook option\n"
        f"{commented_pyspark}\n"
    )


def render_readme(target_table: str, parquet_path: str, artifacts: dict, local_path_warning: str = "") -> str:
    warning = f"\nWarning: {local_path_warning}\n" if local_path_warning else ""
    return (
        f"# Databricks Load Steps: {target_table}\n\n"
        "These artifacts are generated for manual execution in Databricks. No Databricks API call has been made.\n"
        f"{warning}\n"
        "## Recommended Execution Order\n\n"
        "1. Upload or expose the Parquet output where Databricks can read it.\n"
        "2. Run `create_table.sql` in a Databricks SQL warehouse or notebook.\n"
        "3. Run `insert_select_cast.sql` to load data with explicit type casts (recommended).\n"
        "   - This creates a TEMP VIEW over the Parquet path and inserts with explicit CASTs.\n"
        "4. Run `validation.sql` to verify row counts.\n\n"
        "## Alternative: COPY INTO (raw, no type casting)\n\n"
        "- Run the COPY INTO statement in `load_parquet_to_delta.sql`.\n"
        "- Note: direct COPY INTO may fail with DELTA_FAILED_TO_MERGE_FIELDS if the Parquet\n"
        "  physical types differ from the Delta table schema.\n\n"
        f"Target table: `{target_table}`\n\n"
        f"Parquet path: `{parquet_path}`\n\n"
        "Generated files:\n"
        f"- `{artifacts.get('create_table_sql', 'create_table.sql')}`\n"
        f"- `{artifacts.get('insert_select_cast_sql', 'insert_select_cast.sql')}` ← recommended typed load\n"
        f"- `{artifacts.get('validation_sql', 'validation.sql')}`\n"
        f"- `{artifacts.get('load_sql', 'load_parquet_to_delta.sql')}` ← COPY INTO reference only\n"
        f"- `{artifacts.get('load_config_json', 'load_config.json')}`\n"
    )


def generate_databricks_load_artifacts(
    target_table: str,
    parquet_path: str,
    ddl_sql_path: str,
    output_dir: str,
    catalog: str = DEFAULT_CATALOG,
    schema: str = DEFAULT_SCHEMA,
    validation_report_path: str | None = None,
    approved_mapping_path: str | None = None,
) -> dict:
    errors = []
    target_table = str(target_table or "").strip()
    parquet_path = str(parquet_path or "").strip()

    if not target_table:
        errors.append("target_table is required.")
    if not parquet_path:
        errors.append("parquet_path is required.")
    if not ddl_sql_path or not os.path.exists(ddl_sql_path):
        errors.append("Generated DDL artifact not found.")
    if validation_report_path:
        try:
            validation_report = load_validation_report(validation_report_path)
            if not validation_report_passed(validation_report):
                errors.append("Parquet validation has not passed.")
        except FileNotFoundError as exc:
            errors.append(str(exc))

    if errors:
        return {
            "generated": False,
            "mode": "script_artifact",
            "target_table": target_table,
            "errors": errors,
            "artifacts": {},
        }

    with open(ddl_sql_path, encoding="utf-8") as handle:
        create_table_sql = handle.read()

    # Load mapping rows for typed INSERT SELECT CAST generation
    mapping_rows: list[dict] = []
    if approved_mapping_path and os.path.exists(approved_mapping_path):
        try:
            all_rows = read_approved_mapping_csv(approved_mapping_path)
            mapping_rows = [
                row for row in all_rows
                if str(row.get("target_table") or "").strip().lower() == target_table.lower()
            ]
        except Exception:
            mapping_rows = []

    os.makedirs(output_dir, exist_ok=True)
    create_table_path = os.path.join(output_dir, "create_table.sql")
    insert_cast_path = os.path.join(output_dir, "insert_select_cast.sql")
    validation_sql_path = os.path.join(output_dir, "validation.sql")
    load_sql_path = os.path.join(output_dir, "load_parquet_to_delta.sql")
    config_path = os.path.join(output_dir, "load_config.json")
    readme_path = os.path.join(output_dir, "README_load_steps.md")

    local_path_warning = ""
    if is_local_parquet_path(parquet_path):
        local_path_warning = "The Parquet path is local. Databricks must be able to access this path, or use a cloud path override."

    copy_into_sql = render_copy_into_sql(target_table, parquet_path, catalog, schema)
    pyspark_snippet = render_pyspark_snippet(target_table, parquet_path, catalog, schema)
    load_sql = render_load_sql(create_table_sql, copy_into_sql, pyspark_snippet, local_path_warning)

    # Staged INSERT SELECT CAST artifacts (query Parquet directly; no temp view)
    create_temp_view_sql = render_create_temp_view_sql(target_table, parquet_path)
    insert_select_cast_sql = render_insert_select_cast_sql(
        target_table, mapping_rows, parquet_columns=None, catalog=catalog, schema=schema, parquet_path=parquet_path
    )
    validation_sql = render_validation_sql(target_table, catalog, schema)

    artifacts = {
        "create_table_sql": create_table_path,
        "insert_select_cast_sql": insert_cast_path,
        "validation_sql": validation_sql_path,
        "load_sql": load_sql_path,
        "load_config_json": config_path,
        "readme": readme_path,
    }
    config_payload = {
        "mode": "script_artifact",
        "target_table": target_table,
        "qualified_table": plain_qualified_table_name(target_table, catalog, schema),
        "catalog": catalog,
        "schema": schema,
        "parquet_path": parquet_path,
        "local_path_warning": local_path_warning,
        "ddl_sql_path": ddl_sql_path,
        "approved_mapping_path": approved_mapping_path,
        "validation_report_path": validation_report_path,
        "created_at": datetime.utcnow().isoformat(),
        "direct_execution": direct_databricks_execution_placeholder(),
    }

    with open(create_table_path, "w", encoding="utf-8") as handle:
        handle.write(create_table_sql)
    with open(insert_cast_path, "w", encoding="utf-8") as handle:
        # The typed insert now queries the Parquet path directly; no temp view required.
        handle.write(insert_select_cast_sql)
    with open(validation_sql_path, "w", encoding="utf-8") as handle:
        handle.write(validation_sql)
    with open(load_sql_path, "w", encoding="utf-8") as handle:
        handle.write(load_sql)
    with open(config_path, "w", encoding="utf-8") as handle:
        json.dump(config_payload, handle, indent=2, ensure_ascii=False)
    with open(readme_path, "w", encoding="utf-8") as handle:
        handle.write(render_readme(target_table, parquet_path, artifacts, local_path_warning))

    return {
        "generated": True,
        "mode": "script_artifact",
        "target_table": target_table,
        "qualified_table": plain_qualified_table_name(target_table, catalog, schema),
        "parquet_path": parquet_path,
        "local_path_warning": local_path_warning,
        "artifacts": artifacts,
        "create_table_sql": create_table_sql,
        "copy_into_sql": copy_into_sql,
        "insert_select_cast_sql": insert_select_cast_sql,
        "create_temp_view_sql": create_temp_view_sql,
        "validation_sql": validation_sql,
        "pyspark_snippet": pyspark_snippet,
        "load_sql": load_sql,
        "errors": [],
    }


def default_ddl_path(qvd_output_dir: str, target_table: str) -> str:
    return os.path.join(qvd_output_dir, "ddl", f"create_{safe_sql_filename(target_table)}.sql")
