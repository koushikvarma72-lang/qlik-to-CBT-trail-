"""Generate generic business lineage for the QVD to Databricks flow."""

from __future__ import annotations

import csv
import json
import os
import re


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


def _snake_case(value: str) -> str:
    text = re.sub(r"^%+", "", str(value or "").strip())
    text = re.sub(r"[./\\\s]+", "_", text)
    text = re.sub(r"[^0-9A-Za-z_]+", "_", text)
    text = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", text)
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
    text = re.sub(r"_+", "_", text).strip("_").lower()
    text = re.sub(r"\b([a-z]{1,2})_([a-z])(?=_)", r"\1\2", text)
    return text or "unnamed"


def _business_analysis_dir(output_dir: str) -> str:
    return os.path.join(output_dir, "business_analysis")


def _mapping_rows(output_dir: str) -> list[dict]:
    rows = _read_csv(os.path.join(output_dir, "approved_databricks_mapping.csv"))
    if rows:
        return rows
    payload = _read_json(os.path.join(output_dir, "approved_databricks_mapping.json"))
    return payload.get("mapping_rows") or []


def _source_qvd_label(inspection: dict) -> str:
    uploaded = inspection.get("uploaded_files") or []
    if uploaded and uploaded[0].get("file_name"):
        return uploaded[0]["file_name"]
    tables = inspection.get("tables") or []
    summary = (tables[0].get("summary") or {}) if tables else {}
    return summary.get("file_name") or summary.get("table_name") or "uploaded_qvd"


def _target_table(output_dir: str, inspection: dict) -> str:
    for row in _mapping_rows(output_dir):
        if row.get("target_table"):
            return _snake_case(row["target_table"])
    tables = inspection.get("tables") or []
    summary = (tables[0].get("summary") or {}) if tables else {}
    return _snake_case(summary.get("table_name") or summary.get("file_name") or "qvd_table")


def _domain_from_table(target_table: str, entities: dict) -> str:
    measures = entities.get("measures") or []
    for measure in measures:
        name = _snake_case(measure.get("target_column") or measure.get("source_column"))
        tokens = [token for token in name.split("_") if token]
        for token in tokens:
            if token not in {"actual", "budget", "forecast", "py", "yoy", "sales", "amount", "units"}:
                return token
    return re.sub(r"(_raw|_fact|_table)$", "", target_table) or target_table


def _dedupe_nodes(nodes: list[dict]) -> list[dict]:
    seen = set()
    unique = []
    for node in nodes:
        node_id = node.get("id")
        if not node_id or node_id in seen:
            continue
        seen.add(node_id)
        unique.append(node)
    return unique


def _node_id(prefix: str, value: str) -> str:
    return f"{prefix}_{_snake_case(value)}"


def _entity_nodes(entities: dict, entity_key: str, node_type: str) -> list[dict]:
    nodes = []
    for entity in entities.get(entity_key) or []:
        label = entity.get("business_name") or entity.get("source_column") or node_type
        nodes.append({
            "id": _node_id(node_type, entity.get("target_column") or entity.get("source_column") or label),
            "label": label,
            "type": node_type,
            "source_column": entity.get("source_column", ""),
            "target_column": entity.get("target_column", ""),
        })
    return nodes


def generate_lineage(output_dir: str) -> dict:
    inspection = _read_json(os.path.join(output_dir, "qvd_inspection.json"))
    entities = _read_json(os.path.join(_business_analysis_dir(output_dir), "business_entities.json"))
    kpi_catalog = _read_json(os.path.join(_business_analysis_dir(output_dir), "kpi_catalog.json"))

    source_label = _source_qvd_label(inspection)
    target_table = _target_table(output_dir, inspection)
    domain = _domain_from_table(target_table, entities)
    silver_type = "fact" if entities.get("measures") else "dim"

    nodes = [
        {"id": "source_qvd", "label": source_label, "type": "source"},
        {"id": "bronze_table", "label": f"bronze.{target_table}_raw", "type": "bronze"},
        {"id": "silver_table", "label": f"silver.{silver_type}_{domain}", "type": "silver"},
        {"id": "gold_kpi", "label": f"gold.{domain}_kpis", "type": "gold"},
    ]
    edges = [
        {"from": "source_qvd", "to": "bronze_table", "label": "ingested as"},
        {"from": "bronze_table", "to": "silver_table", "label": "modeled in"},
        {"from": "silver_table", "to": "gold_kpi", "label": "aggregated into"},
    ]

    dimension_nodes = _entity_nodes(entities, "dimensions", "dimension")
    measure_nodes = _entity_nodes(entities, "measures", "measure")
    hierarchy_nodes = _entity_nodes(entities, "hierarchies", "hierarchy")
    nodes.extend([*dimension_nodes, *measure_nodes, *hierarchy_nodes])

    for node in [*dimension_nodes, *measure_nodes]:
        edges.append({"from": "source_qvd", "to": node["id"], "label": "provides field"})
        edges.append({"from": node["id"], "to": "silver_table", "label": "modeled in"})

    for hierarchy in entities.get("hierarchies") or []:
        hierarchy_id = _node_id("hierarchy", hierarchy.get("target_column") or hierarchy.get("source_column") or hierarchy.get("business_name"))
        edges.append({"from": "silver_table", "to": hierarchy_id, "label": "defines hierarchy"})
        for level in hierarchy.get("levels") or []:
            dimension_id = _node_id("dimension", level.get("target_column") or level.get("source_column"))
            edges.append({"from": dimension_id, "to": hierarchy_id, "label": "hierarchy level"})

    for kpi in kpi_catalog.get("kpis") or []:
        kpi_name = kpi.get("kpi_name") or "KPI"
        kpi_id = f"kpi_{_snake_case(kpi_name)}"
        nodes.append({
            "id": kpi_id,
            "label": kpi_name,
            "type": "kpi",
            "formula": kpi.get("recommended_formula", ""),
        })
        edges.append({"from": "gold_kpi", "to": kpi_id, "label": "aggregated into"})
        for source_column in kpi.get("source_columns") or []:
            measure_id = _node_id("measure", source_column)
            edges.append({"from": measure_id, "to": kpi_id, "label": "feeds KPI"})
        for dimension in kpi.get("dimensions") or []:
            dimension_id = _node_id("dimension", dimension)
            edges.append({"from": dimension_id, "to": kpi_id, "label": "slices KPI"})

    return {
        "nodes": _dedupe_nodes(nodes),
        "edges": edges,
    }


def write_lineage_artifact(output_dir: str, lineage: dict) -> str:
    artifact_dir = _business_analysis_dir(output_dir)
    os.makedirs(artifact_dir, exist_ok=True)
    path = os.path.join(artifact_dir, "lineage.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(lineage, handle, indent=2, ensure_ascii=False)
    return path
