"""Generate business documentation for QVD analysis artifacts."""

from __future__ import annotations

import json
import os


def _read_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _bullet_rows(rows: list[dict], label_key: str = "business_name", limit: int = 20) -> str:
    if not rows:
        return "- None detected.\n"
    lines = []
    for row in rows[:limit]:
        label = row.get(label_key) or row.get("source_column") or row.get("kpi_name") or "Unnamed"
        source = row.get("source_column") or ", ".join(row.get("source_columns") or [])
        reason = row.get("reason") or row.get("business_description") or ""
        lines.append(f"- **{label}** (`{source}`): {reason}")
    return "\n".join(lines) + "\n"


def _source_overview(output_dir: str) -> dict:
    inspection = _read_json(os.path.join(output_dir, "qvd_inspection.json"))
    tables = inspection.get("tables") or []
    if not tables:
        return {"file_name": "", "table_name": "", "records": "", "fields": ""}
    summary = tables[0].get("summary") or {}
    return {
        "file_name": summary.get("file_name", ""),
        "table_name": summary.get("table_name", ""),
        "records": summary.get("no_of_records", ""),
        "fields": summary.get("field_count", ""),
    }


def render_business_documentation(output_dir: str, entities: dict, kpi_catalog: dict) -> str:
    source = _source_overview(output_dir)
    summary = entities.get("summary") or {}
    kpis = kpi_catalog.get("kpis") or []

    return (
        "# Business Analysis Accelerator\n\n"
        "## Executive Summary\n\n"
        f"The QVD source contains **{summary.get('dimensions_count', 0)} dimensions**, "
        f"**{summary.get('measures_count', 0)} measures**, **{summary.get('dates_count', 0)} date fields**, "
        f"and **{len(kpis)} KPI candidates**. These outputs are inferred from QVD metadata, optional row previews, "
        "column profiling, and mapping artifacts when available.\n\n"
        "## Source QVD Overview\n\n"
        f"- Source file: `{source.get('file_name', '')}`\n"
        f"- Source table: `{source.get('table_name', '')}`\n"
        f"- Records: `{source.get('records', '')}`\n"
        f"- Fields: `{source.get('fields', '')}`\n\n"
        "## Detected Business Dimensions\n\n"
        f"{_bullet_rows(entities.get('dimensions') or [])}\n"
        "## Detected Measures\n\n"
        f"{_bullet_rows(entities.get('measures') or [])}\n"
        "## KPI Catalog\n\n"
        f"{_bullet_rows(kpis, 'kpi_name')}\n"
        "## Candidate Hierarchies\n\n"
        f"{_bullet_rows(entities.get('hierarchies') or [])}\n"
        "## Recommended Databricks Model\n\n"
        "- **Bronze table:** Raw converted QVD data with ingestion audit columns.\n"
        "- **Silver fact table:** Cleaned business fact table using approved column names and data types.\n"
        "- **Gold KPI table:** Aggregated KPI-ready table grouped by approved dimensions, date fields, and hierarchy levels.\n\n"
        "## Assumptions And Limitations\n\n"
        "- Entity and KPI classifications are rule-based and should be reviewed with business owners.\n"
        "- KPI formulas are recommendations based on generic naming patterns and detected metadata.\n"
        "- Hierarchies are candidates inferred from common business field naming patterns.\n"
        "- Row-level semantics, source system definitions, and official KPI definitions may require manual confirmation.\n"
    )


def write_business_documentation(output_dir: str, entities: dict, kpi_catalog: dict) -> str:
    artifact_dir = os.path.join(output_dir, "business_analysis")
    os.makedirs(artifact_dir, exist_ok=True)
    path = os.path.join(artifact_dir, "business_analysis.md")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(render_business_documentation(output_dir, entities, kpi_catalog))
    return path
