"""Suggested Databricks schema mapping from inspected QVD metadata."""

from __future__ import annotations

import csv
import json
import os
import re
from collections import defaultdict

from qvd_to_databricks.qvd_inspector import infer_field_category


RESERVED_WORDS = {
    "all",
    "and",
    "array",
    "as",
    "between",
    "boolean",
    "by",
    "case",
    "column",
    "create",
    "date",
    "default",
    "delete",
    "double",
    "false",
    "from",
    "group",
    "insert",
    "int",
    "join",
    "null",
    "order",
    "select",
    "string",
    "table",
    "timestamp",
    "true",
    "update",
    "user",
    "where",
}

MAPPING_COLUMNS = [
    "qvd_file",
    "source_table",
    "source_column",
    "source_tags",
    "source_number_format",
    "inferred_category",
    "target_table",
    "target_column",
    "target_type",
    "conversion_rule",
    "confidence",
    "reason",
    "review_status",
]

ALLOWED_TARGET_TYPES = {
    "STRING",
    "BOOLEAN",
    "INT",
    "BIGINT",
    "DOUBLE",
    "DECIMAL(18,2)",
    "DATE",
    "TIMESTAMP",
}

ALLOWED_REVIEW_STATUSES = {
    "AUTO_APPROVED",
    "NEEDS_REVIEW",
    "MANUALLY_APPROVED",
}


def to_snake_case(value: str, reserved_suffix: str = "_col") -> str:
    text = str(value or "").strip()
    text = re.sub(r"^%+", "", text)
    text = re.sub(r"[./\\\s]+", "_", text)
    text = re.sub(r"[^0-9A-Za-z_]+", "_", text)
    text = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", text)
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
    text = re.sub(r"_+", "_", text).strip("_").lower()
    text = re.sub(r"\b([a-z]{1,2})_([a-z])(?=_)", r"\1\2", text)
    if not text:
        text = "unnamed"
    if re.match(r"^\d", text):
        text = f"col_{text}"
    if text in RESERVED_WORDS:
        text = f"{text}{reserved_suffix}"
    return text


def _parse_int(value) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _number_format_type(number_format: dict) -> str:
    return str(number_format.get("Type") or number_format.get("type") or "").upper()


def _number_format_decimals(number_format: dict) -> int | None:
    for key in ("nDec", "NDec", "decimals", "Decimals"):
        value = number_format.get(key)
        if value not in (None, ""):
            return _parse_int(value)
    return None


def _flag_is_high_confidence(field: dict) -> bool:
    name = str(field.get("field_name") or "").lower()
    symbols = _parse_int(field.get("no_of_symbols"))
    boolean_name = (
        "flag" in name
        or "is_" in name
        or "has_" in name
        or "active" in name
        or "enabled" in name
        or "indicator" in name
    )
    return boolean_name and (symbols is None or symbols <= 2)


def _target_type_for_field(field: dict, category: str) -> tuple[str, str, float, str]:
    name = str(field.get("field_name") or "").lower()
    number_format = field.get("number_format") or {}
    decimals = _number_format_decimals(number_format)

    if category == "DATE_LIKE":
        return "DATE", "qlik_serial_to_date", 0.90, "Date-like metadata or field name detected."
    if category == "TEXT_LIKE":
        return "STRING", "cast_string", 0.90, "Text/ascii QVD tags detected."
    if category == "KEY_LIKE":
        return "STRING", "cast_string", 0.90, "Identifier-like field name detected; preserving as string."
    if category == "FLAG_LIKE":
        if _flag_is_high_confidence(field):
            return "BOOLEAN", "flag_to_boolean_or_int_review", 0.90, "Flag-like name with low symbol cardinality."
        return "INT", "flag_to_boolean_or_int_review", 0.50, "Flag-like field needs review before boolean conversion."
    if category == "NUMERIC_LIKE":
        if any(token in name for token in ("units", "count", "qty", "quantity")) or decimals == 0:
            return "BIGINT", "cast_bigint", 0.90, "Numeric count/quantity metadata suggests whole numbers."
        if any(token in name for token in ("amount", "sales", "price", "cost", "value", "revenue", "margin", "difference", "ops")):
            return "DECIMAL(18,2)", "cast_decimal_18_2", 0.90, "Numeric business measure suggests decimal precision."
        return "DOUBLE", "cast_double", 0.70, "Numeric metadata detected without a stronger type hint."
    return "STRING", "no_rule_review", 0.50, "No reliable metadata rule matched."


def suggest_schema_from_inspection(inspection: dict) -> dict:
    rows = []

    for table in inspection.get("tables") or []:
        summary = table.get("summary") or {}
        source_table = summary.get("table_name") or summary.get("file_name") or ""
        target_table = to_snake_case(source_table, "_table")
        seen_columns: defaultdict[str, int] = defaultdict(int)

        for field in table.get("fields") or []:
            source_column = field.get("field_name") or ""
            category = field.get("inferred_category") or infer_field_category(field)
            base_target_column = to_snake_case(source_column, "_col")
            duplicate_count = seen_columns[base_target_column]
            seen_columns[base_target_column] += 1
            target_column = base_target_column if duplicate_count == 0 else f"{base_target_column}_{duplicate_count}"
            target_type, conversion_rule, confidence, reason = _target_type_for_field(field, category)
            review_status = "AUTO_APPROVED" if confidence >= 0.80 else "NEEDS_REVIEW"

            rows.append({
                "qvd_file": summary.get("file_name", ""),
                "source_table": source_table,
                "source_column": source_column,
                "source_tags": field.get("tags") or [],
                "source_number_format": field.get("number_format") or {},
                "inferred_category": category,
                "target_table": target_table,
                "target_column": target_column,
                "target_type": target_type,
                "conversion_rule": conversion_rule,
                "confidence": confidence,
                "reason": reason,
                "review_status": review_status,
            })

    target_tables = sorted({row["target_table"] for row in rows if row.get("target_table")})
    return {
        "total_columns": len(rows),
        "auto_approved_count": sum(1 for row in rows if row["review_status"] == "AUTO_APPROVED"),
        "needs_review_count": sum(1 for row in rows if row["review_status"] == "NEEDS_REVIEW"),
        "target_tables": target_tables,
        "mapping": rows,
    }


def write_schema_suggestion_artifacts(session_id: str, output_dir: str, suggestion: dict) -> dict:
    os.makedirs(output_dir, exist_ok=True)
    payload = {"session_id": session_id, **suggestion}

    json_path = os.path.join(output_dir, "suggested_databricks_mapping.json")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)

    csv_path = os.path.join(output_dir, "suggested_databricks_mapping.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MAPPING_COLUMNS)
        writer.writeheader()
        for row in suggestion.get("mapping") or []:
            csv_row = dict(row)
            csv_row["source_tags"] = "|".join(row.get("source_tags") or [])
            csv_row["source_number_format"] = json.dumps(row.get("source_number_format") or {}, sort_keys=True)
            writer.writerow({key: csv_row.get(key, "") for key in MAPPING_COLUMNS})

    return {
        "suggested_mapping_csv": csv_path,
        "suggested_mapping_json": json_path,
    }


def _serialize_mapping_row_for_csv(row: dict) -> dict:
    csv_row = dict(row)
    source_tags = csv_row.get("source_tags") or []
    if isinstance(source_tags, str):
        csv_row["source_tags"] = source_tags
    else:
        csv_row["source_tags"] = "|".join(source_tags)

    source_number_format = csv_row.get("source_number_format") or {}
    if isinstance(source_number_format, str):
        csv_row["source_number_format"] = source_number_format
    else:
        csv_row["source_number_format"] = json.dumps(source_number_format, sort_keys=True)

    return {key: csv_row.get(key, "") for key in MAPPING_COLUMNS}


def validate_approved_mapping_rows(mapping_rows: list[dict]) -> list[dict]:
    errors = []
    seen_targets: dict[tuple[str, str], int] = {}

    if not isinstance(mapping_rows, list) or not mapping_rows:
        return [{"row": None, "field": "mapping_rows", "error": "At least one mapping row is required."}]

    for index, row in enumerate(mapping_rows):
        if not isinstance(row, dict):
            errors.append({"row": index, "field": "mapping_rows", "error": "Mapping row must be an object."})
            continue

        target_table = str(row.get("target_table") or "").strip()
        target_column = str(row.get("target_column") or "").strip()
        target_type = str(row.get("target_type") or "").strip().upper()
        review_status = str(row.get("review_status") or "").strip().upper()

        if not target_table:
            errors.append({"row": index, "field": "target_table", "error": "target_table cannot be empty."})
        if not target_column:
            errors.append({"row": index, "field": "target_column", "error": "target_column cannot be empty."})
        if target_type not in ALLOWED_TARGET_TYPES:
            errors.append({"row": index, "field": "target_type", "error": f"Unsupported target_type: {target_type or '(empty)'}."})
        if review_status not in ALLOWED_REVIEW_STATUSES:
            errors.append({"row": index, "field": "review_status", "error": f"Unsupported review_status: {review_status or '(empty)'}."})
        elif review_status == "NEEDS_REVIEW":
            errors.append({"row": index, "field": "review_status", "error": "All rows must be AUTO_APPROVED or MANUALLY_APPROVED before saving."})

        if target_table and target_column:
            key = (target_table.lower(), target_column.lower())
            if key in seen_targets:
                errors.append({
                    "row": index,
                    "field": "target_column",
                    "error": f"Duplicate target column '{target_column}' in target table '{target_table}'.",
                })
            else:
                seen_targets[key] = index

    return errors


def normalize_approved_mapping_rows(mapping_rows: list[dict]) -> list[dict]:
    normalized = []
    for row in mapping_rows:
        clean = {key: row.get(key, "") for key in MAPPING_COLUMNS}
        clean["target_table"] = str(clean.get("target_table") or "").strip()
        clean["target_column"] = str(clean.get("target_column") or "").strip()
        clean["target_type"] = str(clean.get("target_type") or "").strip().upper()
        clean["review_status"] = str(clean.get("review_status") or "").strip().upper()
        normalized.append(clean)
    return normalized


def write_approved_mapping_artifacts(session_id: str, output_dir: str, mapping_rows: list[dict]) -> dict:
    os.makedirs(output_dir, exist_ok=True)
    normalized_rows = normalize_approved_mapping_rows(mapping_rows)
    payload = {
        "session_id": session_id,
        "mapping_rows": normalized_rows,
        "total_rows": len(normalized_rows),
        "approved_count": sum(1 for row in normalized_rows if row.get("review_status") in {"AUTO_APPROVED", "MANUALLY_APPROVED"}),
    }

    json_path = os.path.join(output_dir, "approved_databricks_mapping.json")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)

    csv_path = os.path.join(output_dir, "approved_databricks_mapping.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MAPPING_COLUMNS)
        writer.writeheader()
        for row in normalized_rows:
            writer.writerow(_serialize_mapping_row_for_csv(row))

    return {
        "approved_mapping_csv": csv_path,
        "approved_mapping_json": json_path,
        "total_rows": payload["total_rows"],
        "approved_count": payload["approved_count"],
    }
