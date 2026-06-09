"""QVD metadata inspection and source-structure artifacts."""

from __future__ import annotations

import csv
import json
import os

from qvd_to_databricks.qvd_metadata_reader import read_qvd_metadata


CATEGORY_PRIORITY = [
    "DATE_LIKE",
    "KEY_LIKE",
    "TEXT_LIKE",
    "FLAG_LIKE",
    "NUMERIC_LIKE",
    "UNKNOWN",
]


def infer_field_category(field: dict) -> str:
    tags = {str(tag).lower() for tag in (field.get("tags") or [])}
    name = str(field.get("field_name") or "").lower()
    number_format = field.get("number_format") or {}
    number_type = str(number_format.get("Type") or number_format.get("type") or "").upper()

    matches = set()
    if "$date" in tags or "$timestamp" in tags or "date" in name or "timestamp" in name:
        matches.add("DATE_LIKE")
    if any(token in name for token in ("id", "key", "code")):
        matches.add("KEY_LIKE")
    if "$text" in tags or "$ascii" in tags:
        matches.add("TEXT_LIKE")
    if (
        "flag" in name
        or "is_" in name
        or "has_" in name
        or "active" in name
        or "enabled" in name
        or "indicator" in name
    ):
        matches.add("FLAG_LIKE")
    if "$numeric" in tags or number_type in {"REAL", "INTEGER"}:
        matches.add("NUMERIC_LIKE")

    for category in CATEGORY_PRIORITY:
        if category in matches:
            return category
    return "UNKNOWN"


def _quick_analysis(fields: list[dict]) -> dict:
    groups = {
        "date_like_fields": [],
        "numeric_like_fields": [],
        "key_like_fields": [],
        "text_like_fields": [],
        "flag_like_fields": [],
    }
    group_by_category = {
        "DATE_LIKE": "date_like_fields",
        "NUMERIC_LIKE": "numeric_like_fields",
        "KEY_LIKE": "key_like_fields",
        "TEXT_LIKE": "text_like_fields",
        "FLAG_LIKE": "flag_like_fields",
    }
    for field in fields:
        category = infer_field_category(field)
        field_name = field.get("field_name") or ""
        group_name = group_by_category.get(category)
        if group_name and field_name:
            groups[group_name].append(field_name)
    return groups


def inspect_qvd_file(file_path: str) -> dict:
    metadata = read_qvd_metadata(file_path)
    summary_keys = [
        "file_name",
        "table_name",
        "no_of_records",
        "field_count",
        "file_size_bytes",
        "creator_doc",
        "create_utc_time",
        "source_create_utc_time",
    ]
    return {
        "summary": {key: metadata.get(key, "") for key in summary_keys},
        "fields": metadata["fields"],
        "quick_analysis": _quick_analysis(metadata["fields"]),
    }


def inspect_qvd_files(file_paths: list[str]) -> list[dict]:
    return [inspect_qvd_file(path) for path in file_paths]


def write_inspection_artifacts(session_id: str, output_dir: str, uploaded_files: list[dict], tables: list[dict], errors: list[dict] | None = None) -> dict:
    os.makedirs(output_dir, exist_ok=True)
    payload = {
        "session_id": session_id,
        "uploaded_files": uploaded_files,
        "tables": tables,
        "errors": errors or [],
    }

    inspection_path = os.path.join(output_dir, "qvd_inspection.json")
    with open(inspection_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)

    csv_path = os.path.join(output_dir, "source_structure.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "qvd_file",
            "table_name",
            "no_of_records",
            "field_position",
            "field_name",
            "tags",
            "number_format",
            "no_of_symbols",
            "bit_offset",
            "bit_width",
            "inferred_category",
        ])
        writer.writeheader()
        for table in tables:
            summary = table.get("summary") or {}
            for field in table.get("fields") or []:
                writer.writerow({
                    "qvd_file": summary.get("file_name", ""),
                    "table_name": summary.get("table_name", ""),
                    "no_of_records": summary.get("no_of_records", ""),
                    "field_position": field.get("position", ""),
                    "field_name": field.get("field_name", ""),
                    "tags": "|".join(field.get("tags") or []),
                    "number_format": json.dumps(field.get("number_format") or {}, sort_keys=True),
                    "no_of_symbols": field.get("no_of_symbols", ""),
                    "bit_offset": field.get("bit_offset", ""),
                    "bit_width": field.get("bit_width", ""),
                    "inferred_category": infer_field_category(field),
                })

    return {
        "inspection_json": inspection_path,
        "source_structure_csv": csv_path,
    }
