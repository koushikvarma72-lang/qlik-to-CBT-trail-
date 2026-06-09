import os
import tempfile
import unittest

import backend.app as app_module


class HealthAndFilesApiTests(unittest.TestCase):
    def test_health_returns_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = (app_module.DATA_ROOT, app_module.UPLOAD_FOLDER, app_module.ARTIFACT_FOLDER)
            try:
                app_module.DATA_ROOT = tmp
                app_module.UPLOAD_FOLDER = os.path.join(tmp, "uploads")
                app_module.ARTIFACT_FOLDER = os.path.join(tmp, "generated_artifacts")
                os.makedirs(app_module.UPLOAD_FOLDER)
                os.makedirs(app_module.ARTIFACT_FOLDER)

                response = app_module.app.test_client().get("/api/health")
                payload = response.get_json()
            finally:
                app_module.DATA_ROOT, app_module.UPLOAD_FOLDER, app_module.ARTIFACT_FOLDER = original

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["upload_folder_exists"])
        self.assertTrue(payload["artifact_folder_exists"])

    def test_files_rejects_path_traversal(self):
        response = app_module.app.test_client().get("/api/files/%2e%2e/secret.txt")
        self.assertEqual(response.status_code, 400)

    def test_files_download_does_not_parse_direct_passthrough_as_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = app_module.DATA_ROOT
            try:
                app_module.DATA_ROOT = tmp
                file_path = os.path.join(tmp, "generated_artifact.sql")
                with open(file_path, "w", encoding="utf-8") as handle:
                    handle.write("select 1;\n")

                response = app_module.app.test_client().get("/api/files/generated_artifact.sql")
                body = response.data
                response.close()
            finally:
                app_module.DATA_ROOT = original

        self.assertEqual(response.status_code, 200)
        self.assertEqual(body, b"select 1;\n")
        self.assertIn("attachment", response.headers.get("Content-Disposition", ""))


if __name__ == "__main__":
    unittest.main()
