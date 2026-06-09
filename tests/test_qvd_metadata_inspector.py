import csv
import io
import json
import os
import tempfile
import unittest

from flask import Flask

from backend.integrations.qvd_routes import register_qvd_routes
from qvd_to_databricks.qvd_inspector import infer_field_category, inspect_qvd_file, write_inspection_artifacts
from qvd_to_databricks.qvd_metadata_reader import read_qvd_metadata


def synthetic_qvd_bytes():
    xml = """<QvdTableHeader>
  <TableName>SalesFact</TableName>
  <NoOfRecords>42</NoOfRecords>
  <CreatorDoc>UnitTest.qvw</CreatorDoc>
  <CreateUtcTime>2026-01-01T00:00:00Z</CreateUtcTime>
  <SourceCreateUtcTime>2025-12-31T00:00:00Z</SourceCreateUtcTime>
  <Fields>
    <QvdFieldHeader>
      <FieldName>OrderDate</FieldName>
      <Tags><String>$date</String><String>$numeric</String></Tags>
      <NumberFormat><Type>DATE</Type><Fmt>YYYY-MM-DD</Fmt></NumberFormat>
      <BitOffset>0</BitOffset>
      <BitWidth>10</BitWidth>
      <Bias>0</Bias>
      <NoOfSymbols>7</NoOfSymbols>
      <Offset>128</Offset>
      <Length>64</Length>
    </QvdFieldHeader>
    <QvdFieldHeader>
      <FieldName>CustomerID</FieldName>
      <Tags><String>$ascii</String><String>$text</String></Tags>
      <NumberFormat><Type>UNKNOWN</Type></NumberFormat>
      <BitOffset>10</BitOffset>
      <BitWidth>8</BitWidth>
      <Bias>0</Bias>
      <NoOfSymbols>12</NoOfSymbols>
      <Offset>192</Offset>
      <Length>96</Length>
    </QvdFieldHeader>
    <QvdFieldHeader>
      <FieldName>IsActive</FieldName>
      <Tags><String>$numeric</String></Tags>
      <NumberFormat><Type>INTEGER</Type></NumberFormat>
      <BitOffset>18</BitOffset>
      <BitWidth>1</BitWidth>
      <Bias>0</Bias>
      <NoOfSymbols>2</NoOfSymbols>
      <Offset>288</Offset>
      <Length>16</Length>
    </QvdFieldHeader>
  </Fields>
</QvdTableHeader>"""
    return b"\x00\x00" + xml.encode("utf-8") + b"\x00binary-row-pages-not-read"


class QvdMetadataInspectorTests(unittest.TestCase):
    def test_qvd_xml_metadata_parsing_from_synthetic_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sales.qvd")
            with open(path, "wb") as handle:
                handle.write(synthetic_qvd_bytes())

            metadata = read_qvd_metadata(path)

        self.assertEqual(metadata["table_name"], "SalesFact")
        self.assertEqual(metadata["no_of_records"], "42")
        self.assertEqual(metadata["field_count"], 3)
        self.assertEqual(metadata["creator_doc"], "UnitTest.qvw")
        self.assertEqual(metadata["fields"][0]["field_name"], "OrderDate")
        self.assertEqual(metadata["fields"][0]["tags"], ["$date", "$numeric"])
        self.assertEqual(metadata["fields"][0]["number_format"]["Type"], "DATE")

    def test_category_inference_priority(self):
        field = {
            "field_name": "IsCustomerDateKey",
            "tags": ["$numeric", "$text"],
            "number_format": {"Type": "INTEGER"},
        }
        self.assertEqual(infer_field_category(field), "DATE_LIKE")

        flag_over_key = {
            "field_name": "is_customer_id_enabled",
            "tags": ["$numeric"],
            "number_format": {"Type": "INTEGER"},
        }
        self.assertEqual(infer_field_category(flag_over_key), "KEY_LIKE")

    def test_low_cardinality_text_field_stays_text_like(self):
        field = {
            "field_name": "Franchise",
            "tags": ["$ascii", "$text"],
            "number_format": {"Type": "UNKNOWN"},
            "no_of_symbols": "2",
        }
        self.assertEqual(infer_field_category(field), "TEXT_LIKE")

    def test_flag_named_fields_remain_flag_like(self):
        actual_flag = {
            "field_name": "ActualFlag",
            "tags": ["$numeric"],
            "number_format": {"Type": "INTEGER"},
            "no_of_symbols": "2",
        }
        growth_flag = {
            "field_name": "GrowthContributorFlag",
            "tags": ["$numeric"],
            "number_format": {"Type": "INTEGER"},
            "no_of_symbols": "2",
        }
        self.assertEqual(infer_field_category(actual_flag), "FLAG_LIKE")
        self.assertEqual(infer_field_category(growth_flag), "FLAG_LIKE")

    def test_source_structure_csv_artifact_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            qvd_path = os.path.join(tmp, "sales.qvd")
            with open(qvd_path, "wb") as handle:
                handle.write(synthetic_qvd_bytes())

            table = inspect_qvd_file(qvd_path)
            artifacts = write_inspection_artifacts(
                "session-1",
                os.path.join(tmp, "qvd_outputs"),
                [{"file_name": "sales.qvd", "file_path": qvd_path, "file_size_bytes": os.path.getsize(qvd_path)}],
                [table],
                [],
            )

            with open(artifacts["source_structure_csv"], newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))

            with open(artifacts["inspection_json"], encoding="utf-8") as handle:
                payload = json.load(handle)

        self.assertEqual(payload["session_id"], "session-1")
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["qvd_file"], "sales.qvd")
        self.assertEqual(rows[0]["inferred_category"], "DATE_LIKE")
        self.assertEqual(rows[1]["inferred_category"], "KEY_LIKE")
        self.assertEqual(rows[2]["inferred_category"], "FLAG_LIKE")

    def test_upload_inspect_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Flask(__name__)
            register_qvd_routes(app, tmp)
            client = app.test_client()

            response = client.post(
                "/api/qvd/upload-inspect",
                data={
                    "session_id": "route-session",
                    "files": (io.BytesIO(synthetic_qvd_bytes()), "sales.qvd"),
                },
                content_type="multipart/form-data",
            )

            payload = response.get_json()
            output_dir = os.path.join(tmp, "route-session", "qvd_outputs")
            inspection_exists = os.path.exists(os.path.join(output_dir, "qvd_inspection.json"))
            csv_exists = os.path.exists(os.path.join(output_dir, "source_structure.csv"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["session_id"], "route-session")
        self.assertEqual(len(payload["uploaded_files"]), 1)
        self.assertEqual(payload["tables"][0]["summary"]["table_name"], "SalesFact")
        self.assertTrue(inspection_exists)
        self.assertTrue(csv_exists)


if __name__ == "__main__":
    unittest.main()
