"""Discover business entities from QVD metadata and profiling artifacts."""

from __future__ import annotations

import csv
import json
import os
import re


MEASURE_TOKENS = {
    "sales",
    "amount",
    "revenue",
    "budget",
    "forecast",
    "quantity",
    "qty",
    "units",
    "cost",
    "price",
    "value",
    "margin",
    "difference",
    "ops",
}

DIMENSION_TOKENS = {
    "customer",
    "product",
    "platform",
    "category",
    "franchise",
    "region",
    "country",
    "state",
    "area",
    "tier",
    "market",
    "segment",
    "channel",
    "brand",
}

HIERARCHY_PATTERNS = [
    ["region", "country", "state"],
    ["area", "tier"],
    ["category", "franchise", "platform"],
    ["customer", "segment", "channel"],
]


def _read_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _read_csv(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _safe_artifact_name(file_name: str) -> str:
    return re.sub(r"[^0-9A-Za-z_-]+", "_", str(file_name or "qvd_file").replace(".", "_")).strip("_") or "qvd_file"


def _column_key(value: str) -> str:
    return str(value or "").strip().casefold()


def _mapping_rows(output_dir: str) -> list[dict]:
    approved = _read_csv(os.path.join(output_dir, "approved_databricks_mapping.csv"))
    if approved:
        return approved
    suggested = _read_csv(os.path.join(output_dir, "suggested_databricks_mapping.csv"))
    if suggested:
        return suggested
    approved_json = _read_json(os.path.join(output_dir, "approved_databricks_mapping.json"))
    if isinstance(approved_json.get("mapping_rows"), list):
        return approved_json["mapping_rows"]
    suggested_json = _read_json(os.path.join(output_dir, "suggested_databricks_mapping.json"))
    if isinstance(suggested_json.get("mapping"), list):
        return suggested_json["mapping"]
    return []


def _profiles_by_column(output_dir: str, inspection: dict) -> dict[str, dict]:
    profiles: dict[str, dict] = {}
    for table in inspection.get("tables") or []:
        file_name = (table.get("summary") or {}).get("file_name") or ""
        profile = _read_json(os.path.join(output_dir, f"column_profile_{_safe_artifact_name(file_name)}.json"))
        for row in profile.get("profile_rows") or []:
            name = row.get("column_name")
            if name:
                profiles[_column_key(name)] = row
    return profiles


def _previews_by_column(output_dir: str, inspection: dict) -> dict[str, list[str]]:
    samples: dict[str, list[str]] = {}
    for table in inspection.get("tables") or []:
        file_name = (table.get("summary") or {}).get("file_name") or ""
        preview = _read_json(os.path.join(output_dir, f"row_preview_{_safe_artifact_name(file_name)}.json"))
        columns = preview.get("columns") or []
        rows = preview.get("rows") or []
        for column in columns:
            values = []
            for row in rows:
                value = row.get(column)
                text = "" if value is None else str(value)
                if text and text not in values:
                    values.append(text)
                if len(values) >= 5:
                    break
            if values:
                samples[_column_key(column)] = values
    return samples


def _target_by_source(mapping_rows: list[dict]) -> dict[str, str]:
    return {
        _column_key(row.get("source_column")): str(row.get("target_column") or "")
        for row in mapping_rows
        if row.get("source_column")
    }


def _business_name(column: str) -> str:
    text = re.sub(r"[%_/.\-]+", " ", str(column or "")).strip()
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.title() if text else "Unnamed"


def _has_token(name: str, tokens: set[str]) -> bool:
    normalized = re.sub(r"[^0-9a-z]+", " ", str(name or "").casefold())
    return any(token in normalized.split() or token in normalized for token in tokens)


def _classify_field(field: dict, source_row: dict, profile: dict) -> tuple[str, float, str]:
    name = str(field.get("field_name") or source_row.get("field_name") or "")
    lower = name.casefold()
    tags = {str(tag).casefold() for tag in field.get("tags") or []}
    inferred = str(source_row.get("inferred_category") or field.get("inferred_category") or "")
    runtime_type = str(profile.get("detected_runtime_type") or "").upper()

    if "$date" in tags or "$timestamp" in tags or "date" in lower or "timestamp" in lower or inferred == "DATE_LIKE":
        return "DATE", 0.92, "Date metadata, tags, or field name detected."
    if "flag" in lower or "is_" in lower or "has_" in lower or "active" in lower or "enabled" in lower or "indicator" in lower or runtime_type == "BOOLEAN_LIKE" or inferred == "FLAG_LIKE":
        return "FLAG", 0.88, "Flag-like name or boolean-like profile values detected."
    if any(token in lower for token in ("id", "key", "code")) or inferred == "KEY_LIKE":
        return "IDENTIFIER", 0.86, "Identifier-like field name detected."
    if _has_token(lower, MEASURE_TOKENS) or inferred == "NUMERIC_LIKE" or runtime_type in {"INTEGER", "DECIMAL"}:
        return "MEASURE", 0.84, "Numeric metric naming or runtime profile detected."
    if inferred == "TEXT_LIKE" or _has_token(lower, DIMENSION_TOKENS):
        return "DIMENSION", 0.82, "Descriptive text or common business dimension name detected."
    return "DIMENSION", 0.55, "Defaulted to descriptive dimension pending review."


def _entity_row(field: dict, source_row: dict, profile: dict, samples: list[str], target_column: str, entity_type: str, confidence: float, reason: str) -> dict:
    source_column = field.get("field_name") or source_row.get("field_name") or ""
    profile_samples = profile.get("sample_values") or []
    return {
        "source_column": source_column,
        "target_column": target_column,
        "entity_type": entity_type,
        "business_name": _business_name(source_column),
        "confidence": round(confidence, 2),
        "reason": reason,
        "sample_values": samples or profile_samples[:5],
    }


def _discover_hierarchies(entities: list[dict], profiles: dict[str, dict]) -> list[dict]:
    by_name = {_column_key(entity["source_column"]): entity for entity in entities}
    hierarchies = []
    for tokens in HIERARCHY_PATTERNS:
        matched = []
        for token in tokens:
            candidates = [
                entity
                for key, entity in by_name.items()
                if token in key and entity.get("entity_type") in {"DIMENSION", "IDENTIFIER"}
            ]
            if candidates:
                matched.append(candidates[0])
        if len(matched) >= 2:
            hierarchy_name = " > ".join(entity.get("business_name") or _business_name(entity["source_column"]) for entity in matched)
            hierarchies.append({
                "source_column": " > ".join(entity["source_column"] for entity in matched),
                "target_column": " > ".join(entity.get("target_column") or "" for entity in matched if entity.get("target_column")),
                "entity_type": "HIERARCHY",
                "business_name": hierarchy_name,
                "confidence": 0.72,
                "reason": "Common business hierarchy token sequence detected from available fields.",
                "sample_values": [],
                "levels": [
                    {
                        "source_column": entity["source_column"],
                        "target_column": entity.get("target_column", ""),
                        "distinct_count": profiles.get(_column_key(entity["source_column"]), {}).get("distinct_count", ""),
                    }
                    for entity in matched
                ],
            })
    return hierarchies


def discover_business_entities(output_dir: str) -> dict:
    inspection = _read_json(os.path.join(output_dir, "qvd_inspection.json"))
    source_rows = _read_csv(os.path.join(output_dir, "source_structure.csv"))
    source_by_column = {_column_key(row.get("field_name")): row for row in source_rows}
    profiles = _profiles_by_column(output_dir, inspection)
    previews = _previews_by_column(output_dir, inspection)
    targets = _target_by_source(_mapping_rows(output_dir))

    grouped = {
        "dimensions": [],
        "measures": [],
        "dates": [],
        "flags": [],
        "identifiers": [],
        "hierarchies": [],
    }
    flat_entities = []
    for table in inspection.get("tables") or []:
        for field in table.get("fields") or []:
            source_column = field.get("field_name") or ""
            key = _column_key(source_column)
            source_row = source_by_column.get(key, {})
            profile = profiles.get(key, {})
            entity_type, confidence, reason = _classify_field(field, source_row, profile)
            row = _entity_row(field, source_row, profile, previews.get(key, []), targets.get(key, ""), entity_type, confidence, reason)
            flat_entities.append(row)
            if entity_type == "DATE":
                grouped["dates"].append(row)
            elif entity_type == "MEASURE":
                grouped["measures"].append(row)
            elif entity_type == "FLAG":
                grouped["flags"].append(row)
            elif entity_type == "IDENTIFIER":
                grouped["identifiers"].append(row)
            else:
                grouped["dimensions"].append(row)

    grouped["hierarchies"] = _discover_hierarchies(flat_entities, profiles)
    return {
        "summary": {
            "dimensions_count": len(grouped["dimensions"]),
            "measures_count": len(grouped["measures"]),
            "dates_count": len(grouped["dates"]),
            "flags_count": len(grouped["flags"]),
            "identifiers_count": len(grouped["identifiers"]),
            "hierarchies_count": len(grouped["hierarchies"]),
        },
        **grouped,
    }


def write_business_entities_artifact(output_dir: str, result: dict) -> str:
    artifact_dir = os.path.join(output_dir, "business_analysis")
    os.makedirs(artifact_dir, exist_ok=True)
    path = os.path.join(artifact_dir, "business_entities.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, ensure_ascii=False)
    return path
