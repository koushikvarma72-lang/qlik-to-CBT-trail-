"""Convert QVD rows to local Parquet using an approved mapping contract."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import uuid
from datetime import date, datetime, timedelta

from qvd_to_databricks import qvd_row_reader


AUDIT_COLUMNS = [
    "_source_file_name",
    "_source_file_path",
    "_ingestion_timestamp",
    "_batch_id",
    "_record_hash",
]


def qlik_serial_to_iso_date(value):
    if value in (None, ""):
        return None
    try:
        return (date(1899, 12, 30) + timedelta(days=int(float(value)))).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def load_approved_mapping(approved_mapping_path: str) -> list[dict]:
    if not os.path.exists(approved_mapping_path):
        raise FileNotFoundError("Approved mapping artifact not found.")
    with open(approved_mapping_path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _mapping_rows_for_file(mapping_rows: list[dict], file_name: str) -> list[dict]:
    exact = [row for row in mapping_rows if str(row.get("qvd_file") or "") == file_name]
    return exact or mapping_rows


def _require_pandas():
    try:
        import pandas as pd
        return pd
    except ImportError as exc:
        raise RuntimeError("Pandas is required for QVD to Parquet conversion. Install it with `pip install pandas`.") from exc


def _ensure_parquet_engine():
    try:
        import pyarrow  # noqa: F401
        return
    except ImportError:
        pass
    try:
        import fastparquet  # noqa: F401
        return
    except ImportError as exc:
        raise RuntimeError("A Parquet engine is required. Install pyarrow with `pip install pyarrow`.") from exc


def _cast_series(series, conversion_rule: str, target_type: str, warnings: list[str]):
    pd = _require_pandas()
    rule = str(conversion_rule or "").strip()
    target = str(target_type or "").strip().upper()

    try:
        if rule == "cast_string":
            return series.astype("string")
        if rule == "cast_bigint":
            return pd.to_numeric(series, errors="coerce").astype("Int64")
        if rule == "cast_double":
            return pd.to_numeric(series, errors="coerce").astype("float64")
        if rule == "cast_decimal_18_2":
            return pd.to_numeric(series, errors="coerce").round(2)
        if rule == "qlik_serial_to_date":
            return pd.to_datetime(series.apply(qlik_serial_to_iso_date), errors="coerce").dt.date
        if rule == "flag_to_boolean_or_int_review":
            normalized = series.astype("string").str.strip().str.lower()
            if target == "BOOLEAN":
                return normalized.map({
                    "1": True,
                    "true": True,
                    "y": True,
                    "yes": True,
                    "0": False,
                    "false": False,
                    "n": False,
                    "no": False,
                }).astype("boolean")
            return pd.to_numeric(series, errors="coerce").astype("Int64")
        warnings.append(f"Unknown conversion rule '{rule}', preserving values as strings.")
        return series.astype("string")
    except Exception as exc:
        warnings.append(f"Conversion rule '{rule}' failed for target type '{target}': {exc}")
        return series


def _record_hash(row: dict, columns: list[str]) -> str:
    payload = json.dumps({column: row.get(column) for column in columns}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _safe_table_name(value: str) -> str:
    text = str(value or "").strip().replace("/", "_").replace("\\", "_")
    return text or "unnamed_table"


def convert_qvd_to_parquet(file_path: str, approved_mapping_path: str, output_dir: str, batch_id: str | None = None) -> dict:
    warnings: list[str] = []
    errors: list[str] = []
    batch = batch_id or str(uuid.uuid4())

    try:
        pd = _require_pandas()
        _ensure_parquet_engine()
        mapping_rows = _mapping_rows_for_file(load_approved_mapping(approved_mapping_path), os.path.basename(file_path))
        if not mapping_rows:
            raise ValueError("Approved mapping has no rows.")

        target_tables = sorted({str(row.get("target_table") or "").strip() for row in mapping_rows if str(row.get("target_table") or "").strip()})
        if len(target_tables) != 1:
            raise ValueError("Parquet conversion currently expects one target_table per QVD file.")
        target_table = target_tables[0]

        qvd_result = qvd_row_reader.read_qvd_rows(file_path)
        if not qvd_result.get("success"):
            raise RuntimeError(qvd_result.get("error") or "QVD row reader failed.")

        source_df = pd.DataFrame(qvd_result.get("rows") or [], columns=qvd_result.get("columns") or None)
        target_df = pd.DataFrame()

        for row in mapping_rows:
            source_column = str(row.get("source_column") or "").strip()
            target_column = str(row.get("target_column") or "").strip()
            if not source_column or not target_column:
                warnings.append(f"Skipped mapping with missing source or target column: {row}")
                continue
            if source_column not in source_df.columns:
                warnings.append(f"Source column '{source_column}' not found in QVD rows; output column '{target_column}' will be null.")
                target_df[target_column] = None
                continue
            target_df[target_column] = _cast_series(
                source_df[source_column],
                row.get("conversion_rule"),
                row.get("target_type"),
                warnings,
            )

        data_columns = list(target_df.columns)
        ingestion_timestamp = datetime.utcnow().isoformat()
        target_df["_source_file_name"] = os.path.basename(file_path)
        target_df["_source_file_path"] = file_path
        target_df["_ingestion_timestamp"] = ingestion_timestamp
        target_df["_batch_id"] = batch
        target_df["_record_hash"] = [
            _record_hash(record, data_columns)
            for record in target_df[data_columns].to_dict(orient="records")
        ]

        table_dir = os.path.join(output_dir, _safe_table_name(target_table))
        os.makedirs(table_dir, exist_ok=True)
        parquet_path = os.path.join(table_dir, f"part-{batch}.parquet")
        target_df.to_parquet(parquet_path, index=False)

        return {
            "success": True,
            "target_table": target_table,
            "row_count": int(len(target_df)),
            "column_count": int(len(target_df.columns)),
            "parquet_path": parquet_path,
            "conversion_warnings": warnings,
            "errors": errors,
            "batch_id": batch,
        }
    except Exception as exc:
        return {
            "success": False,
            "target_table": "",
            "row_count": 0,
            "column_count": 0,
            "parquet_path": "",
            "conversion_warnings": warnings,
            "errors": [str(exc)],
            "batch_id": batch,
        }
