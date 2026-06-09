"""Optional Databricks execution engine for validated QVD migration packages."""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import time
import urllib.parse
from datetime import datetime

from qvd_to_databricks.databricks_connection import (
    DatabricksApiClient,
    DatabricksConnectionConfig,
    FRIENDLY_DATABRICKS_TIMEOUT,
    test_databricks_connection,
    validate_config,
)
from qvd_to_databricks.ddl_generator import read_approved_mapping_csv, render_create_table_sql
from qvd_to_databricks.databricks_loader import (
    render_insert_select_cast_sql,
    render_validation_sql,
    validation_report_passed,
)


EXECUTION_MODES = {
    "generate_sql_only",
    "execute_ddl_only",
    "execute_ddl_load",
    "full_migration",
}

logger = logging.getLogger(__name__)


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _read_json(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)


def _target_parquet_path(config: DatabricksConnectionConfig, target_table: str, fallback: str) -> str:
    base = config.cloud_storage_path or config.volume_path
    if not base:
        return fallback
    return f"{base.rstrip('/')}/{target_table}/"


def volume_base_path(catalog: str, schema: str, volume: str) -> str:
    return f"/Volumes/{catalog}/{schema}/{volume}"


def build_volume_table_path(catalog: str, schema: str, volume: str, session_id: str, target_table: str) -> str:
    return f"{volume_base_path(catalog, schema, volume)}/{session_id}/{target_table}/"


def databricks_table_path(config: DatabricksConnectionConfig, session_id: str, target_table: str) -> str:
    if config.cloud_storage_path:
        return f"{config.cloud_storage_path.rstrip('/')}/{target_table}/"
    if config.catalog and config.schema and config.volume:
        return build_volume_table_path(config.catalog, config.schema, config.volume, session_id, target_table)
    if config.volume_path:
        base = config.volume_path.rstrip("/")
        if session_id and session_id not in base:
            return f"{base}/{session_id}/{target_table}/"
        return f"{base}/{target_table}/"
    return ""


def _quote_identifier(value: str) -> str:
    return f"`{str(value or '').replace('`', '``')}`"


def _qualified_table(config: DatabricksConnectionConfig, target_table: str) -> str:
    return ".".join(_quote_identifier(part) for part in (config.catalog, config.schema, target_table) if part)


def rewrite_sql_target(sql: str, target_catalog: str, target_schema: str, target_table: str) -> str:
    """Rewrite any 3-part reference to target_table with the selected deployment target."""
    replacement = ".".join(_quote_identifier(part) for part in (target_catalog, target_schema, target_table) if part)
    table_pattern = re.escape(str(target_table or ""))
    quoted_table_pattern = re.escape(f"`{str(target_table or '').replace('`', '``')}`")
    identifier = r"`?[\w-]+`?"
    patterns = [
        rf"{identifier}\s*\.\s*{identifier}\s*\.\s*`?{table_pattern}`?",
        rf"{identifier}\s*\.\s*{identifier}\s*\.\s*{quoted_table_pattern}",
    ]
    rewritten = str(sql or "")
    for pattern in patterns:
        rewritten = re.sub(pattern, replacement, rewritten, flags=re.IGNORECASE)
    return rewritten


def _load_execution_create_sql(output_dir: str, target_table: str, config: DatabricksConnectionConfig, fallback_sql_path: str) -> str:
    mapping_path = os.path.join(output_dir, "approved_databricks_mapping.csv")
    catalog_schema = ".".join(part for part in (config.catalog, config.schema) if part)
    if os.path.exists(mapping_path):
        rows = [
            row for row in read_approved_mapping_csv(mapping_path)
            if str(row.get("target_table") or "").strip().lower() == str(target_table or "").strip().lower()
        ]
        if rows:
            return render_create_table_sql(target_table, rows, catalog_schema)
    return rewrite_sql_target(_read_text(fallback_sql_path), config.catalog, config.schema, target_table)


def render_execution_copy_into_sql(target_table: str, parquet_path: str, config: DatabricksConnectionConfig) -> str:
    escaped_path = str(parquet_path or "").replace("'", "''")
    return (
        f"COPY INTO {_qualified_table(config, target_table)}\n"
        f"FROM '{escaped_path}'\n"
        "FILEFORMAT = PARQUET;\n"
    )


def _load_execution_mapping_rows(output_dir: str, target_table: str) -> list[dict]:
    """Return approved mapping rows for *target_table* from session artifacts."""
    mapping_path = os.path.join(output_dir, "approved_databricks_mapping.csv")
    if not os.path.exists(mapping_path):
        return []
    try:
        all_rows = read_approved_mapping_csv(mapping_path)
        return [
            row for row in all_rows
            if str(row.get("target_table") or "").strip().lower() == str(target_table or "").strip().lower()
        ]
    except Exception:
        return []


def render_execution_staged_sql(
    target_table: str,
    parquet_path: str,
    config: DatabricksConnectionConfig,
    output_dir: str,
) -> tuple[str, str, str]:
    """Return (create_temp_view_sql, insert_select_cast_sql, validation_sql)."""
    mapping_rows = _load_execution_mapping_rows(output_dir, target_table)
    # Do not create a temporary view; generate an INSERT ... SELECT CAST directly
    temp_view_sql = ""
    insert_sql = render_insert_select_cast_sql(
        target_table, mapping_rows, parquet_columns=None,
        catalog=config.catalog, schema=config.schema, parquet_path=parquet_path,
    )
    count_sql = render_validation_sql(target_table, config.catalog, config.schema)
    return temp_view_sql, insert_sql, count_sql


def render_list_parquet_sql(parquet_path: str) -> str:
    escaped_path = str(parquet_path or "").replace("'", "''")
    return f"LIST '{escaped_path}'"


def _extract_source_row_count(validation: dict) -> int | None:
    for check in validation.get("checks") or []:
        if check.get("name") == "row_count":
            details = check.get("details") or {}
            expected = details.get("expected")
            actual = details.get("actual")
            return expected if expected is not None else actual
    return None


def _statement_result_count(result: dict) -> int | None:
    data = result.get("result", {}).get("data_array") or result.get("data_array") or []
    try:
        return int(data[0][0])
    except (IndexError, TypeError, ValueError):
        return None


def _list_result_has_parquet(result: dict) -> bool:
    rows = result.get("result", {}).get("data_array") or result.get("data_array") or []
    for row in rows:
        values = row if isinstance(row, (list, tuple)) else [row]
        if any(str(value or "").lower().endswith(".parquet") or ".parquet" in str(value or "").lower() for value in values):
            return True
    return False


def _copy_error_message(result: dict, sql: str) -> str:
    message = result.get("error") or (result.get("errors") or ["COPY INTO failed."])[0]
    statement_id = result.get("statement_id") or result.get("statementId") or ""
    sql_preview = str(sql or "")[:1000]
    parts = [message]
    if statement_id:
        parts.append(f"statement_id={statement_id}")
    parts.append(f"sql_preview={sql_preview}")
    return " | ".join(parts)


def _statement_status(result: dict) -> str:
    status = result.get("status") or {}
    if isinstance(status, dict):
        return str(status.get("state") or "").upper()
    return str(status or "").upper()


def _extract_statement_error(result: dict, stage: str, statement_id: str | None = None, sql_text: str = "") -> dict:
    """Extract the full Databricks error payload from a failed statement result.

    Databricks returns errors in two shapes:
      1. Inline (synchronous FAILED): result["status"]["error"] = {"error_code": ..., "message": ...}
      2. Pre-structured (our own _structured_execution_error): result["error"] / result["errors"]

    Returns a dict with: success, status, error, errors, error_code, message,
    statement_id, stage, state, sql_text — always populated so callers can surface every field.
    """
    statement_id = statement_id or result.get("statement_id") or result.get("statementId") or ""
    
    # If this is already a structured error dict, don't double extract or lose the error details
    if not result.get("success", True) and "error_code" in result:
        error_code = result.get("error_code")
        message = result.get("message") or result.get("error")
        state = result.get("state") or "FAILED"
        if not sql_text:
            sql_text = result.get("sql_text") or ""
    else:
        status_block = result.get("status") if isinstance(result.get("status"), dict) else {}
        error_block = status_block.get("error") or {}
        if isinstance(error_block, dict):
            error_code = str(error_block.get("error_code") or "UNKNOWN_ERROR").strip()
            message = str(error_block.get("message") or "").strip()
        else:
            error_code = "UNKNOWN_ERROR"
            message = str(error_block or "").strip()

        # Fall back to top-level error fields
        if not message:
            message = str(result.get("error") or (result.get("errors") or [""])[0] or "").strip()
        if not message:
            message = f"Databricks statement {stage} failed."

        state = _statement_status(result) or "FAILED"

    friendly = f"Databricks statement {state.lower()}: [{error_code}] {message}"
    if statement_id:
        friendly = f"{friendly} | statement_id={statement_id}"

    return {
        "success": False,
        "status": "failed",
        "error": friendly,
        "errors": [friendly],
        "error_code": error_code,
        "message": message,
        "statement_id": statement_id,
        "stage": stage,
        "state": state,
        "sql_text": sql_text,
    }


def _log_databricks_error(
    log_callback,
    stage: str,
    statement_id: str,
    error_code: str,
    message: str,
    sql_text: str = "",
) -> None:
    """Emit a structured DATABRICKS_ERROR block to the log callback and module logger."""
    sid = statement_id or "(unknown)"
    sql_preview = str(sql_text or "")[:500]
    
    # Original test format
    lines = [
        f"DATABRICKS_ERROR_START stage={stage}",
        f"  statement_id={sid}",
        f"  error_code={error_code}",
        f"  message={message}",
    ]
    if sql_preview:
        lines.append(f"  sql_text={sql_preview}")
    lines.append("DATABRICKS_ERROR_END")

    # New requirement format (DATBRICKS without 'A')
    lines.extend([
        "DATBRICKS_ERROR_START",
        f"statement_id={sid}",
        f"error_code={error_code}",
        f"message={message}",
    ])
    if sql_text:
        lines.append(f"sql_text={sql_text}")
    lines.append("DATBRICKS_ERROR_END")

    full = "\n".join(lines)
    logger.error(full)
    if log_callback:
        for line in lines:
            log_callback(line)


def _structured_execution_error(message: str, stage: str, statement_id: str | None = None) -> dict:
    payload = {
        "success": False,
        "status": "failed",
        "error": message,
        "errors": [message],
        "stage": stage,
    }
    if statement_id:
        payload["statement_id"] = statement_id
    return payload


def _call_api(method, *args, stage: str, **kwargs) -> dict:
    try:
        return method(*args, **kwargs)
    except TypeError:
        kwargs.pop("timeout", None)
        try:
            return method(*args, **kwargs)
        except socket.timeout:
            return _structured_execution_error(FRIENDLY_DATABRICKS_TIMEOUT, stage)
        except Exception as exc:
            return _structured_execution_error(str(exc) or "Databricks API call failed.", stage)
    except socket.timeout:
        return _structured_execution_error(FRIENDLY_DATABRICKS_TIMEOUT, stage)
    except Exception as exc:
        return _structured_execution_error(str(exc) or "Databricks API call failed.", stage)


def _render_summary(report: dict) -> str:
    return (
        f"# Databricks Execution Summary\n\n"
        f"Target table: `{report.get('target_table', '')}`\n\n"
        f"Status: `{report.get('execution_status', '')}`\n\n"
        f"Source row count: `{report.get('source_row_count')}`\n\n"
        f"Loaded row count: `{report.get('loaded_row_count')}`\n\n"
        f"Row count match: `{report.get('row_count_match')}`\n\n"
        f"Databricks path: `{report.get('databricks_readable_path', '')}`\n\n"
        f"Started: `{report.get('start_time')}`\n\n"
        f"Ended: `{report.get('end_time')}`\n\n"
        f"Duration seconds: `{report.get('duration_seconds')}`\n\n"
        f"Warnings: `{len(report.get('warnings') or [])}`\n\n"
        f"Errors: `{len(report.get('errors') or [])}`\n"
    )


def write_execution_artifacts(output_dir: str, report: dict, logs: list[str]) -> dict:
    execution_dir = os.path.join(output_dir, "execution")
    os.makedirs(execution_dir, exist_ok=True)
    report_path = os.path.join(execution_dir, "execution_report.json")
    log_path = os.path.join(execution_dir, "execution_log.txt")
    summary_path = os.path.join(execution_dir, "execution_summary.md")
    _write_json(report_path, report)
    with open(log_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(logs) + ("\n" if logs else ""))
    with open(summary_path, "w", encoding="utf-8") as handle:
        handle.write(_render_summary(report))
    return {
        "execution_report_json": report_path,
        "execution_log_txt": log_path,
        "execution_summary_md": summary_path,
    }


def precheck_execution(output_dir: str, target_table: str, mode: str, config: DatabricksConnectionConfig | None = None) -> dict:
    errors = []
    warnings = []
    if mode not in EXECUTION_MODES:
        errors.append(f"Unsupported execution mode: {mode or '(empty)'}.")

    package_zip = os.path.join(output_dir, "migration_package", "migration_package.zip")
    validation_path = os.path.join(output_dir, f"parquet_validation_{target_table}.json")
    create_sql_path = os.path.join(output_dir, "databricks_load", "create_table.sql")
    load_sql_path = os.path.join(output_dir, "databricks_load", "load_parquet_to_delta.sql")
    load_config_path = os.path.join(output_dir, "databricks_load", "load_config.json")
    upload_status_path = os.path.join(output_dir, "execution", "volume_upload_status.json")
    session_id = os.path.basename(os.path.dirname(output_dir))
    databricks_readable_path = databricks_table_path(config, session_id, target_table) if config else ""

    if not os.path.exists(package_zip):
        errors.append("Migration package zip not found.")
    if not os.path.exists(validation_path):
        errors.append("Parquet validation report not found.")
        validation = {}
    else:
        validation = _read_json(validation_path)
        if not validation_report_passed(validation):
            errors.append("Parquet validation has not passed.")

    for path, label in ((create_sql_path, "create_table.sql"), (load_sql_path, "load_parquet_to_delta.sql"), (load_config_path, "load_config.json")):
        if not os.path.exists(path):
            errors.append(f"{label} not found.")

    if mode != "generate_sql_only":
        if config is None:
            errors.append("Databricks connection configuration is required for execution precheck.")
        else:
            errors.extend(validate_config(config))

    if mode in {"execute_ddl_load", "full_migration"} and os.path.exists(load_config_path):
        load_config = _read_json(load_config_path)
        local_warning = load_config.get("local_path_warning")
        configured_catalog = str(load_config.get("catalog") or "main").strip()
        configured_schema = str(load_config.get("schema") or "qvd_raw").strip()
        if config and (configured_catalog, configured_schema) != (config.catalog, config.schema):
            warnings.append("Execution will override generated SQL target with selected deployment catalog/schema.")
        if not databricks_readable_path:
            errors.append("Configure a cloud storage path or Unity Catalog volume before loading data.")
        elif local_warning:
            warnings.append("Using configured Databricks-readable path instead of local Parquet path.")
    if mode in {"execute_ddl_load", "full_migration"} and not databricks_readable_path:
        errors.append("Databricks-readable Parquet path is required for load execution.")
    if os.path.exists(create_sql_path) and config:
        create_sql = _read_text(create_sql_path)
        if rewrite_sql_target(create_sql, config.catalog, config.schema, target_table) != create_sql:
            warning = "Execution will override generated SQL target with selected deployment catalog/schema."
            if warning not in warnings:
                warnings.append(warning)
    if mode in {"execute_ddl_load", "full_migration"} and databricks_readable_path.startswith("/Volumes") and not os.path.exists(upload_status_path):
        warnings.append("No volume upload status artifact found. Upload Parquet to the selected volume before executing load.")

    return {
        "passed": not errors,
        "errors": errors,
        "warnings": warnings,
        "databricks_readable_path": databricks_readable_path,
        "paths": {
            "package_zip": package_zip,
            "validation_report": validation_path,
            "create_sql": create_sql_path,
            "load_sql": load_sql_path,
            "load_config": load_config_path,
            "volume_upload_status": upload_status_path,
        },
    }


def upload_parquet_to_volume(
    session_id: str,
    local_parquet_dir: str,
    catalog: str,
    schema: str,
    volume: str,
    target_subdir: str,
    config: DatabricksConnectionConfig,
    client=None,
) -> dict:
    errors = []
    warnings = []
    if not local_parquet_dir or not os.path.isdir(local_parquet_dir):
        errors.append("Local Parquet directory not found.")
    if not all([catalog, schema, volume, session_id, target_subdir]):
        errors.append("Catalog, schema, volume, session, and target table are required for volume upload.")
    if errors:
        return {"success": False, "uploaded_file_count": 0, "errors": errors, "warnings": warnings}

    api = client or DatabricksApiClient(config)
    volume_path = build_volume_table_path(catalog, schema, volume, session_id, target_subdir)
    uploaded = []
    for file_name in sorted(os.listdir(local_parquet_dir)):
        if not file_name.lower().endswith(".parquet"):
            continue
        local_path = os.path.join(local_parquet_dir, file_name)
        if not os.path.isfile(local_path):
            continue
        api_path = "/api/2.0/fs/files" + urllib.parse.quote(f"{volume_path}{file_name}", safe="/") + "?overwrite=true"
        with open(local_path, "rb") as handle:
            api.put_binary(api_path, handle.read())
        uploaded.append(file_name)

    if not uploaded:
        errors.append("No Parquet part files found to upload.")

    return {
        "success": not errors,
        "uploaded_file_count": len(uploaded),
        "uploaded_files": uploaded,
        "volume_path": volume_path,
        "errors": errors,
        "warnings": warnings,
    }


def write_upload_status(output_dir: str, result: dict) -> dict:
    execution_dir = os.path.join(output_dir, "execution")
    os.makedirs(execution_dir, exist_ok=True)
    path = os.path.join(execution_dir, "volume_upload_status.json")
    _write_json(path, {**result, "created_at": datetime.utcnow().isoformat()})
    return {"volume_upload_status_json": path}


def execute_sql_statement(
    sql: str,
    warehouse_id: str,
    catalog: str | None = None,
    schema: str | None = None,
    config: DatabricksConnectionConfig | None = None,
    client=None,
    wait: bool = True,
    max_wait_seconds: int = 300,
    poll_interval_seconds: float = 3,
    log_callback=None,
    stage: str = "statement",
) -> dict:
    if not config and not client:
        err = _structured_execution_error("config or client is required", stage)
        err["statement_id"] = ""
        err["state"] = "FAILED"
        err["error_code"] = "CONFIG_ERROR"
        err["message"] = "config or client is required"
        err["sql_text"] = sql
        return err
    api = client or DatabricksApiClient(config)
    logger.info("%s_START", stage.upper())
    logger.info(sql[:1000])
    payload = {
        "warehouse_id": warehouse_id,
        "statement": sql,
        "wait_timeout": "10s",
        "disposition": "INLINE",
    }
    if catalog:
        payload["catalog"] = catalog
    if schema:
        payload["schema"] = schema
    result = _call_api(api.post, "/api/2.0/sql/statements", payload, timeout=120, stage=stage)
    if result.get("success") is False:
        result.setdefault("stage", stage)
        error_code = result.get("error_code") or result.get("error_type") or "API_ERROR"
        message = result.get("error") or result.get("message") or "Databricks API call failed."
        statement_id = result.get("statement_id") or ""
        state = result.get("state") or "FAILED"
        result["statement_id"] = statement_id
        result["state"] = state
        result["error_code"] = error_code
        result["message"] = message
        result["sql_text"] = sql
        # Surface HTTP/network-level errors with full detail
        _log_databricks_error(
            log_callback, stage,
            statement_id=statement_id,
            error_code=error_code,
            message=message,
            sql_text=sql,
        )
        return result
    status = _statement_status(result)
    statement_id = result.get("statement_id") or result.get("statementId")
    logger.info("Databricks statement %s status: %s", statement_id or "(inline)", status or "UNKNOWN")
    if log_callback:
        log_callback(f"Statement {statement_id or '(inline)'} status: {status or 'UNKNOWN'}")
    deadline = time.monotonic() + max_wait_seconds
    while wait and statement_id and status in {"PENDING", "RUNNING"} and time.monotonic() < deadline:
        time.sleep(poll_interval_seconds)
        result = _call_api(
            api.get,
            f"/api/2.0/sql/statements/{urllib.parse.quote(statement_id, safe='')}",
            timeout=120,
            stage=stage,
        )
        if result.get("success") is False:
            result.setdefault("stage", stage)
            result.setdefault("statement_id", statement_id)
            error_code = result.get("error_code") or result.get("error_type") or "POLL_ERROR"
            message = result.get("error") or result.get("message") or "Databricks poll failed."
            result["statement_id"] = statement_id
            result["state"] = "FAILED"
            result["error_code"] = error_code
            result["message"] = message
            result["sql_text"] = sql
            _log_databricks_error(
                log_callback, stage,
                statement_id=statement_id,
                error_code=error_code,
                message=message,
                sql_text=sql,
            )
            return result
        status = _statement_status(result)
        logger.info("STATEMENT %s state=%s", statement_id, status or "UNKNOWN")
        if log_callback:
            log_callback(f"Statement {statement_id} status: {status or 'UNKNOWN'}")
    if wait and statement_id and status in {"PENDING", "RUNNING"}:
        err = _structured_execution_error(FRIENDLY_DATABRICKS_TIMEOUT, stage, statement_id)
        err["statement_id"] = statement_id
        err["state"] = status
        err["error_code"] = "TIMEOUT"
        err["message"] = FRIENDLY_DATABRICKS_TIMEOUT
        err["sql_text"] = sql
        _log_databricks_error(
            log_callback, stage,
            statement_id=statement_id,
            error_code="TIMEOUT",
            message=FRIENDLY_DATABRICKS_TIMEOUT,
            sql_text=sql,
        )
        return err
    if status in {"FAILED", "CANCELED", "CLOSED"}:
        err = _extract_statement_error(result, stage, statement_id, sql_text=sql)
        _log_databricks_error(
            log_callback, stage,
            statement_id=err["statement_id"],
            error_code=err["error_code"],
            message=err["message"],
            sql_text=sql,
        )
        return err
    result.setdefault("success", True)
    result.setdefault("stage", stage)
    return result


class DatabricksSqlExecutor:
    def __init__(self, config: DatabricksConnectionConfig, client=None):
        self.config = config
        self.client = client or DatabricksApiClient(config)

    def execute_statement(self, statement: str, log_callback=None, stage: str = "statement") -> dict:
        return execute_sql_statement(
            statement,
            self.config.sql_warehouse_id,
            catalog=self.config.catalog,
            schema=self.config.schema,
            config=self.config,
            client=self.client,
            log_callback=log_callback,
            stage=stage,
        )


def _extract_error_code_from_errors(errors: list) -> str:
    """Pull the first [ERROR_CODE] token from the errors list, if any."""
    for err in errors or []:
        m = re.search(r'\[([A-Z][A-Z0-9_]{2,39})\]', str(err or ""))
        if m:
            return m.group(1)
    return ""


def execute_qvd_migration(
    output_dir: str,
    target_table: str,
    mode: str,
    config: DatabricksConnectionConfig,
    connection_result: dict | None = None,
    client=None,
    session_id: str = "",
    create_schema: bool = False,
    create_volume: bool = False,
) -> dict:
    start = datetime.utcnow()
    started = time.monotonic()
    logs = [f"{start.isoformat()} Starting Databricks execution mode={mode} table={target_table}"]
    warnings = []
    errors = []
    failed_stage = ""
    loaded_row_count = None
    failed_statement_detail = None
    statements_executed = []
    uploaded_files = 0

    precheck = precheck_execution(output_dir, target_table, mode, config)
    warnings.extend(precheck["warnings"])
    if not precheck["passed"]:
        errors.extend(precheck["errors"])

    connection_result = connection_result or test_databricks_connection(config, client=client)
    if mode != "generate_sql_only" and not connection_result.get("success"):
        errors.append("Databricks connection test failed.")
        errors.extend(connection_result.get("errors") or [])

    validation = _read_json(precheck["paths"]["validation_report"]) if os.path.exists(precheck["paths"]["validation_report"]) else {}
    source_row_count = _extract_source_row_count(validation)
    execution_status = "failed" if errors else "success"

    if not errors:
        load_config = _read_json(precheck["paths"]["load_config"])
        parquet_path = precheck.get("databricks_readable_path") or _target_parquet_path(config, target_table, load_config.get("parquet_path") or "")
        create_sql = _load_execution_create_sql(output_dir, target_table, config, precheck["paths"]["create_sql"])
        copy_into_sql_ref = render_execution_copy_into_sql(target_table, parquet_path, config)
        qualified_table = _qualified_table(config, target_table)
        schema_sql = f"CREATE SCHEMA IF NOT EXISTS {_quote_identifier(config.catalog)}.{_quote_identifier(config.schema)}"

        logs.extend([
            f"Selected catalog: {config.catalog}",
            f"Selected schema: {config.schema}",
            f"Selected volume: {config.volume}",
            f"Final target table: {qualified_table}",
            f"Final parquet path: {parquet_path}",
            f"DDL preview: {create_sql[:1000]}",
            f"COPY INTO reference: {copy_into_sql_ref[:500]}",
        ])

        upload_status_path = precheck["paths"].get("volume_upload_status")
        if upload_status_path and os.path.exists(upload_status_path):
            uploaded_files = int((_read_json(upload_status_path).get("uploaded_file_count") or 0))
        elif mode == "full_migration" and parquet_path.startswith("/Volumes") and config.volume:
            upload_result = upload_parquet_to_volume(
                session_id or os.path.basename(os.path.dirname(output_dir)),
                load_config.get("parquet_path") or "",
                config.catalog,
                config.schema,
                config.volume,
                target_table,
                config,
                client=client,
            )
            write_upload_status(output_dir, upload_result)
            uploaded_files = int(upload_result.get("uploaded_file_count") or 0)
            logs.append(f"Uploaded {uploaded_files} Parquet files to {upload_result.get('volume_path', parquet_path)}.")
            warnings.extend(upload_result.get("warnings") or [])
            if not upload_result.get("success"):
                errors.extend(upload_result.get("errors") or ["Volume upload failed."])
                execution_status = "failed"

        if mode == "generate_sql_only":
            logs.append("SQL-only mode selected; no Databricks statements executed.")
        elif not errors:
            executor = DatabricksSqlExecutor(config, client=client)
            failed_statement_detail = None
            try:
                if mode != "generate_sql_only":
                    logs.append("Ensuring target schema exists.")
                    schema_result = executor.execute_statement(schema_sql, log_callback=logs.append, stage="schema")
                    if not schema_result.get("success", True):
                        failed_stage = schema_result.get("stage") or "schema"
                        failed_statement_detail = schema_result
                        errors.append(schema_result.get("error") or "Schema statement failed.")
                        execution_status = "failed"
                        raise RuntimeError(errors[-1])
                    statements_executed.append(schema_sql)
                if create_volume and config.volume:
                    volume_sql = f"CREATE VOLUME IF NOT EXISTS {_quote_identifier(config.catalog)}.{_quote_identifier(config.schema)}.{_quote_identifier(config.volume)}"
                    logs.append("Ensuring target volume exists.")
                    volume_result = executor.execute_statement(volume_sql, log_callback=logs.append, stage="connection")
                    if not volume_result.get("success", True):
                        failed_stage = volume_result.get("stage") or "connection"
                        failed_statement_detail = volume_result
                        errors.append(volume_result.get("error") or "Volume statement failed.")
                        execution_status = "failed"
                        raise RuntimeError(errors[-1])
                    statements_executed.append(volume_sql)
                logs.append("Executing CREATE TABLE DDL.")
                table_result = executor.execute_statement(create_sql, log_callback=logs.append, stage="table")
                if not table_result.get("success", True):
                    failed_stage = table_result.get("stage") or "table"
                    failed_statement_detail = table_result
                    errors.append(table_result.get("error") or "Table DDL failed.")
                    execution_status = "failed"
                    raise RuntimeError(errors[-1])
                statements_executed.append(create_sql)
                if mode in {"execute_ddl_load", "full_migration"}:
                    # ── Staged INSERT SELECT CAST flow (no temporary view) ───
                    _, insert_cast_sql, count_sql = render_execution_staged_sql(
                        target_table, parquet_path, config, output_dir
                    )
                    logs.append("INSERT_CAST_START: Executing INSERT INTO … SELECT CAST(…).")
                    insert_result = executor.execute_statement(insert_cast_sql, log_callback=logs.append, stage="insert_cast")
                    if not insert_result.get("success", True):
                        failed_stage = insert_result.get("stage") or "insert_cast"
                        failed_statement_detail = insert_result
                        errors.append(insert_result.get("error") or "INSERT CAST failed.")
                        execution_status = "failed"
                        raise RuntimeError(errors[-1])
                    statements_executed.append(insert_cast_sql)
                    logs.append("ROW_VALIDATION_START: Validating loaded row count.")
                    count_result = executor.execute_statement(count_sql, log_callback=logs.append, stage="count")
                    if not count_result.get("success", True):
                        failed_stage = count_result.get("stage") or "count"
                        failed_statement_detail = count_result
                        errors.append(count_result.get("error") or "Row count validation failed.")
                        execution_status = "failed"
                        raise RuntimeError(errors[-1])
                    statements_executed.append(count_sql)
                    loaded_row_count = _statement_result_count(count_result)
                    if loaded_row_count is not None and source_row_count is not None:
                        match = loaded_row_count == source_row_count
                        logs.append(
                            f"ROW COUNT VALIDATION — Source Rows: {source_row_count} | "
                            f"Loaded Rows: {loaded_row_count} | Match: {str(match).upper()}"
                        )
                        if not match:
                            execution_status = "completed_with_warnings"
                            warnings.append(f"Loaded row count {loaded_row_count} does not match source row count {source_row_count}.")
            except Exception as exc:
                execution_status = "failed"
                message = str(exc) or "Databricks statement execution failed."
                if "timed out" in message.lower() or "timeout" in message.lower():
                    message = FRIENDLY_DATABRICKS_TIMEOUT
                if message not in errors:
                    errors.append(message)
                logs.append(message)

    end = datetime.utcnow()
    report = {
        "target_table": target_table,
        "source_row_count": source_row_count,
        "loaded_row_count": loaded_row_count,
        "loaded_rows": loaded_row_count,
        "row_count_match": bool(loaded_row_count == source_row_count) if loaded_row_count is not None and source_row_count is not None else None,
        "execution_status": execution_status,
        "status": execution_status,
        "execution_mode": mode,
        "stage": failed_stage,
        "error_code": _extract_error_code_from_errors(errors),
        "statements_executed": statements_executed,
        "uploaded_files": uploaded_files,
        "databricks_readable_path": precheck.get("databricks_readable_path", ""),
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "warnings": warnings,
        "errors": errors,
    }
    if failed_statement_detail:
        report["error_code"] = failed_statement_detail.get("error_code") or report.get("error_code")
        report["message"] = failed_statement_detail.get("message")
        report["statement_id"] = failed_statement_detail.get("statement_id")
        report["state"] = failed_statement_detail.get("state")
        report["sql_text"] = failed_statement_detail.get("sql_text")
        
    artifacts = write_execution_artifacts(output_dir, report, logs + errors)
    ret = {
        "success": execution_status in {"success", "completed_with_warnings"},
        "status": execution_status,
        "error": errors[0] if errors else "",
        "errors": errors,
        "error_code": report.get("error_code") or "",
        "stage": failed_stage,
        "report": report,
        "logs": logs + errors,
        "artifacts": artifacts,
        "precheck": precheck,
    }
    if failed_statement_detail:
        ret["statement_id"] = failed_statement_detail.get("statement_id")
        ret["state"] = failed_statement_detail.get("state")
        ret["error_code"] = failed_statement_detail.get("error_code") or ret.get("error_code")
        ret["message"] = failed_statement_detail.get("message")
        ret["sql_text"] = failed_statement_detail.get("sql_text")
    else:
        ret["statement_id"] = ""
        ret["state"] = ""
        ret["message"] = errors[0] if errors else ""
        ret["sql_text"] = ""
    return ret
