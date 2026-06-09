"""Generate Databricks Delta DDL from approved QVD mapping artifacts."""

from __future__ import annotations

import csv
import os
import re
from collections import defaultdict

from qvd_to_databricks.schema_suggester import ALLOWED_TARGET_TYPES


AUDIT_COLUMNS = [
    ("_source_file_name", "STRING"),
    ("_source_file_path", "STRING"),
    ("_ingestion_timestamp", "TIMESTAMP"),
    ("_batch_id", "STRING"),
    ("_record_hash", "STRING"),
]


def read_approved_mapping_csv(mapping_csv_path: str) -> list[dict]:
    if not os.path.exists(mapping_csv_path):
        raise FileNotFoundError("Approved mapping artifact not found")
    with open(mapping_csv_path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def validate_mapping_rows_for_ddl(mapping_rows: list[dict]) -> list[dict]:
    errors = []
    seen_by_table: dict[str, set[str]] = defaultdict(set)

    if not mapping_rows:
        return [{"row": None, "field": "approved_mapping_csv", "error": "Approved mapping has no rows."}]

    for index, row in enumerate(mapping_rows):
        target_table = str(row.get("target_table") or "").strip()
        target_column = str(row.get("target_column") or "").strip()
        target_type = str(row.get("target_type") or "").strip().upper()

        if not target_table:
            errors.append({"row": index, "field": "target_table", "error": "target_table cannot be empty."})
        if not target_column:
            errors.append({"row": index, "field": "target_column", "error": "target_column cannot be empty."})
        if target_type not in ALLOWED_TARGET_TYPES:
            errors.append({"row": index, "field": "target_type", "error": f"Unsupported target_type: {target_type or '(empty)'}."})

        if target_table and target_column:
            column_key = target_column.lower()
            table_key = target_table.lower()
            if column_key in seen_by_table[table_key]:
                errors.append({
                    "row": index,
                    "field": "target_column",
                    "error": f"Duplicate target column '{target_column}' in target table '{target_table}'.",
                })
            else:
                seen_by_table[table_key].add(column_key)

    return errors


def quote_identifier(identifier: str) -> str:
    return f"`{str(identifier).replace('`', '``')}`"


def qualified_table_name(target_table: str, catalog_schema: str = "main.qvd_raw") -> str:
    prefix_parts = [part for part in str(catalog_schema or "main.qvd_raw").split(".") if part]
    return ".".join([quote_identifier(part) for part in [*prefix_parts, target_table]])


def safe_sql_filename(target_table: str) -> str:
    text = re.sub(r"[^0-9A-Za-z_]+", "_", str(target_table or "").strip())
    text = re.sub(r"_+", "_", text).strip("_").lower()
    return text or "unnamed_table"


def render_create_table_sql(target_table: str, rows: list[dict], catalog_schema: str = "main.qvd_raw") -> str:
    columns = [
        (str(row.get("target_column") or "").strip(), str(row.get("target_type") or "").strip().upper())
        for row in rows
    ]
    columns.extend(AUDIT_COLUMNS)

    column_lines = [
        f"  {quote_identifier(column_name)} {target_type}"
        for column_name, target_type in columns
    ]

    return (
        f"CREATE TABLE IF NOT EXISTS {qualified_table_name(target_table, catalog_schema)} (\n"
        + ",\n".join(column_lines)
        + "\n)\nUSING DELTA;\n"
    )


def generate_delta_ddl(mapping_rows: list[dict], ddl_dir: str, catalog_schema: str = "main.qvd_raw") -> dict:
    errors = validate_mapping_rows_for_ddl(mapping_rows)
    if errors:
        return {"generated": False, "ddl_files": [], "table_count": 0, "errors": errors, "sql_preview": {}}

    os.makedirs(ddl_dir, exist_ok=True)
    rows_by_table: dict[str, list[dict]] = defaultdict(list)
    for row in mapping_rows:
        clean_row = dict(row)
        clean_row["target_table"] = str(clean_row.get("target_table") or "").strip()
        clean_row["target_column"] = str(clean_row.get("target_column") or "").strip()
        clean_row["target_type"] = str(clean_row.get("target_type") or "").strip().upper()
        rows_by_table[clean_row["target_table"]].append(clean_row)

    ddl_files = []
    sql_preview = {}
    for target_table in sorted(rows_by_table):
        sql = render_create_table_sql(target_table, rows_by_table[target_table], catalog_schema)
        file_path = os.path.join(ddl_dir, f"create_{safe_sql_filename(target_table)}.sql")
        with open(file_path, "w", encoding="utf-8") as handle:
            handle.write(sql)
        ddl_files.append(file_path)
        sql_preview[file_path] = sql

    return {
        "generated": True,
        "ddl_files": ddl_files,
        "table_count": len(rows_by_table),
        "errors": [],
        "sql_preview": sql_preview,
    }


def generate_delta_ddl_from_approved_mapping(mapping_csv_path: str, ddl_dir: str, catalog_schema: str = "main.qvd_raw") -> dict:
    rows = read_approved_mapping_csv(mapping_csv_path)
    return generate_delta_ddl(rows, ddl_dir, catalog_schema)
