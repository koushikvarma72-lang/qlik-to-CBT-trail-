"""Pluggable QVD row preview reader.

This module intentionally does not make a QVD row-reader package mandatory.
If a compatible reader is unavailable, callers receive a structured error
instead of an exception.
"""

from __future__ import annotations

import importlib
from datetime import date, datetime
from decimal import Decimal


def _json_safe(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _records_from_pandas_frame(frame, limit: int | None) -> tuple[list[str], list[dict]]:
    limited = frame.head(limit) if limit is not None else frame
    columns = [str(column) for column in limited.columns.tolist()]
    rows = []
    for record in limited.to_dict(orient="records"):
        rows.append({str(key): _json_safe(value) for key, value in record.items()})
    return columns, rows


def _try_pyqvd(file_path: str, limit: int | None) -> dict | None:
    try:
        module = importlib.import_module("pyqvd")
    except ImportError:
        return None

    table_cls = getattr(module, "QvdTable", None)
    if table_cls is None:
        return None

    table = table_cls.from_qvd(file_path)
    if hasattr(table, "to_pandas"):
        columns, rows = _records_from_pandas_frame(table.to_pandas(), limit)
    elif hasattr(table, "to_dataframe"):
        columns, rows = _records_from_pandas_frame(table.to_dataframe(), limit)
    else:
        raw_rows = getattr(table, "rows", [])
        if limit is not None:
            raw_rows = raw_rows[:limit]
        columns = [str(column) for column in getattr(table, "columns", [])]
        rows = [
            {columns[index]: _json_safe(value) for index, value in enumerate(row)}
            if not isinstance(row, dict)
            else {str(key): _json_safe(value) for key, value in row.items()}
            for row in raw_rows
        ]
        if not columns and rows:
            columns = list(rows[0].keys())

    return {
        "success": True,
        "columns": columns,
        "rows": rows,
        "row_count_returned": len(rows),
        "limit": limit,
        "reader_used": "pyqvd",
        "error": None,
    }


def _try_qvd_reader(file_path: str, limit: int | None) -> dict | None:
    try:
        module = importlib.import_module("qvd")
    except ImportError:
        return None

    reader = getattr(module, "read", None) or getattr(module, "read_qvd", None)
    if reader is None:
        return None

    data = reader(file_path)
    if hasattr(data, "head") and hasattr(data, "to_dict"):
        columns, rows = _records_from_pandas_frame(data, limit)
    else:
        raw_rows = list(data or [])
        if limit is not None:
            raw_rows = raw_rows[:limit]
        rows = [
            {str(key): _json_safe(value) for key, value in row.items()}
            if isinstance(row, dict)
            else {str(index): _json_safe(value) for index, value in enumerate(row)}
            for row in raw_rows
        ]
        columns = list(rows[0].keys()) if rows else []

    return {
        "success": True,
        "columns": columns,
        "rows": rows,
        "row_count_returned": len(rows),
        "limit": limit,
        "reader_used": "qvd",
        "error": None,
    }


def _read_qvd_rows(file_path: str, limit: int | None) -> dict:
    readers = (_try_pyqvd, _try_qvd_reader)

    try:
        for reader in readers:
            result = reader(file_path, limit)
            if result is not None:
                return result
    except Exception as exc:
        return {
            "success": False,
            "columns": [],
            "rows": [],
            "row_count_returned": 0,
            "limit": limit,
            "reader_used": None,
            "error": f"QVD row preview failed: {exc}",
        }

    return {
        "success": False,
        "columns": [],
        "rows": [],
        "row_count_returned": 0,
        "limit": limit,
        "reader_used": None,
        "error": "No compatible Python QVD row reader is installed. Install a supported QVD reader package to enable row preview.",
    }


def preview_qvd_rows(file_path: str, limit: int = 100) -> dict:
    safe_limit = max(1, min(int(limit or 100), 10000))
    return _read_qvd_rows(file_path, safe_limit)


def read_qvd_rows(file_path: str) -> dict:
    return _read_qvd_rows(file_path, None)
