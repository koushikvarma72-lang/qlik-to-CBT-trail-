import csv
import json
import os
import tempfile
import unittest

from flask import Flask

from backend.integrations.qvd_routes import register_qvd_routes
from qvd_business_analysis.glossary_summary import generate_business_glossary, generate_executive_summary
from qvd_business_analysis.lineage_generator import generate_lineage, write_lineage_artifact
from qvd_business_analysis.reconciliation_rules import (
    generate_reconciliation_rules,
    write_reconciliation_artifacts,
)


def write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


def build_fixture(output_dir):
    business_dir = os.path.join(output_dir, "business_analysis")
    os.makedirs(business_dir, exist_ok=True)
    write_json(os.path.join(output_dir, "qvd_inspection.json"), {
        "uploaded_files": [{"file_name": "source.qvd"}],
        "tables": [{
            "summary": {
                "file_name": "source.qvd",
                "table_name": "Source Table",
                "no_of_records": "100",
                "field_count": 6,
            }
        }],
    })
    write_json(os.path.join(business_dir, "business_entities.json"), {
        "summary": {
            "dimensions_count": 3,
            "measures_count": 1,
            "dates_count": 1,
            "flags_count": 0,
            "identifiers_count": 0,
            "hierarchies_count": 1,
        },
        "dimensions": [
            {"source_column": "Region", "target_column": "region", "entity_type": "DIMENSION"},
            {"source_column": "Country", "target_column": "country", "entity_type": "DIMENSION"},
            {"source_column": "Category", "target_column": "category", "entity_type": "DIMENSION"},
        ],
        "measures": [
            {"source_column": "ActualSales", "target_column": "actual_sales", "entity_type": "MEASURE"},
        ],
        "dates": [
            {"source_column": "Calendar.Date", "target_column": "calendar_date", "entity_type": "DATE"},
        ],
        "flags": [],
        "identifiers": [],
        "hierarchies": [{
            "source_column": "Region > Country",
            "target_column": "region > country",
            "business_name": "Region > Country",
            "entity_type": "HIERARCHY",
            "levels": [
                {"source_column": "Region", "target_column": "region"},
                {"source_column": "Country", "target_column": "country"},
            ],
        }],
    })
    write_json(os.path.join(business_dir, "kpi_catalog.json"), {
        "kpi_count": 1,
        "kpis": [{
            "kpi_name": "Actual Sales",
            "business_description": "Actual Sales derived from ActualSales.",
            "source_columns": ["ActualSales"],
            "recommended_formula": "SUM(actual_sales)",
            "aggregation_type": "SUM",
            "grain": "By Region, Country, Calendar Date",
            "dimensions": ["region", "country", "category"],
            "date_column": "calendar_date",
            "confidence": 0.86,
            "reason": "Additive business metric naming suggests summation.",
        }],
    })
    with open(os.path.join(output_dir, "approved_databricks_mapping.csv"), "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["source_column", "target_table", "target_column", "target_type"])
        writer.writeheader()
        writer.writerow({
            "source_column": "ActualSales",
            "target_table": "sales_table",
            "target_column": "actual_sales",
            "target_type": "DECIMAL(18,2)",
        })


class QvdLineageReconciliationTests(unittest.TestCase):
    def test_lineage_node_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_fixture(tmp)
            lineage = generate_lineage(tmp)
            nodes = {node["id"]: node for node in lineage["nodes"]}

        self.assertEqual(nodes["source_qvd"]["label"], "source.qvd")
        self.assertEqual(nodes["bronze_table"]["type"], "bronze")
        self.assertEqual(nodes["silver_table"]["type"], "silver")
        self.assertIn("dimension_region", nodes)
        self.assertIn("measure_actual_sales", nodes)
        self.assertIn("hierarchy_region_country", nodes)
        self.assertIn("kpi_actual_sales", nodes)

    def test_lineage_edge_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_fixture(tmp)
            lineage = generate_lineage(tmp)
            edge_pairs = {(edge["from"], edge["to"]) for edge in lineage["edges"]}

        self.assertIn(("source_qvd", "bronze_table"), edge_pairs)
        self.assertIn(("gold_kpi", "kpi_actual_sales"), edge_pairs)
        self.assertIn(("measure_actual_sales", "kpi_actual_sales"), edge_pairs)
        self.assertIn(("dimension_region", "kpi_actual_sales"), edge_pairs)

    def test_reconciliation_rule_generation_from_kpi_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_fixture(tmp)
            payload = generate_reconciliation_rules(tmp)

        self.assertGreaterEqual(payload["rule_count"], 3)
        self.assertTrue(any(rule["rule_name"] == "reconcile_actual_sales" for rule in payload["rules"]))
        self.assertTrue(any(rule["date_grain"] == "month" for rule in payload["rules"]))

    def test_databricks_sql_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_fixture(tmp)
            payload = generate_reconciliation_rules(tmp)
            base_rule = next(rule for rule in payload["rules"] if rule["rule_name"] == "reconcile_actual_sales")

        self.assertIn("SELECT SUM(actual_sales) AS actual_sales", base_rule["databricks_sql"])
        self.assertIn("FROM main.qvd_raw.sales_table", base_rule["databricks_sql"])
        self.assertEqual(base_rule["aggregation"], "SUM")
        self.assertIn("absolute_variance", base_rule["comparison_sql"])
        self.assertEqual(base_rule["qlik_expression_placeholder"], "SUM(ActualSales)")

    def test_business_glossary_and_executive_summary_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_fixture(tmp)
            lineage = generate_lineage(tmp)
            reconciliation = generate_reconciliation_rules(tmp)
            glossary = generate_business_glossary(tmp)
            summary = generate_executive_summary(tmp, lineage, reconciliation, glossary)

        self.assertGreaterEqual(glossary["glossary_count"], 1)
        self.assertTrue(any(term["term_type"] == "KPI" for term in glossary["terms"]))
        self.assertEqual(summary["kpi_count"], 1)
        self.assertGreater(summary["migration_readiness_score"], 0)

    def test_route_artifact_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "lineage-session"
            output_dir = os.path.join(tmp, session_id, "qvd_outputs")
            build_fixture(output_dir)
            app = Flask(__name__)
            register_qvd_routes(app, tmp)
            response = app.test_client().post(
                f"/api/qvd/business-analysis/lineage-reconciliation/{session_id}"
            )
            payload = response.get_json()
            lineage_exists = os.path.exists(os.path.join(output_dir, "business_analysis", "lineage.json"))
            rules_json_exists = os.path.exists(os.path.join(output_dir, "business_analysis", "reconciliation_rules.json"))
            rules_md_exists = os.path.exists(os.path.join(output_dir, "business_analysis", "reconciliation_rules.md"))
            glossary_exists = os.path.exists(os.path.join(output_dir, "business_analysis", "business_glossary.json"))
            summary_exists = os.path.exists(os.path.join(output_dir, "business_analysis", "executive_summary.json"))

        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(payload["lineage_nodes"], 5)
        self.assertGreaterEqual(payload["reconciliation_rule_count"], 3)
        self.assertGreaterEqual(payload["glossary_count"], 1)
        self.assertGreater(payload["migration_readiness_score"], 0)
        self.assertTrue(lineage_exists)
        self.assertTrue(rules_json_exists)
        self.assertTrue(rules_md_exists)
        self.assertTrue(glossary_exists)
        self.assertTrue(summary_exists)

    def test_artifact_writers(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_fixture(tmp)
            lineage_path = write_lineage_artifact(tmp, generate_lineage(tmp))
            reconciliation_paths = write_reconciliation_artifacts(tmp, generate_reconciliation_rules(tmp))
            self.assertTrue(os.path.exists(lineage_path))
            self.assertTrue(os.path.exists(reconciliation_paths["reconciliation_rules_json"]))
            self.assertTrue(os.path.exists(reconciliation_paths["reconciliation_rules_md"]))


if __name__ == "__main__":
    unittest.main()
