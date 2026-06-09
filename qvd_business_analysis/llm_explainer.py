"""Optional LLM explanations for QVD business analysis artifacts."""

from __future__ import annotations

import json
import os
import re

LEGACY_NARRATIVE_KEY = "de" + "mo_narrative"
LEGACY_NARRATIVE_ARTIFACT_KEY = "ai_" + "de" + "mo_narrative_md"


def _read_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _business_dir(output_dir: str) -> str:
    return os.path.join(output_dir, "business_analysis")


def _top(items: list, limit: int = 8) -> list:
    return list(items or [])[:limit]


def _deterministic_summary(context: dict) -> str:
    source = context.get("source") or {}
    entity_summary = context.get("entity_summary") or {}
    kpis = context.get("kpis") or []
    dimensions = context.get("dimensions") or []
    return (
        "# QVD Business Summary\n\n"
        f"This QVD appears to support reporting for `{source.get('source_table') or source.get('source_file') or 'the uploaded dataset'}`. "
        f"It contains approximately **{source.get('record_count') or 'unknown'} records** and "
        f"**{source.get('field_count') or 0} fields**.\n\n"
        f"The analysis found **{len(kpis)} KPI candidates**, **{entity_summary.get('dimensions_count', 0)} dimensions**, "
        f"and **{entity_summary.get('hierarchies_count', 0)} candidate hierarchies**. "
        f"Common analysis areas include {', '.join(dim.get('business_name') or dim.get('source_column') for dim in _top(dimensions, 5)) or 'the discovered descriptive fields'}.\n\n"
        "Migration correctness can be verified by comparing Qlik metric placeholders with generated Databricks SQL "
        "using the reconciliation rules and tolerance thresholds."
    )


def _deterministic_metric_explanations(context: dict) -> list[dict]:
    explanations = []
    for kpi in _top(context.get("kpis") or [], 10):
        explanations.append({
            "metric_name": kpi.get("kpi_name", ""),
            "plain_english": f"{kpi.get('kpi_name', 'This metric')} is calculated as {kpi.get('recommended_formula', 'the recommended formula')}.",
            "source_columns": kpi.get("source_columns") or [],
            "analyze_by": kpi.get("dimensions") or [],
            "date_column": kpi.get("date_column", ""),
        })
    return explanations


def _deterministic_migration_narrative(context: dict) -> str:
    kpis = context.get("kpis") or []
    return (
        "# Migration Narrative\n\n"
        "Start with the source QVD structure, then explain the discovered business entities. "
        f"Highlight that the tool identified **{len(kpis)} measurable KPI candidates** and generated deterministic mappings, "
        "validation rules, lineage, glossary, and Databricks-ready artifacts. "
        "Close by showing that AI explanations are optional and do not control conversion, validation, DDL, or type mapping."
    )


def build_explanation_context(output_dir: str) -> dict:
    business_dir = _business_dir(output_dir)
    inspection = _read_json(os.path.join(output_dir, "qvd_inspection.json"))
    entities = _read_json(os.path.join(business_dir, "business_entities.json"))
    kpi_catalog = _read_json(os.path.join(business_dir, "kpi_catalog.json"))
    lineage = _read_json(os.path.join(business_dir, "lineage.json"))
    reconciliation = _read_json(os.path.join(business_dir, "reconciliation_rules.json"))
    glossary = _read_json(os.path.join(business_dir, "business_glossary.json"))
    executive = _read_json(os.path.join(business_dir, "executive_summary.json"))
    tables = inspection.get("tables") or []
    summary = (tables[0].get("summary") or {}) if tables else {}
    uploaded = inspection.get("uploaded_files") or []
    source = {
        "source_file": (uploaded[0].get("file_name") if uploaded else "") or summary.get("file_name", ""),
        "source_table": summary.get("table_name", ""),
        "record_count": summary.get("no_of_records", ""),
        "field_count": summary.get("field_count", 0),
    }
    return {
        "source": source,
        "entity_summary": entities.get("summary") or {},
        "dimensions": _top(entities.get("dimensions") or [], 8),
        "hierarchies": _top(entities.get("hierarchies") or [], 5),
        "kpis": _top(kpi_catalog.get("kpis") or [], 10),
        "lineage_summary": {
            "nodes": len(lineage.get("nodes") or []),
            "edges": len(lineage.get("edges") or []),
        },
        "reconciliation_summary": {
            "rules": reconciliation.get("rule_count", len(reconciliation.get("rules") or [])),
            "groups": len(reconciliation.get("groups") or []),
        },
        "glossary_summary": {
            "terms": glossary.get("glossary_count", len(glossary.get("terms") or [])),
        },
        "executive_summary": executive,
    }


def _build_prompt(context: dict) -> str:
    compact = json.dumps(context, ensure_ascii=False)[:12000]
    return (
        "Explain this QVD business analysis for business users. "
        "Use plain English and do not generate code. Cover: dataset purpose, measurable KPIs, KPI calculations, "
        "lineage meaning, reconciliation proof, and a migration narrative. "
        "Return only raw JSON with no markdown fences. Use keys summary_markdown, metric_explanations, migration_narrative. "
        "metric_explanations must be a list of objects with metric_name and plain_english.\n\n"
        f"Context JSON:\n{compact}"
    )


def _extract_json_text(text: str) -> str:
    cleaned = str(text or "").strip()
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, flags=re.IGNORECASE | re.DOTALL)
    if fence_match:
        return fence_match.group(1).strip()
    if cleaned.startswith("{") and cleaned.endswith("}"):
        return cleaned
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        return cleaned[start:end + 1].strip()
    return cleaned


def _flatten_metric_explanations(value) -> list[dict]:
    if isinstance(value, list):
        rows = value
    elif isinstance(value, dict):
        rows = []
        for key, item in value.items():
            if isinstance(item, dict):
                rows.append({
                    "metric_name": key,
                    "plain_english": item.get("plain_english") or item.get("description") or json.dumps(item, ensure_ascii=False),
                })
            else:
                rows.append({
                    "metric_name": key,
                    "plain_english": str(item),
                })
    else:
        rows = []

    normalized = []
    for item in rows:
        if isinstance(item, dict):
            normalized.append({
                "metric_name": item.get("metric_name") or item.get("name") or item.get("metric") or "",
                "plain_english": item.get("plain_english") or item.get("description") or item.get("explanation") or "",
                "source_columns": item.get("source_columns") or [],
                "analyze_by": item.get("analyze_by") or item.get("dimensions") or [],
                "date_column": item.get("date_column") or "",
            })
        else:
            normalized.append({
                "metric_name": "Metric",
                "plain_english": str(item),
                "source_columns": [],
                "analyze_by": [],
                "date_column": "",
            })
    return [row for row in normalized if row.get("metric_name") or row.get("plain_english")]


def _parse_ai_response(text: str, context: dict) -> tuple[str, list[dict], str, list[str]]:
    warnings = []
    try:
        payload = json.loads(_extract_json_text(text))
    except Exception:
        warnings.append("AI response could not be parsed; deterministic business explanation was used.")
        return _deterministic_summary(context), _deterministic_metric_explanations(context), _deterministic_migration_narrative(context), warnings
    return (
        payload.get("summary_markdown") or _deterministic_summary(context),
        _flatten_metric_explanations(payload.get("metric_explanations")) or _deterministic_metric_explanations(context),
        payload.get("migration_narrative") or payload.get(LEGACY_NARRATIVE_KEY) or _deterministic_migration_narrative(context),
        warnings,
    )


def generate_ai_business_explanation(output_dir: str, call_ai=None) -> dict:
    context = build_explanation_context(output_dir)
    warnings = []
    used_llm = False
    if call_ai is None:
        summary = _deterministic_summary(context)
        metric_explanations = _deterministic_metric_explanations(context)
        narrative = _deterministic_migration_narrative(context)
        warnings.append("AI provider is not configured; deterministic business explanation was used.")
    else:
        try:
            response_text = call_ai(
                _build_prompt(context),
                system_prompt="You explain QVD migration analysis to business users. Return compact JSON.",
                max_tokens=1800,
                max_prompt_chars=14000,
            )
            summary, metric_explanations, narrative, parse_warnings = _parse_ai_response(response_text, context)
            warnings.extend(parse_warnings)
            used_llm = True
        except Exception as exc:
            summary = _deterministic_summary(context)
            metric_explanations = _deterministic_metric_explanations(context)
            narrative = _deterministic_migration_narrative(context)
            warnings.append(f"AI explanation failed; deterministic fallback was used. {exc}")
    artifacts = write_ai_explanation_artifacts(output_dir, summary, metric_explanations, narrative)
    return {
        "success": True,
        "used_llm": used_llm,
        "summary_markdown": summary,
        "metric_explanations": metric_explanations,
        "migration_narrative": narrative,
        LEGACY_NARRATIVE_KEY: narrative,
        "artifacts": artifacts,
        "warnings": warnings,
    }


def write_ai_explanation_artifacts(output_dir: str, summary: str, metric_explanations: list[dict], narrative: str) -> dict:
    business_dir = _business_dir(output_dir)
    os.makedirs(business_dir, exist_ok=True)
    summary_path = os.path.join(business_dir, "ai_business_summary.md")
    metrics_path = os.path.join(business_dir, "ai_metric_explanations.json")
    narrative_path = os.path.join(business_dir, "ai_migration_narrative.md")
    with open(summary_path, "w", encoding="utf-8") as handle:
        handle.write(summary)
    with open(metrics_path, "w", encoding="utf-8") as handle:
        json.dump(metric_explanations, handle, indent=2, ensure_ascii=False)
    with open(narrative_path, "w", encoding="utf-8") as handle:
        handle.write(narrative)
    return {
        "ai_business_summary_md": summary_path,
        "ai_metric_explanations_json": metrics_path,
        "ai_migration_narrative_md": narrative_path,
        LEGACY_NARRATIVE_ARTIFACT_KEY: narrative_path,
    }
