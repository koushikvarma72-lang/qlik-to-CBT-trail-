"""Column profiling for QVD row samples."""

from __future__ import annotations

import csv
import json
import os
from decimal import Decimal, InvalidOperation

from qvd_to_databricks import qvd_row_reader


PROFILE_COLUMNS = [
    "column_name",
    "row_count_checked",
    "null_count",
    "non_null_count",
    "distinct_count",
    "sample_values",
    "min_value",
    "max_value",
    "detected_runtime_type",
    "approved_target_type",
    "type_match_status",
    "warning_reason",
]


def load_approved_mapping_rows(mapping_csv_path: str) -> list[dict]:
    if os.path.exists(mapping_csv_path):
        with open(mapping_csv_path, newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        if rows:
            return rows

    json_path = os.path.splitext(mapping_csv_path)[0] + ".json"
    if not os.path.exists(json_path):
        return []
    with open(json_path, encoding="utf-8") as handle:
        payload = json.load(handle)
    rows = payload.get("mapping_rows") if isinstance(payload, dict) else payload
    return rows if isinstance(rows, list) else []


def _is_null(value) -> bool:
    return value is None or str(value).strip() == ""


def _to_decimal(value) -> Decimal | None:
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None


def _all_integer(values: list) -> bool:
    if not values:
        return False
    for value in values:
        decimal_value = _to_decimal(value)
        if decimal_value is None or decimal_value != decimal_value.to_integral_value():
            return False
    return True


def _all_decimal(values: list) -> bool:
    return bool(values) and all(_to_decimal(value) is not None for value in values)


def _boolean_like(values: list) -> bool:
    allowed = {"0", "1", "true", "false", "y", "n"}
    return bool(values) and all(str(value).strip().lower() in allowed for value in values)


def _qlik_date_serial_like(values: list, approved_target_type: str) -> bool:
    if approved_target_type != "DATE" or not _all_decimal(values):
        return False
    serials = [_to_decimal(value) for value in values]
    return all(serial is not None and Decimal("20000") <= serial <= Decimal("80000") for serial in serials)


def detect_runtime_type(values: list, approved_target_type: str = "") -> str:
    non_null_values = [value for value in values if not _is_null(value)]
    approved_type = str(approved_target_type or "").upper()
    if not non_null_values:
        return "STRING"
    if _qlik_date_serial_like(non_null_values, approved_type):
        return "QLIK_DATE_SERIAL"
    if _boolean_like(non_null_values):
        return "BOOLEAN_LIKE"
    if _all_integer(non_null_values):
        return "INTEGER"
    if _all_decimal(non_null_values):
        return "DECIMAL"
    return "STRING"


def _match_status(detected_runtime_type: str, approved_target_type: str) -> tuple[str, str]:
    approved = str(approved_target_type or "").upper()
    if not approved:
        return "NEEDS_REVIEW", "No approved mapping target type found for this source column."

    if detected_runtime_type == "QLIK_DATE_SERIAL":
        return ("MATCH", "") if approved == "DATE" else ("MISMATCH", "Qlik date serial values do not match approved target type.")
    if detected_runtime_type == "BOOLEAN_LIKE":
        return ("MATCH", "") if approved == "BOOLEAN" else ("NEEDS_REVIEW", "Boolean-like values may need target type review.")
    if detected_runtime_type == "INTEGER":
        return ("MATCH", "") if approved in {"INT", "BIGINT"} else ("MISMATCH", "Integer runtime values do not match approved target type.")
    if detected_runtime_type == "DECIMAL":
        return ("MATCH", "") if approved in {"DOUBLE", "DECIMAL(18,2)"} else ("MISMATCH", "Decimal runtime values do not match approved target type.")
    if detected_runtime_type == "STRING":
        return ("MATCH", "") if approved == "STRING" else ("MISMATCH", "String runtime values do not match approved target type.")
    return "NEEDS_REVIEW", "Runtime type could not be confidently matched."


def _mapping_by_source_column(approved_mapping_rows: list[dict] | None) -> dict[str, dict]:
    mapping = {}
    for row in approved_mapping_rows or []:
        source_column = str(row.get("source_column") or "").strip()
        if source_column:
            mapping[_column_key(source_column)] = row
    return mapping


def _column_key(value: str) -> str:
    return str(value or "").strip().casefold()


def profile_rows(columns: list[str], rows: list[dict], approved_mapping_rows: list[dict] | None = None) -> list[dict]:
    mapping = _mapping_by_source_column(approved_mapping_rows)
    profile = []

    for column in columns:
        values = [row.get(column) for row in rows]
        non_null_values = [value for value in values if not _is_null(value)]
        approved_target_type = str(mapping.get(_column_key(column), {}).get("target_type") or "").upper()
        detected_runtime_type = detect_runtime_type(non_null_values, approved_target_type)
        status, warning = _match_status(detected_runtime_type, approved_target_type)
        decimal_values = [_to_decimal(value) for value in non_null_values]
        comparable_values = [value for value in decimal_values if value is not None]
        if comparable_values:
            min_value = str(min(comparable_values))
            max_value = str(max(comparable_values))
        else:
            string_values = [str(value) for value in non_null_values]
            min_value = min(string_values) if string_values else ""
            max_value = max(string_values) if string_values else ""

        sample_values = []
        for value in non_null_values:
            text = str(value)
            if text not in sample_values:
                sample_values.append(text)
            if len(sample_values) >= 5:
                break

        profile.append({
            "column_name": column,
            "row_count_checked": len(rows),
            "null_count": len(values) - len(non_null_values),
            "non_null_count": len(non_null_values),
            "distinct_count": len({str(value) for value in non_null_values}),
            "sample_values": sample_values,
            "min_value": min_value,
            "max_value": max_value,
            "detected_runtime_type": detected_runtime_type,
            "approved_target_type": approved_target_type,
            "type_match_status": status,
            "warning_reason": warning,
        })

    return profile


def summarize_profile(profile: list[dict], rows_checked: int) -> dict:
    return {
        "total_columns": len(profile),
        "rows_checked": rows_checked,
        "match_count": sum(1 for row in profile if row.get("type_match_status") == "MATCH"),
        "needs_review_count": sum(1 for row in profile if row.get("type_match_status") == "NEEDS_REVIEW"),
        "mismatch_count": sum(1 for row in profile if row.get("type_match_status") == "MISMATCH"),
        "profile_rows": profile,
    }


def profile_qvd_columns(file_path: str, approved_mapping_rows: list[dict] | None = None, limit: int | None = None) -> dict:
    safe_limit = max(1, min(int(limit or 10000), 10000))
    preview = qvd_row_reader.preview_qvd_rows(file_path, limit=safe_limit)
    if not preview.get("success"):
        return {
            "success": False,
            "reader_used": preview.get("reader_used"),
            "error": preview.get("error") or "QVD row profiling failed.",
            **summarize_profile([], 0),
        }

    columns = preview.get("columns") or []
    rows = preview.get("rows") or []
    profile = profile_rows(columns, rows, approved_mapping_rows)
    return {
        "success": True,
        "reader_used": preview.get("reader_used"),
        "error": None,
        **summarize_profile(profile, len(rows)),
    }


def write_profile_artifacts(output_dir: str, safe_file_name: str, profile_payload: dict) -> dict:
    os.makedirs(output_dir, exist_ok=True)
    json_path = os.path.join(output_dir, f"column_profile_{safe_file_name}.json")
    csv_path = os.path.join(output_dir, f"column_profile_{safe_file_name}.csv")

    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(profile_payload, handle, indent=2, ensure_ascii=False)

    with open(csv_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=PROFILE_COLUMNS)
        writer.writeheader()
        for row in profile_payload.get("profile_rows") or []:
            csv_row = dict(row)
            csv_row["sample_values"] = "|".join(row.get("sample_values") or [])
            writer.writerow({column: csv_row.get(column, "") for column in PROFILE_COLUMNS})

    return {
        "column_profile_json": json_path,
        "column_profile_csv": csv_path,
    }
