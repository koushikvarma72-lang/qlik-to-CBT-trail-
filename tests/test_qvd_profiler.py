import csv
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask

from backend.integrations.qvd_routes import register_qvd_routes
from qvd_to_databricks.qvd_profiler import detect_runtime_type, load_approved_mapping_rows, profile_rows
from qvd_to_databricks.schema_suggester import MAPPING_COLUMNS


def mapping_row(source_column, target_type):
    return {
        "qvd_file": "source.qvd",
        "source_table": "Source",
        "source_column": source_column,
        "source_tags": "",
        "source_number_format": "{}",
        "inferred_category": "",
        "target_table": "source_table",
        "target_column": source_column.lower(),
        "target_type": target_type,
        "conversion_rule": "",
        "confidence": "0.90",
        "reason": "",
        "review_status": "MANUALLY_APPROVED",
    }


def write_approved_mapping(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MAPPING_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in MAPPING_COLUMNS})


class QvdProfilerTests(unittest.TestCase):
    def test_integer_detection(self):
        self.assertEqual(detect_runtime_type(["1", "2", "3"], "BIGINT"), "INTEGER")

    def test_decimal_detection(self):
        self.assertEqual(detect_runtime_type(["1.25", "2.50", "3"], "DECIMAL(18,2)"), "DECIMAL")

    def test_boolean_like_detection(self):
        self.assertEqual(detect_runtime_type(["0", "1", "true", "N"], "BOOLEAN"), "BOOLEAN_LIKE")

    def test_qlik_date_serial_detection_when_target_is_date(self):
        self.assertEqual(detect_runtime_type(["45292", "45293"], "DATE"), "QLIK_DATE_SERIAL")

    def test_mismatch_detection(self):
        profile = profile_rows(
            ["Amount"],
            [{"Amount": "10.5"}, {"Amount": "12.75"}],
            [mapping_row("Amount", "BIGINT")],
        )
        self.assertEqual(profile[0]["detected_runtime_type"], "DECIMAL")
        self.assertEqual(profile[0]["type_match_status"], "MISMATCH")

    def test_profile_uses_approved_mapping_for_source_column(self):
        profile = profile_rows(
            ["Customer"],
            [{"Customer": "A"}, {"Customer": "B"}],
            [mapping_row("Customer", "STRING")],
        )
        self.assertEqual(profile[0]["approved_target_type"], "STRING")
        self.assertEqual(profile[0]["type_match_status"], "MATCH")

    def test_load_approved_mapping_rows_falls_back_to_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = os.path.join(tmp, "approved_databricks_mapping.csv")
            json_path = os.path.join(tmp, "approved_databricks_mapping.json")
            with open(json_path, "w", encoding="utf-8") as handle:
                json.dump({"mapping_rows": [mapping_row("Customer", "STRING")]}, handle)

            rows = load_approved_mapping_rows(csv_path)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_column"], "Customer")

    def test_profile_route_writes_csv_and_json_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "profile-session"
            input_dir = os.path.join(tmp, session_id, "qvd_inputs")
            output_dir = os.path.join(tmp, session_id, "qvd_outputs")
            os.makedirs(input_dir, exist_ok=True)
            with open(os.path.join(input_dir, "sales.qvd"), "wb") as handle:
                handle.write(b"fake qvd")
            write_approved_mapping(os.path.join(output_dir, "approved_databricks_mapping.csv"), [
                mapping_row("OrderDate", "DATE"),
                mapping_row("Amount", "DECIMAL(18,2)"),
            ])

            preview = {
                "success": True,
                "columns": ["OrderDate", "Amount"],
                "rows": [
                    {"OrderDate": "45292", "Amount": "10.5"},
                    {"OrderDate": "45293", "Amount": "12.75"},
                ],
                "row_count_returned": 2,
                "limit": 10000,
                "reader_used": "mock",
                "error": None,
            }

            app = Flask(__name__)
            register_qvd_routes(app, tmp)
            client = app.test_client()
            with patch("qvd_to_databricks.qvd_profiler.qvd_row_reader.preview_qvd_rows", return_value=preview):
                response = client.post(
                    f"/api/qvd/profile-columns/{session_id}",
                    json={"file_name": "sales.qvd", "limit": 10000},
                )

            payload = response.get_json()
            csv_path = os.path.join(output_dir, "column_profile_sales_qvd.csv")
            json_path = os.path.join(output_dir, "column_profile_sales_qvd.json")
            with open(json_path, encoding="utf-8") as handle:
                artifact = json.load(handle)
            csv_exists = os.path.exists(csv_path)
            json_exists = os.path.exists(json_path)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["rows_checked"], 2)
        self.assertTrue(csv_exists)
        self.assertTrue(json_exists)
        self.assertEqual(artifact["profile_rows"][0]["detected_runtime_type"], "QLIK_DATE_SERIAL")


if __name__ == "__main__":
    unittest.main()
