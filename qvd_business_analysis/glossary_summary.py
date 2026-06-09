"""Generate business glossary and executive summary artifacts for QVD analysis."""

from __future__ import annotations

import csv
import json
import os


GLOSSARY_COLUMNS = [
    "term_type",
    "name",
    "business_definition",
    "source_columns",
    "dimensions_used",
    "date_grain",
    "owner",
    "confidence",
]


def _read_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _business_analysis_dir(output_dir: str) -> str:
    return os.path.join(output_dir, "business_analysis")


def _source_overview(output_dir: str) -> dict:
    inspection = _read_json(os.path.join(output_dir, "qvd_inspection.json"))
    uploaded_files = inspection.get("uploaded_files") or []
    tables = inspection.get("tables") or []
    summary = (tables[0].get("summary") or {}) if tables else {}
    return {
        "source_system": "QVD",
        "source_file": (uploaded_files[0].get("file_name") if uploaded_files else "") or summary.get("file_name", ""),
        "source_table": summary.get("table_name", ""),
        "record_count": summary.get("no_of_records", ""),
        "field_count": summary.get("field_count", 0),
        "file_count": len(uploaded_files),
    }


def generate_business_glossary(output_dir: str) -> dict:
    entities = _read_json(os.path.join(_business_analysis_dir(output_dir), "business_entities.json"))
    kpi_catalog = _read_json(os.path.join(_business_analysis_dir(output_dir), "kpi_catalog.json"))
    rows = []

    for kpi in kpi_catalog.get("kpis") or []:
        rows.append({
            "term_type": "KPI",
            "name": kpi.get("kpi_name", ""),
            "business_definition": kpi.get("business_description") or f"{kpi.get('kpi_name', 'KPI')} generated from discovered measure metadata.",
            "source_columns": kpi.get("source_columns") or [],
            "dimensions_used": kpi.get("dimensions") or [],
            "date_grain": kpi.get("date_column") or "not_specified",
            "owner": "Business owner to be assigned",
            "confidence": kpi.get("confidence", 0),
        })

    for key, term_type in (("dimensions", "DIMENSION"), ("hierarchies", "HIERARCHY")):
        for entity in entities.get(key) or []:
            rows.append({
                "term_type": term_type,
                "name": entity.get("business_name") or entity.get("source_column", ""),
                "business_definition": entity.get("reason") or f"{term_type.title()} discovered from QVD metadata.",
                "source_columns": [entity.get("source_column", "")],
                "dimensions_used": [],
                "date_grain": "not_applicable",
                "owner": "Business owner to be assigned",
                "confidence": entity.get("confidence", 0),
            })

    return {
        "glossary_count": len(rows),
        "terms": rows,
    }


def _readiness_score(entities: dict, kpi_catalog: dict, reconciliation: dict, output_dir: str) -> tuple[int, list[str]]:
    score = 40
    reasons = ["QVD metadata inspection completed."]
    if entities.get("summary", {}).get("dimensions_count", 0) > 0:
        score += 10
        reasons.append("Business dimensions detected.")
    if kpi_catalog.get("kpi_count", 0) > 0:
        score += 15
        reasons.append("KPI catalog generated.")
    if entities.get("summary", {}).get("hierarchies_count", 0) > 0:
        score += 10
        reasons.append("Candidate hierarchies detected.")
    if reconciliation.get("rule_count", 0) > 0:
        score += 15
        reasons.append("Reconciliation rules generated.")
    if os.path.exists(os.path.join(output_dir, "approved_databricks_mapping.csv")):
        score += 10
        reasons.append("Approved mapping artifact exists.")
    return min(score, 100), reasons


def generate_executive_summary(output_dir: str, lineage: dict, reconciliation: dict, glossary: dict) -> dict:
    entities = _read_json(os.path.join(_business_analysis_dir(output_dir), "business_entities.json"))
    kpi_catalog = _read_json(os.path.join(_business_analysis_dir(output_dir), "kpi_catalog.json"))
    source = _source_overview(output_dir)
    readiness_score, readiness_reasons = _readiness_score(entities, kpi_catalog, reconciliation, output_dir)
    entity_summary = entities.get("summary") or {}

    return {
        "source_system_overview": source,
        "kpi_count": kpi_catalog.get("kpi_count", 0),
        "dimension_count": entity_summary.get("dimensions_count", 0),
        "hierarchy_count": entity_summary.get("hierarchies_count", 0),
        "reconciliation_count": reconciliation.get("rule_count", 0),
        "glossary_count": glossary.get("glossary_count", 0),
        "lineage_node_count": len(lineage.get("nodes") or []),
        "lineage_edge_count": len(lineage.get("edges") or []),
        "migration_readiness_score": readiness_score,
        "readiness_reasons": readiness_reasons,
    }


def write_glossary_summary_artifacts(output_dir: str, glossary: dict, executive_summary: dict) -> dict:
    artifact_dir = _business_analysis_dir(output_dir)
    os.makedirs(artifact_dir, exist_ok=True)
    glossary_json = os.path.join(artifact_dir, "business_glossary.json")
    glossary_csv = os.path.join(artifact_dir, "business_glossary.csv")
    summary_json = os.path.join(artifact_dir, "executive_summary.json")

    with open(glossary_json, "w", encoding="utf-8") as handle:
        json.dump(glossary, handle, indent=2, ensure_ascii=False)
    with open(summary_json, "w", encoding="utf-8") as handle:
        json.dump(executive_summary, handle, indent=2, ensure_ascii=False)
    with open(glossary_csv, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=GLOSSARY_COLUMNS)
        writer.writeheader()
        for row in glossary.get("terms") or []:
            csv_row = dict(row)
            csv_row["source_columns"] = "|".join(row.get("source_columns") or [])
            csv_row["dimensions_used"] = "|".join(row.get("dimensions_used") or [])
            writer.writerow({column: csv_row.get(column, "") for column in GLOSSARY_COLUMNS})

    return {
        "business_glossary_json": glossary_json,
        "business_glossary_csv": glossary_csv,
        "executive_summary_json": summary_json,
    }
