import csv
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask

from backend.integrations.qvd_routes import register_qvd_routes
from qvd_to_databricks.qvd_to_parquet_converter import convert_qvd_to_parquet, qlik_serial_to_iso_date
from qvd_to_databricks.schema_suggester import MAPPING_COLUMNS


def mapping_row(source_column, target_column, target_type, conversion_rule, qvd_file="sales.qvd"):
    return {
        "qvd_file": qvd_file,
        "source_table": "Sales",
        "source_column": source_column,
        "source_tags": "",
        "source_number_format": "{}",
        "inferred_category": "",
        "target_table": "sales_table",
        "target_column": target_column,
        "target_type": target_type,
        "conversion_rule": conversion_rule,
        "confidence": "0.90",
        "reason": "",
        "review_status": "MANUALLY_APPROVED",
    }


def write_mapping(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MAPPING_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in MAPPING_COLUMNS})


def mocked_rows():
    return {
        "success": True,
        "columns": ["OrderDate", "Customer Name", "IsActive", "Amount"],
        "rows": [
            {"OrderDate": 44197, "Customer Name": "A", "IsActive": 1, "Amount": "10.50"},
            {"OrderDate": 44198, "Customer Name": "B", "IsActive": 0, "Amount": "12.75"},
        ],
        "row_count_returned": 2,
        "limit": None,
        "reader_used": "mock",
        "error": None,
    }


class QvdToParquetConverterTests(unittest.TestCase):
    def test_qlik_serial_to_date_converts_known_value(self):
        self.assertEqual(qlik_serial_to_iso_date(44197), "2021-01-01")

    def test_conversion_renames_columns_adds_audit_and_boolean(self):
        with tempfile.TemporaryDirectory() as tmp:
            qvd_path = os.path.join(tmp, "sales.qvd")
            with open(qvd_path, "wb") as handle:
                handle.write(b"fake qvd")
            mapping_path = os.path.join(tmp, "approved_databricks_mapping.csv")
            write_mapping(mapping_path, [
                mapping_row("OrderDate", "order_date", "DATE", "qlik_serial_to_date"),
                mapping_row("Customer Name", "customer_name", "STRING", "cast_string"),
                mapping_row("IsActive", "is_active", "BOOLEAN", "flag_to_boolean_or_int_review"),
                mapping_row("Amount", "amount", "DECIMAL(18,2)", "cast_decimal_18_2"),
            ])

            with patch("qvd_to_databricks.qvd_to_parquet_converter.qvd_row_reader.read_qvd_rows", return_value=mocked_rows()):
                result = convert_qvd_to_parquet(qvd_path, mapping_path, os.path.join(tmp, "parquet"), batch_id="batch-1")

            import pandas as pd
            frame = pd.read_parquet(result["parquet_path"])

        self.assertTrue(result["success"])
        self.assertIn("customer_name", frame.columns)
        self.assertIn("_source_file_name", frame.columns)
        self.assertIn("_source_file_path", frame.columns)
        self.assertIn("_ingestion_timestamp", frame.columns)
        self.assertIn("_batch_id", frame.columns)
        self.assertIn("_record_hash", frame.columns)
        self.assertEqual(str(frame.loc[0, "order_date"]), "2021-01-01")
        self.assertEqual(bool(frame.loc[0, "is_active"]), True)
        self.assertEqual(bool(frame.loc[1, "is_active"]), False)

    def test_missing_approved_mapping_returns_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            qvd_path = os.path.join(tmp, "sales.qvd")
            with open(qvd_path, "wb") as handle:
                handle.write(b"fake qvd")
            result = convert_qvd_to_parquet(qvd_path, os.path.join(tmp, "missing.csv"), os.path.join(tmp, "parquet"))

        self.assertFalse(result["success"])
        self.assertIn("Approved mapping artifact not found", result["errors"][0])

    def test_conversion_route_writes_report_json_and_parquet(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "convert-session"
            input_dir = os.path.join(tmp, session_id, "qvd_inputs")
            output_dir = os.path.join(tmp, session_id, "qvd_outputs")
            os.makedirs(input_dir, exist_ok=True)
            qvd_path = os.path.join(input_dir, "sales.qvd")
            with open(qvd_path, "wb") as handle:
                handle.write(b"fake qvd")
            write_mapping(os.path.join(output_dir, "approved_databricks_mapping.csv"), [
                mapping_row("Customer Name", "customer_name", "STRING", "cast_string"),
                mapping_row("IsActive", "is_active", "BOOLEAN", "flag_to_boolean_or_int_review"),
            ])

            app = Flask(__name__)
            register_qvd_routes(app, tmp)
            client = app.test_client()
            with patch("qvd_to_databricks.qvd_to_parquet_converter.qvd_row_reader.read_qvd_rows", return_value=mocked_rows()):
                response = client.post(
                    f"/api/qvd/convert-parquet/{session_id}",
                    json={"file_name": "sales.qvd", "batch_id": "batch-route"},
                )
            payload = response.get_json()
            report_path = os.path.join(output_dir, "conversion_report_sales_qvd.json")
            with open(report_path, encoding="utf-8") as handle:
                report = json.load(handle)
            report_exists = os.path.exists(report_path)
            parquet_exists = os.path.exists(payload["parquet_path"])

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["success"])
        self.assertTrue(report_exists)
        self.assertTrue(parquet_exists)
        self.assertEqual(report["batch_id"], "batch-route")


if __name__ == "__main__":
    unittest.main()
