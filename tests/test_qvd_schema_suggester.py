import csv
import json
import os
import tempfile
import unittest

from flask import Flask

from backend.integrations.qvd_routes import register_qvd_routes
from qvd_to_databricks.schema_suggester import suggest_schema_from_inspection, to_snake_case


def inspection_payload(fields):
    return {
        "session_id": "schema-session",
        "uploaded_files": [],
        "tables": [
            {
                "summary": {
                    "file_name": "sales.qvd",
                    "table_name": "Sales Fact",
                    "no_of_records": "10",
                    "field_count": len(fields),
                    "file_size_bytes": 123,
                },
                "fields": fields,
                "quick_analysis": {},
            }
        ],
        "errors": [],
    }


def field(name, category, number_format=None, tags=None, symbols="10"):
    return {
        "position": 1,
        "field_name": name,
        "tags": tags or [],
        "number_format": number_format or {},
        "no_of_symbols": symbols,
        "inferred_category": category,
    }


def approved_mapping_row(**overrides):
    row = {
        "qvd_file": "source.qvd",
        "source_table": "Source Table",
        "source_column": "SourceColumn",
        "source_tags": ["$text"],
        "source_number_format": {},
        "inferred_category": "TEXT_LIKE",
        "target_table": "source_table",
        "target_column": "source_column",
        "target_type": "STRING",
        "conversion_rule": "cast_string",
        "confidence": 0.90,
        "reason": "Text/ascii QVD tags detected.",
        "review_status": "MANUALLY_APPROVED",
    }
    row.update(overrides)
    return row


class QvdSchemaSuggesterTests(unittest.TestCase):
    def test_snake_case_conversion_and_special_characters(self):
        self.assertEqual(to_snake_case("Calendar.Date"), "calendar_date")
        self.assertEqual(to_snake_case("ActualSales.Date"), "actual_sales_date")
        self.assertEqual(to_snake_case("Developed/Emerging"), "developed_emerging")
        self.assertEqual(to_snake_case("%Source"), "source")
        self.assertEqual(to_snake_case("select"), "select_col")
        self.assertEqual(to_snake_case("PYActualSales"), "py_actual_sales")
        self.assertEqual(to_snake_case("YoYActualSalesDifference"), "yoy_actual_sales_difference")
        self.assertEqual(to_snake_case("XMLParserName"), "xml_parser_name")

    def test_duplicate_column_name_handling(self):
        suggestion = suggest_schema_from_inspection(inspection_payload([
            field("A B", "TEXT_LIKE"),
            field("A/B", "TEXT_LIKE"),
            field("A.B", "TEXT_LIKE"),
        ]))

        target_columns = [row["target_column"] for row in suggestion["mapping"]]
        self.assertEqual(target_columns, ["a_b", "a_b_1", "a_b_2"])

    def test_category_to_target_type_rules(self):
        suggestion = suggest_schema_from_inspection(inspection_payload([
            field("OrderDate", "DATE_LIKE"),
            field("CustomerName", "TEXT_LIKE"),
            field("SalesAmount", "NUMERIC_LIKE", {"Type": "REAL", "nDec": "2"}),
            field("UnitCount", "NUMERIC_LIKE", {"Type": "INTEGER"}),
            field("Mystery", "UNKNOWN"),
        ]))

        by_source = {row["source_column"]: row for row in suggestion["mapping"]}
        self.assertEqual(by_source["OrderDate"]["target_type"], "DATE")
        self.assertEqual(by_source["OrderDate"]["conversion_rule"], "qlik_serial_to_date")
        self.assertEqual(by_source["CustomerName"]["target_type"], "STRING")
        self.assertEqual(by_source["SalesAmount"]["target_type"], "DECIMAL(18,2)")
        self.assertEqual(by_source["UnitCount"]["target_type"], "BIGINT")
        self.assertEqual(by_source["Mystery"]["target_type"], "STRING")
        self.assertEqual(by_source["Mystery"]["review_status"], "NEEDS_REVIEW")

    def test_low_cardinality_text_mapping_stays_string(self):
        suggestion = suggest_schema_from_inspection(inspection_payload([
            field("Franchise", "TEXT_LIKE", {"Type": "UNKNOWN"}, ["$ascii", "$text"], symbols="2"),
        ]))

        row = suggestion["mapping"][0]
        self.assertEqual(row["inferred_category"], "TEXT_LIKE")
        self.assertEqual(row["target_type"], "STRING")
        self.assertEqual(row["review_status"], "AUTO_APPROVED")

    def test_suggest_schema_route_creates_csv_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "route-session"
            output_dir = os.path.join(tmp, session_id, "qvd_outputs")
            os.makedirs(output_dir, exist_ok=True)
            with open(os.path.join(output_dir, "qvd_inspection.json"), "w", encoding="utf-8") as handle:
                json.dump(inspection_payload([
                    field("SalesAmount", "NUMERIC_LIKE", {"Type": "REAL", "nDec": "2"}),
                    field("Mystery", "UNKNOWN"),
                ]), handle)

            app = Flask(__name__)
            register_qvd_routes(app, tmp)
            client = app.test_client()
            response = client.post(f"/api/qvd/suggest-schema/{session_id}")
            payload = response.get_json()
            csv_path = os.path.join(output_dir, "suggested_databricks_mapping.csv")

            with open(csv_path, newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            csv_exists = os.path.exists(csv_path)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["total_columns"], 2)
        self.assertEqual(payload["needs_review_count"], 1)
        self.assertTrue(csv_exists)
        self.assertEqual(rows[0]["target_type"], "DECIMAL(18,2)")

    def test_save_approved_mapping_route_success_and_csv_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Flask(__name__)
            register_qvd_routes(app, tmp)
            client = app.test_client()
            response = client.post(
                "/api/qvd/save-approved-mapping/save-session",
                json={"mapping_rows": [
                    approved_mapping_row(),
                    approved_mapping_row(source_column="SecondColumn", target_column="second_column", review_status="AUTO_APPROVED"),
                ]},
            )
            payload = response.get_json()
            csv_path = os.path.join(tmp, "save-session", "qvd_outputs", "approved_databricks_mapping.csv")
            with open(csv_path, newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            csv_exists = os.path.exists(csv_path)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["saved"])
        self.assertEqual(payload["total_rows"], 2)
        self.assertEqual(payload["approved_count"], 2)
        self.assertTrue(csv_exists)
        self.assertEqual(rows[0]["target_column"], "source_column")

    def test_save_approved_mapping_rejects_invalid_target_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Flask(__name__)
            register_qvd_routes(app, tmp)
            client = app.test_client()
            response = client.post(
                "/api/qvd/save-approved-mapping/save-session",
                json={"mapping_rows": [approved_mapping_row(target_type="VARCHAR")]},
            )
            payload = response.get_json()

        self.assertEqual(response.status_code, 400)
        self.assertFalse(payload["saved"])
        self.assertEqual(payload["errors"][0]["field"], "target_type")

    def test_save_approved_mapping_rejects_empty_target_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Flask(__name__)
            register_qvd_routes(app, tmp)
            client = app.test_client()
            response = client.post(
                "/api/qvd/save-approved-mapping/save-session",
                json={"mapping_rows": [approved_mapping_row(target_column="")]},
            )
            payload = response.get_json()

        self.assertEqual(response.status_code, 400)
        self.assertFalse(payload["saved"])
        self.assertEqual(payload["errors"][0]["field"], "target_column")

    def test_save_approved_mapping_rejects_duplicate_target_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Flask(__name__)
            register_qvd_routes(app, tmp)
            client = app.test_client()
            response = client.post(
                "/api/qvd/save-approved-mapping/save-session",
                json={"mapping_rows": [
                    approved_mapping_row(source_column="FirstColumn", target_column="same_column"),
                    approved_mapping_row(source_column="SecondColumn", target_column="same_column"),
                ]},
            )
            payload = response.get_json()

        self.assertEqual(response.status_code, 400)
        self.assertFalse(payload["saved"])
        self.assertEqual(payload["errors"][0]["field"], "target_column")


if __name__ == "__main__":
    unittest.main()
