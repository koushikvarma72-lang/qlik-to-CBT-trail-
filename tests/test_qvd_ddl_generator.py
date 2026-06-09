import csv
import os
import tempfile
import unittest

from flask import Flask

from backend.integrations.qvd_routes import register_qvd_routes
from qvd_to_databricks.ddl_generator import generate_delta_ddl
from qvd_to_databricks.schema_suggester import MAPPING_COLUMNS


def mapping_row(**overrides):
    row = {
        "qvd_file": "source.qvd",
        "source_table": "Source",
        "source_column": "SourceColumn",
        "source_tags": "$text",
        "source_number_format": "{}",
        "inferred_category": "TEXT_LIKE",
        "target_table": "source_table",
        "target_column": "source_column",
        "target_type": "STRING",
        "conversion_rule": "cast_string",
        "confidence": "0.90",
        "reason": "Approved",
        "review_status": "MANUALLY_APPROVED",
    }
    row.update(overrides)
    return row


def write_approved_mapping(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MAPPING_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in MAPPING_COLUMNS})


class QvdDdlGeneratorTests(unittest.TestCase):
    def test_ddl_generation_from_approved_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = generate_delta_ddl([
                mapping_row(target_column="customer", target_type="STRING"),
                mapping_row(target_column="amount", target_type="DECIMAL(18,2)"),
            ], os.path.join(tmp, "ddl"))
            ddl_path = result["ddl_files"][0]
            with open(ddl_path, encoding="utf-8") as handle:
                sql = handle.read()

        self.assertTrue(result["generated"])
        self.assertEqual(result["table_count"], 1)
        self.assertIn("CREATE TABLE IF NOT EXISTS `main`.`qvd_raw`.`source_table`", sql)
        self.assertIn("`customer` STRING", sql)
        self.assertIn("`amount` DECIMAL(18,2)", sql)

    def test_invalid_target_type_rejected(self):
        result = generate_delta_ddl([
            mapping_row(target_type="VARCHAR"),
        ], "/tmp/not-used")

        self.assertFalse(result["generated"])
        self.assertEqual(result["errors"][0]["field"], "target_type")

    def test_duplicate_target_column_rejected(self):
        result = generate_delta_ddl([
            mapping_row(target_column="duplicate_column"),
            mapping_row(source_column="Other", target_column="duplicate_column"),
        ], "/tmp/not-used")

        self.assertFalse(result["generated"])
        self.assertEqual(result["errors"][0]["field"], "target_column")

    def test_audit_columns_included_and_using_delta(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = generate_delta_ddl([
                mapping_row(),
            ], os.path.join(tmp, "ddl"))
            with open(result["ddl_files"][0], encoding="utf-8") as handle:
                sql = handle.read()

        self.assertIn("`_source_file_name` STRING", sql)
        self.assertIn("`_source_file_path` STRING", sql)
        self.assertIn("`_ingestion_timestamp` TIMESTAMP", sql)
        self.assertIn("`_batch_id` STRING", sql)
        self.assertIn("`_record_hash` STRING", sql)
        self.assertIn("USING DELTA", sql)

    def test_generate_ddl_route_creates_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "ddl-session"
            output_dir = os.path.join(tmp, session_id, "qvd_outputs")
            write_approved_mapping(os.path.join(output_dir, "approved_databricks_mapping.csv"), [
                mapping_row(target_column="customer"),
                mapping_row(source_column="Amount", target_column="amount", target_type="DOUBLE"),
            ])

            app = Flask(__name__)
            register_qvd_routes(app, tmp)
            client = app.test_client()
            response = client.post(f"/api/qvd/generate-ddl/{session_id}")
            payload = response.get_json()
            ddl_path = os.path.join(output_dir, "ddl", "create_source_table.sql")
            with open(ddl_path, encoding="utf-8") as handle:
                sql = handle.read()
            ddl_exists = os.path.exists(ddl_path)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["generated"])
        self.assertEqual(payload["table_count"], 1)
        self.assertTrue(ddl_exists)
        self.assertIn("USING DELTA", sql)


if __name__ == "__main__":
    unittest.main()
