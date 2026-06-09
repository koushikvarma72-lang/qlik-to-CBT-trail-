"""Databricks connection configuration and validation helpers."""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime


FRIENDLY_DATABRICKS_TIMEOUT = "Databricks statement is still running or warehouse startup timed out. Retry after warehouse is running."


@dataclass
class DatabricksConnectionConfig:
    workspace_url: str = ""
    personal_access_token: str = ""
    sql_warehouse_id: str = ""
    catalog: str = "main"
    schema: str = "qvd_raw"
    volume: str = ""
    volume_path: str = ""
    cloud_storage_path: str = ""

    @classmethod
    def from_payload(cls, payload: dict) -> "DatabricksConnectionConfig":
        return cls(
            workspace_url=_normalize_workspace_url(payload.get("workspace_url") or payload.get("workspaceUrl") or ""),
            personal_access_token=str(payload.get("personal_access_token") or payload.get("personalAccessToken") or payload.get("token") or "").strip(),
            sql_warehouse_id=str(payload.get("sql_warehouse_id") or payload.get("sqlWarehouseId") or "").strip(),
            catalog=str(payload.get("catalog") or "main").strip() or "main",
            schema=str(payload.get("schema") or "qvd_raw").strip() or "qvd_raw",
            volume=str(payload.get("volume") or "").strip(),
            volume_path=str(payload.get("volume_path") or payload.get("volumePath") or "").strip(),
            cloud_storage_path=str(payload.get("cloud_storage_path") or payload.get("cloudStoragePath") or "").strip(),
        )

    def masked(self) -> dict:
        data = asdict(self)
        data["personal_access_token"] = ""
        data["personal_access_token_present"] = bool(self.personal_access_token)
        data["personal_access_token_encrypted_or_masked"] = "********" if self.personal_access_token else ""
        return data

    def public_persisted(self) -> dict:
        computed_volume_path = self.volume_path
        if not computed_volume_path and self.catalog and self.schema and self.volume:
            computed_volume_path = f"/Volumes/{self.catalog}/{self.schema}/{self.volume}"
        return {
            "workspace_url": self.workspace_url,
            "personal_access_token_present": bool(self.personal_access_token),
            "personal_access_token_encrypted_or_masked": "********" if self.personal_access_token else "",
            "sql_warehouse_id": self.sql_warehouse_id,
            "catalog": self.catalog,
            "schema": self.schema,
            "volume": self.volume,
            "volume_path": computed_volume_path,
            "cloud_storage_path": self.cloud_storage_path,
            "last_saved_at": datetime.utcnow().isoformat(),
        }


def _normalize_workspace_url(value: str) -> str:
    text = str(value or "").strip().rstrip("/")
    if text and not text.startswith(("http://", "https://")):
        text = f"https://{text}"
    return text


def validate_config(config: DatabricksConnectionConfig, require_token: bool = True) -> list[str]:
    errors = []
    if not config.workspace_url:
        errors.append("Databricks Workspace URL is required.")
    elif not _valid_workspace_url(config.workspace_url):
        errors.append("Enter a valid Databricks Workspace URL, for example https://dbc-xxxx.cloud.databricks.com.")
    if require_token and not config.personal_access_token:
        errors.append("Personal Access Token is required.")
    if not config.sql_warehouse_id:
        errors.append("SQL Warehouse ID is required before Databricks precheck or execution.")
    if not config.catalog:
        errors.append("Catalog is required.")
    if not config.schema:
        errors.append("Schema is required.")
    return errors


def _valid_workspace_url(value: str) -> bool:
    parsed = urllib.parse.urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc) and "." in parsed.netloc


class DatabricksApiClient:
    def __init__(self, config: DatabricksConnectionConfig):
        self.config = config

    def get(self, path: str, timeout: int = 120) -> dict:
        return self._request("GET", path, timeout=timeout)

    def post(self, path: str, payload: dict, timeout: int = 120) -> dict:
        return self._request("POST", path, payload, timeout=timeout)

    def put_binary(self, path: str, data: bytes, content_type: str = "application/octet-stream") -> dict:
        return self._request("PUT", path, raw_body=data, content_type=content_type)

    def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        raw_body: bytes | None = None,
        content_type: str = "application/json",
        timeout: int = 120,
    ) -> dict:
        url = f"{self.config.workspace_url}{path}"
        body = raw_body if raw_body is not None else (json.dumps(payload or {}).encode("utf-8") if payload is not None else None)
        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.config.personal_access_token}",
                "Content-Type": content_type,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                content_type_header = response.headers.get("Content-Type", "")
                text = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            # Extract Databricks error_code and message from JSON body if present
            db_error_code = ""
            db_message = ""
            try:
                body = json.loads(detail)
                db_error_code = str(body.get("error_code") or body.get("errorCode") or "").strip()
                db_message = str(body.get("message") or "").strip()
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass
            err = _structured_api_error(_friendly_databricks_error(method, path, exc.code, detail), method, path, "http_error", exc.code)
            if db_error_code:
                err["error_code"] = db_error_code
            if db_message:
                err["message"] = db_message
            return err
        except socket.timeout:
            return _structured_api_error(FRIENDLY_DATABRICKS_TIMEOUT, method, path, "timeout")
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, socket.timeout):
                return _structured_api_error(FRIENDLY_DATABRICKS_TIMEOUT, method, path, "timeout")
            return _structured_api_error(f"Databricks API {method} {path} failed: {exc.reason}", method, path, "url_error")
        except Exception as exc:
            return _structured_api_error(f"Databricks API {method} {path} failed: {exc}", method, path, "exception")
        if not text:
            return {}
        if "json" not in content_type_header.lower():
            return {"raw_response": text}
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            return _structured_api_error(f"Databricks API {method} {path} returned invalid JSON: {exc}", method, path, "json_decode_error")


def _structured_api_error(message: str, method: str, path: str, error_type: str, status_code: int | None = None) -> dict:
    payload = {
        "success": False,
        "status": "failed",
        "error": message,
        "errors": [message],
        "error_type": error_type,
        "method": method,
        "path": path,
        "error_code": error_type.upper(),
        "message": message,
    }
    if status_code is not None:
        payload["status_code"] = status_code
    return payload


def _friendly_databricks_error(method: str, path: str, status_code: int, detail: str) -> str:
    safe_detail = str(detail or "").replace("\n", " ")[:1000]
    lowered = safe_detail.lower()

    # Try to extract error_code from Databricks JSON error body
    error_code = ""
    try:
        body = json.loads(detail or "{}")
        error_code = str(body.get("error_code") or body.get("errorCode") or "").strip()
        db_message = str(body.get("message") or "").strip()
        if db_message:
            safe_detail = db_message
            lowered = safe_detail.lower()
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass

    if status_code in {401, 403}:
        reason = "Permission denied or token is invalid."
    elif "warehouse" in lowered:
        reason = "SQL Warehouse is missing, invalid, stopped, or not accessible."
    elif "catalog" in lowered:
        reason = "Catalog is missing or not accessible."
    elif "schema" in lowered:
        reason = "Schema is missing or not accessible."
    elif "path" in lowered or "volume" in lowered:
        reason = "Databricks path or Unity Catalog volume is missing or not accessible."
    elif "already exists" in lowered:
        reason = "Target object already exists."
    else:
        reason = "Databricks API request failed."

    code_prefix = f"[{error_code}] " if error_code else ""
    return f"{reason} {code_prefix}API {method} {path} returned HTTP {status_code}. {safe_detail}"


def test_databricks_connection(config: DatabricksConnectionConfig, client=None) -> dict:
    errors = validate_config(config)
    checks = {
        "connection": False,
        "warehouse": False,
        "catalog": False,
        "schema": False,
    }
    if errors:
        return {"success": False, "checks": checks, "errors": errors, "config": config.masked()}

    api = client or DatabricksApiClient(config)
    try:
        response = api.get("/api/2.0/sql/warehouses")
        if response.get("success") is False:
            errors.extend(response.get("errors") or [response.get("error", "Databricks connection failed.")])
            return {"success": False, "checks": checks, "errors": errors, "config": config.masked()}
        checks["connection"] = True
        warehouses = api.get(f"/api/2.0/sql/warehouses/{urllib.parse.quote(config.sql_warehouse_id, safe='')}")
        if warehouses.get("success") is False:
            errors.extend(warehouses.get("errors") or [warehouses.get("error", "SQL Warehouse validation failed.")])
            return {"success": False, "checks": checks, "errors": errors, "config": config.masked()}
        checks["warehouse"] = bool(warehouses is not None)
        catalog_response = api.get(f"/api/2.1/unity-catalog/catalogs/{urllib.parse.quote(config.catalog, safe='')}")
        if catalog_response.get("success") is False:
            errors.extend(catalog_response.get("errors") or [catalog_response.get("error", "Catalog validation failed.")])
            return {"success": False, "checks": checks, "errors": errors, "config": config.masked()}
        checks["catalog"] = True
        schema_full_name = f"{config.catalog}.{config.schema}"
        schema_response = api.get(f"/api/2.1/unity-catalog/schemas/{urllib.parse.quote(schema_full_name, safe='')}")
        if schema_response.get("success") is False:
            errors.extend(schema_response.get("errors") or [schema_response.get("error", "Schema validation failed.")])
            return {"success": False, "checks": checks, "errors": errors, "config": config.masked()}
        checks["schema"] = True
    except Exception as exc:
        errors.append(str(exc))

    return {
        "success": all(checks.values()) and not errors,
        "checks": checks,
        "errors": errors,
        "config": config.masked(),
    }


test_databricks_connection.__test__ = False


def save_connection_config(output_dir: str, config: DatabricksConnectionConfig) -> str:
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "databricks_config.json")
    secret_path = os.path.join(output_dir, "databricks_config.secret.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(config.public_persisted(), handle, indent=2, ensure_ascii=False)
    if config.personal_access_token:
        with open(secret_path, "w", encoding="utf-8") as handle:
            json.dump({"personal_access_token": config.personal_access_token}, handle, indent=2, ensure_ascii=False)
    elif os.path.exists(secret_path):
        os.unlink(secret_path)
    return path


def load_connection_config(output_dir: str) -> DatabricksConnectionConfig | None:
    path = os.path.join(output_dir, "databricks_config.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    secret_path = os.path.join(output_dir, "databricks_config.secret.json")
    if os.path.exists(secret_path):
        with open(secret_path, encoding="utf-8") as handle:
            payload.update(json.load(handle))
    return DatabricksConnectionConfig.from_payload(payload)


def merge_connection_config(output_dir: str, payload: dict | None) -> DatabricksConnectionConfig | None:
    saved = load_connection_config(output_dir)
    incoming = payload or {}
    if not incoming:
        return saved
    merged = asdict(saved) if saved else {}
    for key, value in incoming.items():
        if value in (None, ""):
            continue
        if key in {"personal_access_token", "personalAccessToken", "token"} and str(value).strip("*") == "":
            continue
        merged[key] = value
    return DatabricksConnectionConfig.from_payload(merged)


def list_warehouses(config: DatabricksConnectionConfig, client=None) -> list[dict]:
    payload = (client or DatabricksApiClient(config)).get("/api/2.0/sql/warehouses")
    if payload.get("success") is False:
        return []
    rows = payload.get("warehouses") or []
    return [
        {
            "id": row.get("id") or row.get("warehouse_id"),
            "name": row.get("name") or row.get("id") or row.get("warehouse_id"),
            "state": row.get("state", ""),
        }
        for row in rows
    ]


def list_catalogs(config: DatabricksConnectionConfig, client=None) -> list[dict]:
    payload = (client or DatabricksApiClient(config)).get("/api/2.1/unity-catalog/catalogs")
    if payload.get("success") is False:
        return []
    rows = payload.get("catalogs") or []
    return [{"name": row.get("name", ""), "type": row.get("catalog_type") or row.get("type", ""), "owner": row.get("owner", "")} for row in rows]


def list_schemas(config: DatabricksConnectionConfig, catalog: str, client=None) -> list[dict]:
    path = f"/api/2.1/unity-catalog/schemas?catalog_name={urllib.parse.quote(str(catalog or ''), safe='')}"
    payload = (client or DatabricksApiClient(config)).get(path)
    if payload.get("success") is False:
        return []
    rows = payload.get("schemas") or []
    return [{"name": row.get("name", ""), "catalog_name": row.get("catalog_name") or catalog, "owner": row.get("owner", "")} for row in rows]


def list_volumes(config: DatabricksConnectionConfig, catalog: str, schema: str, client=None) -> list[dict]:
    path = (
        "/api/2.1/unity-catalog/volumes"
        f"?catalog_name={urllib.parse.quote(str(catalog or ''), safe='')}"
        f"&schema_name={urllib.parse.quote(str(schema or ''), safe='')}"
    )
    payload = (client or DatabricksApiClient(config)).get(path)
    if payload.get("success") is False:
        return []
    rows = payload.get("volumes") or []
    return [
        {
            "name": row.get("name", ""),
            "catalog_name": row.get("catalog_name") or catalog,
            "schema_name": row.get("schema_name") or schema,
            "volume_type": row.get("volume_type") or row.get("type", ""),
            "owner": row.get("owner", ""),
            "volume_path": f"/Volumes/{row.get('catalog_name') or catalog}/{row.get('schema_name') or schema}/{row.get('name', '')}",
        }
        for row in rows
    ]
