"""Generate reconciliation rule artifacts for QVD business KPIs."""

from __future__ import annotations

import csv
import json
import os
import re


MAJOR_DIMENSION_TOKENS = ("region", "country", "category", "franchise")
DEFAULT_COMPARISON_TYPE = "absolute_or_percentage_variance"
DEFAULT_TOLERANCE = "0.01%"


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


def _target_table(output_dir: str, mapping_rows: list[dict]) -> str:
    for row in mapping_rows:
        if row.get("target_table"):
            return _snake_case(row["target_table"])
    inspection = _read_json(os.path.join(output_dir, "qvd_inspection.json"))
    tables = inspection.get("tables") or []
    summary = (tables[0].get("summary") or {}) if tables else {}
    return _snake_case(summary.get("table_name") or summary.get("file_name") or "qvd_table")


def _source_to_target(mapping_rows: list[dict]) -> dict[str, str]:
    result = {}
    for row in mapping_rows:
        source = str(row.get("source_column") or "").casefold()
        target = row.get("target_column")
        if source and target:
            result[source] = target
    return result


def _formula_parts(formula: str) -> tuple[str, str]:
    match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*([^)]+?)\s*\)\s*$", str(formula or ""))
    if not match:
        return "SUM", _snake_case(formula or "value")
    return match.group(1).upper(), _snake_case(match.group(2))


def _sql_alias(kpi_name: str) -> str:
    return _snake_case(kpi_name)


def _qlik_expression(kpi: dict, aggregation: str) -> str:
    source_columns = kpi.get("source_columns") or []
    source = source_columns[0] if source_columns else kpi.get("kpi_name") or "Measure"
    return f"{aggregation}({source})"


def _comparison_sql(databricks_sql: str, alias: str) -> str:
    return (
        "WITH qlik_metric AS (\n"
        f"  SELECT CAST(:qlik_value AS DOUBLE) AS {alias}\n"
        "),\n"
        "databricks_metric AS (\n"
        f"  {databricks_sql}\n"
        ")\n"
        "SELECT\n"
        f"  qlik_metric.{alias} AS qlik_value,\n"
        f"  databricks_metric.{alias} AS databricks_value,\n"
        f"  databricks_metric.{alias} - qlik_metric.{alias} AS absolute_variance,\n"
        f"  CASE WHEN qlik_metric.{alias} = 0 THEN NULL ELSE "
        f"(databricks_metric.{alias} - qlik_metric.{alias}) / qlik_metric.{alias} END AS percentage_variance\n"
        "FROM qlik_metric CROSS JOIN databricks_metric"
    )


def _dimension_targets(entities: dict, mapping_by_source: dict[str, str]) -> list[str]:
    dimensions = []
    for row in entities.get("dimensions") or []:
        source = row.get("source_column") or ""
        target = row.get("target_column") or mapping_by_source.get(source.casefold()) or _snake_case(source)
        if target:
            dimensions.append(target)
    return dimensions


def _major_dimensions(dimensions: list[str]) -> list[str]:
    selected = []
    for token in MAJOR_DIMENSION_TOKENS:
        selected.extend(dim for dim in dimensions if token in dim.casefold() and dim not in selected)
    return selected[:4]


def _base_rule(kpi: dict, table_name: str) -> dict:
    aggregation, target_column = _formula_parts(kpi.get("recommended_formula"))
    alias = _sql_alias(kpi.get("kpi_name") or target_column)
    qlik_metric = _qlik_expression(kpi, aggregation)
    databricks_sql = f"SELECT {aggregation}({target_column}) AS {alias} FROM {table_name}"
    return {
        "rule_name": f"reconcile_{alias}",
        "metric_name": kpi.get("kpi_name") or alias,
        "qlik_metric": qlik_metric,
        "qlik_expression_placeholder": qlik_metric,
        "databricks_sql": databricks_sql,
        "aggregation": aggregation,
        "comparison_sql": _comparison_sql(databricks_sql, alias),
        "comparison_type": DEFAULT_COMPARISON_TYPE,
        "tolerance": DEFAULT_TOLERANCE,
        "dimensions": [],
        "date_grain": "overall",
        "expected_result": "Qlik and Databricks aggregate values should match within tolerance.",
    }


def _date_rule(kpi: dict, table_name: str) -> dict | None:
    date_column = kpi.get("date_column")
    if not date_column:
        return None
    aggregation, target_column = _formula_parts(kpi.get("recommended_formula"))
    alias = _sql_alias(kpi.get("kpi_name") or target_column)
    qlik_metric = f"{_qlik_expression(kpi, aggregation)} by month({date_column})"
    databricks_sql = (
        f"SELECT DATE_TRUNC('month', {date_column}) AS reconciliation_month, "
        f"{aggregation}({target_column}) AS {alias} "
        f"FROM {table_name} GROUP BY DATE_TRUNC('month', {date_column})"
    )
    return {
        "rule_name": f"reconcile_{alias}_by_month",
        "metric_name": kpi.get("kpi_name") or alias,
        "qlik_metric": qlik_metric,
        "qlik_expression_placeholder": qlik_metric,
        "databricks_sql": databricks_sql,
        "aggregation": aggregation,
        "comparison_sql": (
            "WITH qlik_metric AS (\n"
            f"  SELECT reconciliation_month, CAST(:qlik_value AS DOUBLE) AS {alias}\n"
            "),\n"
            "databricks_metric AS (\n"
            f"  {databricks_sql}\n"
            ")\n"
            f"SELECT COALESCE(q.reconciliation_month, d.reconciliation_month) AS reconciliation_month, "
            f"q.{alias} AS qlik_value, d.{alias} AS databricks_value, "
            f"d.{alias} - q.{alias} AS absolute_variance "
            "FROM qlik_metric q FULL OUTER JOIN databricks_metric d USING (reconciliation_month)"
        ),
        "comparison_type": DEFAULT_COMPARISON_TYPE,
        "tolerance": DEFAULT_TOLERANCE,
        "dimensions": [date_column],
        "date_grain": "month",
        "expected_result": "Monthly Qlik and Databricks aggregate values should match within tolerance.",
    }


def _dimension_rule(kpi: dict, table_name: str, dimension: str) -> dict:
    aggregation, target_column = _formula_parts(kpi.get("recommended_formula"))
    alias = _sql_alias(kpi.get("kpi_name") or target_column)
    qlik_metric = f"{_qlik_expression(kpi, aggregation)} by {dimension}"
    databricks_sql = (
        f"SELECT {dimension}, {aggregation}({target_column}) AS {alias} "
        f"FROM {table_name} GROUP BY {dimension}"
    )
    return {
        "rule_name": f"reconcile_{alias}_by_{_snake_case(dimension)}",
        "metric_name": kpi.get("kpi_name") or alias,
        "qlik_metric": qlik_metric,
        "qlik_expression_placeholder": qlik_metric,
        "databricks_sql": databricks_sql,
        "aggregation": aggregation,
        "comparison_sql": (
            "WITH qlik_metric AS (\n"
            f"  SELECT {dimension}, CAST(:qlik_value AS DOUBLE) AS {alias}\n"
            "),\n"
            "databricks_metric AS (\n"
            f"  {databricks_sql}\n"
            ")\n"
            f"SELECT COALESCE(q.{dimension}, d.{dimension}) AS {dimension}, "
            f"q.{alias} AS qlik_value, d.{alias} AS databricks_value, "
            f"d.{alias} - q.{alias} AS absolute_variance "
            f"FROM qlik_metric q FULL OUTER JOIN databricks_metric d USING ({dimension})"
        ),
        "comparison_type": DEFAULT_COMPARISON_TYPE,
        "tolerance": DEFAULT_TOLERANCE,
        "dimensions": [dimension],
        "date_grain": "overall",
        "expected_result": "Grouped Qlik and Databricks aggregate values should match within tolerance.",
    }


def generate_reconciliation_rules(output_dir: str, catalog_schema: str = "main.qvd_raw") -> dict:
    kpi_catalog = _read_json(os.path.join(_business_analysis_dir(output_dir), "kpi_catalog.json"))
    entities = _read_json(os.path.join(_business_analysis_dir(output_dir), "business_entities.json"))
    mapping_rows = _mapping_rows(output_dir)
    mapping_by_source = _source_to_target(mapping_rows)
    target_table = _target_table(output_dir, mapping_rows)
    table_name = f"{catalog_schema}.{target_table}"
    dimensions = _dimension_targets(entities, mapping_by_source)
    major_dimensions = _major_dimensions(dimensions)

    rules = []
    for kpi in kpi_catalog.get("kpis") or []:
        rules.append(_base_rule(kpi, table_name))
        date_rule = _date_rule(kpi, table_name)
        if date_rule:
            rules.append(date_rule)
        for dimension in major_dimensions:
            rules.append(_dimension_rule(kpi, table_name, dimension))
    grouped = group_rules_by_metric(rules)

    return {
        "target_table": table_name,
        "rule_count": len(rules),
        "kpi_check_count": sum(1 for rule in rules if not rule.get("dimensions")),
        "date_check_count": sum(1 for rule in rules if rule.get("date_grain") == "month"),
        "dimension_check_count": sum(1 for rule in rules if rule.get("dimensions") and rule.get("date_grain") != "month"),
        "groups": grouped,
        "rules": rules,
    }


def group_rules_by_metric(rules: list[dict]) -> list[dict]:
    grouped: dict[str, dict] = {}
    for rule in rules:
        metric = rule.get("metric_name") or rule.get("rule_name") or "Metric"
        group = grouped.setdefault(metric, {
            "metric_name": metric,
            "rules": [],
            "total_checks": 0,
            "has_total_check": False,
            "date_check_count": 0,
            "dimension_check_count": 0,
            "tolerance": rule.get("tolerance", DEFAULT_TOLERANCE),
        })
        group["rules"].append(rule)
        group["total_checks"] += 1
        if not rule.get("dimensions"):
            group["has_total_check"] = True
        elif rule.get("date_grain") == "month":
            group["date_check_count"] += 1
        else:
            group["dimension_check_count"] += 1
    return list(grouped.values())


def render_reconciliation_markdown(payload: dict) -> str:
    lines = [
        "# Reconciliation Rules",
        "",
        f"Target table: `{payload.get('target_table', '')}`",
        f"Generated rules: **{payload.get('rule_count', 0)}**",
        "",
        "| Rule | Qlik Placeholder | Databricks SQL | Tolerance |",
        "| --- | --- | --- | --- |",
    ]
    for rule in payload.get("rules") or []:
        sql = str(rule.get("databricks_sql") or "").replace("|", "\\|")
        qlik = str(rule.get("qlik_expression_placeholder") or "").replace("|", "\\|")
        lines.append(
            f"| {rule.get('rule_name', '')} | `{qlik}` | `{sql}` | {rule.get('tolerance', '')} |"
        )
    lines.extend([
        "",
        "## Notes",
        "",
        "- These rules are generated from business metadata and approved mappings.",
        "- Actual Qlik Engine comparison execution is not performed in this phase.",
        "- Review grouped rules before using them as production reconciliation checks.",
    ])
    return "\n".join(lines) + "\n"


def write_reconciliation_artifacts(output_dir: str, payload: dict) -> dict:
    artifact_dir = _business_analysis_dir(output_dir)
    os.makedirs(artifact_dir, exist_ok=True)
    json_path = os.path.join(artifact_dir, "reconciliation_rules.json")
    markdown_path = os.path.join(artifact_dir, "reconciliation_rules.md")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    with open(markdown_path, "w", encoding="utf-8") as handle:
        handle.write(render_reconciliation_markdown(payload))
    return {
        "reconciliation_rules_json": json_path,
        "reconciliation_rules_md": markdown_path,
    }
