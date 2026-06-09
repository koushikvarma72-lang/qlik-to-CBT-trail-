import os
import tempfile
import unittest

from flask import Flask

from backend.integrations.qvd_routes import register_qvd_routes


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
