"""Centralized runtime storage paths for cloud deployments."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv


BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_DIR.parent
LOCAL_DATA_ROOT = BACKEND_DIR / "backend_runtime_data"

load_dotenv(PROJECT_ROOT / ".env", override=False)
load_dotenv(BACKEND_DIR / ".env", override=False)


def _is_writable_directory(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _resolve_data_root() -> str:
    configured = os.environ.get("DATA_ROOT")
    if configured:
        return str(Path(configured).expanduser())

    default_root = Path("/data")
    if _is_writable_directory(default_root):
        return str(default_root)
    return str(LOCAL_DATA_ROOT)


DATA_ROOT = _resolve_data_root()
UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", str(Path(DATA_ROOT) / "uploads"))
ARTIFACT_FOLDER = os.environ.get("ARTIFACT_FOLDER", str(Path(DATA_ROOT) / "generated_artifacts"))
QVD_OUTPUT_FOLDER = os.environ.get("QVD_OUTPUT_FOLDER", str(Path(DATA_ROOT) / "qvd_outputs"))
MIGRATION_PACKAGE_FOLDER = os.environ.get(
    "MIGRATION_PACKAGE_FOLDER",
    str(Path(DATA_ROOT) / "migration_packages"),
)
LOG_FOLDER = os.environ.get("LOG_FOLDER", str(Path(DATA_ROOT) / "logs"))


def ensure_directories() -> None:
    for folder in (
        DATA_ROOT,
        UPLOAD_FOLDER,
        ARTIFACT_FOLDER,
        QVD_OUTPUT_FOLDER,
        MIGRATION_PACKAGE_FOLDER,
        LOG_FOLDER,
    ):
        Path(folder).mkdir(parents=True, exist_ok=True)


def safe_join(base: str | os.PathLike, *paths: str | os.PathLike) -> str:
    base_path = Path(base).resolve()
    candidate = base_path.joinpath(*(str(path) for path in paths)).resolve()
    if candidate != base_path and base_path not in candidate.parents:
        raise ValueError("Path traversal is not allowed")
    return str(candidate)


def relative_artifact_path(path: str | os.PathLike) -> str:
    return Path(path).resolve().relative_to(Path(DATA_ROOT).resolve()).as_posix()


def relative_artifact_url(path: str | os.PathLike) -> str:
    relative_path = relative_artifact_path(path)
    return "/api/files/" + "/".join(quote(part) for part in relative_path.split("/"))


def file_download_metadata(path: str | os.PathLike) -> dict:
    path_obj = Path(path)
    relative_path = relative_artifact_path(path_obj)
    return {
        "file_name": path_obj.name,
        "relative_path": relative_path,
        "download_url": relative_artifact_url(path_obj),
    }
