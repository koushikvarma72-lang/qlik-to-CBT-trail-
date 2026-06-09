import csv
import json
import os
import tempfile
import unittest

from flask import Flask

from backend.integrations.qvd_routes import register_qvd_routes
from qvd_business_analysis.business_documentation import render_business_documentation, write_business_documentation
from qvd_business_analysis.kpi_catalog import generate_kpi_catalog, write_kpi_catalog_artifacts


def write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


def build_fixture(output_dir):
    os.makedirs(os.path.join(output_dir, "business_analysis"), exist_ok=True)
    entities = {
        "summary": {
            "dimensions_count": 2,
            "measures_count": 5,
            "dates_count": 1,
            "flags_count": 1,
            "identifiers_count": 0,
            "hierarchies_count": 1,
        },
        "dimensions": [
            {"source_column": "Customer", "target_column": "customer", "business_name": "Customer"},
            {"source_column": "Region", "target_column": "region", "business_name": "Region"},
        ],
        "measures": [
            {"source_column": "ActualSales", "target_column": "actual_sales", "entity_type": "MEASURE"},
            {"source_column": "BudgetSales", "target_column": "budget_sales", "entity_type": "MEASURE"},
            {"source_column": "ForecastSales", "target_column": "forecast_sales", "entity_type": "MEASURE"},
            {"source_column": "GrossMarginRate", "target_column": "gross_margin_rate", "entity_type": "MEASURE"},
            {"source_column": "Units", "target_column": "units", "entity_type": "MEASURE"},
        ],
        "dates": [{"source_column": "Calendar.Date", "target_column": "calendar_date", "business_name": "Calendar Date"}],
        "flags": [{"source_column": "ActualFlag", "target_column": "actual_flag", "entity_type": "FLAG"}],
        "identifiers": [],
        "hierarchies": [{"business_name": "Geography", "source_column": "Region > Country"}],
    }
    write_json(os.path.join(output_dir, "business_analysis", "business_entities.json"), entities)
    write_json(os.path.join(output_dir, "qvd_inspection.json"), {
        "tables": [{
            "summary": {
                "file_name": "source.qvd",
                "table_name": "Source",
                "no_of_records": "100",
                "field_count": 8,
            }
        }]
    })
    with open(os.path.join(output_dir, "approved_databricks_mapping.csv"), "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["source_column", "target_column", "target_type"])
        writer.writeheader()
        writer.writerow({"source_column": "ActualFlag", "target_column": "actual_flag", "target_type": "BOOLEAN"})
    return entities


def build_claims_fixture(output_dir):
    os.makedirs(os.path.join(output_dir, "business_analysis"), exist_ok=True)
    entities = {
        "summary": {
            "dimensions_count": 2,
            "measures_count": 2,
            "dates_count": 1,
            "flags_count": 0,
            "identifiers_count": 2,
            "hierarchies_count": 0,
        },
        "dimensions": [
            {"source_column": "ProviderState", "target_column": "provider_state", "business_name": "Provider State"},
            {"source_column": "ClaimType", "target_column": "claim_type", "business_name": "Claim Type"},
        ],
        "measures": [
            {"source_column": "ClaimAmount", "target_column": "claim_amount", "entity_type": "MEASURE"},
            {"source_column": "AllowedAmount", "target_column": "allowed_amount", "entity_type": "MEASURE"},
        ],
        "dates": [{"source_column": "ServiceDate", "target_column": "service_date", "business_name": "Service Date"}],
        "flags": [],
        "identifiers": [
            {"source_column": "PatientID", "target_column": "patient_id", "entity_type": "IDENTIFIER"},
            {"source_column": "ClaimID", "target_column": "claim_id", "entity_type": "IDENTIFIER"},
        ],
        "hierarchies": [],
    }
    write_json(os.path.join(output_dir, "business_analysis", "business_entities.json"), entities)
    write_json(os.path.join(output_dir, "qvd_inspection.json"), {
        "tables": [{
            "summary": {
                "file_name": "claims.qvd",
                "table_name": "Claims",
                "no_of_records": "100",
                "field_count": 7,
            }
        }]
    })
    return entities


class QvdKpiCatalogTests(unittest.TestCase):
    def test_actual_budget_forecast_kpi_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_fixture(tmp)
            catalog = generate_kpi_catalog(tmp)
            names = {row["kpi_name"] for row in catalog["kpis"]}

        self.assertIn("Actual Sales", names)
        self.assertIn("Budget Sales", names)
        self.assertIn("Forecast Sales", names)

    def test_sum_formula_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_fixture(tmp)
            catalog = generate_kpi_catalog(tmp)
            actual = next(row for row in catalog["kpis"] if row["kpi_name"] == "Actual Sales")

        self.assertEqual(actual["aggregation_type"], "SUM")
        self.assertEqual(actual["recommended_formula"], "SUM(actual_sales)")

    def test_avg_formula_for_rate_percentage_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_fixture(tmp)
            catalog = generate_kpi_catalog(tmp)
            rate = next(row for row in catalog["kpis"] if row["kpi_name"] == "Gross Margin Rate")

        self.assertEqual(rate["aggregation_type"], "AVG")
        self.assertEqual(rate["recommended_formula"], "AVG(gross_margin_rate)")

    def test_claims_fixture_does_not_generate_sales_kpis_without_sales_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_claims_fixture(tmp)
            catalog = generate_kpi_catalog(tmp)
            names = {row["kpi_name"] for row in catalog["kpis"]}
            formulas = {row["recommended_formula"] for row in catalog["kpis"]}

        self.assertIn("Claim Amount", names)
        self.assertIn("Allowed Amount", names)
        self.assertNotIn("Actual Sales", names)
        self.assertFalse(any("sales" in formula.lower() for formula in formulas))

    def test_documentation_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            entities = build_fixture(tmp)
            catalog = generate_kpi_catalog(tmp)
            markdown = render_business_documentation(tmp, entities, catalog)
            path = write_business_documentation(tmp, entities, catalog)
            exists = os.path.exists(path)

        self.assertIn("Executive Summary", markdown)
        self.assertIn("Recommended Databricks Model", markdown)
        self.assertTrue(exists)

    def test_route_creates_kpi_and_markdown_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "kpi-session"
            output_dir = os.path.join(tmp, session_id, "qvd_outputs")
            build_fixture(output_dir)
            app = Flask(__name__)
            register_qvd_routes(app, tmp)
            response = app.test_client().post(f"/api/qvd/business-analysis/kpis/{session_id}")
            payload = response.get_json()
            json_exists = os.path.exists(os.path.join(output_dir, "business_analysis", "kpi_catalog.json"))
            csv_exists = os.path.exists(os.path.join(output_dir, "business_analysis", "kpi_catalog.csv"))
            md_exists = os.path.exists(os.path.join(output_dir, "business_analysis", "business_analysis.md"))

        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(payload["kpi_count"], 5)
        self.assertTrue(json_exists)
        self.assertTrue(csv_exists)
        self.assertTrue(md_exists)


if __name__ == "__main__":
    unittest.main()
