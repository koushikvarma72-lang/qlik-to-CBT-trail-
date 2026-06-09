"""Build a downloadable QVD to Databricks migration package."""

from __future__ import annotations

import csv
import json
import os
import shutil
import zipfile
from datetime import datetime

from qvd_to_databricks.ddl_generator import safe_sql_filename
from qvd_to_databricks.databricks_loader import validation_report_passed


PACKAGE_FILES = {
    "source_structure": "source_structure.csv",
    "approved_mapping": "approved_mapping.csv",
    "create_table": "create_table.sql",
    "parquet_validation": "parquet_validation.json",
    "load_sql": "load_parquet_to_delta.sql",
    "readme": "README.md",
    "summary": "migration_summary.json",
}


def _read_json(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _copy_if_exists(source: str, destination: str) -> bool:
    if not source or not os.path.exists(source):
        return False
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    shutil.copyfile(source, destination)
    return True


def _source_summary(output_dir: str, target_table: str, file_name: str | None = None) -> dict:
    inspection_path = os.path.join(output_dir, "qvd_inspection.json")
    if not os.path.exists(inspection_path):
        return {"source_qvd": file_name or "", "records": 0, "columns": 0}

    inspection = _read_json(inspection_path)
    tables = inspection.get("tables") or []
    selected = None
    for table in tables:
        summary = table.get("summary") or {}
        if file_name and summary.get("file_name") == file_name:
            selected = table
            break
        if target_table in {summary.get("table_name"), safe_sql_filename(summary.get("table_name") or "")}:
            selected = table
            break
    selected = selected or (tables[0] if tables else {})
    summary = selected.get("summary") or {}
    try:
        records = int(str(summary.get("no_of_records") or "0"))
    except ValueError:
        records = 0
    return {
        "source_qvd": summary.get("file_name") or file_name or "",
        "records": records,
        "columns": int(summary.get("field_count") or len(selected.get("fields") or [])),
    }


def _approved_mapping_count(path: str, target_table: str) -> int:
    if not os.path.exists(path):
        return 0
    with open(path, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    table_rows = [row for row in rows if str(row.get("target_table") or "") == target_table]
    return len(table_rows or rows)


def _render_readme(summary: dict, local_path_warning: str = "") -> str:
    warning = f"\nPath warning: {local_path_warning}\n" if local_path_warning else ""
    return (
        f"# QVD to Databricks Migration Package\n\n"
        f"Source QVD: `{summary.get('source_qvd', '')}`\n\n"
        f"Target table: `{summary.get('target_table', '')}`\n\n"
        f"Records: `{summary.get('records', 0)}`\n\n"
        f"Columns: `{summary.get('columns', 0)}`\n"
        f"{warning}\n"
        "Included files:\n"
        "- `source_structure.csv`\n"
        "- `approved_mapping.csv`\n"
        "- `create_table.sql`\n"
        "- `parquet_validation.json`\n"
        "- `load_parquet_to_delta.sql`\n"
        "- `migration_summary.json`\n\n"
        "Run `create_table.sql` in Databricks first, then run the COPY INTO SQL after making the Parquet path accessible to Databricks.\n"
    )


def _zip_directory(package_dir: str, zip_path: str) -> None:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(os.listdir(package_dir)):
            path = os.path.join(package_dir, name)
            if os.path.isfile(path) and path != zip_path:
                archive.write(path, arcname=name)


def generate_migration_package(
    output_dir: str,
    target_table: str,
    file_name: str | None = None,
    package_dir: str | None = None,
) -> dict:
    target_table = str(target_table or "").strip()
    if not target_table:
        return {"generated": False, "errors": ["target_table is required."], "artifacts": {}}

    validation_path = os.path.join(output_dir, f"parquet_validation_{target_table}.json")
    load_dir = os.path.join(output_dir, "databricks_load")
    package_dir = package_dir or os.path.join(output_dir, "migration_package")
    errors = []

    if not os.path.exists(validation_path):
        validation = {}
    else:
        validation = _read_json(validation_path)
        if not validation_report_passed(validation):
            errors.append("Parquet validation has not passed.")

    source_structure_path = os.path.join(output_dir, "source_structure.csv")
    approved_mapping_path = os.path.join(output_dir, "approved_databricks_mapping.csv")
    create_table_path = os.path.join(load_dir, "create_table.sql")
    if not os.path.exists(create_table_path):
        create_table_path = os.path.join(output_dir, "ddl", f"create_{safe_sql_filename(target_table)}.sql")
    load_sql_path = os.path.join(load_dir, "load_parquet_to_delta.sql")
    load_config_path = os.path.join(load_dir, "load_config.json")
    load_config = _read_json(load_config_path) if os.path.exists(load_config_path) else {}

    required = [
        (source_structure_path, "source_structure.csv"),
        (approved_mapping_path, "approved_databricks_mapping.csv"),
        (create_table_path, "create_table.sql"),
        (load_sql_path, "load_parquet_to_delta.sql"),
    ]
    for path, label in required:
        if not os.path.exists(path):
            errors.append(f"{label} not found.")

    if errors:
        return {"generated": False, "errors": errors, "artifacts": {}}

    os.makedirs(package_dir, exist_ok=True)
    for name in os.listdir(package_dir):
        path = os.path.join(package_dir, name)
        if os.path.isfile(path):
            os.unlink(path)

    source = _source_summary(output_dir, target_table, file_name)
    summary = {
        **source,
        "target_table": target_table,
        "ddl_generated": os.path.exists(create_table_path),
        "parquet_generated": bool(validation.get("parquet_path")),
        "validation_passed": validation_report_passed(validation),
        "load_scripts_generated": os.path.exists(load_sql_path),
        "created_at": datetime.utcnow().isoformat(),
    }
    if not summary["columns"]:
        summary["columns"] = _approved_mapping_count(approved_mapping_path, target_table)

    destinations = {
        "source_structure": os.path.join(package_dir, PACKAGE_FILES["source_structure"]),
        "approved_mapping": os.path.join(package_dir, PACKAGE_FILES["approved_mapping"]),
        "create_table": os.path.join(package_dir, PACKAGE_FILES["create_table"]),
        "parquet_validation": os.path.join(package_dir, PACKAGE_FILES["parquet_validation"]),
        "load_sql": os.path.join(package_dir, PACKAGE_FILES["load_sql"]),
        "readme": os.path.join(package_dir, PACKAGE_FILES["readme"]),
        "summary": os.path.join(package_dir, PACKAGE_FILES["summary"]),
    }

    _copy_if_exists(source_structure_path, destinations["source_structure"])
    _copy_if_exists(approved_mapping_path, destinations["approved_mapping"])
    _copy_if_exists(create_table_path, destinations["create_table"])
    if not _copy_if_exists(validation_path, destinations["parquet_validation"]):
        with open(destinations["parquet_validation"], "w", encoding="utf-8") as handle:
            json.dump({
                "success": None,
                "passed": None,
                "target_table": target_table,
                "parquet_path": load_config.get("parquet_path") or "",
                "message": "Parquet validation was not generated before packaging.",
            }, handle, indent=2, ensure_ascii=False)
    _copy_if_exists(load_sql_path, destinations["load_sql"])
    with open(destinations["summary"], "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, ensure_ascii=False)
    with open(destinations["readme"], "w", encoding="utf-8") as handle:
        handle.write(_render_readme(summary, load_config.get("local_path_warning") or ""))

    zip_path = os.path.join(package_dir, "migration_package.zip")
    _zip_directory(package_dir, zip_path)

    return {
        "generated": True,
        "target_table": target_table,
        "package_dir": package_dir,
        "migration_package_zip": zip_path,
        "summary": summary,
        "artifacts": {**destinations, "zip": zip_path},
        "errors": [],
    }
