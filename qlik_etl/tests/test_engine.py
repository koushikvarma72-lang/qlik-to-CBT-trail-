import unittest

from qlik_etl import QlikToSqlEngine
from qlik_etl.errors import QlikParserError


class QlikEtlEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = QlikToSqlEngine(dialect_name='spark')

    def test_parse_simple_load(self):
        script = """
        Customers:
        LOAD
            [Customer Number],
            Customer,
            Region
        FROM CustomerMaster;
        """
        parsed = self.engine.parse(script)
        self.assertEqual(len(parsed.statements), 1)
        self.assertEqual(parsed.statements[0].type, 'LOAD')
        self.assertEqual(parsed.statements[0].target_table, 'Customers')
        self.assertEqual(parsed.statements[0].source, 'CustomerMaster')
        self.assertEqual(parsed.statements[0].fields, ['[Customer Number]', 'Customer', 'Region'])

    def test_generate_sql_from_simple_load(self):
        script = """
        Customers:
        LOAD
            [Customer Number],
            Customer,
            Region
        FROM CustomerMaster;
        """
        sql = self.engine.generate_sql(script)
        self.assertIn('WITH', sql)
        self.assertIn('FROM "CustomerMaster"', sql)
        self.assertIn('SELECT', sql)
        self.assertIn('"Customer Number"', sql)

    def test_generate_sql_with_group_and_where(self):
        script = """
        SalesSummary:
        LOAD CustomerID, Sum(SalesAmount) AS TotalSales
        FROM Sales
        WHERE Country = 'US'
        GROUP BY CustomerID;
        """
        sql = self.engine.generate_sql(script)
        self.assertIn("WHERE Country = 'US'", sql)
        self.assertIn('GROUP BY CustomerID', sql)
        self.assertIn('SUM(SalesAmount) AS TotalSales', sql)

    def test_select_only_parses_as_other_statement(self):
        script = """
        Invalid:
        SELECT * FROM Customers;
        """
        parsed = self.engine.parse(script)
        self.assertEqual(len(parsed.statements), 1)
        self.assertEqual(parsed.statements[0].type, 'OTHER')
        self.assertIn('SELECT * FROM Customers', parsed.statements[0].raw)


if __name__ == '__main__':
    unittest.main()
