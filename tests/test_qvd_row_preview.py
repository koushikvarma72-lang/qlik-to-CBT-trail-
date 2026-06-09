import json
import os
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask

from backend.integrations.qvd_routes import register_qvd_routes
from qvd_to_databricks.qvd_row_reader import preview_qvd_rows


class QvdRowPreviewTests(unittest.TestCase):
    def test_row_reader_unavailable_returns_graceful_error(self):
        with patch("qvd_to_databricks.qvd_row_reader.importlib.import_module", side_effect=ImportError):
            result = preview_qvd_rows("/tmp/missing.qvd", limit=100)

        self.assertFalse(result["success"])
        self.assertEqual(result["columns"], [])
        self.assertEqual(result["rows"], [])
        self.assertIn("No compatible Python QVD row reader", result["error"])

    def test_preview_route_missing_qvd_file_returns_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Flask(__name__)
            register_qvd_routes(app, tmp)
            client = app.test_client()

            response = client.post(
                "/api/qvd/preview-rows/missing-session",
                json={"file_name": "missing.qvd", "limit": 100},
            )
            payload = response.get_json()

        self.assertEqual(response.status_code, 404)
        self.assertEqual(payload["file_name"], "missing.qvd")
        self.assertIn("not found", payload["error"])

    def test_preview_route_writes_json_artifact_when_successful(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "preview-session"
            input_dir = os.path.join(tmp, session_id, "qvd_inputs")
            os.makedirs(input_dir, exist_ok=True)
            qvd_path = os.path.join(input_dir, "sales.qvd")
            with open(qvd_path, "wb") as handle:
                handle.write(b"fake qvd bytes")

            mocked_preview = {
                "success": True,
                "columns": ["Customer", "Sales"],
                "rows": [{"Customer": "A", "Sales": 10}],
                "row_count_returned": 1,
                "limit": 100,
                "reader_used": "mock",
                "error": None,
            }

            app = Flask(__name__)
            register_qvd_routes(app, tmp)
            client = app.test_client()
            with patch("backend.integrations.qvd_routes.qvd_row_reader.preview_qvd_rows", return_value=mocked_preview):
                response = client.post(
                    f"/api/qvd/preview-rows/{session_id}",
                    json={"file_name": "sales.qvd", "limit": 100},
                )

            payload = response.get_json()
            artifact_path = os.path.join(tmp, session_id, "qvd_outputs", "row_preview_sales_qvd.json")
            with open(artifact_path, encoding="utf-8") as handle:
                artifact = json.load(handle)
            artifact_exists = os.path.exists(artifact_path)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["reader_used"], "mock")
        self.assertTrue(artifact_exists)
        self.assertEqual(artifact["rows"][0]["Customer"], "A")


if __name__ == "__main__":
    unittest.main()
