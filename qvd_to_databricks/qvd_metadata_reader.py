"""Header-only QVD metadata reader.

QVD files begin with an XML metadata header followed by binary symbol/data
pages. This module stops after ``</QvdTableHeader>`` and never attempts to read
row-level data.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET


HEADER_END = b"</QvdTableHeader>"
HEADER_START = b"<QvdTableHeader"


def _local_name(tag: str) -> str:
    return str(tag or "").rsplit("}", 1)[-1]


def _child_text(node: ET.Element | None, name: str, default: str = "") -> str:
    if node is None:
        return default
    for child in list(node):
        if _local_name(child.tag) == name:
            return (child.text or "").strip()
    return default


def _child(node: ET.Element | None, name: str) -> ET.Element | None:
    if node is None:
        return None
    for child in list(node):
        if _local_name(child.tag) == name:
            return child
    return None


def _number_format(field_node: ET.Element) -> dict:
    node = _child(field_node, "NumberFormat")
    if node is None:
        return {}
    result = {}
    for child in list(node):
        name = _local_name(child.tag)
        value = (child.text or "").strip()
        if name and value:
            result[name] = value
    return result


def _tags(field_node: ET.Element) -> list[str]:
    node = _child(field_node, "Tags")
    if node is None:
        return []
    tags = []
    for child in list(node):
        value = (child.text or "").strip()
        if value:
            tags.append(value)
    return tags


def _read_header_xml(file_path: str, max_scan_bytes: int = 16 * 1024 * 1024) -> bytes:
    header = bytearray()
    with open(file_path, "rb") as handle:
        while len(header) < max_scan_bytes:
            chunk = handle.read(64 * 1024)
            if not chunk:
                break
            header.extend(chunk)
            end_index = header.find(HEADER_END)
            if end_index >= 0:
                end_index += len(HEADER_END)
                start_index = header.find(HEADER_START)
                if start_index < 0:
                    raise ValueError("QVD XML metadata header start was not found.")
                return bytes(header[start_index:end_index])
    raise ValueError("QVD XML metadata header was not found.")


def read_qvd_metadata(file_path: str) -> dict:
    """Read QVD table metadata from the XML header only."""
    header_xml = _read_header_xml(file_path)
    root = ET.fromstring(header_xml.decode("utf-8", errors="replace").lstrip("\ufeff\x00"))

    fields_node = _child(root, "Fields")
    field_nodes = [
        node for node in list(fields_node or [])
        if _local_name(node.tag) == "QvdFieldHeader"
    ]

    fields = []
    for index, field_node in enumerate(field_nodes, start=1):
        fields.append({
            "position": index,
            "field_name": _child_text(field_node, "FieldName"),
            "tags": _tags(field_node),
            "number_format": _number_format(field_node),
            "bit_offset": _child_text(field_node, "BitOffset"),
            "bit_width": _child_text(field_node, "BitWidth"),
            "bias": _child_text(field_node, "Bias"),
            "no_of_symbols": _child_text(field_node, "NoOfSymbols"),
            "offset": _child_text(field_node, "Offset"),
            "length": _child_text(field_node, "Length"),
        })

    return {
        "file_name": os.path.basename(file_path),
        "file_path": file_path,
        "file_size_bytes": os.path.getsize(file_path),
        "table_name": _child_text(root, "TableName") or os.path.splitext(os.path.basename(file_path))[0],
        "no_of_records": _child_text(root, "NoOfRecords"),
        "field_count": len(fields),
        "creator_doc": _child_text(root, "CreatorDoc"),
        "create_utc_time": _child_text(root, "CreateUtcTime"),
        "source_create_utc_time": _child_text(root, "SourceCreateUtcTime"),
        "fields": fields,
    }
