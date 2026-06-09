import csv
import json
import os
import tempfile
import unittest

from flask import Flask

from backend.integrations.qvd_routes import register_qvd_routes
from qvd_to_databricks.parquet_validator import validate_parquet_output
from qvd_to_databricks.schema_suggester import MAPPING_COLUMNS


def mapping_row(source_column, target_column, target_type):
    return {
        "qvd_file": "sales.qvd",
        "source_table": "Sales",
        "source_column": source_column,
        "source_tags": "",
        "source_number_format": "{}",
        "inferred_category": "",
        "target_table": "sales_table",
        "target_column": target_column,
        "target_type": target_type,
        "conversion_rule": "",
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


def write_inspection(path, no_of_records=2):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({
            "tables": [{
                "summary": {
                    "file_name": "sales.qvd",
                    "table_name": "sales_table",
                    "no_of_records": str(no_of_records),
                }
            }]
        }, handle)


def write_parquet(path, rows):
    import pandas as pd
    os.makedirs(path, exist_ok=True)
    frame = pd.DataFrame(rows)
    frame["_source_file_name"] = "sales.qvd"
    frame["_source_file_path"] = "/tmp/sales.qvd"
    frame["_ingestion_timestamp"] = "2026-06-05T00:00:00"
    frame["_batch_id"] = "batch"
    frame["_record_hash"] = ["h1", "h2"][:len(frame)]
    frame.to_parquet(os.path.join(path, "part.parquet"), index=False)


class ParquetValidatorTests(unittest.TestCase):
    def setUp(self):
        self.mapping_rows = [
            mapping_row("OrderDate", "order_date", "DATE"),
            mapping_row("IsActive", "is_active", "BOOLEAN"),
            mapping_row("Amount", "amount", "DECIMAL(18,2)"),
        ]

    def _fixture(self, rows, no_of_records=2):
        tmp = tempfile.TemporaryDirectory()
        parquet_dir = os.path.join(tmp.name, "parquet", "sales_table")
        mapping_path = os.path.join(tmp.name, "approved_databricks_mapping.csv")
        inspection_path = os.path.join(tmp.name, "qvd_inspection.json")
        write_mapping(mapping_path, self.mapping_rows)
        write_inspection(inspection_path, no_of_records=no_of_records)
        write_parquet(parquet_dir, rows)
        return tmp, parquet_dir, mapping_path, inspection_path

    def test_valid_parquet_passes(self):
        tmp, parquet_dir, mapping_path, inspection_path = self._fixture([
            {"order_date": "2021-01-01", "is_active": True, "amount": 10.5},
            {"order_date": "2021-01-02", "is_active": False, "amount": 12.75},
        ])
        with tmp:
            result = validate_parquet_output(parquet_dir, mapping_path, inspection_path, "sales_table")
        self.assertTrue(result["success"])

    def test_row_count_mismatch_fails(self):
        tmp, parquet_dir, mapping_path, inspection_path = self._fixture([
            {"order_date": "2021-01-01", "is_active": True, "amount": 10.5},
            {"order_date": "2021-01-02", "is_active": False, "amount": 12.75},
        ], no_of_records=3)
        with tmp:
            result = validate_parquet_output(parquet_dir, mapping_path, inspection_path, "sales_table")
        self.assertFalse(result["success"])
        self.assertIn("row_count", [check["name"] for check in result["failed_checks"]])

    def test_missing_target_column_fails(self):
        tmp, parquet_dir, mapping_path, inspection_path = self._fixture([
            {"order_date": "2021-01-01", "is_active": True},
            {"order_date": "2021-01-02", "is_active": False},
        ])
        with tmp:
            result = validate_parquet_output(parquet_dir, mapping_path, inspection_path, "sales_table")
        self.assertFalse(result["success"])
        self.assertIn("approved_target_columns_exist", [check["name"] for check in result["failed_checks"]])

    def test_date_still_numeric_fails(self):
        tmp, parquet_dir, mapping_path, inspection_path = self._fixture([
            {"order_date": 44197, "is_active": True, "amount": 10.5},
            {"order_date": 44198, "is_active": False, "amount": 12.75},
        ])
        with tmp:
            result = validate_parquet_output(parquet_dir, mapping_path, inspection_path, "sales_table")
        self.assertFalse(result["success"])
        self.assertIn("date_conversion", [check["name"] for check in result["failed_checks"]])

    def test_boolean_invalid_values_fail(self):
        tmp, parquet_dir, mapping_path, inspection_path = self._fixture([
            {"order_date": "2021-01-01", "is_active": "maybe", "amount": 10.5},
            {"order_date": "2021-01-02", "is_active": "false", "amount": 12.75},
        ])
        with tmp:
            result = validate_parquet_output(parquet_dir, mapping_path, inspection_path, "sales_table")
        self.assertFalse(result["success"])
        self.assertIn("boolean_values", [check["name"] for check in result["failed_checks"]])

    def test_validation_route_writes_json_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "validate-session"
            output_dir = os.path.join(tmp, session_id, "qvd_outputs")
            parquet_dir = os.path.join(output_dir, "parquet", "sales_table")
            write_mapping(os.path.join(output_dir, "approved_databricks_mapping.csv"), self.mapping_rows)
            write_inspection(os.path.join(output_dir, "qvd_inspection.json"), no_of_records=2)
            write_parquet(parquet_dir, [
                {"order_date": "2021-01-01", "is_active": True, "amount": 10.5},
                {"order_date": "2021-01-02", "is_active": False, "amount": 12.75},
            ])

            app = Flask(__name__)
            register_qvd_routes(app, tmp)
            response = app.test_client().post(
                f"/api/qvd/validate-parquet/{session_id}",
                json={"target_table": "sales_table"},
            )
            payload = response.get_json()
            artifact_path = os.path.join(output_dir, "parquet_validation_sales_table.json")
            with open(artifact_path, encoding="utf-8") as handle:
                artifact = json.load(handle)
            artifact_exists = os.path.exists(artifact_path)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["success"])
        self.assertTrue(artifact_exists)
        self.assertEqual(artifact["target_table"], "sales_table")


if __name__ == "__main__":
    unittest.main()
