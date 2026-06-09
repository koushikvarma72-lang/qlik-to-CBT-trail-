import csv
import json
import os
import tempfile
import unittest

from flask import Flask

from backend.integrations.qvd_routes import register_qvd_routes
from qvd_business_analysis.entity_discovery import discover_business_entities


def write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


def field(name, tags=None, number_format=None):
    return {
        "field_name": name,
        "tags": tags or [],
        "number_format": number_format or {},
        "no_of_symbols": "10",
    }


def build_fixture(output_dir):
    inspection = {
        "session_id": "business-session",
        "uploaded_files": [{"file_name": "sales.qvd"}],
        "tables": [{
            "summary": {
                "file_name": "sales.qvd",
                "table_name": "Sales",
                "no_of_records": "2",
                "field_count": 8,
            },
            "fields": [
                field("Customer", ["$text"]),
                field("ActualSales", ["$numeric"], {"Type": "REAL"}),
                field("Calendar.Date", ["$date"]),
                field("ActualFlag"),
                field("ProductCode"),
                field("Region", ["$text"]),
                field("Country", ["$text"]),
                field("State", ["$text"]),
            ],
        }],
    }
    write_json(os.path.join(output_dir, "qvd_inspection.json"), inspection)
    os.makedirs(output_dir, exist_ok=True)
    with open(os.path.join(output_dir, "source_structure.csv"), "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["field_name", "inferred_category"])
        writer.writeheader()
        writer.writerow({"field_name": "Customer", "inferred_category": "TEXT_LIKE"})
        writer.writerow({"field_name": "ActualSales", "inferred_category": "NUMERIC_LIKE"})
        writer.writerow({"field_name": "Calendar.Date", "inferred_category": "DATE_LIKE"})
        writer.writerow({"field_name": "ActualFlag", "inferred_category": "FLAG_LIKE"})
        writer.writerow({"field_name": "ProductCode", "inferred_category": "KEY_LIKE"})
        writer.writerow({"field_name": "Region", "inferred_category": "TEXT_LIKE"})
        writer.writerow({"field_name": "Country", "inferred_category": "TEXT_LIKE"})
        writer.writerow({"field_name": "State", "inferred_category": "TEXT_LIKE"})
    with open(os.path.join(output_dir, "suggested_databricks_mapping.csv"), "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["source_column", "target_column"])
        writer.writeheader()
        writer.writerow({"source_column": "Customer", "target_column": "customer"})
        writer.writerow({"source_column": "ActualSales", "target_column": "actual_sales"})
    write_json(os.path.join(output_dir, "row_preview_sales_qvd.json"), {
        "columns": ["Customer", "ActualSales"],
        "rows": [{"Customer": "A", "ActualSales": 10.5}, {"Customer": "B", "ActualSales": 12.0}],
    })
    write_json(os.path.join(output_dir, "column_profile_sales_qvd.json"), {
        "profile_rows": [
            {"column_name": "ActualFlag", "detected_runtime_type": "BOOLEAN_LIKE", "sample_values": ["0", "1"]},
            {"column_name": "ActualSales", "detected_runtime_type": "DECIMAL", "sample_values": ["10.5", "12.0"]},
        ]
    })


def build_metadata_fixture(output_dir, table_name, file_name, fields, source_rows=None, profile_rows=None):
    write_json(os.path.join(output_dir, "qvd_inspection.json"), {
        "session_id": "generic-session",
        "uploaded_files": [{"file_name": file_name}],
        "tables": [{
            "summary": {
                "file_name": file_name,
                "table_name": table_name,
                "no_of_records": "3",
                "field_count": len(fields),
            },
            "fields": fields,
        }],
    })
    os.makedirs(output_dir, exist_ok=True)
    with open(os.path.join(output_dir, "source_structure.csv"), "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["field_name", "inferred_category"])
        writer.writeheader()
        for row in source_rows or []:
            writer.writerow(row)
    if profile_rows:
        safe_name = file_name.replace(".", "_")
        write_json(os.path.join(output_dir, f"column_profile_{safe_name}.json"), {
            "profile_rows": profile_rows,
        })


class QvdBusinessEntityDiscoveryTests(unittest.TestCase):
    def _result(self):
        tmp = tempfile.TemporaryDirectory()
        build_fixture(tmp.name)
        return tmp, discover_business_entities(tmp.name)

    def test_dimension_detection(self):
        tmp, result = self._result()
        with tmp:
            names = {row["source_column"] for row in result["dimensions"]}
        self.assertIn("Customer", names)

    def test_measure_detection(self):
        tmp, result = self._result()
        with tmp:
            names = {row["source_column"] for row in result["measures"]}
        self.assertIn("ActualSales", names)

    def test_date_detection(self):
        tmp, result = self._result()
        with tmp:
            names = {row["source_column"] for row in result["dates"]}
        self.assertIn("Calendar.Date", names)

    def test_flag_detection(self):
        tmp, result = self._result()
        with tmp:
            names = {row["source_column"] for row in result["flags"]}
        self.assertIn("ActualFlag", names)

    def test_identifier_detection(self):
        tmp, result = self._result()
        with tmp:
            names = {row["source_column"] for row in result["identifiers"]}
        self.assertIn("ProductCode", names)

    def test_hierarchy_detection(self):
        tmp, result = self._result()
        with tmp:
            names = {row["business_name"] for row in result["hierarchies"]}
        self.assertIn("Region > Country > State", names)

    def test_non_sales_metadata_still_produces_business_entities(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_metadata_fixture(
                tmp,
                "Claims",
                "claims.qvd",
                [
                    field("ClaimID"),
                    field("PatientID"),
                    field("ClaimAmount", ["$numeric"], {"Type": "REAL"}),
                    field("ServiceDate", ["$date"]),
                    field("ProviderState", ["$text"]),
                ],
                [
                    {"field_name": "ClaimID", "inferred_category": "KEY_LIKE"},
                    {"field_name": "PatientID", "inferred_category": "KEY_LIKE"},
                    {"field_name": "ClaimAmount", "inferred_category": "NUMERIC_LIKE"},
                    {"field_name": "ServiceDate", "inferred_category": "DATE_LIKE"},
                    {"field_name": "ProviderState", "inferred_category": "TEXT_LIKE"},
                ],
            )
            result = discover_business_entities(tmp)

        self.assertGreaterEqual(result["summary"]["measures_count"], 1)
        self.assertGreaterEqual(result["summary"]["dates_count"], 1)
        self.assertGreaterEqual(result["summary"]["identifiers_count"], 2)

    def test_inventory_fixture_produces_inventory_measures_and_dimensions(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_metadata_fixture(
                tmp,
                "Inventory Snapshot",
                "inventory.qvd",
                [
                    field("ProductCode"),
                    field("Warehouse"),
                    field("OnHandQuantity", ["$numeric"], {"Type": "INTEGER"}),
                    field("ReorderDate", ["$date"]),
                    field("Category", ["$text"]),
                ],
                [
                    {"field_name": "ProductCode", "inferred_category": "KEY_LIKE"},
                    {"field_name": "Warehouse", "inferred_category": "TEXT_LIKE"},
                    {"field_name": "OnHandQuantity", "inferred_category": "NUMERIC_LIKE"},
                    {"field_name": "ReorderDate", "inferred_category": "DATE_LIKE"},
                    {"field_name": "Category", "inferred_category": "TEXT_LIKE"},
                ],
            )
            result = discover_business_entities(tmp)
            measure_names = {row["source_column"] for row in result["measures"]}
            dimension_names = {row["source_column"] for row in result["dimensions"]}

        self.assertIn("OnHandQuantity", measure_names)
        self.assertIn("Warehouse", dimension_names)
        self.assertIn("Category", dimension_names)

    def test_hierarchy_generation_requires_matching_levels(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_metadata_fixture(
                tmp,
                "Operations",
                "operations.qvd",
                [
                    field("Region", ["$text"]),
                    field("Status", ["$text"]),
                    field("OpenCount", ["$numeric"], {"Type": "INTEGER"}),
                ],
                [
                    {"field_name": "Region", "inferred_category": "TEXT_LIKE"},
                    {"field_name": "Status", "inferred_category": "TEXT_LIKE"},
                    {"field_name": "OpenCount", "inferred_category": "NUMERIC_LIKE"},
                ],
            )
            result = discover_business_entities(tmp)

        self.assertEqual(result["summary"]["hierarchies_count"], 0)

    def test_business_analysis_route_creates_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "business-session"
            output_dir = os.path.join(tmp, session_id, "qvd_outputs")
            build_fixture(output_dir)
            app = Flask(__name__)
            register_qvd_routes(app, tmp)
            response = app.test_client().post(f"/api/qvd/business-analysis/entities/{session_id}")
            payload = response.get_json()
            artifact_path = os.path.join(output_dir, "business_analysis", "business_entities.json")
            artifact_exists = os.path.exists(artifact_path)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["success"])
        self.assertTrue(artifact_exists)


if __name__ == "__main__":
    unittest.main()
