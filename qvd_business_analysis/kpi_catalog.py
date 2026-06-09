"""Generate a generic KPI catalog from discovered QVD business entities."""

from __future__ import annotations

import csv
import json
import os
import re


KPI_COLUMNS = [
    "kpi_name",
    "business_description",
    "source_columns",
    "recommended_formula",
    "aggregation_type",
    "grain",
    "dimensions",
    "date_column",
    "confidence",
    "reason",
]

SUM_TOKENS = {
    "sales",
    "amount",
    "revenue",
    "cost",
    "price",
    "value",
    "margin",
    "difference",
    "quantity",
    "qty",
    "units",
    "budget",
    "forecast",
    "ops",
}

AVG_TOKENS = {"rate", "percentage", "percent", "ratio"}
DISPLAY_TOKEN_OVERRIDES = {
    "py": "Prior Year",
    "yoy": "YoY",
    "ops": "Ops",
    "mth": "Month",
    "busplan": "Business Plan",
    "qty": "Quantity",
}


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


def _column_key(value: str) -> str:
    return str(value or "").strip().casefold()


def _target_by_source(mapping_rows: list[dict]) -> dict[str, str]:
    return {
        _column_key(row.get("source_column")): str(row.get("target_column") or "")
        for row in mapping_rows
        if row.get("source_column")
    }


def _mapping_rows(output_dir: str) -> list[dict]:
    rows = _read_csv(os.path.join(output_dir, "approved_databricks_mapping.csv"))
    if rows:
        return rows
    payload = _read_json(os.path.join(output_dir, "approved_databricks_mapping.json"))
    if isinstance(payload.get("mapping_rows"), list):
        return payload["mapping_rows"]
    return []


def _snake_case(value: str) -> str:
    text = re.sub(r"^%+", "", str(value or "").strip())
    text = re.sub(r"[./\\\s]+", "_", text)
    text = re.sub(r"[^0-9A-Za-z_]+", "_", text)
    text = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", text)
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
    text = re.sub(r"_+", "_", text).strip("_").lower()
    text = re.sub(r"\b([a-z]{1,2})_([a-z])(?=_)", r"\1\2", text)
    return text or "unnamed"


def business_display_name(source_column: str) -> str:
    snake = _snake_case(source_column)
    words = snake.split("_")
    display_words = []
    lower_source = str(source_column or "").casefold()
    for word in words:
        if word == "lf" and any(token in lower_source for token in ("forecast", "budget", "plan")):
            display_words.extend(["Latest", "Forecast"])
        elif word in DISPLAY_TOKEN_OVERRIDES:
            display_words.extend(DISPLAY_TOKEN_OVERRIDES[word].split())
        else:
            display_words.append(word.title())
    return " ".join(display_words) if display_words else "Unnamed"


def _aggregation_type(entity: dict, mapping_row: dict | None = None) -> tuple[str, float, str]:
    name = f"{entity.get('source_column', '')} {entity.get('target_column', '')}".casefold()
    target_type = str((mapping_row or {}).get("target_type") or "").upper()
    if entity.get("entity_type") == "FLAG":
        if target_type == "BOOLEAN":
            return "COUNT", 0.68, "Boolean flag can be counted for true/false population analysis."
        return "SUM", 0.72, "Numeric flag can be summed to count positive indicators."
    if any(token in name for token in AVG_TOKENS):
        return "AVG", 0.78, "Rate, percentage, or ratio naming suggests averaging."
    if any(token in name for token in SUM_TOKENS):
        return "SUM", 0.86, "Additive business metric naming suggests summation."
    return "SUM", 0.6, "Measure detected, defaulting to additive aggregation pending review."


def _grain(dimensions: list[dict], dates: list[dict]) -> str:
    names = [dim.get("business_name") or dim.get("source_column") for dim in dimensions[:4]]
    if dates:
        names.append(dates[0].get("business_name") or dates[0].get("source_column"))
    return "By " + ", ".join(name for name in names if name) if names else "Table grain"


def generate_kpi_catalog(output_dir: str) -> dict:
    business_entities = _read_json(os.path.join(output_dir, "business_analysis", "business_entities.json"))
    mapping_rows = _mapping_rows(output_dir)
    mapping_by_source = {_column_key(row.get("source_column")): row for row in mapping_rows}
    targets = _target_by_source(mapping_rows)

    dimensions = business_entities.get("dimensions") or []
    dates = business_entities.get("dates") or []
    measures = business_entities.get("measures") or []
    flags = business_entities.get("flags") or []
    date_column = (dates[0].get("target_column") or _snake_case(dates[0].get("source_column"))) if dates else ""
    dimension_names = [dim.get("target_column") or _snake_case(dim.get("source_column")) for dim in dimensions[:8]]

    rows = []
    for entity in [*measures, *flags]:
        source_column = entity.get("source_column") or ""
        key = _column_key(source_column)
        target_column = entity.get("target_column") or targets.get(key) or _snake_case(source_column)
        mapping_row = mapping_by_source.get(key)
        aggregation, confidence, reason = _aggregation_type(entity, mapping_row)
        kpi_name = business_display_name(source_column)
        rows.append({
            "kpi_name": kpi_name,
            "business_description": f"{kpi_name} derived from `{source_column}`.",
            "source_columns": [source_column],
            "recommended_formula": f"{aggregation}({target_column})",
            "aggregation_type": aggregation,
            "grain": _grain(dimensions, dates),
            "dimensions": dimension_names,
            "date_column": date_column,
            "confidence": round(confidence, 2),
            "reason": reason,
        })

    return {
        "kpi_count": len(rows),
        "kpis": rows,
    }


def write_kpi_catalog_artifacts(output_dir: str, catalog: dict) -> dict:
    artifact_dir = os.path.join(output_dir, "business_analysis")
    os.makedirs(artifact_dir, exist_ok=True)
    json_path = os.path.join(artifact_dir, "kpi_catalog.json")
    csv_path = os.path.join(artifact_dir, "kpi_catalog.csv")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(catalog, handle, indent=2, ensure_ascii=False)
    with open(csv_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=KPI_COLUMNS)
        writer.writeheader()
        for row in catalog.get("kpis") or []:
            csv_row = dict(row)
            csv_row["source_columns"] = "|".join(row.get("source_columns") or [])
            csv_row["dimensions"] = "|".join(row.get("dimensions") or [])
            writer.writerow({column: csv_row.get(column, "") for column in KPI_COLUMNS})
    return {
        "kpi_catalog_json": json_path,
        "kpi_catalog_csv": csv_path,
    }
