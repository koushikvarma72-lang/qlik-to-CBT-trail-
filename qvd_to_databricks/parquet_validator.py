"""Validate locally generated QVD Parquet outputs before Databricks load."""

from __future__ import annotations

import json
import os
from datetime import date, datetime

from qvd_to_databricks.qvd_to_parquet_converter import AUDIT_COLUMNS, load_approved_mapping


NUMERIC_TARGET_TYPES = {"INT", "BIGINT", "DOUBLE", "DECIMAL(18,2)"}


def _require_pandas():
    try:
        import pandas as pd
        return pd
    except ImportError as exc:
        raise RuntimeError("Pandas is required for Parquet validation. Install it with `pip install pandas`.") from exc


def _check(name: str, passed: bool, details: dict | None = None, message: str = "") -> dict:
    return {
        "name": name,
        "passed": bool(passed),
        "message": message,
        "details": details or {},
    }


def _find_parquet_files(parquet_path: str) -> list[str]:
    if os.path.isfile(parquet_path) and parquet_path.endswith(".parquet"):
        return [parquet_path]
    if not os.path.isdir(parquet_path):
        return []
    return [
        os.path.join(parquet_path, name)
        for name in sorted(os.listdir(parquet_path))
        if name.endswith(".parquet")
    ]


def _expected_qvd_records(inspection: dict, target_table: str) -> int | None:
    tables = inspection.get("tables") or []
    if not tables:
        return None
    for table in tables:
        summary = table.get("summary") or {}
        table_name = str(summary.get("table_name") or "")
        file_name = str(summary.get("file_name") or "")
        if target_table in {table_name, file_name}:
            try:
                return int(str(summary.get("no_of_records") or ""))
            except ValueError:
                return None
    try:
        return int(str((tables[0].get("summary") or {}).get("no_of_records") or ""))
    except ValueError:
        return None


def _is_date_like_value(value) -> bool:
    if value is None:
        return True
    if isinstance(value, (date, datetime)):
        return True
    text = str(value)
    if text == "" or text.lower() in {"nat", "nan", "none"}:
        return True
    if text.replace(".", "", 1).isdigit():
        return False
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def _bool_values_valid(series) -> bool:
    values = [value for value in series.dropna().tolist()]
    return all(isinstance(value, bool) or str(value).lower() in {"true", "false"} for value in values)


def _numeric_values_valid(series) -> bool:
    pd = _require_pandas()
    non_null = series.dropna()
    if non_null.empty:
        return True
    converted = pd.to_numeric(non_null, errors="coerce")
    return not converted.isna().any()


def validate_parquet_output(parquet_path: str, approved_mapping_path: str, inspection_json_path: str | None = None, target_table: str | None = None) -> dict:
    pd = _require_pandas()
    checks = []
    errors = []
    null_percentages = []

    parquet_files = _find_parquet_files(parquet_path)
    checks.append(_check("parquet_exists", bool(parquet_files), {"parquet_path": parquet_path, "files": parquet_files}))
    if not parquet_files:
        return _summary(False, checks, errors + ["Parquet file or directory not found."], null_percentages)

    try:
        frame = pd.read_parquet(parquet_path)
        checks.append(_check("parquet_readable", True, {"parquet_path": parquet_path}))
    except Exception as exc:
        checks.append(_check("parquet_readable", False, {"parquet_path": parquet_path}, str(exc)))
        return _summary(False, checks, errors + [str(exc)], null_percentages)

    try:
        mapping_rows = load_approved_mapping(approved_mapping_path)
    except Exception as exc:
        checks.append(_check("approved_mapping_readable", False, {"approved_mapping_path": approved_mapping_path}, str(exc)))
        return _summary(False, checks, errors + [str(exc)], null_percentages)

    if target_table:
        table_mapping_rows = [row for row in mapping_rows if str(row.get("target_table") or "") == target_table]
        mapping_rows = table_mapping_rows or mapping_rows

    approved_columns = [str(row.get("target_column") or "").strip() for row in mapping_rows if str(row.get("target_column") or "").strip()]
    expected_column_count = len(approved_columns) + len(AUDIT_COLUMNS)
    actual_columns = list(frame.columns)

    checks.append(_check(
        "column_count",
        len(actual_columns) == expected_column_count,
        {"expected": expected_column_count, "actual": len(actual_columns)},
    ))

    missing_target_columns = [column for column in approved_columns if column not in actual_columns]
    checks.append(_check("approved_target_columns_exist", not missing_target_columns, {"missing_columns": missing_target_columns}))

    missing_audit_columns = [column for column in AUDIT_COLUMNS if column not in actual_columns]
    checks.append(_check("audit_columns_exist", not missing_audit_columns, {"missing_columns": missing_audit_columns}))

    inspection = {}
    if inspection_json_path and os.path.exists(inspection_json_path):
        with open(inspection_json_path, encoding="utf-8") as handle:
            inspection = json.load(handle)
    expected_rows = _expected_qvd_records(inspection, target_table or "") if inspection else None
    checks.append(_check(
        "row_count",
        expected_rows is None or len(frame) == expected_rows,
        {"expected": expected_rows, "actual": len(frame)},
        "" if expected_rows is not None else "QVD metadata row count unavailable.",
    ))

    date_failures = []
    boolean_failures = []
    numeric_failures = []
    for row in mapping_rows:
        target_column = str(row.get("target_column") or "").strip()
        target_type = str(row.get("target_type") or "").strip().upper()
        if target_column not in frame.columns:
            continue
        if target_type == "DATE":
            invalid = [value for value in frame[target_column].dropna().head(20).tolist() if not _is_date_like_value(value)]
            if invalid:
                date_failures.append({"column": target_column, "examples": [str(value) for value in invalid[:3]]})
        elif target_type == "BOOLEAN":
            if not _bool_values_valid(frame[target_column]):
                boolean_failures.append(target_column)
        elif target_type in NUMERIC_TARGET_TYPES:
            if not _numeric_values_valid(frame[target_column]):
                numeric_failures.append(target_column)

    checks.append(_check("date_conversion", not date_failures, {"failures": date_failures}))
    checks.append(_check("boolean_values", not boolean_failures, {"failures": boolean_failures}))
    checks.append(_check("numeric_values", not numeric_failures, {"failures": numeric_failures}))

    for column in approved_columns:
        if column not in frame.columns:
            continue
        null_count = int(frame[column].isna().sum())
        null_percentage = (null_count / len(frame) * 100) if len(frame) else 0
        null_percentages.append({
            "column_name": column,
            "null_count": null_count,
            "row_count": int(len(frame)),
            "null_percentage": round(null_percentage, 2),
            "warning": null_percentage >= 25,
        })

    passed = all(check["passed"] for check in checks)
    return _summary(passed, checks, errors, null_percentages)


def _summary(passed: bool, checks: list[dict], errors: list[str], null_percentages: list[dict]) -> dict:
    return {
        "success": bool(passed),
        "passed": bool(passed),
        "checks": checks,
        "errors": errors,
        "null_percentages": null_percentages,
        "failed_checks": [check for check in checks if not check.get("passed")],
    }


def write_validation_artifact(output_dir: str, target_table: str, validation: dict) -> str:
    os.makedirs(output_dir, exist_ok=True)
    artifact_path = os.path.join(output_dir, f"parquet_validation_{target_table}.json")
    with open(artifact_path, "w", encoding="utf-8") as handle:
        json.dump(validation, handle, indent=2, ensure_ascii=False)
    return artifact_path
