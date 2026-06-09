import json
import os
import socket
import tempfile
import unittest
import zipfile

from flask import Flask

from backend.integrations import qvd_routes
from qvd_to_databricks.databricks_connection import (
    DatabricksConnectionConfig,
    list_catalogs,
    list_schemas,
    list_volumes,
    list_warehouses,
    load_connection_config,
    merge_connection_config,
    save_connection_config,
    test_databricks_connection,
)
from qvd_to_databricks.databricks_executor import (
    build_volume_table_path,
    execute_qvd_migration,
    execute_sql_statement,
    precheck_execution,
    rewrite_sql_target,
    upload_parquet_to_volume,
    write_upload_status,
)
from qvd_to_databricks.databricks_loader import (
    render_cast_expression,
    render_create_temp_view_sql,
    render_insert_select_cast_sql,
    render_validation_sql,
)


class FakeClient:
    def __init__(self):
        self.posts = []
        self.uploads = []

    def get(self, path, timeout=120):
        if path == "/api/2.0/sql/warehouses":
            return {"warehouses": [{"id": "warehouse", "name": "Warehouse", "state": "RUNNING"}]}
        if path == "/api/2.1/unity-catalog/catalogs":
            return {"catalogs": [{"name": "workspace", "catalog_type": "REGULAR", "owner": "owner"}]}
        if path.startswith("/api/2.1/unity-catalog/schemas?"):
            return {"schemas": [{"name": "qvd_raw", "catalog_name": "workspace", "owner": "owner"}]}
        if path.startswith("/api/2.1/unity-catalog/volumes?"):
            return {"volumes": [{"name": "qvd_uploads", "catalog_name": "workspace", "schema_name": "qvd_raw", "volume_type": "MANAGED"}]}
        return {"path": path}

    def post(self, path, payload, timeout=120):
        self.posts.append((path, payload))
        if payload.get("statement", "").startswith("LIST "):
            return {"result": {"data_array": [["part-000.parquet"]]}}
        if "COUNT" in payload.get("statement", ""):
            return {"result": {"data_array": [["2"]]}}
        if "FAIL" in payload.get("statement", ""):
            return {"statement_id": "bad", "status": {"state": "FAILED", "error": {"error_code": "BAD_REQUEST", "message": "bad sql"}}}
        return {"statement_id": f"stmt-{len(self.posts)}"}

    def put_binary(self, path, data, content_type="application/octet-stream"):
        self.uploads.append((path, data, content_type))
        return {}


class TimeoutClient(FakeClient):
    def post(self, path, payload, timeout=120):
        raise socket.timeout()


class PollingClient(FakeClient):
    def __init__(self, statuses):
        super().__init__()
        self.statuses = list(statuses)
        self.gets = []

    def post(self, path, payload, timeout=120):
        self.posts.append((path, payload))
        return {"statement_id": "stmt-poll", "status": {"state": "PENDING"}}

    def get(self, path, timeout=120):
        self.gets.append(path)
        status = self.statuses.pop(0) if self.statuses else "SUCCEEDED"
        return {"statement_id": "stmt-poll", "status": {"state": status}}


def write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


def build_execution_fixture(output_dir, validation_passed=True, local_warning=""):
    package_dir = os.path.join(output_dir, "migration_package")
    os.makedirs(package_dir, exist_ok=True)
    with zipfile.ZipFile(os.path.join(package_dir, "migration_package.zip"), "w") as archive:
        archive.writestr("migration_summary.json", "{}")

    write_json(os.path.join(output_dir, "parquet_validation_sales_table.json"), {
        "success": validation_passed,
        "passed": validation_passed,
        "checks": [{"name": "row_count", "details": {"expected": 2, "actual": 2}}],
    })
    write(os.path.join(output_dir, "databricks_load", "create_table.sql"), "CREATE TABLE IF NOT EXISTS main.qvd_raw.sales_table (customer STRING) USING DELTA;")
    write(os.path.join(output_dir, "databricks_load", "load_parquet_to_delta.sql"), "COPY INTO main.qvd_raw.sales_table FROM 's3://bucket/sales_table/' FILEFORMAT = PARQUET;")
    write_json(os.path.join(output_dir, "databricks_load", "load_config.json"), {
        "catalog": "main",
        "schema": "qvd_raw",
        "parquet_path": "s3://bucket/sales_table/",
        "local_path_warning": local_warning,
    })


def config(**overrides):
    values = {
        "workspace_url": "https://example.databricks.com",
        "personal_access_token": "token",
        "sql_warehouse_id": "warehouse",
        "catalog": "main",
        "schema": "qvd_raw",
        "cloud_storage_path": "s3://bucket/qvd",
    }
    values.update(overrides)
    return DatabricksConnectionConfig.from_payload(values)


class DatabricksExecutionTests(unittest.TestCase):
    def test_connection_validation_rejects_missing_required_fields(self):
        result = test_databricks_connection(DatabricksConnectionConfig())

        self.assertFalse(result["success"])
        self.assertIn("Databricks Workspace URL is required.", result["errors"])

    def test_connection_validation_success_with_fake_client(self):
        result = test_databricks_connection(config(), client=FakeClient())

        self.assertTrue(result["success"])
        self.assertTrue(result["checks"]["warehouse"])
        self.assertTrue(result["checks"]["catalog"])
        self.assertTrue(result["checks"]["schema"])

    def test_save_config_then_precheck_uses_saved_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = os.path.join(tmp, "session", "qvd_outputs")
            build_execution_fixture(output_dir)
            save_connection_config(output_dir, config(catalog="workspace", schema="qvd_raw"))
            saved = merge_connection_config(output_dir, {})
            result = precheck_execution(output_dir, "sales_table", "execute_ddl_only", saved)

        self.assertTrue(result["passed"])
        self.assertNotIn("Databricks Workspace URL is required.", result["errors"])

    def test_saved_public_config_masks_token_and_secret_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = os.path.join(tmp, "session", "qvd_outputs")
            save_connection_config(output_dir, config(personal_access_token="secret-token"))
            with open(os.path.join(output_dir, "databricks_config.json"), encoding="utf-8") as handle:
                public_config = json.load(handle)
            loaded = load_connection_config(output_dir)

        self.assertNotIn("personal_access_token", public_config)
        self.assertTrue(public_config["personal_access_token_present"])
        self.assertEqual(loaded.personal_access_token, "secret-token")

    def test_discovery_response_parsing(self):
        fake = FakeClient()
        cfg = config(catalog="workspace")

        self.assertEqual(list_warehouses(cfg, client=fake)[0]["id"], "warehouse")
        self.assertEqual(list_catalogs(cfg, client=fake)[0]["name"], "workspace")
        self.assertEqual(list_schemas(cfg, "workspace", client=fake)[0]["name"], "qvd_raw")
        self.assertEqual(list_volumes(cfg, "workspace", "qvd_raw", client=fake)[0]["volume_path"], "/Volumes/workspace/qvd_raw/qvd_uploads")

    def test_precheck_reports_missing_migration_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = precheck_execution(tmp, "sales_table", "generate_sql_only", config())

        self.assertFalse(result["passed"])
        self.assertIn("Migration package zip not found.", result["errors"])

    def test_precheck_rejects_failed_parquet_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_execution_fixture(tmp, validation_passed=False)
            result = precheck_execution(tmp, "sales_table", "generate_sql_only", config())

        self.assertFalse(result["passed"])
        self.assertIn("Parquet validation has not passed.", result["errors"])

    def test_precheck_rejects_invalid_execution_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_execution_fixture(tmp)
            result = precheck_execution(tmp, "sales_table", "bad_mode", config())

        self.assertFalse(result["passed"])
        self.assertIn("Unsupported execution mode", result["errors"][0])

    def test_precheck_rejects_local_path_without_remote_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_execution_fixture(tmp, local_warning="local path")
            result = precheck_execution(
                tmp,
                "sales_table",
                "execute_ddl_load",
                config(cloud_storage_path="", volume_path=""),
        )

        self.assertFalse(result["passed"])
        self.assertTrue(any("cloud storage path or Unity Catalog volume" in error for error in result["errors"]))

    def test_full_migration_blocks_local_path_without_volume_or_cloud(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_execution_fixture(tmp, local_warning="local path")
            result = precheck_execution(tmp, "sales_table", "full_migration", config(cloud_storage_path="", volume_path="", volume=""))

        self.assertFalse(result["passed"])
        self.assertTrue(any("Databricks-readable Parquet path" in error for error in result["errors"]))

    def test_full_migration_passes_with_volume_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_execution_fixture(tmp, local_warning="local path")
            result = precheck_execution(tmp, "sales_table", "full_migration", config(cloud_storage_path="", catalog="workspace", volume="qvd_uploads"))

        self.assertTrue(result["passed"])
        self.assertTrue(result["databricks_readable_path"].startswith("/Volumes/workspace/qvd_raw/qvd_uploads/"))

    def test_precheck_validates_databricks_config_for_execution_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_execution_fixture(tmp)
            result = precheck_execution(
                tmp,
                "sales_table",
                "execute_ddl_only",
                config(workspace_url="not-a-url", sql_warehouse_id=""),
            )

        self.assertFalse(result["passed"])
        self.assertTrue(any("valid Databricks Workspace URL" in error for error in result["errors"]))
        self.assertTrue(any("SQL Warehouse ID is required" in error for error in result["errors"]))

    def test_execution_report_generation_sql_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_execution_fixture(tmp)
            result = execute_qvd_migration(
                tmp,
                "sales_table",
                "generate_sql_only",
                config(),
                connection_result={"success": True, "errors": []},
                client=FakeClient(),
            )
            report_path = os.path.join(tmp, "execution", "execution_report.json")
            summary_path = os.path.join(tmp, "execution", "execution_summary.md")
            report_exists = os.path.exists(report_path)
            summary_exists = os.path.exists(summary_path)

        self.assertTrue(result["success"])
        self.assertTrue(report_exists)
        self.assertTrue(summary_exists)
        self.assertEqual(result["report"]["execution_mode"], "generate_sql_only")

    def test_execution_mode_execute_ddl_and_load_posts_statements(self):
        fake = FakeClient()
        with tempfile.TemporaryDirectory() as tmp:
            build_execution_fixture(tmp)
            result = execute_qvd_migration(
                tmp,
                "sales_table",
                "execute_ddl_load",
                config(),
                connection_result={"success": True, "errors": []},
                client=fake,
            )

        self.assertTrue(result["success"])
        statements = [payload["statement"] for _, payload in fake.posts]
        # Schema, Table, INSERT SELECT CAST, COUNT
        self.assertEqual(len(fake.posts), 4)
        self.assertIn("CREATE SCHEMA", statements[0])
        self.assertIn("CREATE TABLE", statements[1])
        self.assertNotIn("CREATE OR REPLACE TEMPORARY VIEW", "\n".join(statements))
        self.assertIn("INSERT INTO", statements[2])
        self.assertIn("SELECT CAST", statements[2].upper())
        # Insert should select directly from Parquet path
        self.assertTrue(any("FROM parquet." in s for s in statements))
        self.assertIn("COUNT", statements[3])
        self.assertEqual(result["report"]["loaded_rows"], 2)
        self.assertTrue(result["report"]["row_count_match"])

    def test_rewrite_sql_target_replaces_default_catalog_schema(self):
        sql = (
            "CREATE TABLE IF NOT EXISTS main.qvd_raw.sales_table (id STRING);\n"
            "COPY INTO `main`.`qvd_raw`.`sales_table` FROM 's3://bucket/';"
        )

        rewritten = rewrite_sql_target(sql, "workspace", "qvd_raw", "sales_table")

        self.assertIn("`workspace`.`qvd_raw`.`sales_table`", rewritten)
        self.assertNotIn("main.qvd_raw.sales_table", rewritten)
        self.assertNotIn("`main`.`qvd_raw`.`sales_table`", rewritten)

    def test_precheck_warns_generated_sql_target_will_be_overridden(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_execution_fixture(tmp)
            result = precheck_execution(tmp, "sales_table", "execute_ddl_only", config(catalog="workspace", schema="qvd_raw"))

        self.assertTrue(result["passed"])
        self.assertIn("Execution will override generated SQL target with selected deployment catalog/schema.", result["warnings"])

    def test_full_migration_uses_selected_catalog_schema_and_volume_path(self):
        fake = FakeClient()
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "session-123"
            output_dir = os.path.join(tmp, session_id, "qvd_outputs")
            build_execution_fixture(output_dir, local_warning="local path")
            write_upload_status(output_dir, {
                "success": True,
                "uploaded_file_count": 1,
                "volume_path": "/Volumes/workspace/qvd_raw/qvd_uploads/session-123/sales_table/",
            })
            result = execute_qvd_migration(
                output_dir,
                "sales_table",
                "full_migration",
                config(cloud_storage_path="", catalog="workspace", schema="qvd_raw", volume="qvd_uploads"),
                connection_result={"success": True, "errors": []},
                client=fake,
                session_id=session_id,
            )
            statements = [payload["statement"] for _, payload in fake.posts]

        self.assertTrue(result["success"])
        self.assertIn("CREATE SCHEMA IF NOT EXISTS `workspace`.`qvd_raw`", statements[0])
        self.assertIn("CREATE TABLE IF NOT EXISTS `workspace`.`qvd_raw`.`sales_table`", statements[1])
        self.assertNotIn("CREATE OR REPLACE TEMPORARY VIEW", "\n".join(statements))
        self.assertIn("INSERT INTO", statements[2])
        self.assertIn("SELECT", statements[2])
        self.assertTrue(any("FROM parquet." in s for s in statements))
        self.assertFalse(any("main.qvd_raw" in statement for statement in statements))
        self.assertFalse(any("`main`.`qvd_raw`" in statement for statement in statements))
        # No COPY INTO should be present
        self.assertFalse(any("COPY INTO" in statement for statement in statements))

    def test_upload_parquet_to_volume_uploads_only_parquet_files(self):
        fake = FakeClient()
        with tempfile.TemporaryDirectory() as tmp:
            write(os.path.join(tmp, "part-000.parquet"), "parquet")
            write(os.path.join(tmp, "_SUCCESS"), "")
            result = upload_parquet_to_volume("session", tmp, "workspace", "qvd_raw", "qvd_uploads", "sales_table", config(), client=fake)

        self.assertTrue(result["success"])
        self.assertEqual(result["uploaded_file_count"], 1)
        self.assertIn("/api/2.0/fs/files/Volumes/workspace/qvd_raw/qvd_uploads/session/sales_table/part-000.parquet", fake.uploads[0][0])

    def test_upload_path_equals_copy_volume_path(self):
        expected = build_volume_table_path("workspace", "qvd_raw", "qvd_uploads", "session", "sales_table")
        fake = FakeClient()
        with tempfile.TemporaryDirectory() as tmp:
            write(os.path.join(tmp, "part-000.parquet"), "parquet")
            result = upload_parquet_to_volume("session", tmp, "workspace", "qvd_raw", "qvd_uploads", "sales_table", config(), client=fake)

        self.assertEqual(result["volume_path"], expected)
        self.assertIn(expected, fake.uploads[0][0])

    def test_stage_view_failure_stops_before_insert(self):
        class InsertFailureBeforeClient(FakeClient):
            def post(self, path, payload, timeout=120):
                self.posts.append((path, payload))
                statement = payload.get("statement", "")
                if "INSERT INTO" in statement:
                    return {
                        "statement_id": "insert-err-1",
                        "status": {
                            "state": "FAILED",
                            "error": {"error_code": "DELTA_NOT_FOUND", "message": "Path does not exist"},
                        },
                    }
                return {"statement_id": f"stmt-{len(self.posts)}"}

        fake = InsertFailureBeforeClient()
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "session"
            output_dir = os.path.join(tmp, session_id, "qvd_outputs")
            build_execution_fixture(output_dir, local_warning="local path")
            write_upload_status(output_dir, {"success": True, "uploaded_file_count": 1, "volume_path": build_volume_table_path("workspace", "qvd_raw", "qvd_uploads", session_id, "sales_table")})
            result = execute_qvd_migration(
                output_dir,
                "sales_table",
                "execute_ddl_load",
                config(cloud_storage_path="", catalog="workspace", schema="qvd_raw", volume="qvd_uploads"),
                connection_result={"success": True, "errors": []},
                client=fake,
                session_id=session_id,
            )
            statements = [payload["statement"] for _, payload in fake.posts]

        self.assertFalse(result["success"])
        self.assertEqual(result["stage"], "insert_cast")
        self.assertFalse(any("CREATE OR REPLACE TEMPORARY VIEW" in s for s in statements))
        self.assertTrue(any("INSERT INTO" in s for s in statements))
        self.assertIn("Path does not exist", result["error"])

    def test_insert_cast_failure_returns_error_statement_id_and_sql_preview(self):
        class InsertFailureClient(FakeClient):
            def post(self, path, payload, timeout=120):
                self.posts.append((path, payload))
                statement = payload.get("statement", "")
                if "INSERT INTO" in statement:
                    return {
                        "statement_id": "insert-123",
                        "status": {
                            "state": "FAILED",
                            "error": {"error_code": "DELTA_FAILED_TO_MERGE_FIELDS", "message": "Cannot merge fields"},
                        },
                    }
                return {"statement_id": f"stmt-{len(self.posts)}"}

        with tempfile.TemporaryDirectory() as tmp:
            session_id = "session"
            output_dir = os.path.join(tmp, session_id, "qvd_outputs")
            build_execution_fixture(output_dir, local_warning="local path")
            write_upload_status(output_dir, {"success": True, "uploaded_file_count": 1, "volume_path": build_volume_table_path("workspace", "qvd_raw", "qvd_uploads", session_id, "sales_table")})
            result = execute_qvd_migration(
                output_dir,
                "sales_table",
                "execute_ddl_load",
                config(cloud_storage_path="", catalog="workspace", schema="qvd_raw", volume="qvd_uploads"),
                connection_result={"success": True, "errors": []},
                client=InsertFailureClient(),
                session_id=session_id,
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["stage"], "insert_cast")
        self.assertIn("Cannot merge fields", result["error"])
        self.assertIn("DELTA_FAILED_TO_MERGE_FIELDS", result["error"])
        self.assertEqual(result["error_code"], "DELTA_FAILED_TO_MERGE_FIELDS")

    def test_sql_statement_execution_failure(self):
        result = execute_sql_statement("FAIL SELECT", "warehouse", config=config(), client=FakeClient())

        self.assertFalse(result["success"])
        self.assertEqual(result["stage"], "statement")
        self.assertIn("bad sql", result["error"])
        # Full error payload must be present
        self.assertIn("error_code", result)
        self.assertEqual(result["error_code"], "BAD_REQUEST")
        self.assertIn("message", result)
        self.assertEqual(result["message"], "bad sql")
        self.assertEqual(result["statement_id"], "bad")

    def test_sql_statement_polls_pending_running_until_success(self):
        fake = PollingClient(["RUNNING", "SUCCEEDED"])
        logs = []

        result = execute_sql_statement(
            "SELECT 1",
            "warehouse",
            config=config(),
            client=fake,
            poll_interval_seconds=0,
            log_callback=logs.append,
        )

        self.assertEqual(result["status"]["state"], "SUCCEEDED")
        self.assertEqual(fake.posts[0][1]["wait_timeout"], "10s")
        self.assertEqual(len(fake.gets), 2)
        self.assertTrue(any("PENDING" in line for line in logs))
        self.assertTrue(any("RUNNING" in line for line in logs))
        self.assertTrue(any("SUCCEEDED" in line for line in logs))

    def test_socket_timeout_returns_controlled_failure_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_execution_fixture(tmp)
            result = execute_qvd_migration(
                tmp,
                "sales_table",
                "execute_ddl_only",
                config(),
                connection_result={"success": True, "errors": []},
                client=TimeoutClient(),
            )
            report_path = os.path.join(tmp, "execution", "execution_report.json")
            with open(report_path, encoding="utf-8") as handle:
                report = json.load(handle)

        self.assertFalse(result["success"])
        self.assertEqual(result["stage"], "schema")
        self.assertEqual(report["execution_status"], "failed")
        self.assertEqual(report["stage"], "schema")
        self.assertIn(
            "Databricks statement is still running or warehouse startup timed out. Retry after warehouse is running.",
            report["errors"],
        )

    def test_execute_route_timeout_does_not_return_raw_flask_500(self):
        original_execute = qvd_routes.execute_qvd_migration

        def raise_timeout(*args, **kwargs):
            raise socket.timeout()

        with tempfile.TemporaryDirectory() as tmp:
            session_id = "route-timeout"
            output_dir = os.path.join(tmp, session_id, "qvd_outputs")
            build_execution_fixture(output_dir)
            app = Flask(__name__)
            qvd_routes.register_qvd_routes(app, tmp)
            qvd_routes.execute_qvd_migration = raise_timeout
            try:
                response = app.test_client().post(
                    f"/api/qvd/databricks/execute/{session_id}",
                    json={"target_table": "sales_table", "execution_mode": "generate_sql_only"},
                )
                payload = response.get_json()
                report_path = os.path.join(output_dir, "execution", "execution_report.json")
                report_exists = os.path.exists(report_path)
            finally:
                qvd_routes.execute_qvd_migration = original_execute

        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(payload, dict)
        self.assertFalse(payload["success"])
        self.assertEqual(payload["status"], "failed")
        self.assertTrue(report_exists)
        self.assertIn(
            "Databricks statement is still running or warehouse startup timed out. Retry after warehouse is running.",
            payload["errors"],
        )

    def test_failed_statement_returns_friendly_execution_result_and_report(self):
        class FailingCreateClient(FakeClient):
            def post(self, path, payload, timeout=120):
                self.posts.append((path, payload))
                if len(self.posts) == 1:
                    return {"statement_id": "schema-ok"}
                return {"statement_id": "failed-ddl", "status": {"state": "FAILED", "error": {"error_code": "TABLE_OR_VIEW_NOT_FOUND", "message": "warehouse starting too slowly"}}}

        with tempfile.TemporaryDirectory() as tmp:
            build_execution_fixture(tmp)
            result = execute_qvd_migration(
                tmp,
                "sales_table",
                "execute_ddl_only",
                config(),
                connection_result={"success": True, "errors": []},
                client=FailingCreateClient(),
            )
            report_path = os.path.join(tmp, "execution", "execution_report.json")
            with open(report_path, encoding="utf-8") as handle:
                report = json.load(handle)

        self.assertFalse(result["success"])
        self.assertEqual(result["stage"], "table")
        self.assertEqual(report["stage"], "table")
        self.assertIn("warehouse starting too slowly", result["error"])
        # error_code must surface into result and report
        self.assertEqual(result["error_code"], "TABLE_OR_VIEW_NOT_FOUND")
        self.assertEqual(report["error_code"], "TABLE_OR_VIEW_NOT_FOUND")

    def test_execution_report_includes_volume_upload_count(self):
        fake = FakeClient()
        with tempfile.TemporaryDirectory() as tmp:
            build_execution_fixture(tmp, local_warning="local path")
            write_upload_status(tmp, {"success": True, "uploaded_file_count": 2, "volume_path": "/Volumes/workspace/qvd_raw/qvd_uploads/session/sales_table/"})
            result = execute_qvd_migration(
                tmp,
                "sales_table",
                "execute_ddl_load",
                config(cloud_storage_path="", catalog="workspace", volume="qvd_uploads"),
                connection_result={"success": True, "errors": []},
                client=fake,
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["report"]["uploaded_files"], 2)
        self.assertIn("databricks_readable_path", result["report"])


class InsertSelectCastTests(unittest.TestCase):
    """Unit tests for INSERT SELECT CAST SQL generation."""

    def _mapping_row(self, col, col_type):
        return {"target_table": "sales_sample", "target_column": col, "target_type": col_type}

    def test_decimal_cast_generation(self):
        rows = [self._mapping_row("actual_sales", "DECIMAL(18,2)")]
        sql = render_insert_select_cast_sql("sales_sample", rows, parquet_columns=None)
        self.assertIn("CAST(`actual_sales` AS DECIMAL(18,2)) AS `actual_sales`", sql)

    def test_date_cast_generation(self):
        rows = [self._mapping_row("calendar_date", "DATE")]
        sql = render_insert_select_cast_sql("sales_sample", rows, parquet_columns=None)
        self.assertIn("CAST(`calendar_date` AS DATE) AS `calendar_date`", sql)

    def test_boolean_cast_generation(self):
        rows = [self._mapping_row("actual_flag", "BOOLEAN")]
        sql = render_insert_select_cast_sql("sales_sample", rows, parquet_columns=None)
        self.assertIn("CAST(`actual_flag` AS BOOLEAN) AS `actual_flag`", sql)

    def test_bigint_cast_generation(self):
        rows = [self._mapping_row("units", "BIGINT")]
        sql = render_insert_select_cast_sql("sales_sample", rows, parquet_columns=None)
        self.assertIn("CAST(`units` AS BIGINT) AS `units`", sql)

    def test_string_cast_generation(self):
        rows = [self._mapping_row("customer", "STRING")]
        sql = render_insert_select_cast_sql("sales_sample", rows, parquet_columns=None)
        self.assertIn("CAST(`customer` AS STRING) AS `customer`", sql)

    def test_insert_targets_correct_table(self):
        rows = [self._mapping_row("customer", "STRING")]
        sql = render_insert_select_cast_sql(
            "sales_sample", rows, parquet_columns=None, catalog="workspace", schema="default"
        )
        self.assertIn("INSERT INTO workspace.default.sales_sample", sql)

    def test_insert_selects_from_temp_view(self):
        rows = [self._mapping_row("customer", "STRING")]
        sql = render_insert_select_cast_sql("sales_sample", rows, parquet_columns=None)
        self.assertIn("FROM qvd_stage_sales_sample", sql)

    def test_audit_columns_present_in_parquet_are_cast(self):
        rows = [self._mapping_row("customer", "STRING")]
        parquet_cols = ["customer", "_source_file_name", "_source_file_path", "_ingestion_timestamp", "_batch_id", "_record_hash"]
        sql = render_insert_select_cast_sql("sales_sample", rows, parquet_columns=parquet_cols)
        self.assertIn("CAST(`_source_file_name` AS STRING)", sql)
        self.assertIn("CAST(`_ingestion_timestamp` AS TIMESTAMP)", sql)

    def test_audit_columns_missing_from_parquet_are_generated(self):
        rows = [self._mapping_row("customer", "STRING")]
        # Parquet has no audit columns
        sql = render_insert_select_cast_sql("sales_sample", rows, parquet_columns=["customer"])
        self.assertIn("input_file_name()", sql)
        self.assertIn("CURRENT_TIMESTAMP()", sql)

    def test_audit_columns_always_included_when_parquet_columns_unknown(self):
        rows = [self._mapping_row("customer", "STRING")]
        sql = render_insert_select_cast_sql("sales_sample", rows, parquet_columns=None)
        # When parquet_columns is None (unknown), audit cols are cast from the view
        self.assertIn("_source_file_name", sql)
        self.assertIn("_ingestion_timestamp", sql)

    def test_create_temp_view_sql_uses_correct_path(self):
        sql = render_create_temp_view_sql("sales_sample", "/Volumes/workspace/default/sample/sess/sales_sample/")
        self.assertIn("CREATE OR REPLACE TEMPORARY VIEW qvd_stage_sales_sample", sql)
        self.assertIn("USING PARQUET", sql)
        self.assertIn("path = '/Volumes/workspace/default/sample/sess/sales_sample/'", sql)

    def test_stage_view_sql_syntax(self):
        """Verify the exact syntax format for temporary view SQL generation."""
        view_name = "sales_sample"
        path = "/Volumes/workspace/default/sample/sess/sales_sample/"
        sql = render_create_temp_view_sql(view_name, path)
        expected_sql = (
            "CREATE OR REPLACE TEMPORARY VIEW qvd_stage_sales_sample\n"
            "USING PARQUET\n"
            "OPTIONS (\n"
            "  path = '/Volumes/workspace/default/sample/sess/sales_sample/'\n"
            ");\n"
        )
        self.assertEqual(sql, expected_sql)

    def test_validation_sql_targets_correct_table(self):
        sql = render_validation_sql("sales_sample", "workspace", "default")
        self.assertIn("SELECT COUNT(*) AS loaded_rows FROM `workspace`.`default`.`sales_sample`", sql)

    def test_render_cast_expression_decimal(self):
        expr = render_cast_expression("actual_sales", "DECIMAL(18,2)")
        self.assertEqual(expr, "CAST(`actual_sales` AS DECIMAL(18,2))")

    def test_render_cast_expression_unknown_type_defaults_to_string(self):
        expr = render_cast_expression("some_col", "UNKNOWN_TYPE")
        self.assertIn("CAST(`some_col` AS STRING)", expr)

    def test_full_migration_uses_insert_select_not_copy_into(self):
        """Full migration flow must not use COPY INTO; it uses INSERT SELECT CAST."""
        fake = FakeClient()
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "session-cast"
            output_dir = os.path.join(tmp, session_id, "qvd_outputs")
            build_execution_fixture(output_dir)
            write_upload_status(output_dir, {
                "success": True,
                "uploaded_file_count": 1,
                "volume_path": "/Volumes/main/qvd_raw/qvd_uploads/session-cast/sales_table/",
            })
            result = execute_qvd_migration(
                output_dir,
                "sales_table",
                "full_migration",
                config(cloud_storage_path="s3://bucket/qvd"),
                connection_result={"success": True, "errors": []},
                client=fake,
                session_id=session_id,
            )
            statements = [payload["statement"] for _, payload in fake.posts]

        self.assertTrue(result["success"])
        self.assertFalse(any("COPY INTO" in s for s in statements),
                         "COPY INTO should not appear in full_migration execution")
        self.assertFalse(any("CREATE OR REPLACE TEMPORARY VIEW" in s for s in statements))
        self.assertTrue(any("INSERT INTO" in s for s in statements))

    def test_row_count_validation_match_logged(self):
        """Row count validation result is logged with Source/Loaded/Match."""
        fake = FakeClient()
        with tempfile.TemporaryDirectory() as tmp:
            build_execution_fixture(tmp)
            result = execute_qvd_migration(
                tmp,
                "sales_table",
                "execute_ddl_load",
                config(),
                connection_result={"success": True, "errors": []},
                client=fake,
            )

        # source=2, loaded=2 (FakeClient returns 2 for COUNT), match=TRUE
        self.assertTrue(result["report"]["row_count_match"])
        self.assertEqual(result["report"]["source_row_count"], 2)
        self.assertEqual(result["report"]["loaded_row_count"], 2)
        row_validation_logged = any(
            "ROW COUNT VALIDATION" in line for line in result["logs"]
        )
        self.assertTrue(row_validation_logged)


class DatabricksErrorPayloadTests(unittest.TestCase):
    """Verify that full Databricks error detail (error_code, message, statement_id)
    is captured, logged, and surfaced in all output channels."""

    def _stage_view_failure_client(self, error_code, message, stmt_id="view-err-1"):
        """Return a client that fails on the TEMPORARY VIEW statement with given error detail."""
        class _Client(FakeClient):
            def post(self, path, payload, timeout=120):
                self.posts.append((path, payload))
                # Fail on INSERT INTO (insert-cast) or explicit TEMP VIEW creation
                # to simulate parquet path / merge failures and to support direct
                # execute_sql_statement tests that send a TEMP VIEW statement.
                statement = payload.get("statement", "")
                if "INSERT INTO" in statement or "CREATE OR REPLACE TEMPORARY VIEW" in statement:
                    return {
                        "statement_id": stmt_id,
                        "status": {
                            "state": "FAILED",
                            "error": {"error_code": error_code, "message": message},
                        },
                    }
                return {"statement_id": f"stmt-{len(self.posts)}"}
        return _Client()

    def test_failed_statement_result_contains_error_code_message_statement_id(self):
        """execute_sql_statement must return error_code, message, statement_id on FAILED."""
        result = execute_sql_statement(
            "FAIL SELECT",
            "warehouse",
            config=config(),
            client=FakeClient(),
        )

        self.assertFalse(result["success"])
        self.assertIn("error_code", result)
        self.assertIn("message", result)
        self.assertIn("statement_id", result)
        self.assertNotEqual(result["error_code"], "")
        self.assertNotEqual(result["message"], "")

    def test_failed_statement_error_block_logged(self):
        """DATABRICKS_ERROR_START / END block must appear in the log callback output."""
        logs = []
        execute_sql_statement(
            "FAIL SELECT",
            "warehouse",
            config=config(),
            client=FakeClient(),
            log_callback=logs.append,
        )

        log_text = "\n".join(logs)
        self.assertIn("DATABRICKS_ERROR_START", log_text)
        self.assertIn("DATABRICKS_ERROR_END", log_text)
        self.assertIn("error_code=", log_text)
        self.assertIn("message=", log_text)
        self.assertIn("statement_id=", log_text)
        self.assertIn("sql_text=", log_text)

    def test_stage_view_failure_surfaces_full_error_in_result(self):
        """When STAGE_VIEW fails, error_code and message must be in the top-level result."""
        fake = self._stage_view_failure_client(
            error_code="DELTA_FAILED_TO_MERGE_FIELDS",
            message="Failed to merge fields 'actual_sales' and 'actual_sales'",
        )
        with tempfile.TemporaryDirectory() as tmp:
            build_execution_fixture(tmp)
            result = execute_qvd_migration(
                tmp, "sales_table", "execute_ddl_load",
                config(),
                connection_result={"success": True, "errors": []},
                client=fake,
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["stage"], "insert_cast")
        self.assertIn("DELTA_FAILED_TO_MERGE_FIELDS", result["error"])
        self.assertIn("Failed to merge fields", result["error"])

    def test_stage_view_failure_error_block_in_logs(self):
        """DATABRICKS_ERROR_START block must appear in execution logs on STAGE_VIEW failure."""
        fake = self._stage_view_failure_client(
            error_code="DELTA_FAILED_TO_MERGE_FIELDS",
            message="Failed to merge fields 'actual_sales' and 'actual_sales'",
        )
        with tempfile.TemporaryDirectory() as tmp:
            build_execution_fixture(tmp)
            result = execute_qvd_migration(
                tmp, "sales_table", "execute_ddl_load",
                config(),
                connection_result={"success": True, "errors": []},
                client=fake,
            )

        log_text = "\n".join(result["logs"])
        self.assertIn("DATABRICKS_ERROR_START", log_text)
        self.assertIn("DATABRICKS_ERROR_END", log_text)
        self.assertIn("error_code=DELTA_FAILED_TO_MERGE_FIELDS", log_text)
        self.assertIn("Failed to merge fields", log_text)
        self.assertIn("statement_id=view-err-1", log_text)

    def test_stage_view_failure_error_code_in_report_json(self):
        """error_code must be written to execution_report.json."""
        fake = self._stage_view_failure_client(
            error_code="DELTA_FAILED_TO_MERGE_FIELDS",
            message="Failed to merge fields 'actual_sales' and 'actual_sales'",
        )
        with tempfile.TemporaryDirectory() as tmp:
            build_execution_fixture(tmp)
            execute_qvd_migration(
                tmp, "sales_table", "execute_ddl_load",
                config(),
                connection_result={"success": True, "errors": []},
                client=fake,
            )
            report_path = os.path.join(tmp, "execution", "execution_report.json")
            with open(report_path, encoding="utf-8") as handle:
                report = json.load(handle)

        self.assertIn("error_code", report)
        self.assertEqual(report["error_code"], "DELTA_FAILED_TO_MERGE_FIELDS")
        self.assertIn("errors", report)
        self.assertTrue(any("DELTA_FAILED_TO_MERGE_FIELDS" in e for e in report["errors"]))

    def test_stage_view_failure_error_detail_in_log_txt(self):
        """Full error must be written to execution_log.txt."""
        fake = self._stage_view_failure_client(
            error_code="DELTA_FAILED_TO_MERGE_FIELDS",
            message="Failed to merge fields 'actual_sales' and 'actual_sales'",
        )
        with tempfile.TemporaryDirectory() as tmp:
            build_execution_fixture(tmp)
            execute_qvd_migration(
                tmp, "sales_table", "execute_ddl_load",
                config(),
                connection_result={"success": True, "errors": []},
                client=fake,
            )
            log_path = os.path.join(tmp, "execution", "execution_log.txt")
            with open(log_path, encoding="utf-8") as handle:
                log_text = handle.read()

        self.assertIn("DATABRICKS_ERROR_START", log_text)
        self.assertIn("DELTA_FAILED_TO_MERGE_FIELDS", log_text)
        self.assertIn("Failed to merge fields", log_text)

    def test_failed_statement_returns_full_payload(self):
        """Verify that execute_sql_statement returns statement_id, state, error_code, message, and sql_text on failure."""
        fake_client = self._stage_view_failure_client(
            error_code="MOCK_TEST_ERROR",
            message="This is a mock error statement test",
            stmt_id="stmt-mock-999"
        )
        sql = "CREATE OR REPLACE TEMPORARY VIEW qvd_stage_sales_sample USING PARQUET OPTIONS (path = '/mock/path')"
        result = execute_sql_statement(
            sql,
            "warehouse",
            config=config(),
            client=fake_client
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["statement_id"], "stmt-mock-999")
        self.assertEqual(result["state"], "FAILED")
        self.assertEqual(result["error_code"], "MOCK_TEST_ERROR")
        self.assertEqual(result["message"], "This is a mock error statement test")
        self.assertEqual(result["sql_text"], sql)


if __name__ == "__main__":
    unittest.main()
