"""
test_sql_generation_improvements.py
====================================
Tests for the improvements made in the senior review pass:

1. Typed NULLs in CONCATENATE UNION ALL branches
2. Property-based date transform equivalence
3. Month() → TO_CHAR abbreviated name (not MONTHNAME)
4. Migration telemetry dataclass
5. Validator pass 8 — MONTHNAME and missing dimension joins
"""

import time
import unittest
from unittest.mock import Mock, patch

from backend.migration.sql_generation import (
    _audit_generated_sql_against_plan,
    _invoke_ai_text,
    _infer_sql_type_from_name,
    _typed_null,
    _translate_qlik_expression_to_sql,
    build_fast_sql_generation_prompt,
    build_sql_generation_prompt,
    compare_descriptions,
    detect_repair_regressions,
    enforce_expenses_account_join,
    finalize_generated_sql,
    ONE_SHOT_MAX_TOKENS,
    LOOP_MAX_TOKENS,
    REPAIR_MAX_TOKENS,
    MIN_REQUIRED_OUTPUT_TOKENS,
    MigrationTokenBudgetError,
    needs_sql_repair,
    parse_migration_response,
    render_sql_from_load_plan,
    request_migration_one_shot,
    validate_candidate_integrity,
    validate_generated_sql,
    validation_issue_category,
)
from backend.migration.telemetry import MigrationTelemetry
from backend.migration.validator import validate_migration_sql


# ─── Helpers ─────────────────────────────────────────────────────────────────

def make_load(table, fields, source='src.qvd', source_type='from', sources=None,
              is_concat=False, concat_target=None):
    return {
        'table': table, 'operation': 'LOAD', 'fields': fields,
        'source': source, 'source_type': source_type,
        'source_tables': sources or [source],
        'filters': [], 'is_concatenate': is_concat,
        'concatenate_target': concat_target, 'drop_fields': [], 'raw': '',
    }


def make_drop(table, cols):
    return {
        'table': table, 'operation': 'DROP_FIELDS', 'fields': [],
        'drop_fields': cols, 'source': table, 'source_type': 'resident',
        'source_tables': [table], 'filters': [],
        'is_concatenate': False, 'concatenate_target': None, 'raw': '',
    }


class CandidateIntegrityTests(unittest.TestCase):
    def test_duplicate_ctes_and_repair_suffixes_are_blocking(self):
        sql = """
{{ config(materialized='table') }}
WITH accountgroupmaster AS (SELECT 1 AS id),
accountgroupmaster AS (SELECT 2 AS id),
budget_v2 AS (SELECT 3 AS id)
SELECT * FROM accountgroupmaster
"""
        issues = validate_generated_sql(sql)
        self.assertTrue(any('DUPLICATE_CTE_NAME' in issue for issue in issues), issues)
        self.assertTrue(any('REPAIR_CTE_SUFFIX_LEAK' in issue for issue in issues), issues)
        self.assertTrue(any(validation_issue_category(issue) == 'compile_error' for issue in issues), issues)

    def test_rejects_leaked_qlik_functions_bad_cast_and_union_mismatch(self):
        sql = """
{{ config(materialized='table') }}
WITH facttable AS (
    SELECT if(Account > 0, Account, 0) AS Account,
           CAST(DATEADD(month, 24, OrderDate AS DATE)) AS BadDate
    FROM {{ source('raw', 'FactTable') }}
),
facttable_with_expenses AS (
    SELECT Account, BadDate FROM facttable
    UNION ALL
    SELECT Account FROM facttable
)
SELECT * FROM facttable
"""
        issues = validate_generated_sql(sql)
        self.assertTrue(any('UNRESOLVED_QLIK_FUNCTION' in issue for issue in issues), issues)
        self.assertTrue(any('INVALID_CAST_DATEADD_SYNTAX' in issue for issue in issues), issues)
        self.assertTrue(any('UNION_COLUMN_COUNT_MISMATCH' in issue for issue in issues), issues)

    def test_ignores_qlik_function_names_inside_comments(self):
        sql = """
{{ config(materialized='table') }}
WITH facttable AS (
    -- Qlik: Region & '_' & Date(Addmonths(YYYYMM, 12), 'YYYYMM')
    SELECT DATEADD(month, 12, TO_DATE(YYYYMM::varchar, 'YYYYMM')) AS yyyymm
    FROM {{ source('raw', 'FactTable') }}
)
SELECT * FROM facttable
"""
        issues = validate_generated_sql(sql)
        self.assertFalse(any('UNRESOLVED_QLIK_FUNCTION' in issue for issue in issues), issues)

    def test_validator_ignores_qlik_concat_operator_inside_comments(self):
        sql = """
{{ config(materialized='table') }}
WITH facttable AS (
    -- SOURCE FIELD REGISTRY: MonthlyRegionKey = Region & '_' & Date(Addmonths(YYYYMM, 12), 'YYYYMM')
    SELECT DATEADD(month, 12, TO_DATE(YYYYMM::varchar, 'YYYYMM')) AS yyyymm
    FROM {{ source('raw', 'FactTable') }}
)
SELECT * FROM facttable
"""
        codes = {issue.code for issue in validate_migration_sql(sql, dialect='dbt')}
        self.assertNotIn('SHELL_OPERATOR', codes)

    def test_rejects_raw_fact_final_select_when_final_mart_exists(self):
        sql = """
{{ config(materialized='table') }}
WITH facttable AS (SELECT 1 AS id),
final_mart AS (SELECT * FROM facttable)
SELECT * FROM facttable
"""
        issues = validate_generated_sql(sql)
        self.assertTrue(any('WRONG_FINAL_SELECT_SOURCE' in issue for issue in issues), issues)

    def test_allows_joined_fact_final_select_without_final_model(self):
        sql = """
{{ config(materialized='table') }}
WITH facttable_with_expenses AS (SELECT 1 AS id),
calendar AS (SELECT 1 AS id)
SELECT f.id, c.id AS calendar_id
FROM facttable_with_expenses f
LEFT JOIN calendar c ON f.id = c.id
"""
        issues = validate_generated_sql(sql)
        self.assertFalse(any('WRONG_FINAL_SELECT_SOURCE' in issue for issue in issues), issues)

    def test_rejects_unjoined_raw_fact_final_select_with_dimension_ctes(self):
        sql = """
{{ config(materialized='table') }}
WITH facttable_with_expenses AS (SELECT 1 AS id),
calendar AS (SELECT 1 AS id)
SELECT * FROM facttable_with_expenses
"""
        issues = validate_generated_sql(sql)
        self.assertTrue(any('WRONG_FINAL_SELECT_SOURCE' in issue for issue in issues), issues)

    def test_rejects_fact_expenses_union_that_drops_expense_metrics(self):
        sql = """
{{ config(materialized='table') }}
WITH expenses AS (
  SELECT MonthlyRegionKey, Account, ExpenseActual, ExpenseBudget FROM raw_expenses
),
facttable_with_expenses AS (
  SELECT MonthlyRegionKey, Region, YYYYMM FROM facttable
  UNION ALL
  SELECT MonthlyRegionKey, Region, YYYYMM FROM expenses
)
SELECT * FROM facttable_with_expenses
"""
        issues = validate_generated_sql(sql)
        self.assertTrue(any('FACT_EXPENSES_FIELDS_MISSING' in issue for issue in issues), issues)

    def test_low_credit_fallback_returns_safe_skeleton_when_full_draft_is_corrupt(self):
        try:
            import flask  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest('Flask is not installed in this test environment')
        import backend.app as app_mod

        plan = [
            make_load(
                'FactTable',
                ['ID', 'if(Account > 0, Account, 0) AS Account'],
                source='FactTable.qvd',
                sources=['FactTable.qvd'],
            ),
            make_load(
                'FactTable',
                ['ID', 'if(Account > 0, Account, 0) AS Account'],
                source='FactTable.qvd',
                sources=['FactTable.qvd'],
            ),
        ]
        corrupt = """
{{ config(materialized='table') }}
WITH facttable AS (SELECT if(Account > 0, Account, 0) AS Account FROM {{ source('raw','FactTable') }}),
facttable AS (SELECT 1 AS x)
SELECT * FROM facttable
"""
        with patch.object(app_mod, 'render_sql_from_load_plan', return_value=corrupt):
            result = app_mod._deterministic_migration_result(
                'insufficient OpenRouter credits/token budget: requested 2500 output tokens, can only afford 329, minimum required is 1500.',
                'FactTable:\nLOAD ID FROM FactTable.qvd;',
                plan,
                dialect='dbt',
            )
        self.assertNotEqual(result['status'], 'failed')
        self.assertIn('final_model AS', result['sql'])
        self.assertNotIn('if(', result['sql'].lower())
        self.assertFalse(validate_candidate_integrity(result['sql']), result['sql'])


# ─── 1. Typed NULLs ──────────────────────────────────────────────────────────

class TestTypedNulls(unittest.TestCase):

    def test_infer_varchar_for_text_field(self):
        self.assertEqual(_infer_sql_type_from_name('CustomerName'), 'VARCHAR')
        self.assertEqual(_infer_sql_type_from_name('AccountDesc'), 'VARCHAR')
        self.assertEqual(_infer_sql_type_from_name('Region'), 'VARCHAR')

    def test_infer_number_for_amount_field(self):
        self.assertEqual(_infer_sql_type_from_name('SalesAmount'), 'NUMBER')
        self.assertEqual(_infer_sql_type_from_name('TotalCost'), 'NUMBER')
        self.assertEqual(_infer_sql_type_from_name('CustomerKey'), 'NUMBER')
        self.assertEqual(_infer_sql_type_from_name('ItemID'), 'NUMBER')

    def test_infer_date_for_date_field(self):
        self.assertEqual(_infer_sql_type_from_name('OrderDate'), 'DATE')
        self.assertEqual(_infer_sql_type_from_name('YYYYMM'), 'DATE')
        self.assertEqual(_infer_sql_type_from_name('CreatedAt'), 'DATE')

    def test_infer_boolean_for_flag_field(self):
        self.assertEqual(_infer_sql_type_from_name('IsActive'), 'BOOLEAN')
        self.assertEqual(_infer_sql_type_from_name('HasOrders'), 'BOOLEAN')

    def test_typed_null_format(self):
        self.assertEqual(_typed_null('SalesAmount'), 'CAST(NULL AS NUMBER) AS "SalesAmount"')
        self.assertEqual(_typed_null('CustomerName'), 'CAST(NULL AS VARCHAR) AS "CustomerName"')
        self.assertEqual(_typed_null('OrderDate'), 'CAST(NULL AS DATE) AS "OrderDate"')

    def test_concatenate_union_uses_typed_nulls(self):
        """UNION ALL branch must emit CAST(NULL AS type) not bare NULL."""
        plan = [
            make_load('FactTable',
                      ['MonthlyRegionKey', 'Region', 'YYYYMM', 'Account', 'SalesAmount'],
                      source='Sales.qvd'),
            make_load('FactTable', ['MonthlyRegionKey', 'Region', 'YYYYMM'],
                      source='Expenses', source_type='resident', sources=['Expenses'],
                      is_concat=True, concat_target='FactTable'),
        ]
        sql = render_sql_from_load_plan(plan)
        # Missing columns in the CONCATENATE branch must be typed NULLs
        self.assertIn('CAST(NULL AS', sql)
        self.assertIn('CAST(NULL AS NUMBER) AS "SalesAmount"', sql)
        self.assertIn('CAST(NULL AS VARCHAR) AS "Account"', sql)
        # The base branch must NOT have typed NULLs (it has all columns)
        self.assertIn('UNION ALL', sql)

    def test_no_bare_null_in_union_all(self):
        """Bare NULL AS "col" must not appear — only CAST(NULL AS type)."""
        plan = [
            make_load('T', ['A', 'B', 'C'], source='src.qvd'),
            make_load('T', ['A'], source='other', source_type='resident',
                      sources=['other'], is_concat=True, concat_target='T'),
        ]
        sql = render_sql_from_load_plan(plan)
        # Should not contain bare NULL (without CAST)
        import re
        bare_nulls = re.findall(r'(?<!CAST\()NULL AS "', sql)
        self.assertEqual(bare_nulls, [],
                         f'Found bare NULL AS in UNION ALL branch: {sql}')

    def test_audit_detects_missing_union_owned_column(self):
        """If final SQL uses f.Account, the union/fact CTE must expose Account."""
        plan = [
            make_load('FactTable', ['MonthlyRegionKey', 'SalesAmount'], source='Sales.qvd'),
            make_load('FactTable', ['MonthlyRegionKey', 'Account'],
                      source='Expenses', source_type='resident', sources=['Expenses'],
                      is_concat=True, concat_target='FactTable'),
        ]
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH fact_table AS (\n"
            "  SELECT MonthlyRegionKey, SalesAmount FROM {{ source('raw','Sales') }}\n"
            "), final AS (\n"
            "  SELECT f.Account FROM fact_table f\n"
            ")\nSELECT * FROM final"
        )
        issues = _audit_generated_sql_against_plan(sql, plan=plan, qvs_script='', dialect='dbt')
        self.assertTrue(any('COLUMN_OWNERSHIP_MISMATCH' in i for i in issues), issues)

    def test_prompt_includes_ownership_grain_and_layer_rules(self):
        script = """
        FactTable:
        LOAD Region, YYYYMM, SalesAmount
        FROM Sales.qvd;

        Expenses:
        LOAD Region, Account, YYYYMM, ExpenseActual
        FROM Expenses.qvd;
        """
        _system, prompt = build_sql_generation_prompt(script, dialect='dbt')
        self.assertIn('Qlik Ownership / Grain Contract', prompt)
        self.assertIn('FactTable', prompt)
        self.assertIn('Expenses', prompt)
        self.assertIn('grain', prompt.lower())

    def test_audit_rejects_direct_itemmaster_join(self):
        plan = [make_load('FactTable', ['Item-Branch Key'], source='Sales.qvd')]
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH fact_table AS (SELECT \"Item-Branch Key\" FROM {{ source('raw','Sales') }}),\n"
            "item_master AS (SELECT \"Short Name\" FROM {{ source('raw','ItemMaster') }}),\n"
            "final AS (\n"
            "  SELECT f.* FROM fact_table f\n"
            "  LEFT JOIN item_master im ON f.\"Item-Branch Key\" = im.\"Short Name\"\n"
            ")\nSELECT * FROM final"
        )
        issues = _audit_generated_sql_against_plan(sql, plan=plan, qvs_script='', dialect='dbt')
        self.assertTrue(any('WRONG_PRODUCT_JOIN_PATH' in i for i in issues), issues)

    def test_audit_rejects_expenses_monthlyregion_only_join(self):
        plan = [make_load('FactTable', ['MonthlyRegionKey', 'Account'], source='Sales.qvd')]
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH fact_table AS (SELECT MonthlyRegionKey, Account FROM {{ source('raw','Sales') }}),\n"
            "expenses AS (SELECT MonthlyRegionKey, Account FROM {{ source('raw','Expenses') }}),\n"
            "final AS (\n"
            "  SELECT f.* FROM fact_table f\n"
            "  LEFT JOIN expenses e ON f.MonthlyRegionKey = e.MonthlyRegionKey\n"
            ")\nSELECT * FROM final"
        )
        issues = _audit_generated_sql_against_plan(sql, plan=plan, qvs_script='', dialect='dbt')
        self.assertTrue(any('EXPENSES_GRAIN_JOIN_INCOMPLETE' in i for i in issues), issues)

    def test_validator_preserves_exact_hyphenated_source_names(self):
        plan = [make_load('ARSummary', ['CustKeyAR'], source='ARSummary-1', sources=['ARSummary-1'])]
        sql = "{{ config(materialized='table') }}\nSELECT CustKeyAR FROM {{ source('raw','ARSummary_1') }}"
        codes = {issue.code for issue in validate_migration_sql(sql, plan=plan, dialect='dbt')}
        self.assertIn('SOURCE_TABLE_RENAMED', codes)

    def test_validator_rejects_single_brace_dbt_config(self):
        sql = "{ config(materialized='table', tags=['qlik_migration']) }\nSELECT 1"
        codes = {issue.code for issue in validate_migration_sql(sql, dialect='dbt')}
        self.assertIn('MALFORMED_DBT_CONFIG', codes)

    def test_generator_validator_rejects_single_brace_dbt_config(self):
        sql = "{ config(materialized='table', tags=['qlik_migration']) }\nSELECT 1"
        issues = validate_generated_sql(sql, dialect='dbt')
        self.assertTrue(any('MALFORMED_DBT_CONFIG' in issue for issue in issues), issues)

    def test_source_table_comparison_uses_logical_identity(self):
        comparison = compare_descriptions(
            {
                'source_tables': ["'lib://Executive Dashboard Data/AccountGroupMaster.qvd'(qvd)"],
                'joins': [],
                'filters': [],
                'aggregations': [],
                'output_columns': [],
                'calculated_fields': [],
            },
            {
                'source_tables': ["{{ source('raw', 'AccountGroupMaster') }}"],
                'joins': [],
                'filters': [],
                'aggregations': [],
                'output_columns': [],
                'calculated_fields': [],
            },
        )
        self.assertTrue(comparison['matched'], comparison)
        self.assertEqual(comparison['score'], 1.0)

    def test_metadata_warnings_do_not_trigger_repair(self):
        issues = [
            'IR_AMBIGUITY: YYYYMM cannot determine storage type',
            'ISLAND_TABLE: Table AccountGroupMaster has no shared key',
            'SOURCE_TABLE_MISMATCH: formatting-only source name difference',
        ]
        self.assertFalse(needs_sql_repair(issues))
        self.assertEqual(validation_issue_category(issues[0]), 'metadata_warning')
        self.assertEqual(validation_issue_category(issues[1]), 'metadata_warning')
        self.assertEqual(validation_issue_category(issues[2]), 'metadata_warning')

    def test_compile_and_semantic_issues_still_trigger_repair(self):
        self.assertTrue(needs_sql_repair([
            'MALFORMED_DBT_CONFIG: dbt config block must use double Jinja braces'
        ]))
        self.assertTrue(needs_sql_repair([
            'JOIN_KEY_MISSING: f.Account does not exist on facttable_with_expenses'
        ]))

    def test_parse_response_normalizes_single_brace_dbt_config(self):
        parsed = parse_migration_response(
            "### SQL\n"
            "{ config(materialized='table', tags=['qlik_migration']) }\n"
            "SELECT 1\n"
            "### DESCRIPTION\n"
            "demo"
        )
        self.assertTrue(parsed['sql'].startswith("{{ config(materialized='table', tags=['qlik_migration']) }}"))

    def test_finalize_sql_normalizes_config_after_repair_output(self):
        sql = "{ config(materialized='table', tags=['qlik_migration']) }\nSELECT 1"
        finalized = finalize_generated_sql(sql)
        self.assertIn("{{ config(materialized='table', tags=['qlik_migration']) }}", finalized)
        self.assertNotRegex(finalized, r'(?m)^\s*\{\s*config\s*\(')

    def test_finalize_sql_normalizes_inline_malformed_config(self):
        sql = "{ config(materialized='table', tags=['qlik_migration']) } WITH x AS (SELECT 1 AS id) SELECT * FROM x"
        finalized = finalize_generated_sql(sql)
        self.assertTrue(finalized.startswith("{{ config(materialized='table', tags=['qlik_migration']) }}"))
        self.assertNotRegex(finalized, r'(?<!\{)\{\s*config\s*\(')

    def test_audit_rejects_key_to_descriptive_text_join(self):
        plan = [make_load('FactTable', ['CustKey'], source='Sales.qvd')]
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH fact_table AS (SELECT CustKey FROM {{ source('raw','Sales') }}),\n"
            "customer_master AS (SELECT Customer FROM {{ source('raw','CustomerMaster') }}),\n"
            "final AS (\n"
            "  SELECT f.* FROM fact_table f\n"
            "  LEFT JOIN customer_master cust ON f.\"CustKey\" = cust.\"Customer\"\n"
            ")\nSELECT * FROM final"
        )
        issues = _audit_generated_sql_against_plan(sql, plan=plan, qvs_script='', dialect='dbt')
        self.assertTrue(any('INVALID_KEY_TO_TEXT_JOIN' in i for i in issues), issues)

    def test_audit_requires_account_in_facttable_with_expenses_union(self):
        plan = [make_load('FactTable', ['MonthlyRegionKey', 'Account'], source='Sales.qvd')]
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable_with_expenses AS (\n"
            "  SELECT MonthlyRegionKey, SalesAmount FROM {{ source('raw','FactTable') }}\n"
            "  UNION ALL\n"
            "  SELECT MonthlyRegionKey, Account FROM {{ source('raw','Expenses') }}\n"
            ")\nSELECT * FROM facttable_with_expenses"
        )
        issues = _audit_generated_sql_against_plan(sql, plan=plan, qvs_script='', dialect='dbt')
        self.assertTrue(any('FACT_EXPENSES_ACCOUNT_MISSING' in i for i in issues), issues)
        self.assertTrue(any('UNION_COLUMN_ORDER_MISMATCH' in i for i in issues), issues)

    def test_audit_catches_join_key_hidden_by_select_star_union(self):
        plan = [make_load('FactTable', ['MonthlyRegionKey', 'Region'], source='FactTable.qvd')]
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable AS (\n"
            "  SELECT MonthlyRegionKey, Region FROM {{ source('raw','FactTable') }}\n"
            "),\n"
            "expenses_for_fact AS (\n"
            "  SELECT MonthlyRegionKey, Region, Account FROM {{ source('raw','Expenses') }}\n"
            "),\n"
            "facttable_with_expenses AS (\n"
            "  SELECT * FROM facttable\n"
            "  UNION ALL\n"
            "  SELECT MonthlyRegionKey, Region, Account FROM expenses_for_fact\n"
            "),\n"
            "final_model AS (\n"
            "  SELECT f.MonthlyRegionKey, e.Account\n"
            "  FROM facttable_with_expenses f\n"
            "  LEFT JOIN expenses_for_fact e\n"
            "    ON f.MonthlyRegionKey = e.MonthlyRegionKey\n"
            "   AND f.Account = e.Account\n"
            ")\n"
            "SELECT * FROM final_model"
        )
        issues = _audit_generated_sql_against_plan(sql, plan=plan, qvs_script='', dialect='dbt')
        self.assertTrue(any('JOIN_KEY_MISSING' in i for i in issues), issues)
        self.assertTrue(any('UNION_SELECT_STAR' in i for i in issues), issues)

    def test_finalize_expands_facttable_expenses_union_and_adds_account(self):
        sql = (
            "{ config(materialized='table', tags=['qlik_migration']) }\n"
            "WITH facttable AS (\n"
            "  SELECT\n"
            "    MonthlyRegionKey,\n"
            "    Region,\n"
            "    \"Address Number\",\n"
            "    CustKey,\n"
            "    YYYYMM\n"
            "  FROM {{ source('raw','FactTable') }}\n"
            "),\n"
            "expenses_for_fact AS (\n"
            "  SELECT MonthlyRegionKey, Region, Account, YYYYMM FROM {{ source('raw','Expenses') }}\n"
            "),\n"
            "facttable_with_expenses AS (\n"
            "  SELECT * FROM facttable\n"
            "  UNION ALL\n"
            "  SELECT MonthlyRegionKey, Region, Account, YYYYMM FROM expenses_for_fact\n"
            ")\n"
            "SELECT * FROM facttable_with_expenses"
        )
        finalized = finalize_generated_sql(sql)
        self.assertIn("{{ config(materialized='table', tags=['qlik_migration']) }}", finalized)
        self.assertIn('CAST(NULL AS VARCHAR) AS Account', finalized)
        self.assertNotRegex(finalized, r'UNION\s+ALL\s+SELECT\s+\*')
        self.assertRegex(finalized, r'facttable_with_expenses\s+AS\s*\(\s*SELECT\s+MonthlyRegionKey,')

    def test_finalize_expands_generic_union_star_and_completes_bare_select(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable_raw AS (\n"
            "  SELECT MonthlyRegionKey, Region, YYYYMM FROM {{ source('raw','FactTable') }}\n"
            "),\n"
            "expenses_raw AS (\n"
            "  SELECT MonthlyRegionKey, Region, Account, ExpenseActual FROM {{ source('raw','Expenses') }}\n"
            "),\n"
            "facttable_with_expenses AS (\n"
            "  SELECT * FROM facttable_raw\n"
            "  UNION ALL\n"
            "  SELECT MonthlyRegionKey, Region, Account, ExpenseActual FROM expenses_raw\n"
            "),\n"
            "final_model AS (\n"
            "  SELECT * FROM facttable_with_expenses\n"
            ")\n"
            "SELECT"
        )
        finalized = finalize_generated_sql(sql)
        self.assertNotIn('SELECT * FROM facttable_raw', finalized)
        self.assertIn('CAST(NULL AS VARCHAR) AS Account', finalized)
        self.assertIn('CAST(NULL AS NUMBER) AS ExpenseActual', finalized)
        self.assertTrue(finalized.rstrip().endswith('SELECT *\nFROM final_model'))
        self.assertFalse(any('UNION_SELECT_STAR' in issue for issue in validate_generated_sql(finalized)), finalized)

    def test_finalize_preserves_expense_metrics_in_fact_expenses_union(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable AS (\n"
            "  SELECT MonthlyRegionKey, Region, YYYYMM FROM {{ source('raw','FactTable') }}\n"
            "),\n"
            "expenses AS (\n"
            "  SELECT MonthlyRegionKey, Region, Account, YYYYMM, ExpenseActual, ExpenseBudget FROM {{ source('raw','Expenses') }}\n"
            "),\n"
            "facttable_with_expenses AS (\n"
            "  SELECT * FROM facttable\n"
            "  UNION ALL\n"
            "  SELECT MonthlyRegionKey, Region, YYYYMM FROM expenses\n"
            ")\n"
            "SELECT * FROM facttable_with_expenses"
        )
        finalized = finalize_generated_sql(sql)
        self.assertIn('CAST(NULL AS VARCHAR) AS Account', finalized)
        self.assertIn('CAST(NULL AS NUMBER) AS ExpenseActual', finalized)
        self.assertIn('CAST(NULL AS NUMBER) AS ExpenseBudget', finalized)
        self.assertIn('ExpenseActual', finalized)
        self.assertIn('ExpenseBudget', finalized)
        self.assertFalse(any('FACT_EXPENSES_FIELDS_MISSING' in issue for issue in validate_generated_sql(finalized)), finalized)

    def test_finalize_wraps_joined_fact_query_in_final_model(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable_with_expenses AS (\n"
            "  SELECT MonthlyRegionKey, YYYYMM FROM {{ source('raw','FactTable') }}\n"
            "),\n"
            "calendar AS (\n"
            "  SELECT YYYYMM, FiscalMonth FROM {{ source('raw','Calendar') }}\n"
            ")\n"
            "SELECT f.MonthlyRegionKey, c.FiscalMonth\n"
            "FROM facttable_with_expenses f\n"
            "LEFT JOIN calendar c ON f.YYYYMM = c.YYYYMM"
        )
        finalized = finalize_generated_sql(sql)
        self.assertIn('final_model AS (', finalized)
        self.assertTrue(finalized.rstrip().endswith('SELECT *\nFROM final_model'))
        self.assertFalse(any('WRONG_FINAL_SELECT_SOURCE' in issue for issue in validate_generated_sql(finalized)), finalized)

    def test_one_shot_returns_postprocessed_sql_without_ai_iteration(self):
        script = """
        FactTable:
        LOAD
            MonthlyRegionKey,
            Region,
            [Address Number],
            CustKey,
            YYYYMM
        FROM [lib://ExecutiveDashboardData/FactTable.qvd] (qvd);

        Expenses:
        LOAD
            MonthlyRegionKey,
            Region,
            Account,
            YYYYMM
        FROM [lib://ExecutiveDashboardData/Expenses.qvd] (qvd);
        """
        ai_response = (
            "### SQL\n"
            "{ config(materialized='table', tags=['qlik_migration']) }\n"
            "WITH facttable AS (\n"
            "  SELECT MonthlyRegionKey, Region, \"Address Number\", CustKey, YYYYMM FROM {{ source('raw','FactTable') }}\n"
            "),\n"
            "expenses_for_fact AS (\n"
            "  SELECT MonthlyRegionKey, Region, Account, YYYYMM FROM {{ source('raw','Expenses') }}\n"
            "),\n"
            "facttable_with_expenses AS (\n"
            "  SELECT * FROM facttable\n"
            "  UNION ALL\n"
            "  SELECT MonthlyRegionKey, Region, Account, YYYYMM FROM expenses_for_fact\n"
            ")\n"
            "SELECT * FROM facttable_with_expenses\n"
            "### DESCRIPTION\n"
            "demo"
        )
        ai = Mock(return_value=ai_response)
        result = request_migration_one_shot(ai, script)
        self.assertEqual(ai.call_count, 1)
        self.assertIn("{{ config(materialized='table', tags=['qlik_migration']) }}", result['final_sql'])
        self.assertIn('CAST(NULL AS VARCHAR) AS Account', result['final_sql'])
        self.assertNotRegex(result['final_sql'], r'UNION\s+ALL\s+SELECT\s+\*')
        self.assertFalse(any('UNION_SELECT_STAR' in issue for issue in result['validation_issues']), result['validation_issues'])

    def test_generation_mode_one_shot_never_enters_loop(self):
        try:
            import flask  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest('Flask is not installed in this test environment')
        import backend.app as app_mod

        one_shot_result = {
            'status': 'complete',
            'sql': '{{ config(materialized="table") }}\nWITH c AS (SELECT * FROM facttable UNION ALL SELECT * FROM expenses)\nSELECT * FROM c',
            'final_sql': '{{ config(materialized="table") }}\nWITH c AS (SELECT * FROM facttable UNION ALL SELECT * FROM expenses)\nSELECT * FROM c',
            'validation_issues': ['UNION_SELECT_STAR: UNION ALL branches must enumerate columns explicitly.'],
        }
        with patch.object(app_mod, 'request_migration_one_shot', return_value=one_shot_result) as one_shot, \
             patch.object(app_mod, 'request_migration_with_validation') as loop:
            result = app_mod.migrate_qvs_to_dbt(
                'FactTable:\nLOAD A FROM FactTable.qvd;',
                dialect='dbt',
                generation_mode='one_shot',
            )
        one_shot.assert_called_once()
        loop.assert_not_called()
        self.assertEqual(result['selected_generation_mode'], 'one_shot')
        self.assertIn(result['one_shot_validation_status'], {'blocking_issues', 'passed', 'passed_with_warnings'})

    def test_generation_mode_loop_skips_one_shot(self):
        try:
            import flask  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest('Flask is not installed in this test environment')
        import backend.app as app_mod

        loop_result = {'status': 'matched', 'final_sql': 'SELECT 1', 'sql': 'SELECT 1'}
        with patch.object(app_mod, 'request_migration_one_shot') as one_shot, \
             patch.object(app_mod, 'request_migration_with_validation', return_value=loop_result) as loop:
            result = app_mod.migrate_qvs_to_dbt(
                'FactTable:\nLOAD A FROM FactTable.qvd;',
                dialect='dbt',
                generation_mode='loop',
            )
        one_shot.assert_not_called()
        loop.assert_called_once()
        self.assertEqual(result['selected_generation_mode'], 'loop')
        self.assertEqual(result['reason_for_entering_loop'], 'explicit_loop_mode')

    def test_auto_mode_metadata_warnings_do_not_enter_loop(self):
        try:
            import flask  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest('Flask is not installed in this test environment')
        import backend.app as app_mod

        one_shot_result = {
            'status': 'complete',
            'sql': '{{ config(materialized="table") }}\nSELECT 1 AS id',
            'final_sql': '{{ config(materialized="table") }}\nSELECT 1 AS id',
            'validation_issues': [],
        }
        metadata_issues = [
            'IR_AMBIGUITY: cannot determine storage type',
            "ISLAND_TABLE: Table 'AccountGroupMaster' has no shared key",
        ]
        with patch.object(app_mod, 'request_migration_one_shot', return_value=one_shot_result) as one_shot, \
             patch.object(app_mod, '_audit_generated_sql_against_plan', return_value=metadata_issues), \
             patch.object(app_mod, 'request_migration_with_validation') as loop:
            result = app_mod.migrate_qvs_to_dbt(
                'FactTable:\nLOAD A FROM FactTable.qvd;',
                dialect='dbt',
                generation_mode='auto',
            )
        one_shot.assert_called_once()
        loop.assert_not_called()
        self.assertEqual(result['one_shot_validation_status'], 'passed_with_warnings')
        self.assertEqual(result['validation_issues'], metadata_issues)

    def test_validator_rejects_nullable_or_join_substitute(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH final_model AS (\n"
            "  SELECT 1 AS x\n"
            "  FROM facttable_with_expenses f\n"
            "  LEFT JOIN expenses exp\n"
            "    ON f.MonthlyRegionKey = exp.MonthlyRegionKey\n"
            "   AND (f.Region IS NOT NULL OR exp.Account IS NOT NULL)\n"
            ")\n"
            "SELECT * FROM final_model"
        )
        issues = validate_generated_sql(sql, dialect='dbt')
        self.assertTrue(any('INVALID_NULLABLE_OR_JOIN_PREDICATE' in i for i in issues), issues)
        self.assertTrue(needs_sql_repair(issues))

    def test_validator_rejects_account_is_not_null_join_substitute(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "SELECT * FROM facttable_with_expenses f\n"
            "LEFT JOIN expenses e ON f.MonthlyRegionKey = e.MonthlyRegionKey\n"
            " AND e.Account IS NOT NULL"
        )
        issues = validate_generated_sql(sql, dialect='dbt')
        self.assertTrue(any('INVALID_NULLABLE_ACCOUNT_JOIN_PREDICATE' in i for i in issues), issues)
        self.assertTrue(needs_sql_repair(issues))

    def test_finalize_rewrites_account_is_not_null_join_substitute(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable AS (\n"
            "  SELECT MonthlyRegionKey, Region, YYYYMM FROM {{ source('raw','FactTable') }}\n"
            "),\n"
            "expenses AS (\n"
            "  SELECT MonthlyRegionKey, Account, YYYYMM FROM {{ source('raw','Expenses') }}\n"
            "),\n"
            "facttable_with_expenses AS (\n"
            "  SELECT * FROM facttable\n"
            "  UNION ALL\n"
            "  SELECT MonthlyRegionKey, Account, YYYYMM FROM expenses\n"
            "),\n"
            "final_model AS (\n"
            "  SELECT f.MonthlyRegionKey FROM facttable_with_expenses f\n"
            "  LEFT JOIN expenses e ON f.MonthlyRegionKey = e.MonthlyRegionKey\n"
            "   AND e.Account IS NOT NULL\n"
            ")\n"
            "SELECT * FROM final_model"
        )
        finalized = finalize_generated_sql(sql)
        self.assertNotIn('Account IS NOT NULL', finalized)
        self.assertIn('AND f.Account = e.Account', finalized)
        self.assertNotRegex(finalized, r'UNION\s+ALL\s+SELECT\s+\*')
        self.assertIn('CAST(NULL AS VARCHAR) AS Account', finalized)

    def test_finalize_restores_expenses_account_join(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable AS (\n"
            "  SELECT MonthlyRegionKey, Region, YYYYMM FROM {{ source('raw','FactTable') }}\n"
            "),\n"
            "expenses_for_fact AS (\n"
            "  SELECT MonthlyRegionKey, Region, Account, YYYYMM FROM {{ source('raw','Expenses') }}\n"
            "),\n"
            "facttable_with_expenses AS (\n"
            "  SELECT * FROM facttable\n"
            "  UNION ALL\n"
            "  SELECT MonthlyRegionKey, Region, Account, YYYYMM FROM expenses_for_fact\n"
            "),\n"
            "final_model AS (\n"
            "  SELECT f.MonthlyRegionKey\n"
            "  FROM facttable_with_expenses f\n"
            "  LEFT JOIN expenses_for_fact exp\n"
            "    ON f.MonthlyRegionKey = exp.MonthlyRegionKey\n"
            "   AND (f.Region IS NOT NULL OR exp.Account IS NOT NULL)\n"
            ")\n"
            "SELECT * FROM final_model"
        )
        finalized = finalize_generated_sql(sql)
        self.assertIn('CAST(NULL AS VARCHAR) AS Account', finalized)
        self.assertIn('AND f.Account = exp.Account', finalized)
        self.assertNotIn('IS NOT NULL OR', finalized)
        self.assertNotRegex(finalized, r'UNION\s+ALL\s+SELECT\s+\*')

    def test_finalize_preserves_region_from_facttable_raw(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable_raw AS (\n"
            "  SELECT MonthlyRegionKey, Region, Account, YYYYMM FROM {{ source('raw','FactTable') }}\n"
            "),\n"
            "facttable AS (\n"
            "  SELECT MonthlyRegionKey, Account, YYYYMM FROM facttable_raw\n"
            ")\n"
            "SELECT * FROM facttable"
        )
        finalized = finalize_generated_sql(sql)
        fact_body = finalized.split('facttable AS (', 1)[1].split(')\nSELECT', 1)[0]
        self.assertIn('Region', fact_body)

    def test_expenses_account_join_enforcer_preserves_existing_full_grain(self):
        sql = (
            "SELECT * FROM facttable_with_expenses f\n"
            "LEFT JOIN expenses exp ON f.MonthlyRegionKey = exp.MonthlyRegionKey\n"
            " AND f.Account = exp.Account"
        )
        self.assertEqual(enforce_expenses_account_join(sql), sql)

    def test_audit_catches_quoted_reference_to_unquoted_camel_case_output(self):
        plan = [make_load('InventoryBalances', ['ClassTurns', 'ThroughputQty'], source='InventoryBalances.qvd')]
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH inventorybalances AS (\n"
            "  SELECT ClassTurns, ThroughputQty FROM {{ source('raw','InventoryBalances') }}\n"
            "),\n"
            "final_model AS (\n"
            "  SELECT ib.\"ClassTurns\", ib.\"ThroughputQty\"\n"
            "  FROM inventorybalances ib\n"
            ")\n"
            "SELECT * FROM final_model"
        )
        issues = _audit_generated_sql_against_plan(sql, plan=plan, qvs_script='', dialect='dbt')
        self.assertTrue(any('QUOTED_CASE_MISMATCH' in i for i in issues), issues)

    def test_audit_requires_generated_product_master_joins(self):
        plan = [make_load('FactTable', ['Item-Branch Key'], source='FactTable.qvd')]
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable_with_expenses AS (\n"
            "  SELECT \"Item-Branch Key\" FROM {{ source('raw','FactTable') }}\n"
            "),\n"
            "itembranchmaster AS (\n"
            "  SELECT \"Item-Branch Key\", \"Short Name\" FROM {{ source('raw','ItemBranchMaster') }}\n"
            "),\n"
            "itemmaster AS (\n"
            "  SELECT \"Short Name\", \"Product Group\", \"Product Sub Group\", \"Product Type\" FROM {{ source('raw','ItemMaster') }}\n"
            "),\n"
            "productgroupmaster AS (\n"
            "  SELECT \"Product Group\" FROM {{ source('raw','ProductGroupMaster') }}\n"
            "),\n"
            "productsubgroupmaster AS (\n"
            "  SELECT \"Product Sub Group\" FROM {{ source('raw','ProductSubGroupMaster') }}\n"
            "),\n"
            "producttypemaster AS (\n"
            "  SELECT \"Product Type\" FROM {{ source('raw','ProductTypeMaster') }}\n"
            "),\n"
            "final_model AS (\n"
            "  SELECT f.\"Item-Branch Key\" FROM facttable_with_expenses f\n"
            ")\n"
            "SELECT * FROM final_model"
        )
        issues = _audit_generated_sql_against_plan(sql, plan=plan, qvs_script='', dialect='dbt')
        self.assertTrue(any('MISSING_PRODUCT_BRIDGE_JOIN' in i for i in issues), issues)
        self.assertTrue(any('MISSING_PRODUCT_MASTER_JOIN' in i for i in issues), issues)

    def test_audit_repair_locks_product_bridge_against_direct_join_regression(self):
        plan = [make_load('FactTable', ['Item-Branch Key'], source='FactTable.qvd')]
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable_with_expenses AS (\n"
            "  SELECT \"Item-Branch Key\" FROM {{ source('raw','FactTable') }}\n"
            "),\n"
            "itembranchmaster AS (\n"
            "  SELECT \"Item-Branch Key\", \"Short Name\" FROM {{ source('raw','ItemBranchMaster') }}\n"
            "),\n"
            "itemmaster AS (\n"
            "  SELECT \"Short Name\" FROM {{ source('raw','ItemMaster') }}\n"
            "),\n"
            "final_model AS (\n"
            "  SELECT f.\"Item-Branch Key\" FROM facttable_with_expenses f\n"
            "  LEFT JOIN itembranchmaster ib ON f.\"Item-Branch Key\" = ib.\"Item-Branch Key\"\n"
            "  LEFT JOIN itemmaster im ON f.\"Item-Branch Key\" = im.\"Short Name\"\n"
            ")\n"
            "SELECT * FROM final_model"
        )
        issues = _audit_generated_sql_against_plan(sql, plan=plan, qvs_script='', dialect='dbt')
        self.assertTrue(any('WRONG_PRODUCT_JOIN_PATH' in i for i in issues), issues)

    def test_audit_requires_arsummary_1_join_when_cte_exists(self):
        plan = [make_load('FactTable', ['CustKeyAR'], source='FactTable.qvd')]
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable_with_expenses AS (\n"
            "  SELECT CustKeyAR FROM {{ source('raw','FactTable') }}\n"
            "),\n"
            "customermap AS (\n"
            "  SELECT CustKey, CustKeyAR FROM {{ source('raw','CustomerMap') }}\n"
            "),\n"
            "arsummary AS (\n"
            "  SELECT CustKeyAR FROM {{ source('raw','ARSummary') }}\n"
            "),\n"
            "arsummary_1 AS (\n"
            "  SELECT CustKeyAR, ARAge, ARAgeBal FROM {{ source('raw','ARSummary-1') }}\n"
            "),\n"
            "final_model AS (\n"
            "  SELECT f.CustKeyAR FROM facttable_with_expenses f\n"
            "  LEFT JOIN customermap cmap ON f.CustKeyAR = cmap.CustKeyAR\n"
            "  LEFT JOIN arsummary ar ON cmap.CustKeyAR = ar.CustKeyAR\n"
            ")\n"
            "SELECT * FROM final_model"
        )
        issues = _audit_generated_sql_against_plan(sql, plan=plan, qvs_script='', dialect='dbt')
        self.assertTrue(any('MISSING_ARSUMMARY_1_JOIN' in i for i in issues), issues)

    def test_detect_repair_regressions_blocks_removed_valid_structures(self):
        previous = (
            "{{ config(materialized='table') }}\n"
            "WITH final_model AS (\n"
            "  SELECT 1 AS x FROM facttable_with_expenses f\n"
            "  LEFT JOIN expenses e ON f.MonthlyRegionKey = e.MonthlyRegionKey AND f.Account = e.Account\n"
            "  LEFT JOIN itembranchmaster ib ON f.\"Item-Branch Key\" = ib.\"Item-Branch Key\"\n"
            "  LEFT JOIN itemmaster im ON ib.\"Short Name\" = im.\"Short Name\"\n"
            "  LEFT JOIN arsummary_1 ar1 ON f.CustKeyAR = ar1.CustKeyAR\n"
            ")\nSELECT * FROM final_model"
        )
        candidate = (
            "{{ config(materialized='table') }}\n"
            "WITH final_model AS (\n"
            "  SELECT 1 AS x FROM facttable_with_expenses f\n"
            "  LEFT JOIN expenses e ON f.MonthlyRegionKey = e.MonthlyRegionKey\n"
            ")\nSELECT * FROM final_model"
        )
        regressions = detect_repair_regressions(previous, candidate)
        self.assertTrue(any('itembranchmaster' in r for r in regressions), regressions)
        self.assertTrue(any('arsummary_1' in r for r in regressions), regressions)
        self.assertTrue(any('WEAKENED_EXPENSES_JOIN' in r for r in regressions), regressions)

    def test_audit_requires_generated_account_master_joins(self):
        plan = [make_load('Expenses', ['Account'], source='Expenses.qvd')]
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH expenses AS (\n"
            "  SELECT Account FROM {{ source('raw','Expenses') }}\n"
            "),\n"
            "accountmaster AS (\n"
            "  SELECT Account, AccountGroup FROM {{ source('raw','AccountMaster') }}\n"
            "),\n"
            "accountgroupmaster AS (\n"
            "  SELECT AccountGroup FROM {{ source('raw','AccountGroupMaster') }}\n"
            ")\n"
            "SELECT * FROM expenses"
        )
        issues = _audit_generated_sql_against_plan(sql, plan=plan, qvs_script='', dialect='dbt')
        self.assertTrue(any('UNUSED_ACCOUNT_MASTER' in i for i in issues), issues)
        self.assertTrue(any('UNUSED_ACCOUNT_GROUP_MASTER' in i for i in issues), issues)

    def test_minimum_token_guard_rejects_tiny_generation_budget(self):
        with self.assertRaises(MigrationTokenBudgetError):
            _invoke_ai_text(
                lambda *args, **kwargs: 'unused',
                'prompt',
                max_tokens=200,
                phase='full_generation',
            )

    def test_generation_token_budgets_are_reasonable_not_oversized(self):
        self.assertEqual(ONE_SHOT_MAX_TOKENS, 10000)
        self.assertEqual(LOOP_MAX_TOKENS, 4000)
        self.assertEqual(REPAIR_MAX_TOKENS, 2200)
        self.assertEqual(MIN_REQUIRED_OUTPUT_TOKENS, 1500)

    def test_sql_generation_prompts_do_not_tell_model_to_stop_after_contract(self):
        _, fast_prompt = build_fast_sql_generation_prompt(
            "FactTable:\nLOAD A FROM [lib://FactTable.qvd] (qvd);"
        )
        fast_system, _ = build_fast_sql_generation_prompt(
            "FactTable:\nLOAD A FROM [lib://FactTable.qvd] (qvd);"
        )
        full_system, _ = build_sql_generation_prompt(
            "FactTable:\nLOAD A FROM [lib://FactTable.qvd] (qvd);"
        )
        combined = "\n".join([fast_system, fast_prompt, full_system]).lower()
        self.assertNotIn('stop/ask', combined)
        self.assertNotIn('contract question', combined)
        self.assertIn('never stop early', combined)

    def test_fast_prompt_is_sql_first_without_schema_contract(self):
        system_prompt, prompt = build_fast_sql_generation_prompt(
            "FactTable:\nLOAD A FROM [lib://FactTable.qvd] (qvd);",
        )
        combined = "\n".join([system_prompt, prompt]).lower()
        self.assertIn('start with ### sql', combined)
        self.assertIn('do not begin with analysis', combined)
        self.assertNotIn('pre-generation schema contract', combined)
        self.assertNotIn('source field registry', combined)
        self.assertIn('one concise technical paragraph', combined)

    def test_minimum_token_guard_allows_repair_budget(self):
        result = _invoke_ai_text(
            lambda *args, **kwargs: 'ok',
            'prompt',
            max_tokens=REPAIR_MAX_TOKENS,
            phase='repair',
            min_tokens=MIN_REQUIRED_OUTPUT_TOKENS,
        )
        self.assertEqual(result, 'ok')

    def test_non_stream_callable_does_not_receive_stream_kwarg(self):
        seen = {}

        def call_ai(prompt, **kwargs):
            seen.update(kwargs)
            return '### SQL\nSELECT 1\n### DESCRIPTION\nok'

        text = _invoke_ai_text(
            call_ai,
            'prompt',
            max_tokens=5000,
            phase='full_generation',
            stream_callback=lambda _chunk: None,
        )
        self.assertIn('SELECT 1', text)
        self.assertNotIn('stream', seen)


# ─── 2. Property-based date transform tests ──────────────────────────────────

class TestDateTransformProperties(unittest.TestCase):
    """
    Property-based tests: verify that the expression translator produces
    structurally correct SQL for a range of date inputs.

    We don't have Hypothesis in requirements, so we enumerate representative
    cases that cover the key equivalence classes.
    """

    # (month_offset, description)
    MONTH_OFFSETS = [
        (0,   'zero offset'),
        (1,   'one month forward'),
        (12,  'one year forward — fiscal year shift'),
        (24,  'two years forward'),
        (-1,  'one month back'),
        (-12, 'one year back'),
    ]

    def _translate(self, expr):
        return _translate_qlik_expression_to_sql(expr)

    def test_addmonths_all_offsets_produce_dateadd(self):
        """Addmonths(field, N) → DATEADD(month, N, field) for all N."""
        for offset, desc in self.MONTH_OFFSETS:
            with self.subTest(offset=offset, desc=desc):
                expr = f'Addmonths([YYYYMM], {offset})'
                result = self._translate(expr)
                self.assertIn('DATEADD(month,', result,
                              f'Missing DATEADD for offset {offset}: {result}')
                self.assertIn(str(offset), result,
                              f'Offset {offset} not preserved in: {result}')
                self.assertNotIn('Addmonths', result,
                                 f'Qlik Addmonths not replaced for offset {offset}')

    def test_date_addmonths_all_offsets_produce_to_char(self):
        """Date(Addmonths(field, N), 'YYYYMM') → TO_CHAR(DATEADD(...), 'YYYYMM')."""
        for offset, desc in self.MONTH_OFFSETS:
            with self.subTest(offset=offset, desc=desc):
                expr = f"Date(Addmonths([YYYYMM], {offset}), 'YYYYMM')"
                result = self._translate(expr)
                self.assertIn('TO_CHAR(', result,
                              f'Missing TO_CHAR for offset {offset}: {result}')
                self.assertIn('DATEADD(month,', result,
                              f'Missing DATEADD for offset {offset}: {result}')
                # Must NOT double-cast: CAST(DATEADD(...) is wrong
                self.assertNotIn('CAST(DATEADD', result,
                                 f'Double-cast detected for offset {offset}: {result}')

    def test_month_function_all_fields_produce_to_char_mon(self):
        """Month(expr) → TO_CHAR(expr, 'Mon') for various field names."""
        fields = ['[YYYYMM]', '[OrderDate]', '[FiscalDate]', 'SomeDate']
        for f in fields:
            with self.subTest(field=f):
                expr = f'Month({f})'
                result = self._translate(expr)
                self.assertIn("TO_CHAR(", result,
                              f'Missing TO_CHAR for Month({f}): {result}')
                self.assertIn("'Mon'", result,
                              f"Missing Mon format for Month({f}): {result}")
                self.assertNotIn('MONTHNAME', result,
                                 f'MONTHNAME must not appear for Month({f}): {result}')

    def test_month_addmonths_composition(self):
        """Month(Addmonths(field, N)) → TO_CHAR(DATEADD(month, N, field), 'Mon')."""
        for offset, desc in self.MONTH_OFFSETS:
            with self.subTest(offset=offset, desc=desc):
                expr = f'Month(Addmonths([YYYYMM], {offset}))'
                result = self._translate(expr)
                self.assertIn("TO_CHAR(", result)
                self.assertIn("'Mon'", result)
                self.assertIn('DATEADD(month,', result)
                self.assertNotIn('MONTHNAME', result)

    def test_text_comparison_all_operators(self):
        """[TextCol] op N → IS NOT NULL / IS NULL for all comparison operators."""
        text_fields = ['[AccountDesc]', '[CustomerName]', '[ProductCode]']
        operators_non_zero = ['>', '<', '>=', '<=', '!=', '<>']
        for field in text_fields:
            for op in operators_non_zero:
                with self.subTest(field=field, op=op):
                    expr = f'{field} {op} 5'
                    result = self._translate(expr)
                    self.assertIn('IS NOT NULL', result,
                                  f'{field} {op} 5 should become IS NOT NULL: {result}')
                    self.assertNotIn(f'{op} 5', result,
                                     f'Numeric comparison not removed: {result}')

    def test_numeric_fields_never_rewritten(self):
        """SalesAmount > 0 must NOT be rewritten — it's a valid numeric comparison."""
        numeric_fields = ['SalesAmount', 'TotalCost', 'Quantity', 'Budget']
        for f in numeric_fields:
            with self.subTest(field=f):
                expr = f'[{f}] > 0'
                result = self._translate(expr)
                self.assertNotIn('IS NOT NULL', result,
                                 f'Numeric field {f} wrongly rewritten: {result}')
                self.assertIn('> 0', result,
                              f'Numeric comparison removed for {f}: {result}')


# ─── 3. Migration telemetry ───────────────────────────────────────────────────

class TestMigrationTelemetry(unittest.TestCase):

    def test_basic_lifecycle(self):
        tel = MigrationTelemetry(job_id='test-001', qlik_app_name='SalesApp')
        tel.start_phase('extraction')
        time.sleep(0.01)
        tel.end_phase('extraction')
        tel.record_translation('rule_based')
        tel.record_translation('rule_based')
        tel.record_translation('llm_fallback')
        tel.set_confidence(0.88)
        tel.finalize()

        self.assertTrue(tel.finalized)
        self.assertEqual(tel.rule_based_translations, 2)
        self.assertEqual(tel.llm_fallback_translations, 1)
        self.assertEqual(tel.total_fields, 3)
        self.assertAlmostEqual(tel.llm_fallback_rate, 1/3, places=3)
        self.assertGreater(tel.phase_duration_ms('extraction'), 0)

    def test_to_dict_structure(self):
        tel = MigrationTelemetry(job_id='test-002')
        tel.record_translation('rule_based')
        tel.set_confidence(0.75)
        tel.finalize()
        d = tel.to_dict()

        self.assertIn('jobId', d)
        self.assertIn('totals', d)
        self.assertIn('translations', d)
        self.assertIn('quality', d)
        self.assertIn('timing', d)
        self.assertIn('alerts', d)
        self.assertEqual(d['translations']['ruleBased'], 1)
        self.assertEqual(d['quality']['confidenceScore'], 0.75)

    def test_high_llm_fallback_rate_triggers_alert(self):
        tel = MigrationTelemetry(job_id='test-003')
        # 5 LLM fallbacks out of 6 total = 83% > 20% threshold
        for _ in range(5):
            tel.record_translation('llm_fallback')
        tel.record_translation('rule_based')
        tel.finalize()

        alerts = tel.alerts()
        self.assertTrue(any('LLM fallback rate' in a for a in alerts),
                        f'Expected LLM fallback alert, got: {alerts}')

    def test_low_confidence_triggers_alert(self):
        tel = MigrationTelemetry(job_id='test-004')
        tel.set_confidence(0.55)
        tel.finalize()

        alerts = tel.alerts()
        self.assertTrue(any('confidence' in a.lower() for a in alerts),
                        f'Expected confidence alert, got: {alerts}')

    def test_no_alerts_for_clean_job(self):
        tel = MigrationTelemetry(job_id='test-005')
        for _ in range(10):
            tel.record_translation('rule_based')
        tel.set_confidence(0.92)
        tel.finalize()

        alerts = tel.alerts()
        self.assertEqual(alerts, [], f'Unexpected alerts for clean job: {alerts}')

    def test_failed_translations_trigger_alert(self):
        tel = MigrationTelemetry(job_id='test-006')
        tel.record_translation('rule_based')
        tel.record_translation('failed')
        tel.finalize()

        alerts = tel.alerts()
        self.assertTrue(any('failed' in a.lower() for a in alerts))

    def test_high_repair_iterations_trigger_alert(self):
        tel = MigrationTelemetry(job_id='test-007')
        tel.repair_iterations = 5
        tel.finalize()

        alerts = tel.alerts()
        self.assertTrue(any('repair' in a.lower() for a in alerts))


# ─── 4. Validator pass 8 ─────────────────────────────────────────────────────

class TestValidatorPass8(unittest.TestCase):

    def _codes(self, sql):
        return {i.code for i in validate_migration_sql(sql, dialect='dbt')}

    def test_monthname_triggers_warning(self):
        sql = ("{{ config(materialized='table') }}\n"
               "WITH c AS (SELECT MONTHNAME(d) AS m FROM {{ source('raw','T') }})\n"
               "SELECT * FROM c")
        self.assertIn('MONTHNAME_FULL_NAME', self._codes(sql))

    def test_to_char_mon_no_false_positive(self):
        sql = ("{{ config(materialized='table') }}\n"
               "WITH c AS (SELECT TO_CHAR(d,'Mon') AS m FROM {{ source('raw','T') }})\n"
               "SELECT * FROM c")
        self.assertNotIn('MONTHNAME_FULL_NAME', self._codes(sql))

    def test_missing_dimension_joins_detected(self):
        sql = ("{{ config(materialized='table') }}\n"
               "WITH\n"
               "fact AS (SELECT f1, k FROM {{ source('raw','Sales') }}),\n"
               "cal AS (SELECT YYYYMM, Year FROM {{ source('raw','Calendar') }}),\n"
               "cust AS (SELECT k, Name FROM {{ source('raw','Customers') }})\n"
               "SELECT * FROM fact")
        self.assertIn('MISSING_DIMENSION_JOINS', self._codes(sql))

    def test_joined_model_no_false_positive(self):
        sql = ("{{ config(materialized='table') }}\n"
               "WITH\n"
               "fact AS (SELECT f1, k FROM {{ source('raw','Sales') }}),\n"
               "cust AS (SELECT k, Name FROM {{ source('raw','Customers') }}),\n"
               "final AS (\n"
               "  SELECT f.*, c.Name FROM fact f LEFT JOIN cust c ON f.k = c.k\n"
               ")\n"
               "SELECT * FROM final")
        self.assertNotIn('MISSING_DIMENSION_JOINS', self._codes(sql))

    def test_two_ctes_no_false_positive(self):
        sql = ("{{ config(materialized='table') }}\n"
               "WITH fact AS (SELECT a FROM {{ source('raw','S') }})\n"
               "SELECT * FROM fact")
        self.assertNotIn('MISSING_DIMENSION_JOINS', self._codes(sql))


if __name__ == '__main__':
    unittest.main()
