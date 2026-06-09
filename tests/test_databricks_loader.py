import json
import os
import tempfile
import unittest

from flask import Flask

from backend.integrations.qvd_routes import register_qvd_routes
from qvd_to_databricks.databricks_loader import generate_databricks_load_artifacts


CREATE_SQL = """CREATE TABLE IF NOT EXISTS `main`.`qvd_raw`.`sales_table` (
  `customer` STRING,
  `_source_file_name` STRING
)
USING DELTA;
"""


def write_ddl(output_dir, target_table="sales_table"):
    ddl_dir = os.path.join(output_dir, "ddl")
    os.makedirs(ddl_dir, exist_ok=True)
    ddl_path = os.path.join(ddl_dir, f"create_{target_table}.sql")
    with open(ddl_path, "w", encoding="utf-8") as handle:
        handle.write(CREATE_SQL)
    return ddl_path


def write_validation(output_dir, target_table="sales_table", passed=True, parquet_path="/tmp/local/parquet/sales_table"):
    os.makedirs(output_dir, exist_ok=True)
    validation_path = os.path.join(output_dir, f"parquet_validation_{target_table}.json")
    with open(validation_path, "w", encoding="utf-8") as handle:
        json.dump({
            "success": passed,
            "passed": passed,
            "target_table": target_table,
            "parquet_path": parquet_path,
            "failed_checks": [] if passed else [{"name": "row_count", "passed": False}],
        }, handle)
    return validation_path


class DatabricksLoaderTests(unittest.TestCase):
    def test_generate_load_artifacts_include_copy_into_and_pyspark(self):
        with tempfile.TemporaryDirectory() as tmp:
            ddl_path = write_ddl(tmp)
            validation_path = write_validation(tmp)
            result = generate_databricks_load_artifacts(
                "sales_table",
                "/tmp/local/parquet/sales_table",
                ddl_path,
                os.path.join(tmp, "databricks_load"),
                validation_report_path=validation_path,
            )
            readme_exists = os.path.exists(result["artifacts"]["readme"])

        self.assertTrue(result["generated"])
        self.assertIn("COPY INTO main.qvd_raw.sales_table", result["copy_into_sql"])
        self.assertIn('spark.read.parquet("/tmp/local/parquet/sales_table")', result["pyspark_snippet"])
        self.assertTrue(readme_exists)
        self.assertIn("local", result["local_path_warning"].lower())

    def test_generate_load_artifacts_support_parquet_path_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            ddl_path = write_ddl(tmp)
            validation_path = write_validation(tmp)
            result = generate_databricks_load_artifacts(
                "sales_table",
                "s3://bucket/qvd/sales_table/",
                ddl_path,
                os.path.join(tmp, "databricks_load"),
                validation_report_path=validation_path,
            )

        self.assertTrue(result["generated"])
        self.assertIn("FROM 's3://bucket/qvd/sales_table/'", result["copy_into_sql"])
        self.assertEqual(result["local_path_warning"], "")

    def test_route_rejects_missing_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "load-session"
            output_dir = os.path.join(tmp, session_id, "qvd_outputs")
            write_ddl(output_dir)

            app = Flask(__name__)
            register_qvd_routes(app, tmp)
            response = app.test_client().post(
                f"/api/qvd/generate-databricks-load/{session_id}",
                json={"target_table": "sales_table"},
            )
            payload = response.get_json()

        self.assertEqual(response.status_code, 404)
        self.assertFalse(payload["generated"])
        self.assertIn("validation", payload["errors"][0].lower())

    def test_route_rejects_failed_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "load-session"
            output_dir = os.path.join(tmp, session_id, "qvd_outputs")
            write_ddl(output_dir)
            write_validation(output_dir, passed=False)

            app = Flask(__name__)
            register_qvd_routes(app, tmp)
            response = app.test_client().post(
                f"/api/qvd/generate-databricks-load/{session_id}",
                json={"target_table": "sales_table"},
            )
            payload = response.get_json()

        self.assertEqual(response.status_code, 400)
        self.assertFalse(payload["generated"])
        self.assertIn("not passed", payload["errors"][0])

    def test_route_creates_databricks_load_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "load-session"
            output_dir = os.path.join(tmp, session_id, "qvd_outputs")
            write_ddl(output_dir)
            write_validation(output_dir)

            app = Flask(__name__)
            register_qvd_routes(app, tmp)
            response = app.test_client().post(
                f"/api/qvd/generate-databricks-load/{session_id}",
                json={"target_table": "sales_table", "parquet_path": "dbfs:/mnt/qvd/sales_table"},
            )
            payload = response.get_json()
            artifact_dir = os.path.join(output_dir, "databricks_load")
            readme_path = os.path.join(artifact_dir, "README_load_steps.md")
            load_sql_path = os.path.join(artifact_dir, "load_parquet_to_delta.sql")
            with open(load_sql_path, encoding="utf-8") as handle:
                load_sql = handle.read()
            readme_exists = os.path.exists(readme_path)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["generated"])
        self.assertTrue(readme_exists)
        self.assertIn("COPY INTO main.qvd_raw.sales_table", load_sql)
        self.assertIn('spark.read.parquet("dbfs:/mnt/qvd/sales_table")', payload["pyspark_snippet"])


if __name__ == "__main__":
    unittest.main()
