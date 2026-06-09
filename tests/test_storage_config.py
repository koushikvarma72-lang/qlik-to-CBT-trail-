import os
import tempfile
import unittest

from backend import storage_config


class StorageConfigTests(unittest.TestCase):
    def test_safe_join_blocks_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                storage_config.safe_join(tmp, "..", "secret.txt")

    def test_ensure_directories_creates_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = {
                "DATA_ROOT": storage_config.DATA_ROOT,
                "UPLOAD_FOLDER": storage_config.UPLOAD_FOLDER,
                "ARTIFACT_FOLDER": storage_config.ARTIFACT_FOLDER,
                "QVD_OUTPUT_FOLDER": storage_config.QVD_OUTPUT_FOLDER,
                "MIGRATION_PACKAGE_FOLDER": storage_config.MIGRATION_PACKAGE_FOLDER,
                "LOG_FOLDER": storage_config.LOG_FOLDER,
            }
            try:
                storage_config.DATA_ROOT = os.path.join(tmp, "data")
                storage_config.UPLOAD_FOLDER = os.path.join(tmp, "data", "uploads")
                storage_config.ARTIFACT_FOLDER = os.path.join(tmp, "data", "generated_artifacts")
                storage_config.QVD_OUTPUT_FOLDER = os.path.join(tmp, "data", "qvd_outputs")
                storage_config.MIGRATION_PACKAGE_FOLDER = os.path.join(tmp, "data", "migration_packages")
                storage_config.LOG_FOLDER = os.path.join(tmp, "data", "logs")

                storage_config.ensure_directories()

                for path in original:
                    self.assertTrue(os.path.isdir(getattr(storage_config, path)))
            finally:
                for key, value in original.items():
                    setattr(storage_config, key, value)


if __name__ == "__main__":
    unittest.main()
