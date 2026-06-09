"""Routes for the isolated QVD to Databricks inspection flow."""

from __future__ import annotations

import logging
import json
import os
import socket
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from flask import Blueprint, jsonify, request, send_file
from werkzeug.utils import secure_filename

from backend.storage_config import (
    MIGRATION_PACKAGE_FOLDER,
    QVD_OUTPUT_FOLDER,
    UPLOAD_FOLDER,
    safe_join,
)

from qvd_business_analysis.business_documentation import write_business_documentation
from qvd_business_analysis.entity_discovery import discover_business_entities, write_business_entities_artifact
from qvd_business_analysis.glossary_summary import (
    generate_business_glossary,
    generate_executive_summary,
    write_glossary_summary_artifacts,
)
from qvd_business_analysis.kpi_catalog import generate_kpi_catalog, write_kpi_catalog_artifacts
from qvd_business_analysis.lineage_generator import generate_lineage, write_lineage_artifact
from qvd_business_analysis.llm_explainer import generate_ai_business_explanation
from qvd_business_analysis.reconciliation_rules import (
    generate_reconciliation_rules,
    write_reconciliation_artifacts,
)
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
    EXECUTION_MODES,
    execute_qvd_migration,
    execute_sql_statement,
    precheck_execution,
    upload_parquet_to_volume,
    write_execution_artifacts,
    write_upload_status,
)
from qvd_to_databricks.databricks_loader import (
    default_ddl_path,
    generate_databricks_load_artifacts,
    load_validation_report,
    validation_report_passed,
)
from qvd_to_databricks.ddl_generator import generate_delta_ddl_from_approved_mapping
from qvd_to_databricks.ddl_generator import read_approved_mapping_csv, safe_sql_filename
from qvd_to_databricks.migration_package import generate_migration_package
from qvd_to_databricks.parquet_validator import validate_parquet_output, write_validation_artifact
from qvd_to_databricks.qvd_inspector import inspect_qvd_file, write_inspection_artifacts
from qvd_to_databricks.qvd_profiler import load_approved_mapping_rows, profile_qvd_columns, write_profile_artifacts
from qvd_to_databricks import qvd_row_reader
from qvd_to_databricks.qvd_to_parquet_converter import convert_qvd_to_parquet
from qvd_to_databricks.schema_suggester import (
    suggest_schema_from_inspection,
    validate_approved_mapping_rows,
    write_approved_mapping_artifacts,
    write_schema_suggestion_artifacts,
)


logger = logging.getLogger(__name__)


def _safe_preview_artifact_name(file_name: str) -> str:
    safe_name = secure_filename(file_name) or "qvd_file"
    return safe_name.replace(".", "_")


def _read_json_if_exists(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _quote_artifact_path(relative_path: str) -> str:
    return "/".join(quote(part) for part in str(relative_path or "").split("/"))


def register_qvd_routes(app, upload_folder, call_ai=None):
    qvd_bp = Blueprint(f"qvd_{id(app)}", __name__)
    use_configured_storage = os.path.abspath(upload_folder) == os.path.abspath(UPLOAD_FOLDER)

    def qvd_input_dir(session_id: str) -> str:
        if use_configured_storage:
            return safe_join(UPLOAD_FOLDER, session_id, "qvd_inputs")
        return os.path.join(upload_folder, session_id, "qvd_inputs")

    def qvd_output_dir(session_id: str) -> str:
        if use_configured_storage:
            return safe_join(QVD_OUTPUT_FOLDER, session_id)
        return os.path.join(upload_folder, session_id, "qvd_outputs")

    def qvd_package_zip_path(session_id: str) -> str:
        if use_configured_storage:
            return safe_join(MIGRATION_PACKAGE_FOLDER, session_id, "migration_package.zip")
        return os.path.join(upload_folder, session_id, "qvd_outputs", "migration_package", "migration_package.zip")

    def qvd_public_artifact(session_id: str, path: str | None, *, output_dir: str | None = None, package: bool = False) -> dict | None:
        if not path:
            return None
        output_dir = output_dir or qvd_output_dir(session_id)
        path_obj = Path(path)
        if not path_obj.exists():
            return None

        if package:
            base_dir = Path(MIGRATION_PACKAGE_FOLDER if use_configured_storage else os.path.dirname(qvd_package_zip_path(session_id))).resolve()
            relative_path = path_obj.resolve().relative_to(base_dir).as_posix()
            download_url = f"/api/qvd/download-migration-package/{quote(session_id)}"
        else:
            base_dir = Path(output_dir).resolve()
            relative_path = path_obj.resolve().relative_to(base_dir).as_posix()
            download_url = f"/api/qvd/download-artifact/{quote(session_id)}/{_quote_artifact_path(relative_path)}"

        return {
            "file_name": path_obj.name,
            "relative_path": relative_path,
            "download_url": download_url,
        }

    def qvd_public_artifacts(session_id: str, artifacts: dict | None, *, output_dir: str | None = None, package: bool = False) -> dict:
        public = {}
        for name, path in (artifacts or {}).items():
            metadata = qvd_public_artifact(session_id, path, output_dir=output_dir, package=package)
            if metadata:
                public[name] = metadata
        return public

    def qvd_public_runtime_path(path: str | None, *, output_dir: str) -> str:
        raw = str(path or "")
        if not raw:
            return ""
        try:
            path_obj = Path(raw)
            if path_obj.is_absolute():
                return path_obj.resolve().relative_to(Path(output_dir).resolve()).as_posix()
        except (OSError, ValueError):
            pass
        return raw

    def qvd_response_paths(session_id: str, result: dict, *, output_dir: str, package: bool = False) -> dict:
        payload = dict(result or {})
        original_artifacts = payload.get("artifacts") or {}
        payload["artifact_paths"] = {
            name: meta["relative_path"]
            for name, meta in qvd_public_artifacts(session_id, original_artifacts, output_dir=output_dir, package=package).items()
        }
        payload["artifact_downloads"] = qvd_public_artifacts(session_id, original_artifacts, output_dir=output_dir, package=package)
        payload["artifacts"] = payload["artifact_downloads"]

        if isinstance(payload.get("sql_preview"), dict):
            normalized_preview = {}
            for path, sql in payload["sql_preview"].items():
                metadata = qvd_public_artifact(session_id, path, output_dir=output_dir)
                normalized_preview[metadata["relative_path"] if metadata else os.path.basename(str(path))] = sql
            payload["sql_preview"] = normalized_preview

        for key in (
            "approved_mapping_csv",
            "documentation_path",
            "conversion_report_json",
            "validation_report_json",
            "config_path",
            "status_path",
        ):
            metadata = qvd_public_artifact(session_id, payload.get(key), output_dir=output_dir)
            if metadata:
                payload[key] = metadata["relative_path"]
                payload[f"{key}_download"] = metadata

        package_dir_metadata = qvd_public_artifact(session_id, payload.get("package_dir"), output_dir=output_dir, package=True)
        if package_dir_metadata:
            payload["package_dir"] = package_dir_metadata["relative_path"]

        zip_metadata = qvd_public_artifact(session_id, payload.get("migration_package_zip"), output_dir=output_dir, package=True)
        if zip_metadata:
            payload["migration_package_zip"] = zip_metadata["relative_path"]
            payload["migration_package"] = zip_metadata
            payload["download_url"] = zip_metadata["download_url"]

        if "parquet_path" in payload:
            payload["parquet_path"] = qvd_public_runtime_path(payload.get("parquet_path"), output_dir=output_dir)

        return payload

    def qvd_mapping_rows_by_file(output_dir: str) -> dict[str, list[dict]]:
        mapping_path = os.path.join(output_dir, "approved_databricks_mapping.csv")
        if not os.path.exists(mapping_path):
            return {}
        rows_by_file: dict[str, list[dict]] = {}
        for row in read_approved_mapping_csv(mapping_path):
            file_name = str(row.get("qvd_file") or "").strip()
            if file_name:
                rows_by_file.setdefault(file_name, []).append(row)
        return rows_by_file

    def qvd_target_table_for_file(file_name: str, table: dict, rows_by_file: dict[str, list[dict]]) -> str:
        rows = rows_by_file.get(file_name) or []
        if rows:
            return str(rows[0].get("target_table") or "").strip()
        summary = table.get("summary") or {}
        return safe_sql_filename(summary.get("table_name") or os.path.splitext(file_name)[0] or "")

    def qvd_build_session_state(session_id: str) -> dict:
        output_dir = qvd_output_dir(session_id)
        inspection_path = os.path.join(output_dir, "qvd_inspection.json")
        if not os.path.exists(inspection_path):
            raise FileNotFoundError("QVD inspection artifact not found for this session")

        inspection = _read_json_if_exists(inspection_path)
        rows_by_file = qvd_mapping_rows_by_file(output_dir)
        approved_mapping_path = os.path.join(output_dir, "approved_databricks_mapping.csv")
        ddl_dir = os.path.join(output_dir, "ddl")
        ddl_files = sorted(
            os.path.join(ddl_dir, name)
            for name in os.listdir(ddl_dir)
            if name.endswith(".sql")
        ) if os.path.isdir(ddl_dir) else []

        qvd_databricks_load_scripts = {}
        qvd_migration_packages = {}
        qvd_parquet_validations = {}
        qvd_parquet_conversions = {}

        for table in inspection.get("tables") or []:
            summary = table.get("summary") or {}
            file_name = summary.get("file_name") or ""
            target_table = qvd_target_table_for_file(file_name, table, rows_by_file)
            if not file_name or not target_table:
                continue

            validation_path = os.path.join(output_dir, f"parquet_validation_{target_table}.json")
            validation = _read_json_if_exists(validation_path)
            if validation:
                validation["artifacts"] = qvd_public_artifacts(session_id, {"validation_report_json": validation_path}, output_dir=output_dir)
                qvd_parquet_validations[file_name] = validation
                if validation.get("parquet_path"):
                    qvd_parquet_conversions[file_name] = {
                        "success": True,
                        "target_table": target_table,
                        "parquet_path": validation.get("parquet_path"),
                    }

            load_config_path = os.path.join(output_dir, "databricks_load", "load_config.json")
            if os.path.exists(load_config_path):
                load_config = _read_json_if_exists(load_config_path)
                if str(load_config.get("target_table") or "").strip() == target_table:
                    load_artifacts = {
                        "create_table_sql": os.path.join(output_dir, "databricks_load", "create_table.sql"),
                        "insert_select_cast_sql": os.path.join(output_dir, "databricks_load", "insert_select_cast.sql"),
                        "validation_sql": os.path.join(output_dir, "databricks_load", "validation.sql"),
                        "load_sql": os.path.join(output_dir, "databricks_load", "load_parquet_to_delta.sql"),
                        "load_config_json": load_config_path,
                        "readme": os.path.join(output_dir, "databricks_load", "README_load_steps.md"),
                    }
                    qvd_databricks_load_scripts[file_name] = {
                        "generated": True,
                        "mode": load_config.get("mode") or "script_artifact",
                        "target_table": target_table,
                        "qualified_table": load_config.get("qualified_table"),
                        "parquet_path": qvd_public_runtime_path(load_config.get("parquet_path"), output_dir=output_dir),
                        "local_path_warning": load_config.get("local_path_warning") or "",
                        "artifacts": qvd_public_artifacts(session_id, load_artifacts, output_dir=output_dir),
                        "errors": [],
                    }

            package_zip = qvd_package_zip_path(session_id)
            if not os.path.exists(package_zip):
                package_zip = os.path.join(output_dir, "migration_package", "migration_package.zip")
            package_summary_path = os.path.join(os.path.dirname(package_zip), "migration_summary.json")
            package_summary = _read_json_if_exists(package_summary_path)
            if package_summary and str(package_summary.get("target_table") or "").strip() == target_table and os.path.exists(package_zip):
                qvd_migration_packages[file_name] = {
                    "generated": True,
                    "target_table": target_table,
                    "migration_package_zip": qvd_public_artifact(session_id, package_zip, output_dir=output_dir, package=True)["relative_path"],
                    "migration_package": qvd_public_artifact(session_id, package_zip, output_dir=output_dir, package=True),
                    "download_url": f"/api/qvd/download-migration-package/{quote(session_id)}",
                    "summary": package_summary,
                    "artifacts": qvd_public_artifacts(session_id, {"zip": package_zip}, output_dir=output_dir, package=True),
                    "errors": [],
                }

        ddl_sql_preview = {}
        for ddl_file in ddl_files:
            try:
                ddl_sql_preview[qvd_public_artifact(session_id, ddl_file, output_dir=output_dir)["relative_path"]] = Path(ddl_file).read_text(encoding="utf-8")
            except OSError:
                continue

        return {
            "session_id": session_id,
            "sessionType": "qvd",
            "qvdInspection": inspection,
            "uploaded_files": inspection.get("uploaded_files") or [],
            "tables": inspection.get("tables") or [],
            "approved_mapping": {
                "exists": os.path.exists(approved_mapping_path),
                "path": qvd_public_artifact(session_id, approved_mapping_path, output_dir=output_dir),
                "rows_by_file": rows_by_file,
            },
            "qvdApprovedMapping": {
                "saved": os.path.exists(approved_mapping_path),
                "artifacts": qvd_public_artifacts(session_id, {"approved_mapping_csv": approved_mapping_path}, output_dir=output_dir),
                "mapping_rows": [row for rows in rows_by_file.values() for row in rows],
            } if os.path.exists(approved_mapping_path) else None,
            "qvdDdlGeneration": {
                "generated": bool(ddl_files),
                "ddl_files": [qvd_public_artifact(session_id, path, output_dir=output_dir)["relative_path"] for path in ddl_files],
                "table_count": len(ddl_files),
                "errors": [],
                "sql_preview": ddl_sql_preview,
                "artifacts": qvd_public_artifacts(session_id, {os.path.basename(path): path for path in ddl_files}, output_dir=output_dir),
            } if ddl_files else None,
            "qvdParquetConversions": qvd_parquet_conversions,
            "qvdParquetValidations": qvd_parquet_validations,
            "qvdDatabricksLoadScripts": qvd_databricks_load_scripts,
            "qvdMigrationPackages": qvd_migration_packages,
            "progress": {
                "inspection": True,
                "approved_mapping": os.path.exists(approved_mapping_path),
                "ddl": bool(ddl_files),
                "load_scripts": any(item.get("generated") for item in qvd_databricks_load_scripts.values()),
                "migration_package": any(item.get("generated") for item in qvd_migration_packages.values()),
            },
        }

    @qvd_bp.route("/session/<session_id>", methods=["GET"])
    def qvd_session(session_id):
        try:
            return jsonify(qvd_build_session_state(session_id))
        except FileNotFoundError:
            return jsonify({"error": "QVD inspection artifact not found for this session"}), 404

    @qvd_bp.route("/upload-inspect", methods=["POST"])
    def qvd_upload_inspect():
        files = request.files.getlist("files") or request.files.getlist("file")
        if not files:
            return jsonify({"error": "No QVD files provided"}), 400

        session_id = request.form.get("session_id") or request.form.get("sessionId") or str(uuid.uuid4())
        input_dir = qvd_input_dir(session_id)
        output_dir = qvd_output_dir(session_id)
        os.makedirs(input_dir, exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)

        uploaded_files = []
        tables = []
        errors = []

        for incoming in files:
            original_name = incoming.filename or ""
            filename = secure_filename(original_name)
            if not filename:
                continue
            if not filename.lower().endswith(".qvd"):
                errors.append({
                    "file_name": original_name,
                    "error": "Only .qvd files are supported for this flow.",
                })
                continue

            file_path = os.path.join(input_dir, filename)
            if os.path.exists(file_path):
                stem, ext = os.path.splitext(filename)
                filename = f"{stem}_{uuid.uuid4().hex[:8]}{ext}"
                file_path = os.path.join(input_dir, filename)

            incoming.save(file_path)
            file_record = {
                "file_name": filename,
                "file_path": file_path,
                "file_size_bytes": os.path.getsize(file_path),
            }
            uploaded_files.append(file_record)

            try:
                tables.append(inspect_qvd_file(file_path))
            except Exception as exc:
                logger.warning("QVD metadata inspection failed for %s: %s", filename, exc)
                errors.append({
                    "file_name": filename,
                    "error": str(exc),
                })

        artifact_paths = write_inspection_artifacts(
            session_id,
            output_dir,
            uploaded_files,
            tables,
            errors,
        )

        return jsonify({
            "session_id": session_id,
            "uploaded_files": uploaded_files,
            "tables": tables,
            "errors": errors,
            "artifacts": artifact_paths,
            "created_at": datetime.utcnow().isoformat(),
        })

    @qvd_bp.route("/suggest-schema/<session_id>", methods=["POST"])
    def qvd_suggest_schema(session_id):
        output_dir = qvd_output_dir(session_id)
        inspection_path = os.path.join(output_dir, "qvd_inspection.json")
        if not os.path.exists(inspection_path):
            return jsonify({"error": "QVD inspection artifact not found for this session"}), 404

        with open(inspection_path, encoding="utf-8") as handle:
            inspection = json.load(handle)

        suggestion = suggest_schema_from_inspection(inspection)
        artifact_paths = write_schema_suggestion_artifacts(session_id, output_dir, suggestion)

        return jsonify({
            "session_id": session_id,
            **suggestion,
            "artifacts": artifact_paths,
            "created_at": datetime.utcnow().isoformat(),
        })

    @qvd_bp.route("/business-analysis/entities/<session_id>", methods=["POST"])
    def qvd_business_analysis_entities(session_id):
        output_dir = qvd_output_dir(session_id)
        inspection_path = os.path.join(output_dir, "qvd_inspection.json")
        if not os.path.exists(inspection_path):
            return jsonify({
                "session_id": session_id,
                "success": False,
                "errors": ["QVD inspection artifact not found for this session."],
            }), 404

        result = discover_business_entities(output_dir)
        artifact_path = write_business_entities_artifact(output_dir, result)
        return jsonify({
            "session_id": session_id,
            "success": True,
            **result,
            "artifacts": {
                "business_entities_json": artifact_path,
            },
            "created_at": datetime.utcnow().isoformat(),
        })

    @qvd_bp.route("/business-analysis/kpis/<session_id>", methods=["POST"])
    def qvd_business_analysis_kpis(session_id):
        output_dir = qvd_output_dir(session_id)
        entities_path = os.path.join(output_dir, "business_analysis", "business_entities.json")
        if not os.path.exists(entities_path):
            return jsonify({
                "session_id": session_id,
                "success": False,
                "errors": ["Business entities artifact not found. Run Business Entity Discovery first."],
            }), 404

        with open(entities_path, encoding="utf-8") as handle:
            entities = json.load(handle)
        catalog = generate_kpi_catalog(output_dir)
        artifacts = write_kpi_catalog_artifacts(output_dir, catalog)
        documentation_path = write_business_documentation(output_dir, entities, catalog)
        artifacts["business_analysis_md"] = documentation_path
        with open(documentation_path, encoding="utf-8") as handle:
            documentation_preview = handle.read()

        return jsonify({
            "session_id": session_id,
            "success": True,
            "kpi_count": catalog["kpi_count"],
            "kpis": catalog["kpis"],
            "documentation_path": documentation_path,
            "documentation_preview": documentation_preview[:5000],
            "artifacts": artifacts,
            "created_at": datetime.utcnow().isoformat(),
        })

    @qvd_bp.route("/business-analysis/lineage-reconciliation/<session_id>", methods=["POST"])
    def qvd_business_analysis_lineage_reconciliation(session_id):
        output_dir = qvd_output_dir(session_id)
        business_dir = os.path.join(output_dir, "business_analysis")
        entities_path = os.path.join(business_dir, "business_entities.json")
        kpi_catalog_path = os.path.join(business_dir, "kpi_catalog.json")
        if not os.path.exists(entities_path):
            return jsonify({
                "session_id": session_id,
                "success": False,
                "errors": ["Business entities artifact not found. Run Business Entity Discovery first."],
            }), 404
        if not os.path.exists(kpi_catalog_path):
            return jsonify({
                "session_id": session_id,
                "success": False,
                "errors": ["KPI catalog artifact not found. Generate KPI Catalog & Documentation first."],
            }), 404

        lineage = generate_lineage(output_dir)
        lineage_path = write_lineage_artifact(output_dir, lineage)
        reconciliation = generate_reconciliation_rules(output_dir)
        reconciliation_artifacts = write_reconciliation_artifacts(output_dir, reconciliation)
        glossary = generate_business_glossary(output_dir)
        executive_summary = generate_executive_summary(output_dir, lineage, reconciliation, glossary)
        glossary_summary_artifacts = write_glossary_summary_artifacts(output_dir, glossary, executive_summary)
        markdown_preview = ""
        markdown_path = reconciliation_artifacts.get("reconciliation_rules_md")
        if markdown_path and os.path.exists(markdown_path):
            with open(markdown_path, encoding="utf-8") as handle:
                markdown_preview = handle.read()

        artifacts = {
            "lineage_json": lineage_path,
            **reconciliation_artifacts,
            **glossary_summary_artifacts,
        }
        return jsonify({
            "session_id": session_id,
            "success": True,
            "lineage_nodes": len(lineage.get("nodes") or []),
            "lineage_edges": len(lineage.get("edges") or []),
            "reconciliation_rule_count": len(reconciliation.get("rules") or []),
            "glossary_count": glossary.get("glossary_count", 0),
            "migration_readiness_score": executive_summary.get("migration_readiness_score", 0),
            "lineage": lineage,
            "reconciliation": reconciliation,
            "business_glossary": glossary,
            "executive_summary": executive_summary,
            "reconciliation_markdown_preview": markdown_preview[:5000],
            "artifacts": artifacts,
            "created_at": datetime.utcnow().isoformat(),
        })

    @qvd_bp.route("/business-analysis/ai-explain/<session_id>", methods=["POST"])
    def qvd_business_analysis_ai_explain(session_id):
        output_dir = qvd_output_dir(session_id)
        business_dir = os.path.join(output_dir, "business_analysis")
        entities_path = os.path.join(business_dir, "business_entities.json")
        kpi_catalog_path = os.path.join(business_dir, "kpi_catalog.json")
        if not os.path.exists(entities_path) or not os.path.exists(kpi_catalog_path):
            return jsonify({
                "session_id": session_id,
                "success": False,
                "used_llm": False,
                "warnings": ["Run Business Entity Discovery and KPI Catalog generation before AI explanation."],
                "errors": ["Required business analysis artifacts are missing."],
            }), 404
        result = generate_ai_business_explanation(output_dir, call_ai=call_ai)
        return jsonify({
            "session_id": session_id,
            **result,
            "created_at": datetime.utcnow().isoformat(),
        })

    @qvd_bp.route("/save-approved-mapping/<session_id>", methods=["POST"])
    def qvd_save_approved_mapping(session_id):
        payload = request.get_json(silent=True) or {}
        mapping_rows = payload.get("mapping_rows")
        errors = validate_approved_mapping_rows(mapping_rows)
        if errors:
            return jsonify({
                "session_id": session_id,
                "saved": False,
                "errors": errors,
            }), 400

        output_dir = qvd_output_dir(session_id)
        artifact_paths = write_approved_mapping_artifacts(session_id, output_dir, mapping_rows)

        return jsonify({
            "session_id": session_id,
            "saved": True,
            "approved_mapping_csv": artifact_paths["approved_mapping_csv"],
            "total_rows": artifact_paths["total_rows"],
            "approved_count": artifact_paths["approved_count"],
            "errors": [],
            "artifacts": artifact_paths,
            "created_at": datetime.utcnow().isoformat(),
        })

    @qvd_bp.route("/generate-ddl/<session_id>", methods=["POST"])
    def qvd_generate_ddl(session_id):
        output_dir = qvd_output_dir(session_id)
        approved_mapping_path = os.path.join(output_dir, "approved_databricks_mapping.csv")
        if not os.path.exists(approved_mapping_path):
            return jsonify({
                "session_id": session_id,
                "generated": False,
                "ddl_files": [],
                "table_count": 0,
                "errors": [{"field": "approved_mapping_csv", "error": "Approved mapping artifact not found."}],
            }), 404

        payload = request.get_json(silent=True) or {}
        catalog_schema = payload.get("catalog_schema") or payload.get("catalogSchema") or "main.qvd_raw"
        result = generate_delta_ddl_from_approved_mapping(
            approved_mapping_path,
            os.path.join(output_dir, "ddl"),
            catalog_schema,
        )
        status = 200 if result["generated"] else 400
        result = qvd_response_paths(session_id, result, output_dir=output_dir)
        return jsonify({
            "session_id": session_id,
            **result,
            "created_at": datetime.utcnow().isoformat(),
        }), status

    @qvd_bp.route("/preview-rows/<session_id>", methods=["POST"])
    def qvd_preview_rows(session_id):
        payload = request.get_json(silent=True) or {}
        requested_name = payload.get("file_name") or payload.get("fileName")
        if not requested_name:
            return jsonify({"error": "file_name is required"}), 400

        file_name = secure_filename(requested_name)
        if not file_name:
            return jsonify({"error": "Invalid QVD file name"}), 400

        try:
            limit = int(payload.get("limit") or 100)
        except (TypeError, ValueError):
            limit = 100
        limit = max(1, min(limit, 100))

        input_dir = qvd_input_dir(session_id)
        file_path = os.path.join(input_dir, file_name)
        if not os.path.exists(file_path):
            return jsonify({
                "session_id": session_id,
                "file_name": file_name,
                "error": "QVD file not found for this session",
            }), 404

        preview = qvd_row_reader.preview_qvd_rows(file_path, limit=limit)
        output_dir = qvd_output_dir(session_id)
        os.makedirs(output_dir, exist_ok=True)
        artifact_path = os.path.join(output_dir, f"row_preview_{_safe_preview_artifact_name(file_name)}.json")

        response_payload = {
            "session_id": session_id,
            "file_name": file_name,
            "file_path": file_path,
            **preview,
            "artifact": artifact_path,
            "created_at": datetime.utcnow().isoformat(),
        }
        with open(artifact_path, "w", encoding="utf-8") as handle:
            json.dump(response_payload, handle, indent=2, ensure_ascii=False)

        return jsonify(response_payload)

    @qvd_bp.route("/profile-columns/<session_id>", methods=["POST"])
    def qvd_profile_columns(session_id):
        payload = request.get_json(silent=True) or {}
        requested_name = payload.get("file_name") or payload.get("fileName")
        if not requested_name:
            return jsonify({"error": "file_name is required"}), 400

        file_name = secure_filename(requested_name)
        if not file_name:
            return jsonify({"error": "Invalid QVD file name"}), 400

        try:
            limit = int(payload.get("limit") or 10000)
        except (TypeError, ValueError):
            limit = 10000
        limit = max(1, min(limit, 10000))

        input_dir = qvd_input_dir(session_id)
        file_path = os.path.join(input_dir, file_name)
        if not os.path.exists(file_path):
            return jsonify({
                "session_id": session_id,
                "file_name": file_name,
                "error": "QVD file not found for this session",
            }), 404

        output_dir = qvd_output_dir(session_id)
        approved_mapping_path = os.path.join(output_dir, "approved_databricks_mapping.csv")
        approved_mapping_rows = load_approved_mapping_rows(approved_mapping_path)
        profile = profile_qvd_columns(file_path, approved_mapping_rows=approved_mapping_rows, limit=limit)

        safe_file_name = _safe_preview_artifact_name(file_name)
        response_payload = {
            "session_id": session_id,
            "file_name": file_name,
            "file_path": file_path,
            "limit": limit,
            **profile,
            "created_at": datetime.utcnow().isoformat(),
        }
        artifacts = write_profile_artifacts(output_dir, safe_file_name, response_payload)
        response_payload["artifacts"] = artifacts

        return jsonify(response_payload)

    @qvd_bp.route("/convert-parquet/<session_id>", methods=["POST"])
    def qvd_convert_parquet(session_id):
        payload = request.get_json(silent=True) or {}
        requested_name = payload.get("file_name") or payload.get("fileName")
        if not requested_name:
            return jsonify({"error": "file_name is required"}), 400

        file_name = secure_filename(requested_name)
        if not file_name:
            return jsonify({"error": "Invalid QVD file name"}), 400

        input_dir = qvd_input_dir(session_id)
        file_path = os.path.join(input_dir, file_name)
        if not os.path.exists(file_path):
            return jsonify({
                "session_id": session_id,
                "file_name": file_name,
                "success": False,
                "errors": ["QVD file not found for this session"],
            }), 404

        output_dir = qvd_output_dir(session_id)
        approved_mapping_path = os.path.join(output_dir, "approved_databricks_mapping.csv")
        if not os.path.exists(approved_mapping_path):
            return jsonify({
                "session_id": session_id,
                "file_name": file_name,
                "success": False,
                "errors": ["Approved mapping artifact not found."],
            }), 404

        report = convert_qvd_to_parquet(
            file_path,
            approved_mapping_path,
            os.path.join(output_dir, "parquet"),
            batch_id=payload.get("batch_id") or payload.get("batchId"),
        )
        safe_file_name = _safe_preview_artifact_name(file_name)
        report_payload = {
            "session_id": session_id,
            "file_name": file_name,
            "file_path": file_path,
            **report,
            "created_at": datetime.utcnow().isoformat(),
        }
        report_path = os.path.join(output_dir, f"conversion_report_{safe_file_name}.json")
        with open(report_path, "w", encoding="utf-8") as handle:
            json.dump(report_payload, handle, indent=2, ensure_ascii=False)
        report_payload["conversion_report_json"] = report_path

        return jsonify(report_payload), 200 if report.get("success") else 400

    @qvd_bp.route("/validate-parquet/<session_id>", methods=["POST"])
    def qvd_validate_parquet(session_id):
        payload = request.get_json(silent=True) or {}
        target_table = str(payload.get("target_table") or payload.get("targetTable") or "").strip()
        if not target_table:
            return jsonify({"error": "target_table is required"}), 400

        output_dir = qvd_output_dir(session_id)
        parquet_path = os.path.join(output_dir, "parquet", target_table)
        approved_mapping_path = os.path.join(output_dir, "approved_databricks_mapping.csv")
        inspection_json_path = os.path.join(output_dir, "qvd_inspection.json")

        validation = validate_parquet_output(
            parquet_path,
            approved_mapping_path,
            inspection_json_path=inspection_json_path,
            target_table=target_table,
        )
        response_payload = {
            "session_id": session_id,
            "target_table": target_table,
            "parquet_path": parquet_path,
            **validation,
            "created_at": datetime.utcnow().isoformat(),
        }
        artifact_path = write_validation_artifact(output_dir, target_table, response_payload)
        response_payload["validation_report_json"] = artifact_path

        return jsonify(response_payload), 200 if validation.get("success") else 400

    @qvd_bp.route("/generate-databricks-load/<session_id>", methods=["POST"])
    def qvd_generate_databricks_load(session_id):
        payload = request.get_json(silent=True) or {}
        target_table = str(payload.get("target_table") or payload.get("targetTable") or "").strip()
        if not target_table:
            return jsonify({
                "session_id": session_id,
                "generated": False,
                "errors": ["target_table is required"],
            }), 400

        output_dir = qvd_output_dir(session_id)
        validation_report_path = os.path.join(output_dir, f"parquet_validation_{target_table}.json")
        validation_report = load_validation_report(validation_report_path) if os.path.exists(validation_report_path) else {}
        if validation_report and not validation_report_passed(validation_report):
            return jsonify({
                "session_id": session_id,
                "target_table": target_table,
                "generated": False,
                "errors": ["Parquet validation has not passed. Fix validation failures before generating Databricks load scripts."],
                "failed_checks": validation_report.get("failed_checks", []),
            }), 400

        parquet_path = qvd_public_runtime_path(
            payload.get("parquet_path")
            or payload.get("parquetPath")
            or validation_report.get("parquet_path")
            or os.path.join(output_dir, "parquet", target_table),
            output_dir=output_dir,
        )
        catalog = str(payload.get("catalog") or "main").strip() or "main"
        schema = str(payload.get("schema") or "qvd_raw").strip() or "qvd_raw"

        result = generate_databricks_load_artifacts(
            target_table=target_table,
            parquet_path=parquet_path,
            ddl_sql_path=default_ddl_path(output_dir, target_table),
            output_dir=os.path.join(output_dir, "databricks_load"),
            catalog=catalog,
            schema=schema,
            validation_report_path=validation_report_path if os.path.exists(validation_report_path) else None,
            approved_mapping_path=os.path.join(output_dir, "approved_databricks_mapping.csv"),
        )
        result = qvd_response_paths(session_id, result, output_dir=output_dir)
        return jsonify({
            "session_id": session_id,
            **result,
            "created_at": datetime.utcnow().isoformat(),
        }), 200 if result.get("generated") else 400

    @qvd_bp.route("/generate-migration-package/<session_id>", methods=["POST"])
    def qvd_generate_migration_package(session_id):
        payload = request.get_json(silent=True) or {}
        target_table = str(payload.get("target_table") or payload.get("targetTable") or "").strip()
        if not target_table:
            return jsonify({
                "session_id": session_id,
                "generated": False,
                "errors": ["target_table is required"],
            }), 400

        output_dir = qvd_output_dir(session_id)
        result = generate_migration_package(
            output_dir,
            target_table,
            file_name=payload.get("file_name") or payload.get("fileName"),
            package_dir=safe_join(MIGRATION_PACKAGE_FOLDER, session_id) if use_configured_storage else None,
        )
        result = qvd_response_paths(session_id, result, output_dir=output_dir, package=True)
        return jsonify({
            "session_id": session_id,
            **result,
            "created_at": datetime.utcnow().isoformat(),
        }), 200 if result.get("generated") else 400

    @qvd_bp.route("/databricks/save-config/<session_id>", methods=["POST"])
    def qvd_databricks_save_config(session_id):
        payload = request.get_json(silent=True) or {}
        output_dir = qvd_output_dir(session_id)
        config = merge_connection_config(output_dir, payload) or DatabricksConnectionConfig.from_payload(payload)
        config_path = save_connection_config(output_dir, config)
        return jsonify({
            "session_id": session_id,
            "saved": True,
            "config": config.masked(),
            "config_path": config_path,
            "created_at": datetime.utcnow().isoformat(),
        })

    @qvd_bp.route("/databricks/test-connection/<session_id>", methods=["POST"])
    def qvd_databricks_test_connection(session_id):
        payload = request.get_json(silent=True) or {}
        output_dir = qvd_output_dir(session_id)
        config = merge_connection_config(output_dir, payload)
        if config is None:
            return jsonify({
                "session_id": session_id,
                "success": False,
                "errors": ["Databricks connection configuration not found."],
            }), 400

        result = test_databricks_connection(config)
        status_path = os.path.join(output_dir, "databricks_connection_status.json")
        os.makedirs(output_dir, exist_ok=True)
        with open(status_path, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, ensure_ascii=False)
        return jsonify({
            "session_id": session_id,
            **result,
            "status_path": status_path,
            "created_at": datetime.utcnow().isoformat(),
        }), 200 if result.get("success") else 400

    def _databricks_discovery_config(session_id, payload=None):
        output_dir = qvd_output_dir(session_id)
        config = merge_connection_config(output_dir, payload or {})
        if config is None:
            raise ValueError("Databricks connection configuration not found.")
        errors = []
        if not config.workspace_url:
            errors.append("Databricks Workspace URL is required.")
        if not config.personal_access_token:
            errors.append("Personal Access Token is required.")
        if errors:
            raise ValueError(" ".join(errors))
        return output_dir, config

    @qvd_bp.route("/databricks/warehouses/<session_id>", methods=["GET", "POST"])
    def qvd_databricks_warehouses(session_id):
        payload = request.get_json(silent=True) or {}
        try:
            _, config = _databricks_discovery_config(session_id, payload)
            return jsonify({"session_id": session_id, "warehouses": list_warehouses(config)})
        except Exception as exc:
            return jsonify({"session_id": session_id, "warehouses": [], "errors": [str(exc)]}), 400

    @qvd_bp.route("/databricks/catalogs/<session_id>", methods=["GET", "POST"])
    def qvd_databricks_catalogs(session_id):
        payload = request.get_json(silent=True) or {}
        try:
            _, config = _databricks_discovery_config(session_id, payload)
            return jsonify({"session_id": session_id, "catalogs": list_catalogs(config)})
        except Exception as exc:
            return jsonify({"session_id": session_id, "catalogs": [], "errors": [str(exc)]}), 400

    @qvd_bp.route("/databricks/schemas/<session_id>", methods=["GET", "POST"])
    def qvd_databricks_schemas(session_id):
        payload = request.get_json(silent=True) or {}
        try:
            _, config = _databricks_discovery_config(session_id, payload)
            catalog = str(payload.get("catalog") or config.catalog or "").strip()
            return jsonify({"session_id": session_id, "schemas": list_schemas(config, catalog)})
        except Exception as exc:
            return jsonify({"session_id": session_id, "schemas": [], "errors": [str(exc)]}), 400

    @qvd_bp.route("/databricks/volumes/<session_id>", methods=["GET", "POST"])
    def qvd_databricks_volumes(session_id):
        payload = request.get_json(silent=True) or {}
        try:
            _, config = _databricks_discovery_config(session_id, payload)
            catalog = str(payload.get("catalog") or config.catalog or "").strip()
            schema = str(payload.get("schema") or config.schema or "").strip()
            return jsonify({"session_id": session_id, "volumes": list_volumes(config, catalog, schema)})
        except Exception as exc:
            return jsonify({"session_id": session_id, "volumes": [], "errors": [str(exc)]}), 400

    @qvd_bp.route("/databricks/create-schema/<session_id>", methods=["POST"])
    def qvd_databricks_create_schema(session_id):
        payload = request.get_json(silent=True) or {}
        try:
            _, config = _databricks_discovery_config(session_id, payload)
            catalog = str(payload.get("catalog") or config.catalog or "").strip()
            schema = str(payload.get("schema") or config.schema or "").strip()
            sql = f"CREATE SCHEMA IF NOT EXISTS `{catalog.replace('`', '``')}`.`{schema.replace('`', '``')}`"
            result = execute_sql_statement(sql, config.sql_warehouse_id, catalog=catalog, schema=schema, config=config, stage="schema")
            return jsonify({"session_id": session_id, "success": result.get("success", True), "statement": sql, "result": result, "errors": result.get("errors", [])}), 200 if result.get("success", True) else 400
        except Exception as exc:
            return jsonify({"session_id": session_id, "success": False, "errors": [str(exc)]}), 400

    @qvd_bp.route("/databricks/create-volume/<session_id>", methods=["POST"])
    def qvd_databricks_create_volume(session_id):
        payload = request.get_json(silent=True) or {}
        try:
            _, config = _databricks_discovery_config(session_id, payload)
            catalog = str(payload.get("catalog") or config.catalog or "").strip()
            schema = str(payload.get("schema") or config.schema or "").strip()
            volume = str(payload.get("volume") or config.volume or "").strip()
            if not volume:
                return jsonify({"session_id": session_id, "success": False, "errors": ["volume is required"]}), 400
            sql = f"CREATE VOLUME IF NOT EXISTS `{catalog.replace('`', '``')}`.`{schema.replace('`', '``')}`.`{volume.replace('`', '``')}`"
            result = execute_sql_statement(sql, config.sql_warehouse_id, catalog=catalog, schema=schema, config=config, stage="connection")
            return jsonify({"session_id": session_id, "success": result.get("success", True), "statement": sql, "result": result, "errors": result.get("errors", [])}), 200 if result.get("success", True) else 400
        except Exception as exc:
            return jsonify({"session_id": session_id, "success": False, "errors": [str(exc)]}), 400

    @qvd_bp.route("/databricks/upload-parquet/<session_id>", methods=["POST"])
    def qvd_databricks_upload_parquet(session_id):
        payload = request.get_json(silent=True) or {}
        target_table = str(payload.get("target_table") or payload.get("targetTable") or "").strip()
        output_dir = qvd_output_dir(session_id)
        try:
            config = merge_connection_config(output_dir, payload.get("config") or payload)
            if config is None:
                return jsonify({"session_id": session_id, "success": False, "errors": ["Databricks connection configuration not found."]}), 400
            if config.cloud_storage_path:
                return jsonify({
                    "session_id": session_id,
                    "success": True,
                    "skipped": True,
                    "message": "Cloud storage path is configured; volume upload is not required.",
                    "volume_path": f"{config.cloud_storage_path.rstrip('/')}/{target_table}/",
                })
            if not target_table:
                return jsonify({"session_id": session_id, "success": False, "errors": ["target_table is required"]}), 400
            load_config_path = os.path.join(output_dir, "databricks_load", "load_config.json")
            if not os.path.exists(load_config_path):
                return jsonify({"session_id": session_id, "success": False, "errors": ["load_config.json not found. Generate Databricks load scripts first."]}), 400
            with open(load_config_path, encoding="utf-8") as handle:
                load_config = json.load(handle)
            result = upload_parquet_to_volume(
                session_id,
                load_config.get("parquet_path") or "",
                config.catalog,
                config.schema,
                config.volume,
                target_table,
                config,
            )
            artifacts = write_upload_status(output_dir, result)
            if result.get("volume_path"):
                config.volume_path = f"/Volumes/{config.catalog}/{config.schema}/{config.volume}"
                save_connection_config(output_dir, config)
            return jsonify({"session_id": session_id, **result, "artifacts": artifacts}), 200 if result.get("success") else 400
        except Exception as exc:
            return jsonify({"session_id": session_id, "success": False, "errors": [str(exc)]}), 400

    @qvd_bp.route("/databricks/precheck/<session_id>", methods=["POST"])
    def qvd_databricks_precheck(session_id):
        payload = request.get_json(silent=True) or {}
        target_table = str(payload.get("target_table") or payload.get("targetTable") or "").strip()
        mode = str(payload.get("execution_mode") or payload.get("executionMode") or "generate_sql_only").strip()
        output_dir = qvd_output_dir(session_id)
        config = merge_connection_config(output_dir, payload.get("config") or payload)
        if not target_table:
            return jsonify({"session_id": session_id, "passed": False, "errors": ["target_table is required"]}), 400
        result = precheck_execution(output_dir, target_table, mode, config)
        return jsonify({"session_id": session_id, **result}), 200 if result.get("passed") else 400

    @qvd_bp.route("/databricks/execute/<session_id>", methods=["POST"])
    def qvd_databricks_execute(session_id):
        payload = request.get_json(silent=True) or {}
        target_table = str(payload.get("target_table") or payload.get("targetTable") or "").strip()
        mode = str(payload.get("execution_mode") or payload.get("executionMode") or "").strip()
        if not target_table:
            return jsonify({"session_id": session_id, "success": False, "status": "failed", "error": "target_table is required", "errors": ["target_table is required"], "stage": "connection"}), 200
        if mode not in EXECUTION_MODES:
            error = f"Unsupported execution mode: {mode or '(empty)'}"
            return jsonify({"session_id": session_id, "success": False, "status": "failed", "error": error, "errors": [error], "stage": "connection"}), 200

        output_dir = qvd_output_dir(session_id)
        try:
            config = merge_connection_config(output_dir, payload.get("config") or payload)
        except Exception as exc:
            error = str(exc) or "Databricks connection configuration could not be loaded."
            return jsonify({"session_id": session_id, "success": False, "status": "failed", "error": error, "errors": [error], "stage": "connection"}), 200
        if config is None and mode == "generate_sql_only":
            config = DatabricksConnectionConfig()
        if config is None:
            return jsonify({
                "session_id": session_id,
                "success": False,
                "status": "failed",
                "error": "Databricks connection configuration not found.",
                "errors": ["Databricks connection configuration not found."],
                "stage": "connection",
            }), 200

        try:
            connection_result = {"success": True, "errors": [], "checks": {}} if mode == "generate_sql_only" else test_databricks_connection(config)
        except Exception as exc:
            message = str(exc) or "Databricks connection test failed."
            if isinstance(exc, TimeoutError) or isinstance(exc, socket.timeout) or "timed out" in message.lower() or "timeout" in message.lower():
                message = "Databricks statement is still running or warehouse startup timed out. Retry after warehouse is running."
            connection_result = {"success": False, "errors": [message], "checks": {}}
        if mode != "generate_sql_only" and not connection_result.get("success"):
            errors = ["Databricks connection test failed.", *(connection_result.get("errors") or [])]
            return jsonify({
                "session_id": session_id,
                "success": False,
                "status": "failed",
                "error": errors[0],
                "errors": errors,
                "stage": "connection",
                "connection": connection_result,
            }), 200

        try:
            result = execute_qvd_migration(
                output_dir,
                target_table,
                mode,
                config,
                connection_result=connection_result,
                session_id=session_id,
                create_schema=bool(payload.get("create_schema") or payload.get("createSchema")),
                create_volume=bool(payload.get("create_volume") or payload.get("createVolume")),
            )
        except Exception as exc:
            message = str(exc) or "Databricks execution failed."
            if isinstance(exc, TimeoutError) or isinstance(exc, socket.timeout) or "timed out" in message.lower() or "timeout" in message.lower():
                message = "Databricks statement is still running or warehouse startup timed out. Retry after warehouse is running."
            logger.error("QVD Databricks execution failed for session=%s table=%s stage=connection error=%s", session_id, target_table, message)
            now = datetime.utcnow().isoformat()
            report = {
                "target_table": target_table,
                "source_row_count": None,
                "loaded_row_count": None,
                "loaded_rows": None,
                "row_count_match": None,
                "execution_status": "failed",
                "status": "failed",
                "execution_mode": mode,
                "statements_executed": [],
                "uploaded_files": 0,
                "databricks_readable_path": "",
                "start_time": now,
                "end_time": now,
                "duration_seconds": 0,
                "warnings": [],
                "errors": [message],
            }
            artifacts = write_execution_artifacts(output_dir, report, [message])
            result = {
                "success": False,
                "status": "failed",
                "error": message,
                "report": report,
                "logs": [message],
                "errors": [message],
                "stage": "connection",
                "artifacts": artifacts,
                "connection": connection_result,
            }
        return jsonify({
            "session_id": session_id,
            **result,
            "created_at": datetime.utcnow().isoformat(),
        }), 200

    @qvd_bp.route("/download-migration-package/<session_id>", methods=["GET"])
    def qvd_download_migration_package(session_id):
        package_path = qvd_package_zip_path(session_id)
        if use_configured_storage and not os.path.exists(package_path):
            package_path = os.path.join(qvd_output_dir(session_id), "migration_package", "migration_package.zip")
        if not os.path.exists(package_path):
            return jsonify({"error": "Migration package zip not found."}), 404
        return send_file(package_path, as_attachment=True, download_name="migration_package.zip")

    @qvd_bp.route("/download-artifact/<session_id>/<path:artifact_name>", methods=["GET"])
    def qvd_download_artifact(session_id, artifact_name):
        safe_name = os.path.normpath(artifact_name).replace("\\", "/")
        if safe_name.startswith("../") or safe_name.startswith("/") or "/../" in safe_name:
            return jsonify({"error": "Invalid artifact path."}), 400
        output_dir = os.path.abspath(qvd_output_dir(session_id))
        artifact_path = os.path.abspath(os.path.join(output_dir, safe_name))
        if not artifact_path.startswith(output_dir + os.sep):
            return jsonify({"error": "Invalid artifact path."}), 400
        if not os.path.exists(artifact_path) or not os.path.isfile(artifact_path):
            return jsonify({"error": "Artifact not found."}), 404
        return send_file(artifact_path, as_attachment=True, download_name=os.path.basename(artifact_path))

    app.register_blueprint(qvd_bp, url_prefix="/api/qvd")
    qvd_routes = sorted(str(rule) for rule in app.url_map.iter_rules() if str(rule).startswith("/api/qvd/"))
    logger.info("Registered QVD routes (%d): %s", len(qvd_routes), ", ".join(qvd_routes))
    return qvd_routes
