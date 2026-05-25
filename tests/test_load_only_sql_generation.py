import json
import unittest
from unittest.mock import Mock

from backend.qlik_script_parser import parse_qlik_load_script
from backend.qvf_runtime import _collect_decoded_script_candidates, _section_might_contain_script
from backend.sql_migration import extract_sql_generation_plan, render_sql_from_load_plan, request_migration, request_migration_with_validation, deduplicate_ctes


class LoadOnlySqlGenerationTests(unittest.TestCase):
    def test_simple_load_block(self):
        script = """
        Customers:
        LOAD
            [Customer Number],
            Customer,
            Region
        FROM CustomerMaster;
        """
        plan = extract_sql_generation_plan(script)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["fields"], ["[Customer Number]", "Customer", "Region"])
        self.assertEqual(plan[0]["source_tables"], ["CustomerMaster"])

    def test_load_with_resident(self):
        script = """
        SalesTemp:
        LOAD CustomerID, SalesAmount
        FROM [lib://sales.qvd];

        SalesSummary:
        LOAD CustomerID, Sum(SalesAmount) AS TotalSales
        RESIDENT SalesTemp
        GROUP BY CustomerID;
        """
        plan = extract_sql_generation_plan(script)
        self.assertEqual(len(plan), 2)
        self.assertIn("SalesTemp", plan[1]["source_tables"])
        self.assertIn("CustomerID", plan[1]["fields"][0])
        self.assertIn("GROUP BY", plan[1]["raw"].upper())

    def test_load_with_where(self):
        script = """
        ActiveCustomers:
        LOAD CustomerID, CustomerName
        FROM Customers
        WHERE Status = 'Active';
        """
        plan = extract_sql_generation_plan(script)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["filters"], ["Status = 'Active'"])

    def test_load_with_nested_expression_fields(self):
        script = """
        Budget:
        LOAD
            Region & '_' & Date(Addmonths(Month, 12), 'YYYYMM') as MonthlyRegionKey,
            Budget as [Budget Amount]
        FROM Budget;
        """
        plan = extract_sql_generation_plan(script)
        self.assertEqual(len(plan), 1)
        self.assertEqual(
            plan[0]["fields"],
            [
                "Region & '_' & Date(Addmonths(Month, 12), 'YYYYMM') as MonthlyRegionKey",
                "Budget as [Budget Amount]",
            ],
        )
        self.assertEqual(plan[0]["source_tables"], ["Budget"])

    def test_join_load(self):
        script = """
        Customers:
        LOAD CustID, CustName
        FROM Customers;

        LEFT JOIN (Customers)
        LOAD CustID, CustAddress
        FROM Addresses;
        """
        plan = extract_sql_generation_plan(script)
        self.assertEqual(len(plan), 2)
        self.assertEqual(plan[1]["joinType"], "JOIN")
        self.assertIn("Customers", plan[1]["source_tables"])
        self.assertIn("Addresses", plan[1]["source_tables"])

    def test_preceding_load_chain(self):
        script = """
        Temp:
        LOAD A, B
        FROM SourceA;

        Final:
        LOAD A, Sum(B) AS TotalB
        RESIDENT Temp
        GROUP BY A;
        """
        plan = extract_sql_generation_plan(script)
        self.assertEqual(len(plan), 2)
        self.assertEqual(plan[0]["table"], "Temp")
        self.assertEqual(plan[1]["table"], "Final")

    def test_metadata_only_returns_no_sql(self):
        metadata_only = '{"qInfo":{"qId":"XFhtUb","qType":"dimension"},"qDim":{"qFieldDefs":["Sales Rep Name"]}}'
        ai = Mock(side_effect=AssertionError("AI should not be called for metadata-only content"))
        result = request_migration(ai, metadata_only)
        self.assertEqual(result, "")
        ai.assert_not_called()

    def test_set_let_only_returns_no_sql(self):
        script = "SET vToday = Today();\nLET vRegion = 'North';"
        ai = Mock(side_effect=AssertionError("AI should not be called for SET/LET-only content"))
        result = request_migration(ai, script)
        self.assertEqual(result, "")
        ai.assert_not_called()

    def test_select_only_returns_no_sql(self):
        script = "SQL SELECT * FROM Customers;"
        ai = Mock(side_effect=AssertionError("AI should not be called for SELECT-only content"))
        result = request_migration(ai, script)
        self.assertEqual(result, "")
        ai.assert_not_called()

    def test_request_migration_iterates_until_semantic_match(self):
        script = """
        SalesSummary:
        LOAD
            CustomerID,
            Sum(SalesAmount) AS TotalSales
        FROM Sales
        WHERE Country = 'US'
        GROUP BY CustomerID;
        """

        first_response = """### SQL
        SELECT CustomerID, SUM(SalesAmount) AS TotalSales
        FROM Sales
        GROUP BY CustomerID;
        ### DESCRIPTION
        First draft without the Country filter.
        """

        second_response = """### SQL
        SELECT CustomerID, SUM(SalesAmount) AS TotalSales
        FROM Sales
        WHERE Country = 'US'
        GROUP BY CustomerID;
        ### DESCRIPTION
        Corrected draft with the filter preserved.
        """

        ai = Mock(side_effect=[first_response, second_response])
        result = request_migration(ai, script)

        self.assertIn("WHERE Country = 'US'", result)
        self.assertEqual(ai.call_count, 2)

    def test_request_migration_with_validation_returns_structured_summary(self):
        script = """
        SalesSummary:
        LOAD
            CustomerID,
            Sum(SalesAmount) AS TotalSales
        FROM Sales
        WHERE Country = 'US'
        GROUP BY CustomerID;
        """

        first_response = """### SQL
        SELECT CustomerID, SUM(SalesAmount) AS TotalSales
        FROM Sales
        GROUP BY CustomerID;
        ### DESCRIPTION
        First draft without the Country filter.
        """

        second_response = """### SQL
        SELECT CustomerID, SUM(SalesAmount) AS TotalSales
        FROM Sales
        WHERE Country = 'US'
        GROUP BY CustomerID;
        ### DESCRIPTION
        Corrected draft with the filter preserved.
        """

        ai = Mock(side_effect=[first_response, second_response])
        result = request_migration_with_validation(ai, script)

        self.assertEqual(result['status'], 'matched')
        self.assertEqual(result['iterations'], 2)
        self.assertTrue(result['comparison_summary']['matched'])
        self.assertIn("Country = 'US'", result['final_sql'])
        self.assertEqual(ai.call_count, 2)

    def test_render_sql_from_plan_preserves_sources_fields_and_filters(self):
        script = """
        SalesSummary:
        LOAD
            CustomerID,
            Sum(SalesAmount) AS TotalSales
        FROM [lib://Data/Sales.qvd] (qvd)
        WHERE Country = 'US'
        GROUP BY CustomerID;
        """
        plan = extract_sql_generation_plan(script)
        sql = render_sql_from_load_plan(plan)

        self.assertIn("CustomerID", sql)
        self.assertIn("SUM(SalesAmount) AS TotalSales", sql)
        self.assertIn("{{ source('raw', 'Sales') }}", sql)
        self.assertIn("WHERE Country = 'US'", sql)
        self.assertIn("GROUP BY CustomerID", sql)

    def test_request_migration_falls_back_to_deterministic_sql_when_ai_drifts(self):
        script = """
        SalesSummary:
        LOAD
            CustomerID,
            Sum(SalesAmount) AS TotalSales
        FROM [lib://Data/Sales.qvd] (qvd)
        WHERE Country = 'US'
        GROUP BY CustomerID;
        """
        bad_response = """### SQL
        SELECT * FROM source_table;
        ### DESCRIPTION
        Generic placeholder.
        """

        ai = Mock(return_value=bad_response)
        result = request_migration_with_validation(ai, script, max_iterations=1)

        self.assertEqual(result['status'], 'retry')
        self.assertIn("{{ source('raw', 'Sales') }}", result['final_sql'])
        self.assertIn("WHERE Country = 'US'", result['final_sql'])
        self.assertIn("GROUP BY CustomerID", result['final_sql'])

    def test_parser_only_counts_load_blocks(self):
        metadata_only = '{"qInfo":{"qId":"XFhtUb","qType":"dimension"},"qDim":{"qFieldDefs":["Sales Rep Name"]}}'
        parsed = parse_qlik_load_script(metadata_only)
        self.assertEqual(parsed["loadBlocks"], [])
        self.assertEqual(parsed["statements"], [])

    def test_qvf_script_candidate_filter_excludes_whole_json_metadata(self):
        metadata = {
            "sheets": [{"title": "Sales", "visualization": "table"}],
            "fields": ["LOAD", "FROM", "SELECT"],
        }
        self.assertFalse(_section_might_contain_script(json.dumps(metadata)))

    def test_qvf_script_candidate_filter_keeps_nested_script_leaf(self):
        script = "SET vPath = 'lib://raw';\nSales:\nLOAD CustomerID FROM [lib://raw/Sales.qvd] (qvd);"
        decoded_sections = [
            {
                "index": 0,
                "_decodedText": json.dumps({"app": "metadata catalog", "qScript": script}),
                "_decodedJsonObjects": [{"qScript": script}],
            }
        ]

        candidates = _collect_decoded_script_candidates(decoded_sections)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["source"], "decoded_gzjson_json.qScript")
        self.assertIn("LOAD CustomerID", candidates[0]["text"])

    def test_deduplicate_ctes_no_duplicates(self):
        sql = "WITH customers AS (SELECT * FROM raw) SELECT * FROM customers"
        self.assertEqual(deduplicate_ctes(sql), sql)

    def test_deduplicate_ctes_with_duplicates(self):
        sql = """
        WITH customers AS (
            SELECT * FROM raw_cust
        ),
        customers AS (
            SELECT * FROM customers
        )
        SELECT * FROM customers
        """
        deduped = deduplicate_ctes(sql)
        self.assertIn("customers AS (", deduped)
        self.assertIn("customers_v2 AS (", deduped)
        self.assertIn("SELECT * FROM customers_v2", deduped)


if __name__ == "__main__":
    unittest.main()
