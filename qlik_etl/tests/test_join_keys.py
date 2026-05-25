import unittest

from qlik_etl import QlikToSqlEngine


class JoinKeyInferenceTests(unittest.TestCase):
    def setUp(self):
        self.engine = QlikToSqlEngine()

    def test_join_infers_keys_and_renders_on_clause(self):
        script = """
        Customers:
        LOAD
            CustID,
            CustName
        FROM Customers;

        LEFT JOIN (Customers)
        LOAD
            CustID,
            CustAddress
        FROM Addresses;
        """
        sql = self.engine.generate_sql(script)
        self.assertIn('LEFT JOIN', sql)
        self.assertIn('ON', sql)
        self.assertIn('CustID', sql)


if __name__ == '__main__':
    unittest.main()
