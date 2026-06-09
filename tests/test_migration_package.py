import csv
import json
import os
import tempfile
import unittest
import zipfile

from flask import Flask

from backend.integrations.qvd_routes import register_qvd_routes
from qvd_to_databricks.migration_package import generate_migration_package
from qvd_to_databricks.schema_suggester import MAPPING_COLUMNS


def write_file(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


def write_mapping(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    row = {
        "qvd_file": "Sales_Sample_3_1.qvd",
        "source_table": "Sales Sample",
        "source_column": "Customer",
        "source_tags": "$text",
        "source_number_format": "{}",
        "inferred_category": "TEXT_LIKE",
        "target_table": "sales_sample",
        "target_column": "customer",
        "target_type": "STRING",
        "conversion_rule": "cast_string",
        "confidence": "0.90",
        "reason": "",
        "review_status": "MANUALLY_APPROVED",
    }
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MAPPING_COLUMNS)
        writer.writeheader()
        writer.writerow(row)


def build_package_fixture(output_dir):
    write_file(os.path.join(output_dir, "source_structure.csv"), "qvd_file,table_name,field_name\nSales_Sample_3_1.qvd,sales_sample,Customer\n")
    write_mapping(os.path.join(output_dir, "approved_databricks_mapping.csv"))
    write_file(os.path.join(output_dir, "databricks_load", "create_table.sql"), "CREATE TABLE IF NOT EXISTS main.qvd_raw.sales_sample (`customer` STRING) USING DELTA;\n")
    write_file(os.path.join(output_dir, "databricks_load", "load_parquet_to_delta.sql"), "COPY INTO main.qvd_raw.sales_sample FROM 's3://bucket/sales_sample/' FILEFORMAT = PARQUET;\n")
    write_json(os.path.join(output_dir, "databricks_load", "load_config.json"), {"local_path_warning": ""})
    write_json(os.path.join(output_dir, "parquet_validation_sales_sample.json"), {
        "success": True,
        "passed": True,
        "target_table": "sales_sample",
        "parquet_path": "/tmp/parquet/sales_sample",
    })
    write_json(os.path.join(output_dir, "qvd_inspection.json"), {
        "tables": [{
            "summary": {
                "file_name": "Sales_Sample_3_1.qvd",
                "table_name": "sales_sample",
                "no_of_records": "84775",
                "field_count": 32,
            },
            "fields": [],
        }]
    })


class MigrationPackageTests(unittest.TestCase):
    def test_generate_migration_package_creates_zip_and_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_package_fixture(tmp)
            result = generate_migration_package(tmp, "sales_sample", "Sales_Sample_3_1.qvd")
            zip_path = result["migration_package_zip"]
            with zipfile.ZipFile(zip_path) as archive:
                names = set(archive.namelist())

        self.assertTrue(result["generated"])
        self.assertEqual(result["summary"]["source_qvd"], "Sales_Sample_3_1.qvd")
        self.assertEqual(result["summary"]["records"], 84775)
        self.assertEqual(result["summary"]["columns"], 32)
        self.assertTrue(result["summary"]["validation_passed"])
        self.assertIn("migration_summary.json", names)
        self.assertIn("load_parquet_to_delta.sql", names)

    def test_package_route_creates_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "package-session"
            output_dir = os.path.join(tmp, session_id, "qvd_outputs")
            build_package_fixture(output_dir)

            app = Flask(__name__)
            register_qvd_routes(app, tmp)
            response = app.test_client().post(
                f"/api/qvd/generate-migration-package/{session_id}",
                json={"target_table": "sales_sample", "file_name": "Sales_Sample_3_1.qvd"},
            )
            payload = response.get_json()
            zip_exists = os.path.exists(payload["migration_package_zip"])

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["generated"])
        self.assertTrue(zip_exists)

    def test_package_download_route_returns_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "package-session"
            output_dir = os.path.join(tmp, session_id, "qvd_outputs")
            build_package_fixture(output_dir)
            generate_migration_package(output_dir, "sales_sample", "Sales_Sample_3_1.qvd")

            app = Flask(__name__)
            register_qvd_routes(app, tmp)
            response = app.test_client().get(f"/api/qvd/download-migration-package/{session_id}")
            response.get_data()
            response.close()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/zip")

    def test_package_rejects_failed_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_package_fixture(tmp)
            write_json(os.path.join(tmp, "parquet_validation_sales_sample.json"), {"success": False, "passed": False})
            result = generate_migration_package(tmp, "sales_sample")

        self.assertFalse(result["generated"])
        self.assertIn("validation", result["errors"][0].lower())


if __name__ == "__main__":
    unittest.main()
