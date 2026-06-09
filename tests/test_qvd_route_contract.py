import csv
import json
import os
import tempfile
import unittest

from flask import Flask

from backend.integrations.qvd_routes import register_qvd_routes
from qvd_to_databricks.schema_suggester import MAPPING_COLUMNS


UI_POST_ROUTES = {
    "/api/qvd/upload-inspect",
    "/api/qvd/suggest-schema/<session_id>",
    "/api/qvd/business-analysis/entities/<session_id>",
    "/api/qvd/business-analysis/kpis/<session_id>",
    "/api/qvd/business-analysis/lineage-reconciliation/<session_id>",
    "/api/qvd/business-analysis/ai-explain/<session_id>",
    "/api/qvd/save-approved-mapping/<session_id>",
    "/api/qvd/generate-ddl/<session_id>",
    "/api/qvd/preview-rows/<session_id>",
    "/api/qvd/profile-columns/<session_id>",
    "/api/qvd/convert-parquet/<session_id>",
    "/api/qvd/validate-parquet/<session_id>",
    "/api/qvd/generate-databricks-load/<session_id>",
    "/api/qvd/generate-migration-package/<session_id>",
    "/api/qvd/databricks/save-config/<session_id>",
    "/api/qvd/databricks/test-connection/<session_id>",
    "/api/qvd/databricks/warehouses/<session_id>",
    "/api/qvd/databricks/catalogs/<session_id>",
    "/api/qvd/databricks/schemas/<session_id>",
    "/api/qvd/databricks/volumes/<session_id>",
    "/api/qvd/databricks/create-schema/<session_id>",
    "/api/qvd/databricks/create-volume/<session_id>",
    "/api/qvd/databricks/upload-parquet/<session_id>",
    "/api/qvd/databricks/precheck/<session_id>",
    "/api/qvd/databricks/execute/<session_id>",
}

UI_GET_ROUTES = {
    "/api/qvd/session/<session_id>",
}

CRITICAL_FRONTEND_QVD_ENDPOINTS = [
    "/api/qvd/business-analysis/entities/route-session",
    "/api/qvd/business-analysis/kpis/route-session",
    "/api/qvd/business-analysis/lineage-reconciliation/route-session",
    "/api/qvd/business-analysis/ai-explain/route-session",
    "/api/qvd/suggest-schema/route-session",
]


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


def build_post_ddl_fixture(output_dir):
    os.makedirs(os.path.join(output_dir, "ddl"), exist_ok=True)
    write_mapping(os.path.join(output_dir, "approved_databricks_mapping.csv"))
    with open(os.path.join(output_dir, "source_structure.csv"), "w", encoding="utf-8") as handle:
        handle.write("qvd_file,table_name,field_name\nSales_Sample_3_1.qvd,sales_sample,Customer\n")
    with open(os.path.join(output_dir, "ddl", "create_sales_sample.sql"), "w", encoding="utf-8") as handle:
        handle.write("CREATE TABLE IF NOT EXISTS main.qvd_raw.sales_sample (`customer` STRING) USING DELTA;\n")
    write_json(os.path.join(output_dir, "qvd_inspection.json"), {
        "session_id": "route-session",
        "uploaded_files": [{"file_name": "Sales_Sample_3_1.qvd"}],
        "tables": [{
            "summary": {
                "file_name": "Sales_Sample_3_1.qvd",
                "table_name": "sales_sample",
                "no_of_records": "10",
                "field_count": 1,
            },
            "fields": [],
        }],
    })


class QvdRouteContractTests(unittest.TestCase):
    def test_all_ui_qvd_post_routes_are_registered_for_post(self):
        app = Flask(__name__)
        with tempfile.TemporaryDirectory() as tmp:
            register_qvd_routes(app, tmp)

        routes = {str(rule): rule.methods for rule in app.url_map.iter_rules()}
        missing = sorted(route for route in UI_POST_ROUTES if route not in routes)
        not_post = sorted(route for route in UI_POST_ROUTES if route in routes and "POST" not in routes[route])

        self.assertEqual(missing, [])
        self.assertEqual(not_post, [])

    def test_all_ui_qvd_get_routes_are_registered_for_get(self):
        app = Flask(__name__)
        with tempfile.TemporaryDirectory() as tmp:
            register_qvd_routes(app, tmp)

        routes = {str(rule): rule.methods for rule in app.url_map.iter_rules()}
        missing = sorted(route for route in UI_GET_ROUTES if route not in routes)
        not_get = sorted(route for route in UI_GET_ROUTES if route in routes and "GET" not in routes[route])

        self.assertEqual(missing, [])
        self.assertEqual(not_get, [])

    def test_qvd_session_route_returns_inspection_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "session-route"
            output_dir = os.path.join(tmp, session_id, "qvd_outputs")
            build_post_ddl_fixture(output_dir)

            app = Flask(__name__)
            register_qvd_routes(app, tmp)
            response = app.test_client().get(f"/api/qvd/session/{session_id}")
            payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["sessionType"], "qvd")
        self.assertEqual(payload["qvdInspection"]["uploaded_files"][0]["file_name"], "Sales_Sample_3_1.qvd")
        self.assertTrue(payload["qvdApprovedMapping"]["saved"])
        self.assertTrue(payload["qvdDdlGeneration"]["generated"])

    def test_generate_databricks_load_route_after_ddl_without_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "load-session"
            output_dir = os.path.join(tmp, session_id, "qvd_outputs")
            build_post_ddl_fixture(output_dir)

            app = Flask(__name__)
            register_qvd_routes(app, tmp)
            response = app.test_client().post(
                f"/api/qvd/generate-databricks-load/{session_id}",
                json={"target_table": "sales_sample"},
            )
            payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["generated"])
        self.assertEqual(payload["target_table"], "sales_sample")
        self.assertIn("load_sql", payload["artifacts"])
        self.assertFalse(payload["artifacts"]["load_sql"]["relative_path"].startswith("/"))
        self.assertEqual(
            payload["artifacts"]["load_sql"]["download_url"],
            f"/api/qvd/download-artifact/{session_id}/databricks_load/load_parquet_to_delta.sql",
        )

    def test_frontend_output_uses_qvd_generation_apis_not_qvf_model(self):
        with open(os.path.join(os.getcwd(), "frontend", "src", "pages", "output.js"), encoding="utf-8") as handle:
            output_js = handle.read()
        with open(os.path.join(os.getcwd(), "frontend", "src", "api.js"), encoding="utf-8") as handle:
            api_js = handle.read()

        self.assertIn("data-qvd-load-scripts-file", output_js)
        self.assertIn("data-qvd-package-file", output_js)
        self.assertIn("/qvd/generate-databricks-load/", api_js)
        self.assertIn("/qvd/generate-migration-package/", api_js)
        self.assertNotIn("/api/model", output_js)

    def test_critical_frontend_qvd_endpoints_are_not_404(self):
        app = Flask(__name__)
        with tempfile.TemporaryDirectory() as tmp:
            register_qvd_routes(app, tmp)
            client = app.test_client()
            responses = {
                endpoint: client.open(endpoint, method="OPTIONS").status_code
                for endpoint in CRITICAL_FRONTEND_QVD_ENDPOINTS
            }

        self.assertEqual(
            {endpoint: status for endpoint, status in responses.items() if status == 404},
            {},
        )

    def test_artifact_download_route_serves_session_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "artifact-session"
            artifact_dir = os.path.join(tmp, session_id, "qvd_outputs", "business_analysis")
            os.makedirs(artifact_dir, exist_ok=True)
            artifact_path = os.path.join(artifact_dir, "kpi_catalog.csv")
            with open(artifact_path, "w", encoding="utf-8") as handle:
                handle.write("kpi_name\nInventory Units\n")
            app = Flask(__name__)
            register_qvd_routes(app, tmp)
            response = app.test_client().get(
                f"/api/qvd/download-artifact/{session_id}/business_analysis/kpi_catalog.csv"
            )
            body = response.data
            response.close()

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Inventory Units", body)

    def test_artifact_download_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Flask(__name__)
            register_qvd_routes(app, tmp)
            response = app.test_client().get("/api/qvd/download-artifact/session/../secret.txt")

        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
