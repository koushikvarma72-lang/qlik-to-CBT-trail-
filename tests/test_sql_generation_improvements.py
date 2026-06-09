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

import re
import json
import os
import shutil
import time
import unittest
import zipfile
import inspect
from unittest.mock import Mock, patch

from backend.migration.sql_generation import (
    _audit_generated_sql_against_plan,
    _invoke_ai_text,
    _infer_sql_type_from_name,
    _typed_null,
    _translate_qlik_expression_to_sql,
    build_fast_sql_generation_prompt,
    build_join_contract,
    build_migration_validation_report,
    build_sql_generation_prompt,
    compose_final_model_from_contract,
    compute_join_contract_coverage,
    compare_descriptions,
    count_select_columns_for_branch,
    detect_repair_regressions,
    dry_run_validation_artifacts,
    enforce_global_yyyymm_dateadd_coercion,
    enforce_expenses_account_join,
    enforce_explicit_concat_target_schema,
    enrich_final_model_projection,
    execute_validation_report,
    export_validation_artifacts,
    force_replace_explicit_facttable_concat_cte,
    generate_validation_artifacts,
    generate_dbt_project_scaffold,
    generate_export_manifest,
    generate_export_summary_report,
    generate_sql_fingerprint,
    extract_output_alias,
    extract_projection_display_names,
    extract_select_projection_columns,
    extract_cte_output_columns,
    extract_cte_output_column_map,
    extract_alias_to_cte_map,
    extract_cte_body,
    iter_alias_column_refs,
    resolve_cte_column_reference,
    final_model_has_bad_expenses_join,
    finalize_generated_sql,
    fact_expenses_cte_has_alias_star,
    expand_fact_expenses_alias_star,
    deterministic_finalize_sql_structure,
    has_union_star_branch,
    ONE_SHOT_MAX_TOKENS,
    LOOP_MAX_TOKENS,
    REPAIR_MAX_TOKENS,
    MIN_REQUIRED_OUTPUT_TOKENS,
    MigrationTokenBudgetError,
    needs_sql_repair,
    parse_migration_response,
    render_sql_from_load_plan,
    request_migration_one_shot,
    remove_bad_expenses_monthly_join,
    rewrite_final_model_to_use_fact_expenses,
    score_generated_sql_quality,
    sanitize_test_sql_projection,
    validate_candidate_integrity,
    validate_execution_safety,
    validate_explicit_concatenate_field_parity,
    validate_fact_expenses_union_semantics,
    validate_generated_sql,
    validate_qlik_semantic_parity,
    validation_issue_category,
    zip_exported_artifacts,
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

    def test_no_false_union_mismatch_for_distinct_non_union_ctes(self):
        sql = """
{{ config(materialized='table') }}
WITH facttable AS (
    SELECT yyyymm, amount FROM {{ source('raw', 'FactTable') }}
),
historyflag AS (
    SELECT DISTINCT
        yyyymm,
        CASE WHEN yyyymm <= DATE_TRUNC('month', TO_DATE('2013-05-31')) THEN 1 ELSE 0 END AS history_flag
    FROM facttable
),
final_model AS (
    SELECT f.yyyymm, h.history_flag
    FROM facttable f
    LEFT JOIN historyflag h ON f.yyyymm = h.yyyymm
)
SELECT * FROM final_model
"""
        issues = validate_generated_sql(sql)
        self.assertFalse(any('UNION_COLUMN_COUNT_MISMATCH' in issue for issue in issues), issues)
        self.assertFalse(any('JOIN_KEY_MISSING' in issue and 'h' in issue and 'yyyymm' in issue for issue in issues), issues)

    def test_real_union_mismatch_still_caught(self):
        sql = """
{{ config(materialized='table') }}
WITH bad_union AS (
    SELECT a, b FROM x
    UNION ALL
    SELECT a FROM y
)
SELECT * FROM bad_union
"""
        issues = validate_generated_sql(sql)
        self.assertTrue(any('UNION_COLUMN_COUNT_MISMATCH' in issue and '[2, 1]' in issue for issue in issues), issues)

    def test_union_star_branch_reports_star_not_fake_count_mismatch(self):
        sql = """
{{ config(materialized='table') }}
WITH facttable AS (SELECT a, b FROM x),
expenses AS (SELECT a FROM y),
facttable_with_expenses AS (
    SELECT * FROM facttable
    UNION ALL
    SELECT a FROM expenses
)
SELECT * FROM facttable_with_expenses
"""
        issues = validate_generated_sql(sql)
        self.assertTrue(any('UNION_SELECT_STAR_BRANCH' in issue for issue in issues), issues)
        self.assertFalse(any('UNION_COLUMN_COUNT_MISMATCH' in issue for issue in issues), issues)

    def test_union_alias_star_expanded_no_false_mismatch(self):
        sql = """
{{ config(materialized='table') }}
WITH facttable AS (SELECT a, b, c FROM x),
expenses AS (SELECT a, b, c, account FROM y),
facttable_with_expenses AS (
    SELECT f.*, CAST(NULL AS VARCHAR) AS account FROM facttable f
    UNION ALL
    SELECT a, b, c, account FROM expenses
)
SELECT * FROM facttable_with_expenses
"""
        issues = validate_generated_sql(sql)
        # Star may still be flagged, but count mismatch should not be fabricated.
        self.assertFalse(any('UNION_COLUMN_COUNT_MISMATCH' in issue for issue in issues), issues)

    def test_union_select_star_branch_is_blocking(self):
        sql = """
{{ config(materialized='table') }}
WITH facttable AS (
    SELECT MonthlyRegionKey, Account, YYYYMM FROM {{ source('raw','FactTable') }}
),
expenses AS (
    SELECT MonthlyRegionKey, Account FROM {{ source('raw','Expenses') }}
),
facttable_with_expenses AS (
    SELECT * FROM facttable
    UNION ALL
    SELECT MonthlyRegionKey, Account FROM expenses
)
SELECT * FROM facttable_with_expenses
"""
        issues = validate_generated_sql(sql)
        self.assertTrue(any('UNION_SELECT_STAR_BRANCH' in issue for issue in issues), issues)

    def test_union_alias_star_branch_is_blocking(self):
        sql = """
{{ config(materialized='table') }}
WITH facttable AS (
    SELECT MonthlyRegionKey, Account, YYYYMM FROM {{ source('raw','FactTable') }}
),
expenses AS (
    SELECT MonthlyRegionKey, Account FROM {{ source('raw','Expenses') }}
),
facttable_with_expenses AS (
    SELECT f.*, CAST(NULL AS VARCHAR) AS account FROM facttable f
    UNION ALL
    SELECT MonthlyRegionKey, Account FROM expenses
)
SELECT * FROM facttable_with_expenses
"""
        issues = validate_generated_sql(sql)
        self.assertTrue(any('UNION_SELECT_STAR_BRANCH' in issue for issue in issues), issues)

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

    def test_low_coverage_sql_is_blocking_for_large_plan(self):
        plan = [make_load(f'T{i}', ['A'], source=f'T{i}.qvd') for i in range(6)]
        issues = validate_generated_sql("{{ config(materialized='table') }}\nSELECT 1", plan=plan, dialect='dbt')
        self.assertTrue(any('LOW_COVERAGE_SQL' in issue for issue in issues), issues)
        self.assertTrue(needs_sql_repair(issues))

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


class SqlGenerationImprovementPatchTests(unittest.TestCase):
    def test_normalize_quoted_identifier_tokens(self):
        from backend.migration.sql_generation import _normalize_column_token
        self.assertEqual(_normalize_column_token('"Customer Number"'), 'customer number')
        self.assertEqual(_normalize_column_token('"Sales Rep Name"'), 'sales rep name')
        self.assertEqual(_normalize_column_token('"AR1-30"'), 'ar1-30')
        self.assertEqual(_normalize_column_token('"AR60+"'), 'ar60+')
        self.assertEqual(_normalize_column_token('FiscalMonthNum'), 'fiscalmonthnum')

    def test_iter_alias_column_refs_preserves_quoted_symbol_columns(self):
        refs = list(iter_alias_column_refs('ar."AR1-30", ar."AR31-60", ar."AR60+", ar.AROpen'))
        cols = [col for _alias, col, _raw in refs]
        self.assertEqual(cols, ['ar1-30', 'ar31-60', 'ar60+', 'aropen'])

    def test_alias_to_cte_map_case_insensitive(self):
        body = (
            'SELECT * FROM FactTable_With_Expenses F '
            'LEFT JOIN CustomerMaster CuSt ON F.CustKey = CuSt.Customer '
            'LEFT JOIN SalesRepMaster SRM ON CuSt."Sales Rep" = SRM."Sales Rep"'
        )
        amap = extract_alias_to_cte_map(body)
        self.assertEqual(amap.get('cust'), 'customermaster')
        self.assertEqual(amap.get('srm'), 'salesrepmaster')
        self.assertEqual(amap.get('f'), 'facttable_with_expenses')

    def test_final_model_bad_expenses_join_detected(self):
        sql = (
            "WITH facttable AS (SELECT customer_id, yyyymm, amount FROM x),\n"
            "expenses AS (SELECT yyyymm, account, expense_actual FROM y),\n"
            "final_model AS (\n"
            "  SELECT f.*, e.account\n"
            "  FROM facttable f\n"
            "  LEFT JOIN expenses e ON f.yyyymm = e.yyyymm\n"
            ")\n"
            "SELECT * FROM final_model"
        )
        self.assertTrue(final_model_has_bad_expenses_join(sql))

    def test_quoted_ar_aging_columns_do_not_cause_ownership_mismatch(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH arsummary AS (\n"
            "  SELECT \"AR1-30\", \"AR31-60\", \"AR60+\", AROpen FROM {{ source('raw','ARSummary') }}\n"
            "),\n"
            "final_model AS (\n"
            "  SELECT ar.\"AR1-30\", ar.\"AR31-60\", ar.\"AR60+\", ar.AROpen\n"
            "  FROM arsummary ar\n"
            ")\n"
            "SELECT * FROM final_model"
        )
        issues = validate_generated_sql(sql, dialect='dbt')
        self.assertFalse(any('COLUMN_OWNERSHIP_MISMATCH' in i for i in issues), issues)
        self.assertFalse(any('JOIN_KEY_MISSING' in i and 'ar' in i for i in issues), issues)

    def test_arsummary_extract_output_columns_includes_quoted_ar_columns(self):
        sql = (
            "WITH arsummary AS (\n"
            "  SELECT CustKeyAR, ARGross, \"AR1-30\", \"AR31-60\", \"AR60+\" FROM src\n"
            ")\nSELECT * FROM arsummary"
        )
        cols = extract_cte_output_columns(sql, 'arsummary')
        lowered = {str(c).strip().strip('"').strip('`').strip('[]').lower() for c in cols}
        self.assertIn('custkeyar', lowered)
        self.assertIn('argross', lowered)
        self.assertIn('ar1-30', lowered)
        self.assertIn('ar31-60', lowered)
        self.assertIn('ar60+', lowered)

    def test_extract_cte_output_column_map_quoted_columns(self):
        sql = (
            "WITH customermaster AS (\n"
            "  SELECT \"Customer Number\", \"Customer Type\", \"Sales Rep\" FROM src\n"
            "), arsummary AS (\n"
            "  SELECT \"AR1-30\", \"AR31-60\", \"AR60+\" FROM src\n"
            "), calendar AS (\n"
            "  SELECT \"Fiscal Quarter\", \"Fiscal Year\" FROM src\n"
            ")\nSELECT 1"
        )
        cmap = extract_cte_output_column_map(sql, 'customermaster')
        amap = extract_cte_output_column_map(sql, 'arsummary')
        calmap = extract_cte_output_column_map(sql, 'calendar')
        self.assertIn('customer number', cmap)
        self.assertIn('customer type', cmap)
        self.assertIn('sales rep', cmap)
        self.assertIn('ar1-30', amap)
        self.assertIn('ar31-60', amap)
        self.assertIn('ar60+', amap)
        self.assertIn('fiscal quarter', calmap)
        self.assertIn('fiscal year', calmap)

    def test_customermaster_quoted_columns_validate(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH customermaster AS (\n"
            "  SELECT \"Customer Number\", \"Customer Type\", \"Sales Rep\" FROM src\n"
            "),\n"
            "final_model AS (\n"
            "  SELECT cust.\"Customer Number\", cust.\"Sales Rep\" FROM customermaster cust\n"
            ")\nSELECT * FROM final_model"
        )
        issues = validate_generated_sql(sql, dialect='dbt')
        self.assertFalse(any('COLUMN_OWNERSHIP_MISMATCH' in i for i in issues), issues)

    def test_product_chain_quoted_columns_validate(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH itembranchmaster AS (\n"
            "  SELECT \"Short Name\" FROM src\n"
            "), itemmaster AS (\n"
            "  SELECT \"Product Group\" FROM src\n"
            "), final_model AS (\n"
            "  SELECT ibm.\"Short Name\", im.\"Product Group\"\n"
            "  FROM itembranchmaster ibm\n"
            "  LEFT JOIN itemmaster im ON 1=1\n"
            ")\nSELECT * FROM final_model"
        )
        issues = validate_generated_sql(sql, dialect='dbt')
        self.assertFalse(any('COLUMN_OWNERSHIP_MISMATCH' in i for i in issues), issues)

    def test_ar_quoted_columns_validate(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH arsummary AS (\n"
            "  SELECT \"AR1-30\", \"AR31-60\", \"AR60+\" FROM src\n"
            "), final_model AS (\n"
            "  SELECT ar.\"AR1-30\", ar.\"AR31-60\", ar.\"AR60+\" FROM arsummary ar\n"
            ")\nSELECT * FROM final_model"
        )
        issues = validate_generated_sql(sql, dialect='dbt')
        self.assertFalse(any('COLUMN_OWNERSHIP_MISMATCH' in i for i in issues), issues)

    def test_facttable_with_expenses_alias_star_lineage_exposes_fact_columns(self):
        sql = (
            "WITH facttable AS (\n"
            "  SELECT \"Address Number\", CustKey FROM src\n"
            "), expenses AS (\n"
            "  SELECT \"Address Number\", CustKey, Account FROM src\n"
            "), facttable_with_expenses AS (\n"
            "  SELECT f.*, CAST(NULL AS VARCHAR) AS account FROM facttable f\n"
            "  UNION ALL\n"
            "  SELECT \"Address Number\", CustKey, Account FROM expenses\n"
            ")\nSELECT * FROM facttable_with_expenses"
        )
        fmap = extract_cte_output_column_map(sql, 'facttable_with_expenses')
        self.assertIn('address number', fmap)
        self.assertIn('custkey', fmap)
        self.assertIn('account', fmap)

    def test_rewrite_final_model_to_use_fact_expenses(self):
        sql = (
            "WITH facttable AS (SELECT customer_id, yyyymm, amount FROM x),\n"
            "expenses AS (SELECT yyyymm, account, expense_actual FROM y),\n"
            "facttable_with_expenses AS (\n"
            "  SELECT customer_id, yyyymm, amount, CAST(NULL AS VARCHAR) AS account, CAST(NULL AS NUMBER) AS expense_actual FROM facttable\n"
            "  UNION ALL\n"
            "  SELECT CAST(NULL AS VARCHAR) AS customer_id, yyyymm, CAST(NULL AS NUMBER) AS amount, account, expense_actual FROM expenses\n"
            "),\n"
            "final_model AS (\n"
            "  SELECT f.*, e.account\n"
            "  FROM facttable f\n"
            "  LEFT JOIN expenses e ON f.yyyymm = e.yyyymm\n"
            ")\n"
            "SELECT * FROM final_model"
        )
        out = rewrite_final_model_to_use_fact_expenses(sql)
        body = out.split("final_model AS (", 1)[1]
        self.assertIn("FROM facttable_with_expenses f", body)
        self.assertNotIn("join expenses", body.lower())
        self.assertNotIn("e.account", body.lower())
        self.assertIn("f.*", body)

    def test_finalize_rewrites_direct_expenses_join_when_plan_has_concat(self):
        plan = [
            {'table': 'FactTable', 'source': 'FactTable.qvd', 'operation': 'LOAD', 'fields': ['customer_id', 'yyyymm', 'amount']},
            {'table': 'Expenses', 'source': 'Expenses.qvd', 'operation': 'LOAD', 'fields': ['yyyymm', 'account', 'expense_actual'], 'is_concatenate': True, 'concatenate_target': 'FactTable'},
        ]
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable AS (\n"
            "  SELECT customer_id, yyyymm, amount FROM {{ source('raw','FactTable') }}\n"
            "),\n"
            "expenses AS (\n"
            "  SELECT yyyymm, account, expense_actual FROM {{ source('raw','Expenses') }}\n"
            "),\n"
            "final_model AS (\n"
            "  SELECT f.*, e.account, e.expense_actual\n"
            "  FROM facttable f\n"
            "  LEFT JOIN expenses e ON f.yyyymm = e.yyyymm\n"
            ")\n"
            "SELECT * FROM final_model"
        )
        out = finalize_generated_sql(sql, plan=plan)
        self.assertIn("facttable_with_expenses AS", out)
        self.assertIn("FROM facttable_with_expenses f", out)
        self.assertNotIn("join expenses", out.lower())

    def test_no_rewrite_when_plan_has_no_concat(self):
        plan = [
            {'table': 'FactTable', 'source': 'FactTable.qvd', 'operation': 'LOAD', 'fields': ['customer_id', 'yyyymm', 'amount']},
            {'table': 'Expenses', 'source': 'Expenses.qvd', 'operation': 'LOAD', 'fields': ['yyyymm', 'account', 'expense_actual'], 'is_concatenate': False},
        ]
        sql = (
            "WITH facttable AS (SELECT customer_id, yyyymm, amount FROM x),\n"
            "expenses AS (SELECT yyyymm, account, expense_actual FROM y),\n"
            "final_model AS (\n"
            "  SELECT f.*, e.account\n"
            "  FROM facttable f\n"
            "  LEFT JOIN expenses e ON f.yyyymm = e.yyyymm\n"
            ")\n"
            "SELECT * FROM final_model"
        )
        out = finalize_generated_sql(sql, plan=plan)
        self.assertIn("LEFT JOIN expenses e", out)

    def test_has_union_star_branch_detects_alias_star(self):
        sql = (
            "WITH facttable_with_expenses AS (\n"
            "  SELECT f.*, CAST(NULL AS VARCHAR) AS account FROM facttable f\n"
            "  UNION ALL\n"
            "  SELECT MonthlyRegionKey, account FROM expenses\n"
            ")\nSELECT * FROM facttable_with_expenses"
        )
        self.assertTrue(has_union_star_branch(sql))

    def test_fact_expenses_cte_alias_star_expand_helper(self):
        sql = (
            "WITH facttable AS (\n"
            "  SELECT monthlyregionkey, region, yyyymm FROM src\n"
            "), expenses AS (\n"
            "  SELECT monthlyregionkey, yyyymm, account, expenseactual, expensebudget FROM src\n"
            "), facttable_with_expenses AS (\n"
            "  SELECT f.*, CAST(NULL AS VARCHAR) AS account, CAST(NULL AS DECIMAL(18,2)) AS expenseactual, CAST(NULL AS DECIMAL(18,2)) AS expensebudget\n"
            "  FROM facttable f\n"
            "  UNION ALL\n"
            "  SELECT monthlyregionkey, yyyymm, account, expenseactual, expensebudget FROM expenses\n"
            ")\nSELECT * FROM facttable_with_expenses"
        )
        self.assertTrue(fact_expenses_cte_has_alias_star(sql))
        out = expand_fact_expenses_alias_star(sql)
        self.assertNotIn('f.*', out.lower())
        self.assertIn('monthlyregionkey', out.lower())
        self.assertIn('from facttable', out.lower())

    def test_remove_bad_expenses_monthly_join_when_fact_expenses_exists(self):
        sql = (
            "WITH facttable_with_expenses AS (SELECT 1 AS MonthlyRegionKey),\n"
            "expenses AS (SELECT 1 AS MonthlyRegionKey),\n"
            "final_model AS (\n"
            "  SELECT f.MonthlyRegionKey\n"
            "  FROM facttable_with_expenses f\n"
            "  LEFT JOIN expenses e ON f.MonthlyRegionKey = e.MonthlyRegionKey\n"
            ")\n"
            "SELECT * FROM final_model"
        )
        out = remove_bad_expenses_monthly_join(sql)
        self.assertNotIn("LEFT JOIN expenses e ON f.MonthlyRegionKey = e.MonthlyRegionKey", out)

    def test_remove_bad_expenses_monthly_join_noop_without_fact_expenses(self):
        sql = (
            "WITH facttable AS (SELECT 1 AS MonthlyRegionKey),\n"
            "expenses AS (SELECT 1 AS MonthlyRegionKey)\n"
            "SELECT * FROM facttable f\n"
            "LEFT JOIN expenses e ON f.MonthlyRegionKey = e.MonthlyRegionKey"
        )
        self.assertEqual(remove_bad_expenses_monthly_join(sql), sql)

    def test_enforce_global_yyyymm_dateadd_coercion_converts_variants(self):
        sql = (
            "SELECT DATEADD(month, 12, YYYYMM) AS a,\n"
            "DATEADD(month, 12, yyyymm) AS b,\n"
            "DATEADD(month, 24, f.YYYYMM) AS c\n"
            "FROM t f"
        )
        out = enforce_global_yyyymm_dateadd_coercion(sql)
        self.assertIn("DATEADD(month, 12, TO_DATE(YYYYMM::varchar, 'YYYYMM'))", out)
        self.assertIn("DATEADD(month, 12, TO_DATE(YYYYMM::varchar, 'YYYYMM'))", out)
        self.assertIn("DATEADD(month, 24, TO_DATE(f.YYYYMM::varchar, 'YYYYMM'))", out)

    def test_enforce_global_yyyymm_dateadd_coercion_does_not_double_wrap(self):
        sql = "SELECT DATEADD(month, 12, TO_DATE(YYYYMM::varchar, 'YYYYMM')) AS y"
        out = enforce_global_yyyymm_dateadd_coercion(sql)
        self.assertEqual(out, sql)

    def test_alias_column_validator_ignores_trailing_whitespace(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable_with_expenses AS (\n"
            "  SELECT CustKey FROM {{ source('raw','FactTable') }}\n"
            "),\n"
            "final_model AS (\n"
            "  SELECT f.CustKey, f.\"CustKey \"\n"
            "  FROM facttable_with_expenses f\n"
            ")\n"
            "SELECT * FROM final_model"
        )
        issues = validate_generated_sql(sql, dialect='dbt')
        self.assertFalse(any('ALIAS_COLUMN_NOT_FOUND' in i for i in issues), issues)
        self.assertFalse(any('JOIN_KEY_MISSING' in i for i in issues), issues)

    def test_alias_column_validator_does_not_capture_as_projection_alias(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH customermaster AS (\n"
            "  SELECT address_number FROM {{ source('raw','CustomerMaster') }}\n"
            "),\n"
            "final_model AS (\n"
            "  SELECT cust.address_number AS customer_address_number\n"
            "  FROM customermaster cust\n"
            ")\n"
            "SELECT * FROM final_model"
        )
        issues = validate_generated_sql(sql, dialect='dbt')
        self.assertFalse(any('ALIAS_COLUMN_NOT_FOUND' in i for i in issues), issues)
        self.assertFalse(any('JOIN_KEY_MISSING' in i for i in issues), issues)

    def test_finalize_generated_sql_rebuilds_alias_star_union(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable AS (\n"
            "  SELECT MonthlyRegionKey, Region, CustKey, YYYYMM FROM {{ source('raw','FactTable') }}\n"
            "),\n"
            "expenses AS (\n"
            "  SELECT MonthlyRegionKey, Account, ExpenseActual, ExpenseBudget, YYYYMM FROM {{ source('raw','Expenses') }}\n"
            "),\n"
            "facttable_with_expenses AS (\n"
            "  SELECT f.*, CAST(NULL AS VARCHAR) AS account, CAST(NULL AS DECIMAL) AS expenseactual, CAST(NULL AS DECIMAL) AS expensebudget\n"
            "  FROM facttable f\n"
            "  UNION ALL\n"
            "  SELECT MonthlyRegionKey, Account, ExpenseActual, ExpenseBudget, YYYYMM FROM expenses\n"
            ")\n"
            "SELECT * FROM facttable_with_expenses"
        )
        out = finalize_generated_sql(sql)
        self.assertNotIn("f.*", out)
        self.assertFalse(fact_expenses_cte_has_alias_star(out))
        self.assertIn("cast(null as varchar) as account", out.lower())
        self.assertIn("from facttable", out.lower())
        self.assertIn("from expenses", out.lower())
        self.assertNotIn('"Address Number"', out)
        self.assertNotIn('"Invoice Number"', out)
        self.assertNotIn('"Item-Branch Key"', out)
        self.assertNotIn('"Sales Amount"', out)
        issues = validate_generated_sql(out, dialect='dbt')
        self.assertFalse(any('UNION_COLUMN_COUNT_MISMATCH' in i for i in issues), issues)

    def test_dynamic_union_rebuild_uses_cte_columns_not_fallback(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable AS (\n"
            "  SELECT sales_key, customer_id, yyyymm, amount FROM {{ source('raw','FactTable') }}\n"
            "),\n"
            "expenses AS (\n"
            "  SELECT yyyymm, account, expense_actual FROM {{ source('raw','Expenses') }}\n"
            "),\n"
            "facttable_with_expenses AS (\n"
            "  SELECT f.*, e.account FROM facttable f LEFT JOIN expenses e ON f.yyyymm = e.yyyymm\n"
            "  UNION ALL\n"
            "  SELECT yyyymm, account, expense_actual FROM expenses\n"
            ")\n"
            "SELECT * FROM facttable_with_expenses"
        )
        out = finalize_generated_sql(sql)
        self.assertIn('sales_key', out.lower())
        self.assertIn('customer_id', out.lower())
        self.assertIn('yyyymm', out.lower())
        self.assertIn('amount', out.lower())
        self.assertIn('account', out.lower())
        self.assertIn('expense_actual', out.lower())
        self.assertNotIn('"Address Number"', out)
        self.assertNotIn('"Invoice Number"', out)
        self.assertNotIn('"Item-Branch Key"', out)
        self.assertNotIn('"Sales Amount"', out)
        self.assertNotIn('LEFT JOIN EXPENSES', out.upper())
        self.assertIn('UNION ALL', out.upper())

    def test_dynamic_union_rebuild_nulls_fact_only_columns_in_expenses_branch(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable AS (\n"
            "  SELECT monthlyregionkey, region, custkey, \"Invoice Number\", yyyymm FROM {{ source('raw','FactTable') }}\n"
            "),\n"
            "expenses AS (\n"
            "  SELECT monthlyregionkey, account, yyyymm, expenseactual FROM {{ source('raw','Expenses') }}\n"
            "),\n"
            "facttable_with_expenses AS (\n"
            "  SELECT * FROM facttable\n"
            "  UNION ALL\n"
            "  SELECT monthlyregionkey, account, yyyymm, expenseactual FROM expenses\n"
            ")\n"
            "SELECT * FROM facttable_with_expenses"
        )
        out = finalize_generated_sql(sql)
        union_body = extract_cte_body(out, 'facttable_with_expenses')
        expenses_branch_match = re.search(r'(?is)UNION\s+ALL\s+SELECT\s+(.*?)\s+FROM\s+expenses\b', union_body)
        self.assertIsNotNone(expenses_branch_match, out)
        expenses_branch = expenses_branch_match.group(1)
        expenses_branch_lower = expenses_branch.lower()

        self.assertRegex(expenses_branch_lower, r'\bmonthlyregionkey\b')
        self.assertIn('CAST(NULL AS VARCHAR) AS region', expenses_branch)
        self.assertIn('CAST(NULL AS NUMBER) AS custkey', expenses_branch)
        self.assertIn('CAST(NULL AS NUMBER) AS "Invoice Number"', expenses_branch)
        self.assertRegex(expenses_branch_lower, r'\byyyymm\b')
        self.assertRegex(expenses_branch_lower, r'\baccount\b')
        self.assertRegex(expenses_branch_lower, r'\bexpenseactual\b')
        self.assertNotRegex(expenses_branch, r'(?m)^\s*custkey\s*,?\s*$')
        self.assertNotRegex(expenses_branch, r'(?m)^\s*"Invoice Number"\s*,?\s*$')

    def test_explicit_expenses_concatenate_only_maps_qlik_listed_fields(self):
        plan = [
            make_load('FactTable', ['MonthlyRegionKey', 'Region', 'CustKey', 'YYYYMM'], source='FactTable.qvd'),
            make_load('Expenses', ['MonthlyRegionKey', 'Region', 'YYYYMM'], source='Expenses.qvd', is_concat=True, concat_target='FactTable'),
        ]
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable AS (\n"
            "  SELECT MonthlyRegionKey, Region, CustKey, YYYYMM FROM {{ source('raw','FactTable') }}\n"
            "),\n"
            "expenses AS (\n"
            "  SELECT MonthlyRegionKey, Region, YYYYMM, Account, ExpenseActual, ExpenseBudget FROM {{ source('raw','Expenses') }}\n"
            "),\n"
            "facttable_with_expenses AS (\n"
            "  SELECT MonthlyRegionKey, Region, CustKey, YYYYMM, Account, ExpenseActual, ExpenseBudget FROM facttable\n"
            "  UNION ALL\n"
            "  SELECT MonthlyRegionKey, Region, CustKey, YYYYMM, Account, ExpenseActual, ExpenseBudget FROM expenses\n"
            ")\n"
            "SELECT * FROM facttable_with_expenses"
        )
        finalized = finalize_generated_sql(sql, plan=plan)
        union_body = extract_cte_body(finalized, 'facttable_with_expenses')
        first_branch = re.split(r'(?is)\bUNION\s+ALL\b', union_body)[0]
        output_cols = extract_projection_display_names(first_branch)
        self.assertEqual([c.lower() for c in output_cols], ['monthlyregionkey', 'region', 'custkey', 'yyyymm'])
        expenses_branch = re.search(r'(?is)UNION\s+ALL\s+SELECT\s+(.*?)\s+FROM\s+expenses\b', union_body).group(1)
        self.assertIn('MonthlyRegionKey', expenses_branch)
        self.assertIn('Region', expenses_branch)
        self.assertIn('YYYYMM', expenses_branch)
        self.assertIn('CAST(NULL AS NUMBER) AS CustKey', expenses_branch)
        self.assertNotIn('Account', union_body)
        self.assertNotIn('ExpenseActual', union_body)
        self.assertNotIn('ExpenseBudget', union_body)

    def test_explicit_facttable_concatenate_output_exactly_matches_facttable_schema(self):
        plan = [
            make_load('FactTable', ['MonthlyRegionKey', 'Region', 'CustKey', 'YYYYMM'], source='FactTable.qvd'),
            make_load('Expenses', ['MonthlyRegionKey', 'Region', 'YYYYMM'], source='Expenses.qvd', is_concat=True, concat_target='FactTable'),
        ]
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable AS (\n"
            "  SELECT MonthlyRegionKey, Region, CustKey, YYYYMM FROM {{ source('raw','FactTable') }}\n"
            "),\n"
            "expenses AS (\n"
            "  SELECT MonthlyRegionKey, Region, YYYYMM, Account, ExpenseActual, ExpenseBudget FROM {{ source('raw','Expenses') }}\n"
            "),\n"
            "facttable_with_expenses AS (\n"
            "  SELECT MonthlyRegionKey, Region, CustKey, YYYYMM, Account, ExpenseActual, ExpenseBudget FROM facttable\n"
            "  UNION ALL\n"
            "  SELECT MonthlyRegionKey, Region, CustKey, YYYYMM, Account, ExpenseActual, ExpenseBudget FROM expenses\n"
            "),\n"
            "customermap AS (\n"
            "  SELECT CustKey, CustKeyAR FROM {{ source('raw','CustomerMap') }}\n"
            "),\n"
            "final_model AS (\n"
            "  SELECT f.CustKey, cm.CustKeyAR\n"
            "  FROM facttable_with_expenses f\n"
            "  LEFT JOIN customermap cm ON f.CustKey = cm.CustKey\n"
            ")\n"
            "SELECT * FROM final_model"
        )
        finalized = finalize_generated_sql(sql, plan=plan)
        union_body = extract_cte_body(finalized, 'facttable_with_expenses')
        first_branch, expenses_branch = re.split(r'(?is)\bUNION\s+ALL\b', union_body)
        fact_cols = [
            col.lower()
            for col in extract_projection_display_names(
                extract_cte_body(finalized, 'facttable')
            )
        ]
        union_cols = [col.lower() for col in extract_projection_display_names(first_branch)]

        self.assertEqual(union_cols, fact_cols)
        self.assertEqual(union_cols, ['monthlyregionkey', 'region', 'custkey', 'yyyymm'])
        self.assertIn('FROM facttable', first_branch)
        self.assertIn('MonthlyRegionKey', expenses_branch)
        self.assertIn('Region', expenses_branch)
        self.assertIn('YYYYMM', expenses_branch)
        self.assertIn('CAST(NULL AS NUMBER) AS CustKey', expenses_branch)
        self.assertNotIn('Account', union_body)
        self.assertNotIn('ExpenseActual', union_body)
        self.assertNotIn('ExpenseBudget', union_body)
        issues = validate_generated_sql(finalized, plan=plan, dialect='dbt')
        self.assertFalse(any('ALIAS_COLUMN_NOT_FOUND' in issue and 'CustKey' in issue for issue in issues), issues)
        self.assertFalse(any('CONCATENATE_SOURCE_ONLY_FIELD_LEAK' in issue for issue in issues), issues)

    def test_explicit_concat_target_schema_last_pass_removes_source_only_fields(self):
        plan = [
            make_load('FactTable', ['MonthlyRegionKey', 'Region', 'CustKey', 'YYYYMM'], source='FactTable.qvd'),
            make_load('Expenses', ['MonthlyRegionKey', 'Region', 'YYYYMM'], source='Expenses.qvd', is_concat=True, concat_target='FactTable'),
        ]
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable AS (SELECT MonthlyRegionKey, Region, CustKey, YYYYMM FROM src),\n"
            "expenses AS (SELECT MonthlyRegionKey, Region, YYYYMM, Account, ExpenseActual, ExpenseBudget FROM src),\n"
            "facttable_with_expenses AS (\n"
            "  SELECT MonthlyRegionKey, Region, CustKey, YYYYMM, CAST(NULL AS VARCHAR) AS Account, CAST(NULL AS NUMBER) AS ExpenseActual, CAST(NULL AS NUMBER) AS ExpenseBudget FROM facttable\n"
            "  UNION ALL\n"
            "  SELECT MonthlyRegionKey, Region, CAST(NULL AS NUMBER) AS CustKey, YYYYMM, Account, ExpenseActual, ExpenseBudget FROM expenses\n"
            "),\n"
            "final_model AS (\n"
            "  SELECT f.CustKey FROM facttable_with_expenses f\n"
            ")\n"
            "SELECT * FROM final_model"
        )
        enforced = enforce_explicit_concat_target_schema(sql, plan=plan)
        union_body = extract_cte_body(enforced, 'facttable_with_expenses')
        first_branch, expenses_branch = re.split(r'(?is)\bUNION\s+ALL\b', union_body)
        self.assertEqual(
            [col.lower() for col in extract_projection_display_names(first_branch)],
            ['monthlyregionkey', 'region', 'custkey', 'yyyymm'],
        )
        self.assertIn('FROM facttable', first_branch)
        self.assertIn('FROM expenses', expenses_branch)
        self.assertIn('CAST(NULL AS NUMBER) AS CustKey', expenses_branch)
        self.assertNotIn('Account', union_body)
        self.assertNotIn('ExpenseActual', union_body)
        self.assertNotIn('ExpenseBudget', union_body)
        self.assertNotRegex(union_body, r'(?im)^\s*FROM\s*$')

    def test_explicit_concat_qvs_script_fallback_enforces_schema_without_plan_block(self):
        qvs_script = (
            "CONCATENATE (FactTable)\n"
            "LOAD\n"
            "    MonthlyRegionKey,\n"
            "    Region,\n"
            "    YYYYMM\n"
            "RESIDENT Expenses;"
        )
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable AS (SELECT MonthlyRegionKey, Region, CustKey, YYYYMM FROM src),\n"
            "expenses AS (SELECT MonthlyRegionKey, Region, YYYYMM, Account, ExpenseActual, ExpenseBudget FROM src),\n"
            "facttable_with_expenses AS (\n"
            "  SELECT MonthlyRegionKey, Region, CustKey, YYYYMM, Account, ExpenseActual, ExpenseBudget FROM facttable\n"
            "  UNION ALL\n"
            "  SELECT MonthlyRegionKey, Region, CAST(NULL AS NUMBER) AS CustKey, YYYYMM, Account, ExpenseActual, ExpenseBudget FROM expenses\n"
            "),\n"
            "final_model AS (SELECT f.CustKey FROM facttable_with_expenses f)\n"
            "SELECT * FROM final_model"
        )
        finalized = finalize_generated_sql(sql, plan=[], qvs_script=qvs_script)
        union_body = extract_cte_body(finalized, 'facttable_with_expenses')
        first_branch, expenses_branch = re.split(r'(?is)\bUNION\s+ALL\b', union_body)
        self.assertIn('-- EXPLICIT_CONCAT_SCHEMA_ENFORCED', finalized)
        self.assertEqual(
            [col.lower() for col in extract_projection_display_names(first_branch)],
            ['monthlyregionkey', 'region', 'custkey', 'yyyymm'],
        )
        self.assertIn('CAST(NULL AS NUMBER) AS CustKey', expenses_branch)
        self.assertNotIn('Account', union_body)
        self.assertNotIn('ExpenseActual', union_body)
        self.assertNotIn('ExpenseBudget', union_body)

    def test_force_replace_explicit_concat_replaces_entire_cte_body(self):
        plan = [
            make_load('FactTable', ['MonthlyRegionKey', 'Region', 'CustKey', 'YYYYMM'], source='FactTable.qvd'),
            make_load('Expenses', ['MonthlyRegionKey', 'Region', 'YYYYMM'], source='Expenses.qvd', is_concat=True, concat_target='FactTable'),
        ]
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable AS (SELECT MonthlyRegionKey, Region, CustKey, YYYYMM FROM src),\n"
            "expenses AS (SELECT MonthlyRegionKey, Region, YYYYMM, Account, ExpenseActual, ExpenseBudget FROM src),\n"
            "facttable_with_expenses AS (\n"
            "  SELECT MonthlyRegionKey, Region, CustKey, YYYYMM, CAST(NULL AS VARCHAR) AS Account, CAST(NULL AS NUMBER) AS ExpenseActual FROM facttable\n"
            "  WHERE 1 = 1\n"
            "  UNION ALL\n"
            "  SELECT MonthlyRegionKey, Region, CustKey, YYYYMM, Account, ExpenseActual FROM expenses\n"
            "  WHERE Account IS NOT NULL\n"
            "),\n"
            "final_model AS (\n"
            "  SELECT f.CustKey, cmap.CustKeyAR\n"
            "  FROM facttable_with_expenses f\n"
            "  LEFT JOIN customermap cmap ON f.CustKey = cmap.CustKey\n"
            ")\n"
            "SELECT * FROM final_model"
        )
        replaced, applied = force_replace_explicit_facttable_concat_cte(sql, plan=plan)
        self.assertTrue(applied)
        union_body = extract_cte_body(replaced, 'facttable_with_expenses')
        self.assertNotIn('WHERE 1 = 1', union_body)
        self.assertNotIn('WHERE Account IS NOT NULL', union_body)
        self.assertNotIn('Account', union_body)
        self.assertNotIn('ExpenseActual', union_body)
        self.assertNotIn('ExpenseBudget', union_body)
        self.assertEqual(
            extract_cte_output_columns(replaced, 'facttable_with_expenses'),
            extract_cte_output_columns(replaced, 'facttable'),
        )

    def test_force_replace_explicit_concat_missing_facttable_marks_failure(self):
        plan = [
            make_load('Expenses', ['MonthlyRegionKey', 'Region', 'YYYYMM'], source='Expenses.qvd', is_concat=True, concat_target='FactTable'),
        ]
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH expenses AS (SELECT MonthlyRegionKey, Region, YYYYMM FROM src),\n"
            "facttable_with_expenses AS (\n"
            "  SELECT MonthlyRegionKey, Region, YYYYMM FROM expenses\n"
            ")\n"
            "SELECT * FROM facttable_with_expenses"
        )
        replaced, applied = force_replace_explicit_facttable_concat_cte(sql, plan=plan)
        self.assertFalse(applied)
        self.assertIn('DYNAMIC_UNION_REBUILD_FAILED', replaced)
        issues = validate_generated_sql(replaced, plan=plan, dialect='dbt')
        self.assertTrue(any('DYNAMIC_UNION_REBUILD_FAILED' in issue for issue in issues), issues)

    def test_explicit_concat_rebuild_uses_facttable_not_budget_or_empty_from(self):
        plan = [
            make_load('FactTable', ['MonthlyRegionKey', 'Region', 'CustKey', 'YYYYMM'], source='FactTable.qvd'),
            make_load('Budget', ['MonthlyRegionKey', 'Budget Amount'], source='Budget.qvd'),
            make_load('Expenses', ['MonthlyRegionKey', 'Region', 'YYYYMM'], source='Expenses.qvd', is_concat=True, concat_target='FactTable'),
        ]
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable AS (\n"
            "  SELECT MonthlyRegionKey, Region, CustKey, YYYYMM FROM {{ source('raw','FactTable') }}\n"
            "),\n"
            "budget AS (\n"
            "  SELECT MonthlyRegionKey, \"Budget Amount\" FROM {{ source('raw','Budget') }}\n"
            "),\n"
            "expenses AS (\n"
            "  SELECT MonthlyRegionKey, Region, YYYYMM, Account, ExpenseActual, ExpenseBudget FROM {{ source('raw','Expenses') }}\n"
            "),\n"
            "facttable_with_expenses AS (\n"
            "  SELECT MonthlyRegionKey, \"Budget Amount\" FROM \n"
            "  UNION ALL\n"
            "  SELECT MonthlyRegionKey, Region, YYYYMM, Account, ExpenseActual, ExpenseBudget FROM expenses\n"
            "),\n"
            "customermap AS (\n"
            "  SELECT CustKey, CustKeyAR FROM {{ source('raw','CustomerMap') }}\n"
            "),\n"
            "final_model AS (\n"
            "  SELECT f.CustKey, cm.CustKeyAR\n"
            "  FROM facttable_with_expenses f\n"
            "  LEFT JOIN customermap cm ON f.CustKey = cm.CustKey\n"
            ")\n"
            "SELECT * FROM final_model"
        )
        finalized = finalize_generated_sql(sql, plan=plan)
        union_body = extract_cte_body(finalized, 'facttable_with_expenses')
        self.assertRegex(union_body, r'(?is)\bFROM\s+facttable\b')
        self.assertNotRegex(union_body, r'(?im)^\s*FROM\s*$')
        self.assertIn('CustKey', union_body)
        self.assertNotIn('"Budget Amount"', union_body)
        self.assertNotIn('Account', union_body)
        self.assertNotIn('ExpenseActual', union_body)
        self.assertNotIn('ExpenseBudget', union_body)
        issues = validate_generated_sql(finalized, plan=plan, dialect='dbt')
        self.assertFalse(any('ALIAS_COLUMN_NOT_FOUND' in issue and 'CustKey' in issue for issue in issues), issues)
        self.assertFalse(any('DYNAMIC_UNION_REBUILD_FAILED' in issue for issue in issues), issues)

    def test_validator_catches_explicit_concatenate_source_only_field_leak(self):
        plan = [
            make_load('FactTable', ['MonthlyRegionKey', 'Region', 'CustKey', 'YYYYMM'], source='FactTable.qvd'),
            make_load('Expenses', ['MonthlyRegionKey', 'Region', 'YYYYMM'], source='Expenses.qvd', is_concat=True, concat_target='FactTable'),
        ]
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable AS (SELECT MonthlyRegionKey, Region, CustKey, YYYYMM FROM src),\n"
            "expenses AS (SELECT MonthlyRegionKey, Region, YYYYMM, Account, ExpenseActual, ExpenseBudget FROM src),\n"
            "facttable_with_expenses AS (\n"
            "  SELECT MonthlyRegionKey, Region, CustKey, YYYYMM, Account, CAST(NULL AS NUMBER) AS ExpenseActual, CAST(NULL AS NUMBER) AS ExpenseBudget FROM facttable\n"
            "  UNION ALL\n"
            "  SELECT MonthlyRegionKey, Region, CAST(NULL AS NUMBER) AS CustKey, YYYYMM, Account, ExpenseActual, ExpenseBudget FROM expenses\n"
            "),\n"
            "final_model AS (SELECT f.CustKey FROM facttable_with_expenses f)\n"
            "SELECT * FROM final_model"
        )
        issues = validate_generated_sql(sql, plan=plan, dialect='dbt')
        self.assertTrue(any('CONCATENATE_SOURCE_ONLY_FIELD_LEAK' in issue for issue in issues), issues)
        self.assertTrue(any(validation_issue_category(issue) == 'compile_error' for issue in issues if 'CONCATENATE_SOURCE_ONLY_FIELD_LEAK' in issue), issues)

    def test_concatenate_field_parity_catches_extra_expense_columns(self):
        plan = [
            make_load('FactTable', ['MonthlyRegionKey', 'Region', 'CustKey', 'YYYYMM'], source='FactTable.qvd'),
            make_load('Expenses', ['MonthlyRegionKey', 'Region', 'YYYYMM'], source='Expenses.qvd', is_concat=True, concat_target='FactTable'),
        ]
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable AS (SELECT MonthlyRegionKey, Region, CustKey, YYYYMM FROM src),\n"
            "expenses AS (SELECT MonthlyRegionKey, Region, YYYYMM, Account, ExpenseActual FROM src),\n"
            "facttable_with_expenses AS (\n"
            "  SELECT MonthlyRegionKey, Region, CustKey, YYYYMM, Account, CAST(NULL AS NUMBER) AS ExpenseActual FROM facttable\n"
            "  UNION ALL\n"
            "  SELECT MonthlyRegionKey, Region, YYYYMM, CAST(NULL AS NUMBER) AS CustKey, Account, ExpenseActual FROM expenses\n"
            ")\nSELECT * FROM facttable_with_expenses"
        )
        issues = validate_explicit_concatenate_field_parity(sql, plan)
        self.assertTrue(any('CONCATENATE_FIELD_PARITY_MISMATCH' in issue for issue in issues), issues)
        self.assertEqual(validation_issue_category(issues[0]), 'semantic_error')

    def test_quoted_columns_in_union_parser_do_not_become_empty_names(self):
        branch = 'SELECT MonthlyRegionKey, Region, "Address Number", CustKey FROM facttable'
        self.assertEqual(count_select_columns_for_branch(branch), 4)
        self.assertEqual(extract_output_alias('"Address Number"'), 'Address Number')
        self.assertEqual(extract_output_alias('f."Address Number"'), 'Address Number')
        self.assertEqual(extract_output_alias('COALESCE(x, y) AS "Address Number"'), 'Address Number')
        self.assertEqual(
            extract_select_projection_columns('SELECT monthlyregionkey, region, "Address Number", custkey FROM facttable'),
            ['monthlyregionkey', 'region', 'address number', 'custkey'],
        )

        sql = (
            "WITH facttable AS (\n"
            "  SELECT MonthlyRegionKey, \"Address Number\", \"Invoice Number\", \"Item-Branch Key\" FROM src\n"
            ")\n"
            "SELECT * FROM facttable"
        )
        cols = extract_cte_output_columns(sql, 'facttable')
        self.assertIn('Address Number', cols)
        self.assertIn('Invoice Number', cols)
        self.assertIn('Item-Branch Key', cols)
        self.assertNotIn('', cols)

    def test_cast_null_quoted_aliases_extract_real_output_names(self):
        sql = (
            "WITH x AS (\n"
            "  SELECT\n"
            "    CAST(NULL AS NUMBER) AS \"Address Number\",\n"
            "    CAST(NULL AS NUMBER) AS \"Invoice Number\"\n"
            "  FROM facttable\n"
            ")\n"
            "SELECT * FROM x"
        )
        self.assertEqual(extract_cte_output_columns(sql, 'x'), ['Address Number', 'Invoice Number'])
        self.assertEqual(
            extract_select_projection_columns(
                'SELECT CAST(NULL AS NUMBER) AS "Address Number", '
                'CAST(NULL AS NUMBER) AS "Invoice Number" FROM expenses'
            ),
            ['address number', 'invoice number'],
        )
        self.assertEqual(
            extract_projection_display_names(
                'SELECT CAST(NULL AS NUMBER) AS "Address Number", '
                'CAST(NULL AS NUMBER) AS "Invoice Number" FROM expenses'
            ),
            ['Address Number', 'Invoice Number'],
        )

    def test_canonical_projection_parser_handles_dateadd_and_case_aliases(self):
        sql = (
            "SELECT\n"
            "  DATEADD(month, 12, TO_DATE(YYYYMM::varchar, 'YYYYMM')) AS YYYYMM,\n"
            "  CASE WHEN YYYYMM <= DATE_TRUNC('month', TO_DATE('2013-05-31')) THEN 1 ELSE 0 END AS _HistoryFlag\n"
            "FROM x"
        )
        self.assertEqual(extract_select_projection_columns(sql), ['yyyymm', '_historyflag'])

    def test_validator_accepts_aligned_union_branches_with_quoted_columns(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable_with_expenses AS (\n"
            "  SELECT MonthlyRegionKey, Region, \"Address Number\", CustKey FROM facttable\n"
            "  UNION ALL\n"
            "  SELECT MonthlyRegionKey, Region, CAST(NULL AS NUMBER) AS \"Address Number\", CAST(NULL AS NUMBER) AS CustKey FROM expenses\n"
            ")\n"
            "SELECT * FROM facttable_with_expenses"
        )
        issues = validate_generated_sql(sql, dialect='dbt')
        self.assertFalse(any('UNION_COLUMN_COUNT_MISMATCH' in issue for issue in issues), issues)
        self.assertFalse(any('""' in issue for issue in issues), issues)

    def test_validator_accepts_multiline_aligned_union_with_quoted_identifiers(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable_with_expenses AS (\n"
            "  SELECT monthlyregionkey, region, \"Address Number\", custkey FROM facttable\n"
            "  UNION ALL\n"
            "  SELECT monthlyregionkey, CAST(NULL AS VARCHAR) AS region, "
            "CAST(NULL AS NUMBER) AS \"Address Number\", CAST(NULL AS NUMBER) AS custkey FROM expenses\n"
            ")\n"
            "SELECT * FROM facttable_with_expenses"
        )
        issues = validate_generated_sql(sql, dialect='dbt')
        self.assertFalse(any('UNION_COLUMN_COUNT_MISMATCH' in issue for issue in issues), issues)
        self.assertFalse(any('""' in issue for issue in issues), issues)

    def test_validator_still_flags_real_union_count_mismatch(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH bad_union AS (\n"
            "  SELECT a, b FROM x\n"
            "  UNION ALL\n"
            "  SELECT a FROM y\n"
            ")\n"
            "SELECT * FROM bad_union"
        )
        issues = validate_generated_sql(sql, dialect='dbt')
        self.assertTrue(any('UNION_COLUMN_COUNT_MISMATCH' in issue and '[2, 1]' in issue for issue in issues), issues)
        self.assertFalse(any('""' in issue for issue in issues), issues)

    def test_validator_union_mismatch_snippets_preserve_quoted_identifiers(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable_with_expenses AS (\n"
            "  SELECT MonthlyRegionKey, Region, \"Address Number\", CustKey FROM facttable\n"
            "  UNION ALL\n"
            "  SELECT MonthlyRegionKey, Region FROM expenses\n"
            ")\n"
            "SELECT * FROM facttable_with_expenses"
        )
        issues = validate_generated_sql(sql, dialect='dbt')
        mismatch = [issue for issue in issues if 'UNION_COLUMN_COUNT_MISMATCH' in issue]
        self.assertTrue(mismatch, issues)
        self.assertIn('"Address Number"', mismatch[0])
        self.assertNotIn('""', mismatch[0])

    def test_parser_corruption_emits_internal_error_not_union_mismatch(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable_with_expenses AS (\n"
            "  SELECT a, \"\" FROM x\n"
            "  UNION ALL\n"
            "  SELECT a, b, c FROM y\n"
            ")\n"
            "SELECT * FROM facttable_with_expenses"
        )
        issues = validate_generated_sql(sql, dialect='dbt')
        self.assertTrue(any('INTERNAL_PARSER_ERROR' in issue for issue in issues), issues)
        self.assertFalse(any('UNION_COLUMN_COUNT_MISMATCH' in issue for issue in issues), issues)
        self.assertTrue(all(validation_issue_category(issue) != 'compile_error' for issue in issues if 'INTERNAL_PARSER_ERROR' in issue))

    def test_fact_expenses_union_semantics_accepts_correct_union(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable AS (\n"
            "  SELECT MonthlyRegionKey, Region, CustKey, Account FROM src\n"
            "),\n"
            "expenses AS (\n"
            "  SELECT MonthlyRegionKey, Region, Account, ExpenseActual FROM src\n"
            "),\n"
            "facttable_with_expenses AS (\n"
            "  SELECT MonthlyRegionKey, Region, CustKey, Account, CAST(NULL AS NUMBER) AS ExpenseActual FROM facttable\n"
            "  UNION ALL\n"
            "  SELECT MonthlyRegionKey, Region, CAST(NULL AS NUMBER) AS CustKey, Account, ExpenseActual FROM expenses\n"
            "),\n"
            "final_model AS (\n"
            "  SELECT f.MonthlyRegionKey, f.Account FROM facttable_with_expenses f\n"
            ")\n"
            "SELECT * FROM final_model"
        )
        issues = validate_fact_expenses_union_semantics(sql)
        self.assertFalse(any(validation_issue_category(issue) == 'semantic_error' for issue in issues), issues)

    def test_fact_expenses_union_semantics_catches_expenses_fact_column(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable AS (SELECT MonthlyRegionKey, CustKey FROM src),\n"
            "expenses AS (SELECT MonthlyRegionKey, ExpenseActual FROM src),\n"
            "facttable_with_expenses AS (\n"
            "  SELECT MonthlyRegionKey, CustKey, CAST(NULL AS NUMBER) AS ExpenseActual FROM facttable\n"
            "  UNION ALL\n"
            "  SELECT MonthlyRegionKey, CustKey, ExpenseActual FROM expenses\n"
            ")\n"
            "SELECT * FROM facttable_with_expenses"
        )
        issues = validate_fact_expenses_union_semantics(sql)
        self.assertTrue(any('INVALID_EXPENSES_BRANCH_OWNERSHIP' in issue for issue in issues), issues)

    def test_fact_expenses_union_semantics_catches_final_model_expenses_rejoin(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable AS (SELECT MonthlyRegionKey, Account FROM src),\n"
            "expenses AS (SELECT MonthlyRegionKey, Account, ExpenseActual FROM src),\n"
            "facttable_with_expenses AS (\n"
            "  SELECT MonthlyRegionKey, Account, CAST(NULL AS NUMBER) AS ExpenseActual FROM facttable\n"
            "  UNION ALL\n"
            "  SELECT MonthlyRegionKey, Account, ExpenseActual FROM expenses\n"
            "),\n"
            "final_model AS (\n"
            "  SELECT f.MonthlyRegionKey FROM facttable_with_expenses f LEFT JOIN expenses e ON f.MonthlyRegionKey = e.MonthlyRegionKey\n"
            ")\n"
            "SELECT * FROM final_model"
        )
        issues = validate_fact_expenses_union_semantics(sql)
        self.assertTrue(any('LEFTOVER_EXPENSES_REJOIN' in issue for issue in issues), issues)

    def test_qlik_semantic_parity_metadata_warnings(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable_with_expenses AS (SELECT MonthlyRegionKey, CustKey, CustKeyAR, \"Address Number\", YYYYMM FROM src),\n"
            "final_model AS (SELECT f.MonthlyRegionKey FROM facttable_with_expenses f)\n"
            "SELECT * FROM final_model"
        )
        plan = [
            make_load('Calendar', ['YYYYMM', 'FiscalYear'], source='Calendar.qvd'),
            make_load('ItemBranchMaster', ['Item-Branch Key', 'Short Name'], source='ItemBranchMaster.qvd'),
            make_load('ItemMaster', ['Short Name', 'Product Group', 'Product Type'], source='ItemMaster.qvd'),
            make_load('ProductGroupMaster', ['Product Group'], source='ProductGroupMaster.qvd'),
            make_load('ProductTypeMaster', ['Product Type'], source='ProductTypeMaster.qvd'),
            make_load('ARSummary', ['CustKeyAR'], source='ARSummary.qvd'),
            make_load('CustomerMaster', ['Address Number'], source='CustomerMaster.qvd'),
            make_load('Budget', ['MonthlyRegionKey'], source='Budget.qvd'),
        ]
        issues = validate_qlik_semantic_parity(sql, plan)
        expected = {
            'MISSING_CALENDAR_ENRICHMENT',
            'INCOMPLETE_PRODUCT_HIERARCHY',
            'MISSING_AR_ENRICHMENT',
            'MISSING_CUSTOMER_ENRICHMENT',
            'MISSING_BUDGET_ENRICHMENT',
        }
        for code in expected:
            self.assertTrue(any(code in issue for issue in issues), issues)
        self.assertTrue(all(validation_issue_category(issue) == 'metadata_warning' for issue in issues), issues)

    def test_execution_safety_detects_cleanup_corruption(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH final_model AS (\n"
            "  SELECT f.MonthlyRegionKey FROM facttable_with_expenses f\n"
            "   AND f.Account = e.AccountLEFT JOIN customermap cmap ON f.CustKey = cmap.CustKey\n"
            ")\n"
            "SELECT * FROM final_model"
        )
        issues = validate_execution_safety(sql)
        self.assertTrue(any('SQL_CLEANUP_CORRUPTION' in issue for issue in issues), issues)

    def test_finalize_removes_final_model_expenses_rejoin_after_union(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable AS (\n"
            "  SELECT MonthlyRegionKey, Region, Account, YYYYMM FROM {{ source('raw','FactTable') }}\n"
            "),\n"
            "expenses AS (\n"
            "  SELECT MonthlyRegionKey, Region, Account, YYYYMM, ExpenseActual, ExpenseBudget FROM {{ source('raw','Expenses') }}\n"
            "),\n"
            "facttable_with_expenses AS (\n"
            "  SELECT MonthlyRegionKey, Region, Account, YYYYMM, CAST(NULL AS NUMBER) AS ExpenseActual, CAST(NULL AS NUMBER) AS ExpenseBudget FROM facttable\n"
            "  UNION ALL\n"
            "  SELECT MonthlyRegionKey, Region, Account, YYYYMM, ExpenseActual, ExpenseBudget FROM expenses\n"
            "),\n"
            "final_model AS (\n"
            "  SELECT f.MonthlyRegionKey, e.Account, e.ExpenseActual, e.ExpenseBudget\n"
            "  FROM facttable_with_expenses f\n"
            "  LEFT JOIN expenses e ON f.MonthlyRegionKey = e.MonthlyRegionKey\n"
            ")\n"
            "SELECT * FROM final_model"
        )
        finalized = finalize_generated_sql(sql)
        final_body = extract_cte_body(finalized, 'final_model')
        self.assertNotRegex(finalized, r'(?is)\bLEFT\s+JOIN\s+expenses\b')
        self.assertNotIn('e.*', final_body)
        self.assertNotIn('e.Account', final_body)
        self.assertNotIn('e.ExpenseActual', final_body)
        self.assertNotIn('e.ExpenseBudget', final_body)

    def test_expenses_cleanup_removes_full_on_clause_and_keeps_next_join(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable_with_expenses AS (\n"
            "  SELECT MonthlyRegionKey, Account, CustKey, ExpenseActual FROM facttable\n"
            "),\n"
            "expenses AS (\n"
            "  SELECT MonthlyRegionKey, Account, ExpenseActual FROM expenses_src\n"
            "),\n"
            "customermap AS (\n"
            "  SELECT CustKey FROM customer_src\n"
            "),\n"
            "final_model AS (\n"
            "  SELECT f.*, e.account, e.expenseactual\n"
            "  FROM facttable_with_expenses f\n"
            "  LEFT JOIN expenses e\n"
            "    ON f.MonthlyRegionKey = e.MonthlyRegionKey\n"
            "   AND f.Account = e.Account\n"
            "  LEFT JOIN customermap cmap ON f.CustKey = cmap.CustKey\n"
            ")\n"
            "SELECT * FROM final_model"
        )
        finalized = finalize_generated_sql(sql)
        final_body = extract_cte_body(finalized, 'final_model')
        self.assertNotRegex(final_body, r'(?is)\bJOIN\s+expenses\b')
        self.assertNotRegex(final_body, r'(?i)\be\.(?:account|expenseactual)\b')
        self.assertNotIn('AND f.Account = e.Account', final_body)
        self.assertNotIn('AccountLEFT JOIN', final_body)
        self.assertIn('LEFT JOIN customermap cmap ON f.CustKey = cmap.CustKey', final_body)

    def test_expenses_cleanup_rewrites_leftover_account_join_to_fact_alias(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable_with_expenses AS (\n"
            "  SELECT monthlyregionkey, account FROM fact_src\n"
            "),\n"
            "accounts AS (\n"
            "  SELECT account, account_name FROM account_src\n"
            "),\n"
            "final_model AS (\n"
            "  SELECT f.monthlyregionkey, a.account_name\n"
            "  FROM facttable_with_expenses f\n"
            "  LEFT JOIN accounts a ON e.account = a.account\n"
            ")\n"
            "SELECT * FROM final_model"
        )
        finalized = finalize_generated_sql(sql)
        final_body = extract_cte_body(finalized, 'final_model')
        self.assertIn('LEFT JOIN accounts a ON f.account = a.account', final_body)
        self.assertNotRegex(final_body, r'(?is)\be\s*\.')
        issues = validate_generated_sql(finalized, dialect='dbt')
        self.assertFalse(any('ALIAS_COLUMN_NOT_FOUND: alias e' in issue for issue in issues), issues)
        self.assertFalse(any('SQL_CLEANUP_LEFTOVER_ALIAS_E' in issue for issue in issues), issues)

    def test_validator_catches_leftover_e_alias_after_cleanup(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH final_model AS (\n"
            "  SELECT f.*, e.account\n"
            "  FROM facttable_with_expenses f\n"
            ")\n"
            "SELECT * FROM final_model"
        )
        issues = validate_generated_sql(sql, dialect='dbt')
        self.assertTrue(any('SQL_CLEANUP_LEFTOVER_ALIAS_E' in issue for issue in issues), issues)

    def test_validate_candidate_integrity_blocks_alias_star_union(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable AS (\n"
            "  SELECT MonthlyRegionKey, Region, YYYYMM FROM {{ source('raw','FactTable') }}\n"
            "),\n"
            "expenses AS (\n"
            "  SELECT MonthlyRegionKey, Account FROM {{ source('raw','Expenses') }}\n"
            "),\n"
            "facttable_with_expenses AS (\n"
            "  SELECT f.*, CAST(NULL AS VARCHAR) AS account FROM facttable f\n"
            "  UNION ALL\n"
            "  SELECT MonthlyRegionKey, Account FROM expenses\n"
            ")\n"
            "SELECT * FROM facttable_with_expenses"
        )
        issues = validate_candidate_integrity(sql)
        self.assertTrue(any('UNION_SELECT_STAR_BRANCH' in i for i in issues), issues)

    def test_dynamic_union_rebuild_fails_without_schema(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH expenses AS (\n"
            "  SELECT yyyymm, account, expense_actual FROM {{ source('raw','Expenses') }}\n"
            "),\n"
            "facttable_with_expenses AS (\n"
            "  SELECT f.*, account FROM facttable f\n"
            "  UNION ALL\n"
            "  SELECT yyyymm, account, expense_actual FROM expenses\n"
            ")\n"
            "SELECT * FROM facttable_with_expenses"
        )
        out = finalize_generated_sql(sql)
        issues = validate_candidate_integrity(out)
        self.assertTrue(any('DYNAMIC_UNION_REBUILD_FAILED' in i for i in issues), issues)

    def test_no_hardcoded_executive_dashboard_columns(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable AS (\n"
            "  SELECT sales_key, customer_id, yyyymm, amount FROM {{ source('raw','FactTable') }}\n"
            "),\n"
            "expenses AS (\n"
            "  SELECT yyyymm, account, expense_actual FROM {{ source('raw','Expenses') }}\n"
            "),\n"
            "facttable_with_expenses AS (\n"
            "  SELECT f.* FROM facttable f\n"
            "  UNION ALL\n"
            "  SELECT yyyymm, account, expense_actual FROM expenses\n"
            ")\n"
            "SELECT * FROM facttable_with_expenses"
        )
        out = finalize_generated_sql(sql)
        for forbidden in ('"Address Number"', '"Invoice Number"', '"Item-Branch Key"', '"Sales Amount"'):
            self.assertNotIn(forbidden, out)

    def test_repair_attempted_true_when_repair_call_fails(self):
        try:
            import backend.app as app_mod
        except ModuleNotFoundError:
            self.skipTest('backend.app import unavailable in this test environment')

        plan = [make_load('FactTable', ['CustKey'], source='FactTable.qvd')]
        quick_result = {
            'status': 'complete',
            'sql': (
                "{{ config(materialized='table') }}\n"
                "WITH facttable AS (SELECT CustKey FROM {{ source('raw','FactTable') }})\n"
                "SELECT * FROM facttable"
            ),
            'description': '',
        }
        with patch.object(app_mod, 'request_migration_one_shot', return_value=quick_result), \
             patch.object(app_mod, '_audit_generated_sql_against_plan', return_value=['ALIAS_COLUMN_NOT_FOUND: fail']), \
             patch.object(app_mod, '_generic_one_shot_quality_issues', return_value=[]), \
             patch.object(app_mod, 'repair_generated_sql', side_effect=RuntimeError('repair boom')):
            result = app_mod.migrate_qvs_to_dbt(
                'FactTable: LOAD CustKey FROM FactTable.qvd;',
                plan=plan,
                plan_text='FactTable',
                generation_mode='one_shot',
            )
        self.assertTrue(result.get('used_one_shot_repair'))

    def test_post_repair_metadata_only_issues_do_not_enter_loop(self):
        try:
            import backend.app as app_mod
        except ModuleNotFoundError:
            self.skipTest('backend.app import unavailable in this test environment')

        plan = [make_load('FactTable', ['CustKey'], source='FactTable.qvd')]
        quick_result = {
            'status': 'complete',
            'sql': "{{ config(materialized='table') }}\nWITH facttable AS (SELECT CustKey FROM {{ source('raw','FactTable') }})\nSELECT * FROM facttable",
            'description': '',
            'final_sql': '',
        }
        metadata_issues = [
            'UNREACHABLE_CTE_CREATED_NOT_USED: accountmaster',
            'IR_AMBIGUITY: date ambiguity',
            'ISLAND_TABLE: dim not joinable',
        ]
        with patch.object(app_mod, 'request_migration_one_shot', return_value=quick_result), \
             patch.object(app_mod, '_audit_generated_sql_against_plan', side_effect=[['JOIN_KEY_MISSING: x'], metadata_issues]), \
             patch.object(app_mod, '_generic_one_shot_quality_issues', return_value=[]), \
             patch.object(app_mod, '_one_shot_repair_once', return_value=(quick_result, metadata_issues, True)) as repair_once, \
             patch.object(app_mod, 'request_migration_with_validation') as loop_call:
            result = app_mod.migrate_qvs_to_dbt(
                'FactTable: LOAD CustKey FROM FactTable.qvd;',
                plan=plan,
                plan_text='FactTable',
                generation_mode='auto',
            )
        repair_once.assert_called_once()
        loop_call.assert_not_called()
        self.assertEqual(result.get('status'), 'complete_with_validation_issues')
        self.assertEqual(result.get('one_shot_validation_status'), 'passed_with_warnings_after_repair')
        self.assertFalse(result.get('loopNeeded'))
        self.assertEqual(result.get('blockingIssues'), [])

    def test_metadata_only_never_calls_repair_even_with_warning_status_text(self):
        try:
            import backend.app as app_mod
        except ModuleNotFoundError:
            self.skipTest('backend.app import unavailable in this test environment')

        quick_result = {
            'status': 'complete',
            'sql': "{{ config(materialized='table') }}\nSELECT 1",
            'description': '',
            'one_shot_validation_status': 'blocking_issues',
        }
        metadata_issues = [
            'UNREACHABLE_CTE_CREATED_NOT_USED: demo',
            'IR_AMBIGUITY: demo',
            'ISLAND_TABLE: demo',
        ]
        with patch.object(app_mod, 'request_migration_one_shot', return_value=quick_result), \
             patch.object(app_mod, '_audit_generated_sql_against_plan', return_value=metadata_issues), \
             patch.object(app_mod, '_generic_one_shot_quality_issues', return_value=[]), \
             patch.object(app_mod, '_one_shot_repair_once') as repair_once, \
             patch.object(app_mod, 'request_migration_with_validation') as loop_call:
            result = app_mod.migrate_qvs_to_dbt('x', plan=[], plan_text='', generation_mode='auto')
        repair_once.assert_not_called()
        loop_call.assert_not_called()
        self.assertEqual(result.get('status'), 'complete_with_validation_issues')
        self.assertFalse(result.get('loopNeeded'))

    def test_metadata_only_after_repair_attempt_flag_does_not_enter_loop(self):
        try:
            import backend.app as app_mod
        except ModuleNotFoundError:
            self.skipTest('backend.app import unavailable in this test environment')

        plan = [make_load('FactTable', ['CustKey'], source='FactTable.qvd')]
        quick_result = {
            'status': 'complete',
            'sql': "{{ config(materialized='table') }}\nSELECT 1",
            'description': '',
        }
        initial_blocking = ['JOIN_KEY_MISSING: x']
        metadata_issues = ['IR_AMBIGUITY: demo']
        with patch.object(app_mod, 'request_migration_one_shot', return_value=quick_result), \
             patch.object(app_mod, '_audit_generated_sql_against_plan', side_effect=[initial_blocking, metadata_issues]), \
             patch.object(app_mod, '_generic_one_shot_quality_issues', return_value=[]), \
             patch.object(app_mod, '_one_shot_repair_once', return_value=(quick_result, metadata_issues, True)), \
             patch.object(app_mod, 'request_migration_with_validation') as loop_call:
            result = app_mod.migrate_qvs_to_dbt('x', plan=plan, plan_text='p', generation_mode='auto')
        loop_call.assert_not_called()
        self.assertTrue(result.get('used_one_shot_repair'))
        self.assertFalse(result.get('loopNeeded'))

    def test_repair_decision_uses_finalized_sql_not_raw_one_shot_sql(self):
        try:
            import backend.app as app_mod
        except ModuleNotFoundError:
            self.skipTest('backend.app import unavailable in this test environment')

        raw_bad_sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable AS (SELECT 1 AS monthlyregionkey),\n"
            "expenses AS (SELECT 1 AS monthlyregionkey),\n"
            "facttable_with_expenses AS (\n"
            "  SELECT * FROM facttable\n"
            "  UNION ALL\n"
            "  SELECT monthlyregionkey FROM expenses\n"
            "),\n"
            "final_model AS (\n"
            "  SELECT f.*, e.monthlyregionkey\n"
            "  FROM facttable f\n"
            "  LEFT JOIN expenses e ON f.monthlyregionkey = e.monthlyregionkey\n"
            ")\n"
            "SELECT * FROM final_model"
        )
        finalized_good_sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable_with_expenses AS (SELECT 1 AS monthlyregionkey)\n"
            "SELECT * FROM facttable_with_expenses"
        )
        metadata_issues = [
            'UNREACHABLE_CTE_CREATED_NOT_USED: demo',
            'IR_AMBIGUITY: demo',
            'ISLAND_TABLE: demo',
        ]
        quick_result = {'status': 'complete', 'sql': raw_bad_sql, 'description': ''}

        def fake_finalize(sql, plan=None, qvs_script=''):
            return finalized_good_sql

        def fake_audit(sql, plan=None, qvs_script='', dialect='dbt'):
            if sql == finalized_good_sql:
                return metadata_issues
            return ['UNION_COLUMN_COUNT_MISMATCH: should not happen if finalized path is used']

        with patch.object(app_mod, 'request_migration_one_shot', return_value=quick_result), \
             patch.object(app_mod, 'finalize_generated_sql', side_effect=fake_finalize), \
             patch.object(app_mod, '_audit_generated_sql_against_plan', side_effect=fake_audit), \
             patch.object(app_mod, '_generic_one_shot_quality_issues', return_value=[]), \
             patch.object(app_mod, '_one_shot_repair_once') as repair_once, \
             patch.object(app_mod, 'request_migration_with_validation') as loop_call:
            result = app_mod.migrate_qvs_to_dbt('x', plan=[], plan_text='', generation_mode='auto')

        repair_once.assert_not_called()
        loop_call.assert_not_called()
        self.assertEqual(result.get('status'), 'complete_with_validation_issues')
        self.assertFalse(result.get('loopNeeded'))

    def test_one_shot_does_not_repair_false_union_mismatch_after_finalization(self):
        try:
            import backend.app as app_mod
        except ModuleNotFoundError:
            self.skipTest('backend.app import unavailable in this test environment')

        finalized_sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable AS (\n"
            "  SELECT MonthlyRegionKey, Region, \"Address Number\", CustKey FROM src\n"
            "),\n"
            "expenses AS (\n"
            "  SELECT MonthlyRegionKey, Region, Account, YYYYMM, ExpenseActual, ExpenseBudget FROM src\n"
            "),\n"
            "facttable_with_expenses AS (\n"
            "  SELECT MonthlyRegionKey, Region, \"Address Number\", CustKey, CAST(NULL AS VARCHAR) AS Account FROM facttable\n"
            "  UNION ALL\n"
            "  SELECT MonthlyRegionKey, Region, CAST(NULL AS NUMBER) AS \"Address Number\", CAST(NULL AS NUMBER) AS CustKey, Account FROM expenses\n"
            "),\n"
            "final_model AS (\n"
            "  SELECT f.MonthlyRegionKey, f.Account FROM facttable_with_expenses f\n"
            ")\n"
            "SELECT * FROM final_model"
        )
        quick_result = {'status': 'complete', 'sql': finalized_sql, 'description': ''}
        stale_issue = ['UNION_COLUMN_COUNT_MISMATCH: stale parser false positive']

        with patch.object(app_mod, 'request_migration_one_shot', return_value=quick_result), \
             patch.object(app_mod, 'finalize_generated_sql', return_value=finalized_sql), \
             patch.object(app_mod, '_audit_generated_sql_against_plan', return_value=stale_issue), \
             patch.object(app_mod, '_generic_one_shot_quality_issues', return_value=[]), \
             patch.object(app_mod, '_one_shot_repair_once') as repair_once, \
             patch.object(app_mod, 'request_migration_with_validation') as loop_call:
            result = app_mod.migrate_qvs_to_dbt('x', plan=[], plan_text='', generation_mode='auto')

        repair_once.assert_not_called()
        loop_call.assert_not_called()
        self.assertFalse(result.get('loopNeeded'))
        self.assertEqual(result.get('blockingIssues'), [])
        self.assertEqual(result.get('status'), 'complete_with_validation_issues')
        self.assertTrue(any('SAFE_UNION_OVERRIDE' in issue for issue in result.get('validation_issues', [])), result.get('validation_issues'))
        self.assertFalse(any('UNION_COLUMN_COUNT_MISMATCH' in issue for issue in result.get('validation_issues', [])), result.get('validation_issues'))

    def test_safe_union_override_runs_before_repair_decision_for_old_mismatch_text(self):
        try:
            import backend.app as app_mod
        except ModuleNotFoundError:
            self.skipTest('backend.app import unavailable in this test environment')

        finalized_sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable AS (\n"
            "  SELECT MonthlyRegionKey, Region, \"Address Number\", CustKey FROM src\n"
            "),\n"
            "expenses AS (\n"
            "  SELECT MonthlyRegionKey, Region, Account, YYYYMM, ExpenseActual, ExpenseBudget FROM src\n"
            "),\n"
            "facttable_with_expenses AS (\n"
            "  SELECT MonthlyRegionKey, Region, \"Address Number\", CustKey, CAST(NULL AS VARCHAR) AS Account FROM facttable\n"
            "  UNION ALL\n"
            "  SELECT MonthlyRegionKey, Region, CAST(NULL AS NUMBER) AS \"Address Number\", CAST(NULL AS NUMBER) AS CustKey, Account FROM expenses\n"
            "),\n"
            "final_model AS (\n"
            "  SELECT f.MonthlyRegionKey, f.Region, f.\"Address Number\", f.CustKey, f.Account\n"
            "  FROM facttable_with_expenses f\n"
            ")\n"
            "SELECT * FROM final_model"
        )
        old_mismatch = [
            'UNION_COLUMN_COUNT_MISMATCH: UNION ALL branches have different column counts (found counts: [2, 25]).'
        ]
        quick_result = {'status': 'complete', 'sql': finalized_sql, 'description': ''}

        with patch.object(app_mod, 'request_migration_one_shot', return_value=quick_result), \
             patch.object(app_mod, 'finalize_generated_sql', return_value=finalized_sql), \
             patch.object(app_mod, '_audit_generated_sql_against_plan', return_value=old_mismatch), \
             patch.object(app_mod, '_generic_one_shot_quality_issues', return_value=[]), \
             patch.object(app_mod, '_one_shot_repair_once') as repair_once, \
             patch.object(app_mod, 'request_migration_with_validation') as loop_call:
            result = app_mod.migrate_qvs_to_dbt('x', plan=[], plan_text='', generation_mode='auto')

        repair_once.assert_not_called()
        loop_call.assert_not_called()
        self.assertIn(result.get('status'), {'complete', 'complete_with_validation_issues'})
        self.assertEqual(result.get('blockingIssues'), [])
        self.assertFalse(result.get('loopNeeded'))
        self.assertTrue(any('SAFE_UNION_OVERRIDE' in issue for issue in result.get('validation_issues', [])), result.get('validation_issues'))
        self.assertFalse(any('UNION_COLUMN_COUNT_MISMATCH' in issue for issue in result.get('validation_issues', [])), result.get('validation_issues'))
        overridden = app_mod._apply_safe_union_override(finalized_sql, old_mismatch, status='complete')[0]
        self.assertEqual(app_mod._real_blocking_issues(overridden), [])

    def test_forced_safe_union_filter_prevents_repair_when_override_path_misses(self):
        try:
            import backend.app as app_mod
        except ModuleNotFoundError:
            self.skipTest('backend.app import unavailable in this test environment')

        finalized_sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable AS (\n"
            "  SELECT monthlyregionkey, region, custkey, account FROM src\n"
            "),\n"
            "expenses AS (\n"
            "  SELECT monthlyregionkey, region, account, expenseactual FROM src\n"
            "),\n"
            "facttable_with_expenses AS (\n"
            "  SELECT monthlyregionkey, region, custkey, account, CAST(NULL AS NUMBER) AS expenseactual FROM facttable\n"
            "  UNION ALL\n"
            "  SELECT monthlyregionkey, region, CAST(NULL AS NUMBER) AS custkey, account, expenseactual FROM expenses\n"
            "),\n"
            "final_model AS (\n"
            "  SELECT f.monthlyregionkey, f.region, f.custkey, f.account, f.expenseactual\n"
            "  FROM facttable_with_expenses f\n"
            ")\n"
            "SELECT * FROM final_model"
        )
        old_mismatch = [
            'UNION_COLUMN_COUNT_MISMATCH: UNION ALL branches have different column counts (found counts: [2, 25])'
        ]
        quick_result = {'status': 'complete', 'sql': finalized_sql, 'description': ''}

        def no_op_override(sql, issues, status=''):
            return list(issues or []), False

        with patch.object(app_mod, 'request_migration_one_shot', return_value=quick_result), \
             patch.object(app_mod, 'finalize_generated_sql', return_value=finalized_sql), \
             patch.object(app_mod, '_audit_generated_sql_against_plan', return_value=old_mismatch), \
             patch.object(app_mod, '_generic_one_shot_quality_issues', return_value=[]), \
             patch.object(app_mod, '_apply_safe_union_override', side_effect=no_op_override), \
             patch.object(app_mod, '_one_shot_repair_once') as repair_once, \
             patch.object(app_mod, 'request_migration_with_validation') as loop_call:
            result = app_mod.migrate_qvs_to_dbt('x', plan=[], plan_text='', generation_mode='auto')

        repair_once.assert_not_called()
        loop_call.assert_not_called()
        self.assertEqual(result.get('blockingIssues'), [])
        self.assertFalse(result.get('loopNeeded'))
        self.assertTrue(any('SAFE_UNION_OVERRIDE' in issue for issue in result.get('validation_issues', [])), result.get('validation_issues'))
        self.assertFalse(any('UNION_COLUMN_COUNT_MISMATCH' in issue for issue in result.get('validation_issues', [])), result.get('validation_issues'))

    def test_one_shot_quality_warnings_do_not_trigger_repair_or_loop(self):
        try:
            import backend.app as app_mod
        except ModuleNotFoundError:
            self.skipTest('backend.app import unavailable in this test environment')

        quick_result = {
            'status': 'complete',
            'sql': "{{ config(materialized='table') }}\nSELECT 1",
            'description': '',
        }
        quality_issues = [
            'MISSING_AGGREGATION_CTE: none',
            'MISSING_PRODUCT_BRIDGE_JOIN: none',
            'MISSING_PRODUCT_MASTER_JOIN: none',
            'UNUSED_ACCOUNT_MASTER: none',
        ]
        with patch.object(app_mod, 'request_migration_one_shot', return_value=quick_result), \
             patch.object(app_mod, '_audit_generated_sql_against_plan', return_value=quality_issues), \
             patch.object(app_mod, '_generic_one_shot_quality_issues', return_value=[]), \
             patch.object(app_mod, '_one_shot_repair_once') as repair_once, \
             patch.object(app_mod, 'request_migration_with_validation') as loop_call:
            result = app_mod.migrate_qvs_to_dbt('x', plan=[], plan_text='', generation_mode='auto')
        repair_once.assert_not_called()
        loop_call.assert_not_called()
        self.assertFalse(result.get('loopNeeded'))
        self.assertEqual(result.get('blockingIssues'), [])

    def test_unused_account_group_and_missing_arsummary1_do_not_trigger_loop(self):
        try:
            import backend.app as app_mod
        except ModuleNotFoundError:
            self.skipTest('backend.app import unavailable in this test environment')

        quick_result = {
            'status': 'complete',
            'sql': "{{ config(materialized='table') }}\nSELECT 1",
            'description': '',
        }
        warnings = [
            'UNUSED_ACCOUNT_GROUP_MASTER: not joined',
            'MISSING_ARSUMMARY_1_JOIN: expected arsummary_1 join',
        ]
        with patch.object(app_mod, 'request_migration_one_shot', return_value=quick_result), \
             patch.object(app_mod, '_audit_generated_sql_against_plan', return_value=warnings), \
             patch.object(app_mod, '_generic_one_shot_quality_issues', return_value=[]), \
             patch.object(app_mod, '_one_shot_repair_once') as repair_once, \
             patch.object(app_mod, 'request_migration_with_validation') as loop_call:
            result = app_mod.migrate_qvs_to_dbt('x', plan=[], plan_text='', generation_mode='auto')
        repair_once.assert_not_called()
        loop_call.assert_not_called()
        self.assertFalse(result.get('loopNeeded'))

    def test_regeneration_persistence_keeps_plan_context_and_status(self):
        try:
            import backend.app as app_mod
        except ModuleNotFoundError:
            self.skipTest('backend.app import unavailable in this test environment')

        migration_result = {
            'status': 'complete_with_validation_issues',
            'sql': "{{ config(materialized='table') }}\nWITH final_model AS (SELECT 1 AS id)\nSELECT * FROM final_model",
            'final_sql': '',
            'description': 'desc',
            'validation_issues': ['IR_AMBIGUITY: demo'],
            'repairAttempted': False,
            'loopNeeded': False,
        }
        cached_plan = {
            'plan': [make_load('FactTable', ['id'], source='FactTable.qvd')],
            'planText': 'FactTable',
        }
        with patch.object(app_mod, 'migrate_qvs_to_dbt', return_value=migration_result), \
             patch.object(app_mod, 'finalize_generated_sql', side_effect=lambda s, plan=None, qvs_script='': s) as finalize_mock, \
             patch.object(app_mod, 'validate_migration_sql', return_value=[]), \
             patch.object(app_mod, 'validate_generated_sql', return_value=[]), \
             patch.object(app_mod, 'maybe_store_regeneration_state'), \
             patch.object(app_mod, 'finalize_regeneration_history_entry'):
            app_mod.run_regeneration_job(
                job_id='job1',
                session_id='s1',
                file_id='f1',
                edited_sql='',
                edited_text='',
                regenerated_sql='',
                regenerated_text='',
                dialect='dbt',
                combined_scripts='LOAD id FROM FactTable.qvd;',
                cached_plan=cached_plan,
                input_hash='h1',
                trigger_migration=True,
                generation_mode='auto',
            )

        self.assertTrue(finalize_mock.called)
        for call in finalize_mock.call_args_list:
            self.assertIn('plan', call.kwargs)
            self.assertIsNotNone(call.kwargs.get('plan'))

        with app_mod.REGENERATION_LOCK:
            job = app_mod.REGENERATION_JOBS.get('job1') or {}
        result = (job.get('result') or {})
        self.assertEqual(job.get('status'), 'complete_with_validation_issues')
        self.assertEqual(result.get('status'), 'complete_with_validation_issues')
        self.assertGreater(len(result.get('sql') or ''), 0)
        self.assertIn('validationReport', result)
        self.assertIn('validationArtifacts', result)


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

    def test_finalize_restores_arsummary_1_hyphenated_source_name(self):
        plan = [make_load('ARSummary_1', ['CustKeyAR'], source='ARSummary-1', sources=['ARSummary-1'])]
        sql = "{{ config(materialized='table') }}\nWITH arsummary_1 AS (SELECT CustKeyAR FROM {{ source('raw','ARSummary_1') }})\nSELECT * FROM arsummary_1"
        finalized = finalize_generated_sql(sql, plan=plan)
        self.assertIn("{{ source('raw', 'ARSummary-1') }}", finalized)
        codes = {issue.code for issue in validate_migration_sql(finalized, plan=plan, dialect='dbt')}
        self.assertNotIn('SOURCE_TABLE_RENAMED', codes)

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

    def test_auto_mode_with_progress_callback_assigns_one_shot_result(self):
        try:
            import flask  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest('Flask is not installed in this test environment')
        import backend.app as app_mod

        progress_messages = []
        one_shot_result = {
            'status': 'complete',
            'sql': '{{ config(materialized="table") }}\nSELECT 1 AS id',
            'final_sql': '{{ config(materialized="table") }}\nSELECT 1 AS id',
            'validation_issues': [],
        }
        with patch.object(app_mod, 'request_migration_one_shot', return_value=one_shot_result) as one_shot, \
             patch.object(app_mod, '_audit_generated_sql_against_plan', return_value=[]), \
             patch.object(app_mod, '_generic_one_shot_quality_issues', return_value=[]), \
             patch.object(app_mod, 'request_migration_with_validation') as loop:
            result = app_mod.migrate_qvs_to_dbt(
                'FactTable:\nLOAD A FROM FactTable.qvd;',
                dialect='dbt',
                generation_mode='auto',
                progress_callback=progress_messages.append,
            )

        one_shot.assert_called_once()
        loop.assert_not_called()
        self.assertEqual(progress_messages[0], 'Selected generation mode: auto')
        self.assertEqual(result['selected_generation_mode'], 'auto')
        self.assertEqual(result['one_shot_validation_status'], 'passed')

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
            'UNREACHABLE_CTE_CREATED_NOT_USED: accountmaster',
            'IR_AMBIGUITY: cannot determine storage type',
            "ISLAND_TABLE: Table 'AccountGroupMaster' has no shared key",
        ]
        with patch.object(app_mod, 'request_migration_one_shot', return_value=one_shot_result) as one_shot, \
             patch.object(app_mod, '_audit_generated_sql_against_plan', return_value=metadata_issues), \
             patch.object(app_mod, 'repair_generated_sql') as repair, \
             patch.object(app_mod, 'request_migration_with_validation') as loop:
            result = app_mod.migrate_qvs_to_dbt(
                'FactTable:\nLOAD A FROM FactTable.qvd;',
                dialect='dbt',
                generation_mode='auto',
            )
        one_shot.assert_called_once()
        repair.assert_not_called()
        loop.assert_not_called()
        self.assertEqual(result['status'], 'complete_with_validation_issues')
        self.assertFalse(result['loopNeeded'])
        self.assertEqual(result['blockingIssues'], [])
        self.assertEqual(result['one_shot_validation_status'], 'passed_with_warnings')
        self.assertEqual(result['validation_issues'], metadata_issues)

    def test_auto_mode_exposes_contract_and_quality_diagnostics(self):
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
        with patch.object(app_mod, 'request_migration_one_shot', return_value=one_shot_result), \
             patch.object(app_mod, '_audit_generated_sql_against_plan', return_value=[]), \
             patch.object(app_mod, 'request_migration_with_validation') as loop:
            result = app_mod.migrate_qvs_to_dbt(
                'FactTable:\nLOAD A FROM FactTable.qvd;',
                dialect='dbt',
                generation_mode='auto',
            )
        loop.assert_not_called()
        self.assertIn('oneShotQualityScore', result)
        self.assertIn('joinContractCoverage', result)
        self.assertIn('loopPolicy', result)

    def test_skip_ai_repair_avoids_repair_and_loop_calls(self):
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
        with patch.dict(os.environ, {'SKIP_AI_REPAIR': 'true'}), \
             patch.object(app_mod, 'request_migration_one_shot', return_value=one_shot_result) as one_shot, \
             patch.object(app_mod, '_audit_generated_sql_against_plan', return_value=['ALIAS_COLUMN_NOT_FOUND: demo']), \
             patch.object(app_mod, '_generic_one_shot_quality_issues', return_value=[]), \
             patch.object(app_mod, '_one_shot_repair_once') as repair_once, \
             patch.object(app_mod, 'request_migration_with_validation') as loop:
            result = app_mod.migrate_qvs_to_dbt(
                'FactTable:\nLOAD A FROM FactTable.qvd;',
                dialect='dbt',
                generation_mode='auto',
            )
        one_shot.assert_called_once()
        repair_once.assert_not_called()
        loop.assert_not_called()
        self.assertTrue(result.get('skipAiRepair'))
        self.assertFalse(result.get('repairAttempted'))
        self.assertFalse(result.get('loopNeeded'))
        self.assertEqual(result.get('selected_generation_mode'), 'one_shot')
        self.assertEqual(result.get('status'), 'complete_with_validation_issues')

    def test_regenerate_cache_hit_avoids_generation_submit_and_returns_metadata(self):
        try:
            import flask  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest('Flask is not installed in this test environment')
        import backend.app as app_mod

        cached_payload = {
            'status': 'complete',
            'sql': "{{ config(materialized='table') }}\nWITH final_model AS (SELECT 1 AS id)\nSELECT * FROM final_model",
            'description': 'cached',
            'warnings': [],
        }
        cached_row = {
            'id': 'cached_job',
            'status': 'complete',
            'created_at': '2026-05-27T00:00:00',
            'completed_at': '2026-05-27T00:00:01',
            'prompt_version': 'test',
            'model': 'test-model',
        }
        bundle = {
            'latest': {'file_id': 'f1'},
            'cached_plan': {
                'plan': [make_load('FactTable', ['id'], source='FactTable.qvd')],
                'planText': 'FactTable',
                'hash': 'plan-hash',
            },
            'scripts_context': 'FactTable:\nLOAD id FROM FactTable.qvd;',
        }
        client = app_mod.app.test_client()
        with patch.dict(os.environ, {'USE_CACHED_GENERATION': 'true', 'SKIP_AI_REPAIR': 'true'}), \
             patch.object(app_mod, 'build_session_bundle', return_value=bundle), \
             patch.object(app_mod, 'find_cached_generation_result', return_value=(cached_row, cached_payload)) as cache_lookup, \
             patch.object(app_mod.REGENERATION_EXECUTOR, 'submit') as submit, \
             patch.object(app_mod, 'load_regeneration_history', return_value=[]):
            response = client.post('/api/regenerate', json={
                'sessionId': 's1',
                'triggerMigration': True,
                'generationMode': 'auto',
                'dialect': 'dbt',
            })
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        cache_lookup.assert_called_once()
        submit.assert_not_called()
        self.assertFalse(payload.get('queued'))
        self.assertEqual(payload.get('jobId'), 'cached_job')
        self.assertTrue(payload.get('usedCachedGeneration'))
        self.assertTrue(payload.get('skipAiRepair'))
        self.assertTrue(payload.get('regeneration', {}).get('usedCachedGeneration'))
        self.assertTrue(payload.get('regeneration', {}).get('skipAiRepair'))

    def test_repeated_finalize_outputs_identical_sql(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable_with_expenses AS (SELECT CustKey, YYYYMM FROM src),\n"
            "customermap AS (SELECT CustKey, CustKeyAR FROM src),\n"
            "calendar AS (SELECT YYYYMM, \"Fiscal Quarter\" FROM src),\n"
            "final_model AS (\n"
            "  SELECT cal.\"Fiscal Quarter\", f.*, cmap.CustKeyAR, cmap.CustKeyAR AS cmap_custkeyar\n"
            "  FROM facttable_with_expenses f\n"
            "  LEFT JOIN calendar cal ON f.YYYYMM = cal.YYYYMM\n"
            "  LEFT JOIN customermap cmap ON f.CustKey = cmap.CustKey\n"
            ")\n"
            "SELECT * FROM final_model"
        )
        first = finalize_generated_sql(sql)
        second = finalize_generated_sql(first)
        self.assertEqual(first, second)

    def test_deterministic_final_model_removes_duplicates_and_sorts_enrichment(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH final_model AS (\n"
            "  SELECT cal.\"Fiscal Quarter\", f.*, cmap.CustKeyAR, cmap.CustKeyAR AS cmap_custkeyar, cust.\"Customer Number\"\n"
            "  FROM facttable_with_expenses f\n"
            "  LEFT JOIN calendar cal ON f.YYYYMM = cal.YYYYMM\n"
            "  LEFT JOIN customermaster cust ON f.\"Address Number\" = cust.\"Address Number\"\n"
            "  LEFT JOIN customermap cmap ON f.CustKey = cmap.CustKey\n"
            ")\nSELECT * FROM final_model"
        )
        out = deterministic_finalize_sql_structure(sql)
        body = extract_cte_body(out, 'final_model')
        select_list = body.split('FROM facttable_with_expenses', 1)[0]
        self.assertIn('f.*', select_list)
        self.assertEqual(select_list.count('cmap.CustKeyAR'), 1)
        self.assertLess(select_list.index('f.*'), select_list.index('cal."Fiscal Quarter"'))
        self.assertLess(body.index('JOIN customermap'), body.index('JOIN customermaster'))
        self.assertLess(body.index('JOIN customermaster'), body.index('JOIN calendar'))

    def test_sql_fingerprint_stable_across_whitespace(self):
        a = "SELECT  a , b  FROM x WHERE a = 1"
        b = " select a,b from x where a=1 "
        self.assertEqual(generate_sql_fingerprint(a), generate_sql_fingerprint(b))

    def test_validation_artifact_generation_order_is_stable(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH final_model AS (SELECT b, a, MonthlyRegionKey FROM {{ source('raw', 'FactTable') }})\n"
            "SELECT * FROM final_model"
        )
        report = build_migration_validation_report(sql, model_name='executive_dashboard')
        first = generate_validation_artifacts(sql, report, model_name='executive_dashboard')
        second = generate_validation_artifacts(sql, report, model_name='executive_dashboard')
        self.assertEqual(list(first['tests'].keys()), sorted(first['tests'].keys()))
        self.assertEqual(list(first['analyses'].keys()), sorted(first['analyses'].keys()))
        self.assertEqual(first['schema_yml'], second['schema_yml'])
        self.assertLess(first['schema_yml'].index('- name: a'), first['schema_yml'].index('- name: b'))

    def test_build_migration_validation_report_has_executive_dashboard_checks(self):
        sql = "{{ config(materialized='table') }}\nSELECT 1 AS id"
        plan = [
            make_load('FactTable', ['Sales Amount', 'Sales Cost Amount', 'Sales Margin Amount'], source='FactTable.qvd'),
            make_load('Expenses', ['ExpenseActual', 'ExpenseBudget'], source='Expenses.qvd'),
            make_load('Budget', ['Budget Amount'], source='Budget.qvd'),
        ]
        report = build_migration_validation_report(sql, plan=plan, dialect='dbt')
        check_ids = {check['id'] for check in report['checks']}

        self.assertEqual(report['dbtCompile']['command'], 'dbt compile --select migration_output')
        self.assertIn('facttable_count', check_ids)
        self.assertIn('expenses_count', check_ids)
        self.assertIn('facttable_with_expenses_count', check_ids)
        self.assertIn('sum_sales_amount', check_ids)
        self.assertIn('sum_sales_cost_amount', check_ids)
        self.assertIn('sum_sales_margin_amount', check_ids)
        self.assertIn('sum_expenseactual', check_ids)
        self.assertIn('sum_expensebudget', check_ids)
        self.assertIn('sum_budget_amount', check_ids)
        self.assertIn('breakdown_by_region', check_ids)
        self.assertIn('breakdown_by_yyyymm', check_ids)
        self.assertIn('breakdown_by_customer_number', check_ids)
        self.assertIn('breakdown_by_sales_rep', check_ids)
        self.assertIn('breakdown_by_product_group', check_ids)
        self.assertEqual(report['summary']['rowCountChecks'], 3)
        self.assertEqual(report['summary']['metricTotalChecks'], 6)
        self.assertEqual(report['summary']['dimensionBreakdownChecks'], 5)

    def test_execute_validation_report_no_context_pending(self):
        report = build_migration_validation_report('{{ config(materialized="table") }}\nSELECT 1')
        executed = execute_validation_report(report, None)
        self.assertEqual(executed['status'], 'pending')
        self.assertEqual(executed['dbtCompile']['status'], 'pending')
        self.assertTrue(all(check['status'] == 'pending' for check in executed['checks']))
        self.assertTrue(all(check['message'] == 'No execution context configured' for check in executed['checks']))

    def test_execute_validation_report_disabled_pending(self):
        report = build_migration_validation_report('{{ config(materialized="table") }}\nSELECT 1')
        executed = execute_validation_report(report, {'enabled': False, 'run_sql': lambda sql: []})
        self.assertEqual(executed['status'], 'pending')
        self.assertTrue(all(check['status'] == 'pending' for check in executed['checks']))
        self.assertTrue(all(check['message'] == 'Validation execution disabled' for check in executed['checks']))

    def test_execute_validation_report_count_check_passes(self):
        report = {
            'dbtCompile': {'command': 'dbt compile --select migration_output'},
            'checks': [{
                'id': 'facttable_with_expenses_count',
                'type': 'row_count',
                'sql': 'select counts',
            }],
        }
        executed = execute_validation_report(report, {
            'enabled': True,
            'run_command': lambda command: {'ok': True},
            'run_sql': lambda sql: [{'facttable_count': 10, 'expenses_count': 3, 'migrated_count': 13, 'variance': 0}],
        })
        self.assertEqual(executed['dbtCompile']['status'], 'passed')
        self.assertEqual(executed['checks'][0]['status'], 'passed')
        self.assertEqual(executed['checks'][0]['difference'], 0.0)

    def test_execute_validation_report_count_check_fails(self):
        report = {
            'dbtCompile': {'command': 'dbt compile --select migration_output'},
            'checks': [{
                'id': 'facttable_with_expenses_count',
                'type': 'row_count',
                'sql': 'select counts',
            }],
        }
        executed = execute_validation_report(report, {
            'enabled': True,
            'run_sql': lambda sql: [{'facttable_count': 10, 'expenses_count': 3, 'migrated_count': 12, 'variance': -1}],
        })
        self.assertEqual(executed['checks'][0]['status'], 'failed')
        self.assertEqual(executed['checks'][0]['difference'], -1.0)

    def test_execute_validation_report_sql_error(self):
        report = {
            'dbtCompile': {'command': 'dbt compile --select migration_output'},
            'checks': [{
                'id': 'facttable_count',
                'type': 'row_count',
                'sql': 'select count',
            }],
        }
        def boom(_sql):
            raise RuntimeError('warehouse down')
        executed = execute_validation_report(report, {'enabled': True, 'run_sql': boom})
        self.assertEqual(executed['checks'][0]['status'], 'error')
        self.assertIn('warehouse down', executed['checks'][0]['message'])

    def test_generate_validation_artifacts_includes_model_and_row_count_test(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH final_model AS (\n"
            "  SELECT MonthlyRegionKey, YYYYMM, Region, \"Sales Amount\", ExpenseActual FROM {{ source('raw', 'FactTable') }}\n"
            ")\n"
            "SELECT * FROM final_model;"
        )
        report = build_migration_validation_report(sql, plan=[
            make_load('FactTable', ['Sales Amount'], source='FactTable.qvd'),
            make_load('Expenses', ['ExpenseActual'], source='Expenses.qvd'),
        ])
        artifacts = generate_validation_artifacts(sql, report, model_name='executive_dashboard')
        self.assertIn('executive_dashboard.sql', artifacts['models'])
        self.assertIn('assert_fact_expenses_row_count.sql', artifacts['tests'])
        self.assertIn('WHERE COALESCE(variance, 0) != 0', artifacts['tests']['assert_fact_expenses_row_count.sql'])
        self.assertNotRegex(artifacts['tests']['assert_fact_expenses_row_count.sql'], r'(?is)\bSELECT\s+\*')
        self.assertIn('migrated_count', artifacts['tests']['assert_fact_expenses_row_count.sql'])
        self.assertIn('expected_count', artifacts['tests']['assert_fact_expenses_row_count.sql'])
        self.assertIn('difference', artifacts['tests']['assert_fact_expenses_row_count.sql'])

    def test_sanitize_test_sql_projection_removes_select_star(self):
        old_sql = (
            "SELECT *\n"
            "FROM (\n"
            "SELECT 1 AS facttable_count, 2 AS expenses_count, 3 AS migrated_count, 0 AS variance\n"
            ") counts\n"
            "WHERE COALESCE(variance, 0) != 0"
        )
        sanitized = sanitize_test_sql_projection(old_sql)
        self.assertNotRegex(sanitized, r'(?is)\bSELECT\s+\*')
        self.assertIn('migrated_count', sanitized)
        self.assertIn('expected_count', sanitized)
        self.assertIn('difference', sanitized)

    def test_generate_validation_artifacts_include_metric_analyses(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH final_model AS (\n"
            "  SELECT Region, YYYYMM, \"Customer Number\", \"Sales Rep\", \"Product Group\", "
            "\"Sales Amount\", \"Sales Cost Amount\", \"Sales Margin Amount\", ExpenseActual, ExpenseBudget, \"Budget Amount\" "
            "FROM {{ source('raw', 'FactTable') }}\n"
            ")\n"
            "SELECT * FROM final_model"
        )
        report = build_migration_validation_report(sql)
        artifacts = generate_validation_artifacts(sql, report)
        self.assertIn('metric_totals.sql', artifacts['analyses'])
        self.assertIn("'Sales Amount' AS metric_name", artifacts['analyses']['metric_totals.sql'])
        self.assertIn('region_breakdown.sql', artifacts['analyses'])
        self.assertIn('month_breakdown.sql', artifacts['analyses'])
        self.assertIn('customer_breakdown.sql', artifacts['analyses'])
        self.assertIn('sales_rep_breakdown.sql', artifacts['analyses'])
        self.assertIn('product_breakdown.sql', artifacts['analyses'])

    def test_generate_validation_artifacts_schema_yml_includes_sources(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH final_model AS (\n"
            "  SELECT MonthlyRegionKey, _HistoryFlag FROM {{ source('raw', 'FactTable') }}\n"
            ")\nSELECT * FROM final_model"
        )
        artifacts = generate_validation_artifacts(sql, build_migration_validation_report(sql))
        schema_yml = artifacts['schema_yml']
        self.assertIn('sources:', schema_yml)
        self.assertIn('- name: raw', schema_yml)
        self.assertIn('- name: FactTable', schema_yml)
        self.assertIn('- name: executive_dashboard', schema_yml)
        self.assertIn('accepted_values:', schema_yml)

    def test_generate_validation_artifact_tests_do_not_end_with_semicolon(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH final_model AS (SELECT MonthlyRegionKey FROM {{ source('raw', 'FactTable') }})\n"
            "SELECT * FROM final_model;"
        )
        artifacts = generate_validation_artifacts(sql, build_migration_validation_report(sql))
        for sql_text in list(artifacts['tests'].values()) + list(artifacts['analyses'].values()):
            self.assertFalse(sql_text.rstrip().endswith(';'), sql_text)

    def test_generate_validation_artifacts_tolerates_missing_fields(self):
        sql = "{{ config(materialized='table') }}\nSELECT 1 AS id"
        artifacts = generate_validation_artifacts(sql, build_migration_validation_report(sql))
        self.assertIn('assert_no_null_monthlyregionkey.sql', artifacts['tests'])
        self.assertIn('MonthlyRegionKey column not detected', artifacts['tests']['assert_no_null_monthlyregionkey.sql'])
        self.assertIn('metric_totals.sql', artifacts['analyses'])
        self.assertIn('No metric columns detected', artifacts['analyses']['metric_totals.sql'])

    def test_all_generated_singular_tests_avoid_select_star(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH final_model AS (\n"
            "  SELECT MonthlyRegionKey, YYYYMM, Region, CustKey, Account, \"Sales Amount\", ExpenseActual FROM {{ source('raw', 'FactTable') }}\n"
            ")\n"
            "SELECT * FROM final_model"
        )
        artifacts = generate_validation_artifacts(sql, build_migration_validation_report(sql))
        for name, test_sql in artifacts['tests'].items():
            self.assertNotRegex(test_sql, r'(?is)\bSELECT\s+\*', f'{name} contains SELECT *:\n{test_sql}')

    def test_export_validation_artifacts_writes_model_schema_tests_and_analyses(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH final_model AS (\n"
            "  SELECT MonthlyRegionKey, YYYYMM, Region, \"Sales Amount\" FROM {{ source('raw', 'FactTable') }}\n"
            ")\nSELECT * FROM final_model"
        )
        artifacts = generate_validation_artifacts(sql, build_migration_validation_report(sql))
        result = export_validation_artifacts(artifacts, 'unit_test_export')
        try:
            self.assertFalse(result['errors'], result)
            self.assertIn('models/executive_dashboard.sql', result['files_written'])
            self.assertIn('models/schema.yml', result['files_written'])
            self.assertIn('tests/assert_fact_expenses_row_count.sql', result['files_written'])
            self.assertIn('analyses/metric_totals.sql', result['files_written'])
            self.assertIn('manifest.json', result['files_written'])
            self.assertTrue(result.get('manifest_path'))
            self.assertTrue(os.path.exists(os.path.join(result['output_dir'], 'models', 'executive_dashboard.sql')))
            self.assertTrue(os.path.exists(os.path.join(result['output_dir'], 'models', 'schema.yml')))
            self.assertTrue(os.path.exists(result['manifest_path']))
        finally:
            if result.get('output_dir'):
                shutil.rmtree(result['output_dir'], ignore_errors=True)

    def test_generate_export_manifest_includes_counts_and_instructions(self):
        artifacts = {
            'models': {'executive_dashboard.sql': 'select 1'},
            'tests': {'assert_a.sql': 'select 1 where 1 = 0', 'assert_b.sql': 'select 1 where 1 = 0'},
            'analyses': {'metric_totals.sql': 'select 1'},
            'schema_yml': 'version: 2',
        }
        manifest = generate_export_manifest(
            artifacts,
            {'files_written': ['dbt_project.yml', 'models/executive_dashboard.sql']},
            metadata={'status': 'complete', 'sqlQualityScore': 94, 'warningsCount': 2},
        )
        self.assertEqual(manifest['artifactCounts']['models'], 1)
        self.assertEqual(manifest['artifactCounts']['tests'], 2)
        self.assertEqual(manifest['artifactCounts']['analyses'], 1)
        self.assertTrue(manifest['hasSchemaYml'])
        self.assertTrue(manifest['hasDbtProject'])
        self.assertEqual(manifest['quality']['sqlQualityScore'], 94)
        self.assertEqual(manifest['quality']['warningsCount'], 2)
        self.assertIn('Run dbt compile', manifest['instructions'])
        self.assertIn('Review analyses SQL outputs', manifest['instructions'])

    def test_generate_export_summary_report_includes_status_quality_and_commands(self):
        artifacts = {
            'models': {'executive_dashboard.sql': 'select 1'},
            'tests': {'assert_a.sql': 'select 1 where 1 = 0'},
            'analyses': {'metric_totals.sql': 'select 1'},
            'schema_yml': 'version: 2',
        }
        manifest = generate_export_manifest(
            artifacts,
            {'files_written': ['summary_report.md', 'models/executive_dashboard.sql']},
            metadata={'status': 'complete'},
        )
        report = generate_export_summary_report(
            artifacts,
            manifest,
            dry_run_result={'status': 'passed'},
            quality={
                'score': 96,
                'passed': ['final_model exists'],
                'warnings': ['metadata warning'],
                'failures': [],
            },
        )
        self.assertIn('# Qlik Migration Validation Package', report)
        self.assertIn('- Dry-run status: passed', report)
        self.assertIn('- Score: 96', report)
        self.assertIn('- final_model exists', report)
        self.assertIn('- metadata warning', report)
        self.assertIn('dbt compile', report)
        self.assertIn('dbt run --select executive_dashboard', report)
        self.assertIn('Qlik associative behavior', report)

    def test_generate_dbt_project_scaffold_contains_project_paths_and_commands(self):
        scaffold = generate_dbt_project_scaffold('qlik migration validation')
        self.assertIn('dbt_project.yml', scaffold)
        self.assertIn('README.md', scaffold)
        self.assertIn('.gitignore', scaffold)
        project_yml = scaffold['dbt_project.yml']
        self.assertIn('name: qlik_migration_validation', project_yml)
        self.assertIn('model-paths: ["models"]', project_yml)
        self.assertIn('test-paths: ["tests"]', project_yml)
        self.assertIn('analysis-paths: ["analyses"]', project_yml)
        self.assertIn('target-path: "target"', project_yml)
        self.assertIn('clean-targets: ["target", "dbt_packages"]', project_yml)
        self.assertIn('dbt compile', scaffold['README.md'])
        self.assertIn('dbt run --select executive_dashboard', scaffold['README.md'])
        self.assertIn('dbt test', scaffold['README.md'])
        self.assertIn('dbt compile --select analyses', scaffold['README.md'])

    def test_export_validation_artifacts_writes_scaffold_files_when_enabled(self):
        artifacts = {'models': {'executive_dashboard.sql': 'select 1'}, 'schema_yml': 'version: 2'}
        result = export_validation_artifacts(artifacts, 'unit_test_scaffold_export', include_project_scaffold=True)
        try:
            self.assertFalse(result['errors'], result)
            self.assertIn('dbt_project.yml', result['files_written'])
            self.assertIn('README.md', result['files_written'])
            self.assertIn('.gitignore', result['files_written'])
            self.assertIn('summary_report.md', result['files_written'])
            self.assertIn('manifest.json', result['files_written'])
            with open(os.path.join(result['output_dir'], 'dbt_project.yml'), encoding='utf-8') as handle:
                project_yml = handle.read()
            self.assertIn('model-paths: ["models"]', project_yml)
            with open(os.path.join(result['output_dir'], 'summary_report.md'), encoding='utf-8') as handle:
                summary = handle.read()
            self.assertIn('# Qlik Migration Validation Package', summary)
            self.assertIn('dbt test', summary)
            with open(os.path.join(result['output_dir'], 'manifest.json'), encoding='utf-8') as handle:
                manifest = json.load(handle)
            self.assertIn('summary_report.md', manifest['files'])
        finally:
            if result.get('output_dir'):
                shutil.rmtree(result['output_dir'], ignore_errors=True)

    def test_export_validation_artifacts_skips_scaffold_when_disabled(self):
        artifacts = {'models': {'executive_dashboard.sql': 'select 1'}, 'schema_yml': 'version: 2'}
        result = export_validation_artifacts(artifacts, 'unit_test_no_scaffold_export', include_project_scaffold=False)
        try:
            self.assertFalse(result['errors'], result)
            self.assertNotIn('dbt_project.yml', result['files_written'])
            self.assertNotIn('README.md', result['files_written'])
            self.assertNotIn('.gitignore', result['files_written'])
            self.assertIn('models/executive_dashboard.sql', result['files_written'])
            self.assertIn('summary_report.md', result['files_written'])
            self.assertIn('manifest.json', result['files_written'])
        finally:
            if result.get('output_dir'):
                shutil.rmtree(result['output_dir'], ignore_errors=True)

    def test_zip_exported_artifacts_creates_archive_with_expected_files(self):
        artifacts = {
            'models': {'executive_dashboard.sql': 'select 1'},
            'tests': {'assert_fact_expenses_row_count.sql': 'select 1 where 1 = 0'},
            'analyses': {'metric_totals.sql': 'select 1'},
            'schema_yml': 'version: 2',
        }
        result = export_validation_artifacts(artifacts, 'unit_test_zip_export', include_project_scaffold=True)
        zip_result = {}
        try:
            os.makedirs(os.path.join(result['output_dir'], 'target'), exist_ok=True)
            os.makedirs(os.path.join(result['output_dir'], 'logs'), exist_ok=True)
            os.makedirs(os.path.join(result['output_dir'], 'dbt_packages'), exist_ok=True)
            with open(os.path.join(result['output_dir'], '.DS_Store'), 'w', encoding='utf-8') as handle:
                handle.write('ignore me')
            with open(os.path.join(result['output_dir'], 'target', 'compiled.sql'), 'w', encoding='utf-8') as handle:
                handle.write('ignore me')
            zip_result = zip_exported_artifacts(result['output_dir'])
            self.assertTrue(os.path.exists(zip_result['zipPath']))
            self.assertIn('dbt_project.yml', zip_result['filesZipped'])
            self.assertIn('summary_report.md', zip_result['filesZipped'])
            self.assertIn('manifest.json', zip_result['filesZipped'])
            self.assertIn('models/executive_dashboard.sql', zip_result['filesZipped'])
            self.assertIn('tests/assert_fact_expenses_row_count.sql', zip_result['filesZipped'])
            self.assertIn('analyses/metric_totals.sql', zip_result['filesZipped'])
            self.assertNotIn('.DS_Store', zip_result['filesZipped'])
            self.assertFalse(any(path.startswith('target/') for path in zip_result['filesZipped']))
            self.assertFalse(any(path.startswith('logs/') for path in zip_result['filesZipped']))
            self.assertFalse(any(path.startswith('dbt_packages/') for path in zip_result['filesZipped']))
            with zipfile.ZipFile(zip_result['zipPath']) as archive:
                names = set(archive.namelist())
            self.assertIn('dbt_project.yml', names)
            self.assertIn('summary_report.md', names)
            self.assertIn('manifest.json', names)
            self.assertIn('models/executive_dashboard.sql', names)
            self.assertIn('tests/assert_fact_expenses_row_count.sql', names)
        finally:
            if result.get('output_dir'):
                shutil.rmtree(result['output_dir'], ignore_errors=True)
            if zip_result.get('zipPath') and os.path.exists(zip_result['zipPath']):
                os.remove(zip_result['zipPath'])

    def _valid_dry_run_artifacts(self):
        model_sql = (
            "{{ config(materialized='table') }}\n"
            "WITH final_model AS (\n"
            "  SELECT 1 AS id\n"
            ")\n"
            "SELECT *\n"
            "FROM final_model"
        )
        return {
            'models': {'executive_dashboard.sql': model_sql},
            'tests': {'assert_no_bad_rows.sql': "SELECT id\nFROM {{ ref('executive_dashboard') }}\nWHERE id IS NULL"},
            'analyses': {'metric_totals.sql': "SELECT COUNT(*) AS row_count\nFROM {{ ref('executive_dashboard') }}"},
            'schema_yml': 'version: 2',
        }

    def test_dry_run_validation_artifacts_passes_valid_export(self):
        result = export_validation_artifacts(self._valid_dry_run_artifacts(), 'unit_test_dry_run_valid')
        try:
            dry_run = dry_run_validation_artifacts(result['output_dir'])
            self.assertEqual(dry_run['status'], 'passed', dry_run)
            self.assertFalse(dry_run['errors'], dry_run)
        finally:
            if result.get('output_dir'):
                shutil.rmtree(result['output_dir'], ignore_errors=True)

    def test_dry_run_validation_artifacts_missing_model_fails(self):
        result = export_validation_artifacts(self._valid_dry_run_artifacts(), 'unit_test_dry_run_missing_model')
        try:
            os.remove(os.path.join(result['output_dir'], 'models', 'executive_dashboard.sql'))
            dry_run = dry_run_validation_artifacts(result['output_dir'])
            self.assertEqual(dry_run['status'], 'failed')
            self.assertTrue(any('Missing required file: models/executive_dashboard.sql' in error for error in dry_run['errors']), dry_run)
        finally:
            if result.get('output_dir'):
                shutil.rmtree(result['output_dir'], ignore_errors=True)

    def test_dry_run_validation_artifacts_placeholder_fails(self):
        artifacts = self._valid_dry_run_artifacts()
        artifacts['tests']['assert_placeholder.sql'] = 'SELECT <missing_column> FROM x'
        result = export_validation_artifacts(artifacts, 'unit_test_dry_run_placeholder')
        try:
            dry_run = dry_run_validation_artifacts(result['output_dir'])
            self.assertEqual(dry_run['status'], 'failed')
            self.assertTrue(any('Unresolved placeholder' in error for error in dry_run['errors']), dry_run)
        finally:
            if result.get('output_dir'):
                shutil.rmtree(result['output_dir'], ignore_errors=True)

    def test_dry_run_validation_artifacts_bad_join_corruption_fails(self):
        artifacts = self._valid_dry_run_artifacts()
        artifacts['models']['executive_dashboard.sql'] = (
            "WITH final_model AS (SELECT 1 AS id FROM x AccountLEFT JOIN y ON x.id = y.id)\n"
            "SELECT * FROM final_model"
        )
        result = export_validation_artifacts(artifacts, 'unit_test_dry_run_bad_join')
        try:
            dry_run = dry_run_validation_artifacts(result['output_dir'])
            self.assertEqual(dry_run['status'], 'failed')
            self.assertTrue(any('AccountLEFT JOIN corruption' in error for error in dry_run['errors']), dry_run)
        finally:
            if result.get('output_dir'):
                shutil.rmtree(result['output_dir'], ignore_errors=True)

    def test_dry_run_validation_artifacts_trailing_test_semicolon_fails(self):
        artifacts = self._valid_dry_run_artifacts()
        artifacts['tests']['assert_semicolon.sql'] = 'SELECT id FROM x WHERE id IS NULL;'
        result = export_validation_artifacts(artifacts, 'unit_test_dry_run_semicolon')
        try:
            dry_run = dry_run_validation_artifacts(result['output_dir'])
            self.assertEqual(dry_run['status'], 'failed')
            self.assertTrue(any('Trailing semicolon in test SQL' in error for error in dry_run['errors']), dry_run)
        finally:
            if result.get('output_dir'):
                shutil.rmtree(result['output_dir'], ignore_errors=True)

    def test_dry_run_validation_artifacts_manifest_mismatch_fails(self):
        result = export_validation_artifacts(self._valid_dry_run_artifacts(), 'unit_test_dry_run_manifest_mismatch')
        try:
            with open(os.path.join(result['output_dir'], 'manifest.json'), 'r+', encoding='utf-8') as handle:
                manifest = json.load(handle)
                manifest['files'] = []
                handle.seek(0)
                json.dump(manifest, handle)
                handle.truncate()
            dry_run = dry_run_validation_artifacts(result['output_dir'])
            self.assertEqual(dry_run['status'], 'failed')
            self.assertTrue(any('Manifest file list does not match' in error for error in dry_run['errors']), dry_run)
        finally:
            if result.get('output_dir'):
                shutil.rmtree(result['output_dir'], ignore_errors=True)

    def test_export_validation_artifacts_blocks_path_traversal(self):
        result = export_validation_artifacts({'models': {'executive_dashboard.sql': 'select 1'}}, '../outside_artifacts')
        self.assertTrue(result['errors'], result)
        self.assertEqual(result['files_written'], [])

    def test_export_validation_artifacts_sanitizes_file_names(self):
        artifacts = {
            'models': {'../executive_dashboard.sql': 'select 1'},
            'tests': {'../assert.sql': 'select 1 where 1 = 0'},
            'analyses': {'../metric_totals.sql': 'select 1'},
            'schema_yml': 'version: 2',
        }
        result = export_validation_artifacts(artifacts, 'unit_test_sanitize_export')
        try:
            self.assertFalse(result['errors'], result)
            self.assertIn('models/executive_dashboard.sql', result['files_written'])
            self.assertIn('tests/assert.sql', result['files_written'])
            self.assertIn('analyses/metric_totals.sql', result['files_written'])
        finally:
            if result.get('output_dir'):
                shutil.rmtree(result['output_dir'], ignore_errors=True)

    def test_export_validation_artifacts_route_returns_file_list(self):
        try:
            import flask  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest('Flask is not installed in this test environment')
        import backend.app as app_mod

        artifacts = {
            'models': {'executive_dashboard.sql': 'select 1'},
            'tests': {'assert_fact_expenses_row_count.sql': 'select 1 where 1 = 0'},
            'analyses': {'metric_totals.sql': 'select 1'},
            'schema_yml': 'version: 2',
        }
        client = app_mod.app.test_client()
        response = client.post('/api/export-validation-artifacts', json={
            'validationArtifacts': artifacts,
            'outputDir': 'unit_test_route_export',
        })
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        try:
            self.assertEqual(payload['status'], 'success')
            self.assertIn('dbt_project.yml', payload['filesWritten'])
            self.assertIn('summary_report.md', payload['filesWritten'])
            self.assertIn('manifest.json', payload['filesWritten'])
            self.assertTrue(payload.get('manifestPath'))
            self.assertIn('models/executive_dashboard.sql', payload['filesWritten'])
            self.assertIn('models/schema.yml', payload['filesWritten'])
            self.assertTrue(os.path.exists(payload['manifestPath']))
        finally:
            if payload.get('outputDir'):
                shutil.rmtree(payload['outputDir'], ignore_errors=True)

    def test_export_validation_artifacts_route_returns_zip_when_requested(self):
        try:
            import flask  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest('Flask is not installed in this test environment')
        import backend.app as app_mod

        artifacts = {
            'models': {'executive_dashboard.sql': 'select 1'},
            'tests': {'assert_fact_expenses_row_count.sql': 'select 1 where 1 = 0'},
            'analyses': {'metric_totals.sql': 'select 1'},
            'schema_yml': 'version: 2',
        }
        client = app_mod.app.test_client()
        response = client.post('/api/export-validation-artifacts', json={
            'validationArtifacts': artifacts,
            'outputDir': 'unit_test_route_zip_export',
            'zip': True,
        })
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        try:
            self.assertEqual(payload['status'], 'success')
            self.assertTrue(payload.get('manifestPath'))
            self.assertTrue(payload.get('zipPath'))
            self.assertTrue(payload.get('zipFileName', '').endswith('.zip'))
            self.assertIn('manifest.json', payload.get('filesZipped', []))
            self.assertIn('summary_report.md', payload.get('filesZipped', []))
            self.assertIn('dbt_project.yml', payload.get('filesZipped', []))
            self.assertIn('models/executive_dashboard.sql', payload.get('filesZipped', []))
            self.assertIn('tests/assert_fact_expenses_row_count.sql', payload.get('filesZipped', []))
            self.assertTrue(os.path.exists(payload['zipPath']))
        finally:
            if payload.get('outputDir'):
                shutil.rmtree(payload['outputDir'], ignore_errors=True)
            if payload.get('zipPath') and os.path.exists(payload['zipPath']):
                os.remove(payload['zipPath'])

    def test_export_validation_artifacts_route_returns_dry_run_result(self):
        try:
            import flask  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest('Flask is not installed in this test environment')
        import backend.app as app_mod

        client = app_mod.app.test_client()
        response = client.post('/api/export-validation-artifacts', json={
            'validationArtifacts': self._valid_dry_run_artifacts(),
            'outputDir': 'unit_test_route_dry_run_export',
            'dryRun': True,
        })
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        try:
            self.assertIn('dryRunResult', payload)
            self.assertEqual(payload['dryRunResult']['status'], 'passed', payload['dryRunResult'])
        finally:
            if payload.get('outputDir'):
                shutil.rmtree(payload['outputDir'], ignore_errors=True)

    def test_export_validation_artifacts_by_job_id_succeeds(self):
        try:
            import flask  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest('Flask is not installed in this test environment')
        import backend.app as app_mod

        with app_mod.REGENERATION_LOCK:
            app_mod.REGENERATION_JOBS['job_export_ok'] = {
                'status': 'complete',
                'sessionId': 's1',
                'result': {
                    'status': 'complete',
                    'sql': 'WITH final_model AS (SELECT 1 AS id)\nSELECT * FROM final_model',
                    'validationReport': build_migration_validation_report('SELECT 1', model_name='executive_dashboard'),
                    'validationArtifacts': self._valid_dry_run_artifacts(),
                    'warnings': [],
                    'oneShotQualityScore': 0.98,
                },
            }
        client = app_mod.app.test_client()
        response = client.post('/api/export-validation-artifacts', json={
            'jobId': 'job_export_ok',
            'outputDir': 'unit_test_job_export_ok',
            'includeProjectScaffold': True,
            'zip': True,
            'dryRun': True,
        })
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        try:
            self.assertEqual(payload['status'], 'success')
            self.assertTrue(payload.get('zipPath'))
            self.assertTrue(payload.get('manifestPath'))
            self.assertEqual(payload.get('dryRunResult', {}).get('status'), 'passed', payload.get('dryRunResult'))
            self.assertIn('models/executive_dashboard.sql', payload.get('filesWritten', []))
        finally:
            with app_mod.REGENERATION_LOCK:
                app_mod.REGENERATION_JOBS.pop('job_export_ok', None)
            if payload.get('outputDir'):
                shutil.rmtree(payload['outputDir'], ignore_errors=True)
            if payload.get('zipPath') and os.path.exists(payload['zipPath']):
                os.remove(payload['zipPath'])

    def test_export_validation_artifacts_by_job_id_regenerates_missing_artifacts(self):
        try:
            import flask  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest('Flask is not installed in this test environment')
        import backend.app as app_mod

        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH final_model AS (SELECT 1 AS id)\n"
            "SELECT * FROM final_model"
        )
        report = build_migration_validation_report(sql, model_name='executive_dashboard')
        with app_mod.REGENERATION_LOCK:
            app_mod.REGENERATION_JOBS['job_export_regen'] = {
                'status': 'complete',
                'sessionId': 's1',
                'result': {
                    'status': 'complete',
                    'sql': sql,
                    'validationReport': report,
                    'warnings': [],
                },
            }
        client = app_mod.app.test_client()
        response = client.post('/api/export-validation-artifacts', json={
            'jobId': 'job_export_regen',
            'outputDir': 'unit_test_job_export_regen',
        })
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        try:
            self.assertEqual(payload['status'], 'success')
            self.assertIn('models/executive_dashboard.sql', payload.get('filesWritten', []))
            with app_mod.REGENERATION_LOCK:
                stored_result = app_mod.REGENERATION_JOBS['job_export_regen']['result']
            self.assertIn('validationArtifacts', stored_result)
        finally:
            with app_mod.REGENERATION_LOCK:
                app_mod.REGENERATION_JOBS.pop('job_export_regen', None)
            if payload.get('outputDir'):
                shutil.rmtree(payload['outputDir'], ignore_errors=True)

    def test_export_validation_artifacts_by_invalid_job_id_returns_404(self):
        try:
            import flask  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest('Flask is not installed in this test environment')
        import backend.app as app_mod

        client = app_mod.app.test_client()
        response = client.post('/api/export-validation-artifacts', json={'jobId': 'missing_job_for_export'})
        self.assertEqual(response.status_code, 404)

    def test_export_validation_artifacts_by_unfinished_job_id_returns_409(self):
        try:
            import flask  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest('Flask is not installed in this test environment')
        import backend.app as app_mod

        with app_mod.REGENERATION_LOCK:
            app_mod.REGENERATION_JOBS['job_export_running'] = {'status': 'running', 'result': {}}
        client = app_mod.app.test_client()
        response = client.post('/api/export-validation-artifacts', json={'jobId': 'job_export_running'})
        try:
            self.assertEqual(response.status_code, 409)
        finally:
            with app_mod.REGENERATION_LOCK:
                app_mod.REGENERATION_JOBS.pop('job_export_running', None)

    def test_regenerate_result_endpoint_returns_validation_artifacts(self):
        try:
            import flask  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest('Flask is not installed in this test environment')
        import backend.app as app_mod

        artifacts = self._valid_dry_run_artifacts()
        with app_mod.REGENERATION_LOCK:
            app_mod.REGENERATION_JOBS['job_result_ok'] = {
                'status': 'complete',
                'sessionId': 's1',
                'promptVersion': 'test',
                'generationPlan': [],
                'result': {
                    'status': 'complete',
                    'sql': 'WITH final_model AS (SELECT 1 AS id)\nSELECT * FROM final_model',
                    'validationReport': {'checks': []},
                    'validationArtifacts': artifacts,
                    'warnings': ['demo'],
                },
            }
        client = app_mod.app.test_client()
        response = client.get('/api/regenerate/result/job_result_ok')
        try:
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload['status'], 'complete')
            self.assertIn('validationArtifacts', payload)
            self.assertIn('executive_dashboard.sql', payload['validationArtifacts']['models'])
            self.assertEqual(payload['warnings'], ['demo'])
        finally:
            with app_mod.REGENERATION_LOCK:
                app_mod.REGENERATION_JOBS.pop('job_result_ok', None)

    def test_regenerate_result_endpoint_generates_missing_validation_payload(self):
        try:
            import flask  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest('Flask is not installed in this test environment')
        import backend.app as app_mod

        with app_mod.REGENERATION_LOCK:
            app_mod.REGENERATION_JOBS['job_result_missing_payload'] = {
                'status': 'complete',
                'sessionId': 's1',
                'promptVersion': 'test',
                'generationPlan': [],
                'result': {
                    'status': 'complete',
                    'sql': 'WITH final_model AS (SELECT 1 AS id)\nSELECT * FROM final_model',
                    'warnings': [],
                },
            }
        client = app_mod.app.test_client()
        response = client.get('/api/regenerate/result/job_result_missing_payload')
        try:
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertIsNotNone(payload.get('validationReport'), payload)
            self.assertIsNotNone(payload.get('validationArtifacts'), payload)
            self.assertIn('executive_dashboard.sql', payload['validationArtifacts']['models'])
            with app_mod.REGENERATION_LOCK:
                stored_result = app_mod.REGENERATION_JOBS['job_result_missing_payload']['result']
            self.assertIsNotNone(stored_result.get('validationReport'))
            self.assertIsNotNone(stored_result.get('validationArtifacts'))
        finally:
            with app_mod.REGENERATION_LOCK:
                app_mod.REGENERATION_JOBS.pop('job_result_missing_payload', None)

    def test_regenerate_result_route_self_heals_job_sql_only_payload(self):
        try:
            import flask  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest('Flask is not installed in this test environment')
        import backend.app as app_mod

        with app_mod.REGENERATION_LOCK:
            app_mod.REGENERATION_JOBS['job_result_sql_only'] = {
                'status': 'complete',
                'sessionId': 's1',
                'promptVersion': 'test',
                'generationPlan': [],
                'sql': 'WITH final_model AS (SELECT 1 AS id)\nSELECT * FROM final_model',
                'result': {'status': 'complete', 'warnings': []},
            }
        client = app_mod.app.test_client()
        response = client.get('/api/regenerate/result/job_result_sql_only')
        try:
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertIsNotNone(payload.get('validationReport'), payload)
            self.assertIsNotNone(payload.get('validationArtifacts'), payload)
            self.assertIn('executive_dashboard.sql', payload['validationArtifacts']['models'])
            with app_mod.REGENERATION_LOCK:
                stored_job = app_mod.REGENERATION_JOBS['job_result_sql_only']
            self.assertIsNotNone(stored_job['result'].get('validationReport'))
            self.assertIsNotNone(stored_job['result'].get('validationArtifacts'))
        finally:
            with app_mod.REGENERATION_LOCK:
                app_mod.REGENERATION_JOBS.pop('job_result_sql_only', None)

    def test_export_validation_artifacts_route_blocks_path_traversal(self):
        try:
            import flask  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest('Flask is not installed in this test environment')
        import backend.app as app_mod

        client = app_mod.app.test_client()
        response = client.post('/api/export-validation-artifacts', json={
            'validationArtifacts': {'models': {'executive_dashboard.sql': 'select 1'}},
            'outputDir': '../bad',
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['status'], 'error')

    def test_auto_mode_returns_validation_report(self):
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
        with patch.object(app_mod, 'request_migration_one_shot', return_value=one_shot_result), \
             patch.object(app_mod, '_audit_generated_sql_against_plan', return_value=[]), \
             patch.object(app_mod, 'request_migration_with_validation') as loop:
            result = app_mod.migrate_qvs_to_dbt(
                'FactTable:\nLOAD [Sales Amount] FROM FactTable.qvd;',
                plan=[make_load('FactTable', ['Sales Amount'], source='FactTable.qvd')],
                dialect='dbt',
                generation_mode='auto',
            )
        loop.assert_not_called()
        self.assertIn('validationReport', result)
        self.assertIn('validation_report', result)
        self.assertIn('validationArtifacts', result)
        self.assertEqual(result['validationReport']['dbtCompile']['status'], 'pending')
        self.assertTrue(result['validationReport']['checks'])
        self.assertIn('executive_dashboard.sql', result['validationArtifacts']['models'])

    def test_enrich_final_model_projection_adds_joined_dimension_fields(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable_with_expenses AS (SELECT CustKey, MonthlyRegionKey FROM src),\n"
            "customermap AS (SELECT CustKey, CustKeyAR FROM src),\n"
            "budget AS (SELECT MonthlyRegionKey, \"Budget Amount\" FROM src),\n"
            "final_model AS (\n"
            "  SELECT f.CustKey\n"
            "  FROM facttable_with_expenses f\n"
            "  LEFT JOIN customermap cmap ON f.CustKey = cmap.CustKey\n"
            "  LEFT JOIN budget b ON f.MonthlyRegionKey = b.MonthlyRegionKey\n"
            ")\n"
            "SELECT * FROM final_model"
        )
        enriched = enrich_final_model_projection(sql)
        final_body = extract_cte_body(enriched, 'final_model')
        self.assertIn('cmap.CustKeyAR AS cmap_custkeyar', final_body)
        self.assertIn('b."Budget Amount" AS b_budget_amount', final_body)

    def test_enrich_final_model_projection_does_not_duplicate_raw_projection(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable_with_expenses AS (SELECT CustKey FROM src),\n"
            "customermap AS (SELECT CustKey, CustKeyAR FROM src),\n"
            "final_model AS (\n"
            "  SELECT f.CustKey, cmap.CustKeyAR\n"
            "  FROM facttable_with_expenses f\n"
            "  LEFT JOIN customermap cmap ON f.CustKey = cmap.CustKey\n"
            ")\n"
            "SELECT * FROM final_model"
        )
        enriched = enrich_final_model_projection(sql)
        final_body = extract_cte_body(enriched, 'final_model')
        self.assertIn('cmap.CustKeyAR', final_body)
        self.assertNotIn('cmap.CustKeyAR AS cmap_custkeyar', final_body)

    def test_resolve_cte_column_reference_preserves_calendar_quoted_identifier(self):
        self.assertEqual(
            resolve_cte_column_reference('cal', 'fiscalquarter', {'fiscal quarter': 'Fiscal Quarter'}),
            'cal."Fiscal Quarter"',
        )

    def test_calendar_quoted_identifier_preserved_in_final_model(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable_with_expenses AS (SELECT YYYYMM FROM src),\n"
            "calendar AS (SELECT YYYYMM, \"Fiscal Quarter\" FROM src)\n"
            "SELECT * FROM facttable_with_expenses"
        )
        contract = {
            'join_lines': ['- facttable_with_expenses.YYYYMM -> calendar.YYYYMM'],
            'required_aliases': {'calendar': 'cal'},
            'warnings': [],
        }
        shaped = compose_final_model_from_contract(sql, contract, projection_mode='safe')
        enriched = enrich_final_model_projection(shaped)
        final_body = extract_cte_body(enriched, 'final_model')
        self.assertIn('cal."Fiscal Quarter" AS cal_fiscal_quarter', final_body)
        self.assertNotIn('cal.fiscalquarter', final_body.lower())
        issues = validate_generated_sql(enriched, dialect='dbt')
        self.assertFalse(any('ALIAS_COLUMN_NOT_FOUND' in issue and 'cal' in issue for issue in issues), issues)

    def test_high_quality_executive_dashboard_sql_scores_at_least_90(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable AS (SELECT MonthlyRegionKey, Region, CustKey, \"Address Number\", \"Item-Branch Key\", YYYYMM, Account FROM src),\n"
            "expenses AS (SELECT MonthlyRegionKey, Region, Account, YYYYMM, ExpenseActual, ExpenseBudget FROM src),\n"
            "facttable_with_expenses AS (\n"
            "  SELECT MonthlyRegionKey, Region, CustKey, \"Address Number\", \"Item-Branch Key\", YYYYMM, Account, CAST(NULL AS NUMBER) AS ExpenseActual, CAST(NULL AS NUMBER) AS ExpenseBudget FROM facttable\n"
            "  UNION ALL\n"
            "  SELECT MonthlyRegionKey, Region, CAST(NULL AS NUMBER) AS CustKey, CAST(NULL AS NUMBER) AS \"Address Number\", CAST(NULL AS NUMBER) AS \"Item-Branch Key\", YYYYMM, Account, ExpenseActual, ExpenseBudget FROM expenses\n"
            "),\n"
            "customermap AS (SELECT CustKey, CustKeyAR FROM src),\n"
            "customermaster AS (SELECT \"Address Number\", \"Customer Number\" FROM src),\n"
            "arsummary AS (SELECT CustKeyAR, AROpen FROM src),\n"
            "budget AS (SELECT MonthlyRegionKey, \"Budget Amount\" FROM src),\n"
            "historyflag AS (SELECT YYYYMM, _HistoryFlag FROM src),\n"
            "final_model AS (\n"
            "  SELECT f.MonthlyRegionKey, f.Account, cmap.CustKeyAR, cust.\"Customer Number\", ar.AROpen, b.\"Budget Amount\", h._HistoryFlag\n"
            "  FROM facttable_with_expenses f\n"
            "  LEFT JOIN customermap cmap ON f.CustKey = cmap.CustKey\n"
            "  LEFT JOIN customermaster cust ON f.\"Address Number\" = cust.\"Address Number\"\n"
            "  LEFT JOIN arsummary ar ON cmap.CustKeyAR = ar.CustKeyAR\n"
            "  LEFT JOIN budget b ON f.MonthlyRegionKey = b.MonthlyRegionKey\n"
            "  LEFT JOIN historyflag h ON f.YYYYMM = h.YYYYMM\n"
            ")\n"
            "SELECT * FROM final_model"
        )
        plan = [make_load('Expenses', ['MonthlyRegionKey'], is_concat=True, concat_target='FactTable')]
        quality = score_generated_sql_quality(sql, plan=plan)
        self.assertGreaterEqual(quality['score'], 90, quality)
        self.assertFalse(quality['failures'], quality)

    def test_quality_score_missing_final_model_lowers_score(self):
        sql = "{{ config(materialized='table') }}\nWITH facttable AS (SELECT 1 AS id)\nSELECT * FROM facttable"
        quality = score_generated_sql_quality(sql, plan=[])
        self.assertLess(quality['score'], 90, quality)
        self.assertTrue(any('final_model missing' in item for item in quality['failures']), quality)

    def test_quality_score_direct_expenses_join_lowers_score(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable_with_expenses AS (SELECT MonthlyRegionKey, Account FROM src),\n"
            "final_model AS (\n"
            "  SELECT f.MonthlyRegionKey FROM facttable_with_expenses f LEFT JOIN expenses e ON f.MonthlyRegionKey = e.MonthlyRegionKey\n"
            ")\nSELECT * FROM final_model"
        )
        quality = score_generated_sql_quality(sql, plan=[])
        self.assertLess(quality['score'], 90, quality)
        self.assertTrue(any('direct expenses join' in item for item in quality['failures']), quality)

    def test_quality_score_bad_union_lowers_score(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable AS (SELECT a, b FROM src),\n"
            "expenses AS (SELECT a FROM src),\n"
            "facttable_with_expenses AS (\n"
            "  SELECT a, b FROM facttable\n"
            "  UNION ALL\n"
            "  SELECT a FROM expenses\n"
            "),\n"
            "final_model AS (SELECT f.a FROM facttable_with_expenses f)\n"
            "SELECT * FROM final_model"
        )
        quality = score_generated_sql_quality(sql, plan=[])
        self.assertLess(quality['score'], 90, quality)
        self.assertTrue(any('not aligned' in item for item in quality['failures']), quality)

    def test_quality_score_missing_customer_budget_history_are_warnings(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable_with_expenses AS (SELECT CustKey, MonthlyRegionKey, YYYYMM FROM src),\n"
            "customermap AS (SELECT CustKey FROM src),\n"
            "budget AS (SELECT MonthlyRegionKey FROM src),\n"
            "historyflag AS (SELECT YYYYMM FROM src),\n"
            "final_model AS (SELECT f.CustKey FROM facttable_with_expenses f)\n"
            "SELECT * FROM final_model"
        )
        quality = score_generated_sql_quality(sql, plan=[])
        self.assertTrue(any('missing customer map joined' in item for item in quality['warnings']), quality)
        self.assertTrue(any('missing budget joined' in item for item in quality['warnings']), quality)
        self.assertTrue(any('missing historyflag joined' in item for item in quality['warnings']), quality)
        self.assertFalse(any('customer map' in item or 'budget' in item or 'historyflag' in item for item in quality['failures']), quality)

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
        self.assertNotRegex(finalized, r'(?is)\bJOIN\s+expenses\b')
        self.assertNotIn('e.Account', finalized)
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

    def test_finalize_coerces_expenses_yyyymm_when_missing(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable AS (\n"
            "  SELECT MonthlyRegionKey, Account, YYYYMM FROM {{ source('raw','FactTable') }}\n"
            "),\n"
            "expenses AS (\n"
            "  SELECT MonthlyRegionKey, Account, ExpenseActual FROM {{ source('raw','Expenses') }}\n"
            "),\n"
            "facttable_with_expenses AS (\n"
            "  SELECT * FROM facttable\n"
            "  UNION ALL\n"
            "  SELECT MonthlyRegionKey, Account, ExpenseActual FROM expenses\n"
            ")\n"
            "SELECT * FROM facttable_with_expenses"
        )
        finalized = finalize_generated_sql(sql)
        self.assertIn("DATEADD(month, 12, TO_DATE(YYYYMM::varchar, 'YYYYMM')) AS YYYYMM", finalized)
        self.assertNotIn('NULL AS YYYYMM', finalized.upper())

    def test_finalize_removes_bad_expenses_monthly_join_when_fact_expenses_exists(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable_with_expenses AS (\n"
            "  SELECT MonthlyRegionKey, Account FROM {{ source('raw','FactTable') }}\n"
            "),\n"
            "expenses AS (\n"
            "  SELECT MonthlyRegionKey, Account FROM {{ source('raw','Expenses') }}\n"
            "),\n"
            "final_model AS (\n"
            "  SELECT f.MonthlyRegionKey\n"
            "  FROM facttable_with_expenses f\n"
            "  LEFT JOIN expenses e ON f.MonthlyRegionKey = e.MonthlyRegionKey\n"
            ")\n"
            "SELECT * FROM final_model"
        )
        finalized = finalize_generated_sql(sql)
        self.assertNotIn('LEFT JOIN expenses e ON f.MonthlyRegionKey = e.MonthlyRegionKey', finalized)

    def test_finalize_globally_coerces_raw_yyyymm_dateadd(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH x AS (\n"
            "  SELECT DATEADD(month, 12, yyyymm) AS y FROM {{ source('raw','FactTable') }}\n"
            ")\nSELECT * FROM x"
        )
        finalized = finalize_generated_sql(sql)
        self.assertIn("DATEADD(month, 12, TO_DATE(YYYYMM::varchar, 'YYYYMM'))", finalized)

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

    def test_distinct_lineage_exposes_yyyymm_for_alias_validation(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH historyflag AS (\n"
            "  SELECT DISTINCT\n"
            "    yyyymm,\n"
            "    CASE WHEN 1=1 THEN 0 ELSE 1 END AS history_flag\n"
            "  FROM {{ source('raw','Calendar') }}\n"
            "),\n"
            "facttable_with_expenses AS (\n"
            "  SELECT yyyymm FROM {{ source('raw','FactTable') }}\n"
            "),\n"
            "final_model AS (\n"
            "  SELECT f.yyyymm, h.history_flag\n"
            "  FROM facttable_with_expenses f\n"
            "  LEFT JOIN historyflag h ON f.yyyymm = h.yyyymm\n"
            ")\n"
            "SELECT * FROM final_model"
        )
        issues = validate_generated_sql(sql, dialect='dbt')
        self.assertFalse(any('JOIN_KEY_MISSING' in i and 'h' in i for i in issues), issues)

    def test_extract_cte_output_columns_handles_select_distinct_historyflag(self):
        sql = (
            "WITH historyflag AS (\n"
            "  SELECT DISTINCT\n"
            "    yyyymm,\n"
            "    CASE WHEN yyyymm <= 202401 THEN 1 ELSE 0 END AS _historyflag\n"
            "  FROM facttable_with_expenses\n"
            ")\nSELECT * FROM historyflag"
        )
        cols = extract_cte_output_columns(sql, 'historyflag')
        lowered = {c.lower() for c in cols}
        self.assertIn('yyyymm', lowered)
        self.assertIn('_historyflag', lowered)

    def test_finalize_historyflag_uses_clean_date_trunc_month_logic(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH historyflag AS (\n"
            "  SELECT YYYYMM,\n"
            "    CASE WHEN DATEADD(day, -DAY(TO_DATE('2013-05-31')) + 1, TO_DATE('2013-05-31')) >= YYYYMM THEN 1 ELSE 0 END AS _HistoryFlag\n"
            "  FROM calendar\n"
            ")\nSELECT * FROM historyflag"
        )
        finalized = finalize_generated_sql(sql)
        self.assertIn("WHEN yyyymm <= DATE_TRUNC('month', TO_DATE('2013-05-31'))", finalized)
        self.assertNotIn('DATEADD(day', finalized)

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
        self.assertIn('### required join contract', combined)
        self.assertIn('use only these join paths', combined)

    def test_build_join_contract_uses_ir_safe_joins_and_emits_warnings(self):
        plan = [make_load('FactTable', ['CustKey', 'Item-Branch Key'], source='FactTable.qvd')]
        script = (
            "FactTable:\nLOAD CustKey, [Item-Branch Key] FROM [lib://FactTable.qvd] (qvd);\n"
            "CustomerMap:\nLOAD CustKey, CustKeyAR FROM [lib://CustomerMap.qvd] (qvd);\n"
            "ARSummary:\nLOAD CustKeyAR, ARAge FROM [lib://ARSummary.qvd] (qvd);\n"
            "ItemBranchMaster:\nLOAD [Item-Branch Key], [Short Name] FROM [lib://ItemBranchMaster.qvd] (qvd);\n"
            "ItemMaster:\nLOAD [Short Name], [Product Group] FROM [lib://ItemMaster.qvd] (qvd);"
        )
        contract = build_join_contract(plan=plan, qvs_script=script)
        self.assertIn('JOIN CONTRACT:', contract['text'])
        self.assertTrue(contract['lines'])
        self.assertIn('required_aliases', contract)
        self.assertIn('forbidden_patterns', contract)
        self.assertEqual(contract['required_aliases'].get('sales_rep_master'), 'srm')
        self.assertTrue(any('monthlyregionkey only' in rule.lower() for rule in contract['forbidden_patterns']))
        has_fact_join = any('facttable' in line.lower() for line in contract['lines'])
        has_fallback = any('no validated safe joins' in line.lower() for line in contract['lines'])
        self.assertTrue(has_fact_join or has_fallback, contract)

    def test_build_join_contract_fallback_uses_safe_shared_keys(self):
        plan = [
            make_load('FactTable_With_Expenses', ['MonthlyRegionKey', 'CustKey'], source='Fact.qvd'),
            make_load('CustomerMap', ['CustKey', 'CustKeyAR'], source='CustomerMap.qvd'),
            make_load('Budget', ['MonthlyRegionKey'], source='Budget.qvd'),
        ]
        contract = build_join_contract(plan=plan, qvs_script='')
        text = (contract.get('text') or '').lower()
        self.assertIn('join contract', text)
        self.assertTrue(
            any('metadata_fallback' in line.lower() for line in contract.get('join_lines', []))
            or any('facttable_with_expenses.custkey' in line.lower() for line in contract.get('join_lines', []))
            or any('no validated safe joins' in line.lower() for line in contract.get('join_lines', [])),
            contract,
        )

    def test_validator_rejects_expenses_join_on_monthly_key_only(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable_with_expenses AS (\n"
            "  SELECT MonthlyRegionKey, Account FROM {{ source('raw','FactTable') }}\n"
            "),\n"
            "expenses AS (\n"
            "  SELECT MonthlyRegionKey, Account FROM {{ source('raw','Expenses') }}\n"
            "),\n"
            "final_model AS (\n"
            "  SELECT f.MonthlyRegionKey\n"
            "  FROM facttable_with_expenses f\n"
            "  LEFT JOIN expenses e ON f.MonthlyRegionKey = e.MonthlyRegionKey\n"
            ")\n"
            "SELECT * FROM final_model"
        )
        issues = validate_generated_sql(sql, dialect='dbt')
        self.assertTrue(any('INVALID_EXPENSES_JOIN_MONTHLY_ONLY' in i for i in issues), issues)
        self.assertTrue(needs_sql_repair(issues))

    def test_compose_final_model_from_contract_realistic_chain(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable_with_expenses AS (SELECT monthlyregionkey, custkey, address_number, item_branch_key FROM src),\n"
            "customermap AS (SELECT custkey, custkeyar FROM src),\n"
            "arsummary AS (SELECT custkeyar FROM src),\n"
            "customermaster AS (SELECT address_number, sales_rep FROM src),\n"
            "salesrepmaster AS (SELECT sales_rep FROM src),\n"
            "itembranchmaster AS (SELECT item_branch_key, short_name FROM src),\n"
            "itemmaster AS (SELECT short_name, product_group FROM src),\n"
            "productgroupmaster AS (SELECT product_group FROM src)\n"
            "SELECT * FROM facttable_with_expenses"
        )
        contract = {
            'join_lines': [
                "- facttable_with_expenses.custkey -> customermap.custkey",
                "- customermap.custkeyar -> arsummary.custkeyar",
                "- facttable_with_expenses.address_number -> customermaster.address_number",
                "- customermaster.sales_rep -> salesrepmaster.sales_rep",
                "- facttable_with_expenses.item_branch_key -> itembranchmaster.item_branch_key",
                "- itembranchmaster.short_name -> itemmaster.short_name",
                "- itemmaster.product_group -> productgroupmaster.product_group",
            ],
            'required_aliases': {
                'customer_map': 'cmap',
                'customer_master': 'cust',
                'item_branch_master': 'ibm',
                'item_master': 'im',
                'sales_rep_master': 'srm',
                'customermap': 'cmap',
                'customermaster': 'cust',
                'itembranchmaster': 'ibm',
                'itemmaster': 'im',
                'salesrepmaster': 'srm',
            },
            'warnings': [],
        }
        shaped = compose_final_model_from_contract(sql, contract, projection_mode='safe')
        self.assertIn('final_model AS (', shaped)
        self.assertTrue(shaped.rstrip().endswith('SELECT *\nFROM final_model'))
        self.assertNotIn('LEFT JOIN expenses', shaped.lower())
        self.assertNotIn('DUPLICATE_ALIAS', ' '.join(validate_generated_sql(shaped, dialect='dbt')))
        self.assertIn('LEFT JOIN customermap cmap ON ft.custkey = cmap.custkey', shaped)
        self.assertIn('LEFT JOIN customermaster cust ON ft.address_number = cust.address_number', shaped)
        self.assertIn('LEFT JOIN salesrepmaster srm ON cust.sales_rep = srm.sales_rep', shaped)

    def test_compose_projection_modes_safe_vs_rich(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable_with_expenses AS (SELECT custkey FROM src),\n"
            "customermap AS (SELECT custkey, custkeyar FROM src)\n"
            "SELECT * FROM facttable_with_expenses"
        )
        contract = {
            'join_lines': ["- facttable_with_expenses.custkey -> customermap.custkey"],
            'required_aliases': {'customermap': 'cmap'},
            'warnings': [],
        }
        safe_sql = compose_final_model_from_contract(sql, contract, projection_mode='safe')
        rich_sql = compose_final_model_from_contract(sql, contract, projection_mode='rich')
        self.assertIn('SELECT ft.*, cmap.custkey', safe_sql)
        self.assertIn('SELECT ft.*, cmap.*', rich_sql)

    def test_compute_join_contract_coverage_metrics(self):
        sql = (
            "{{ config(materialized='table') }}\n"
            "WITH facttable_with_expenses AS (SELECT custkey FROM src),\n"
            "customermap AS (SELECT custkey FROM src)\n"
            "SELECT * FROM facttable_with_expenses"
        )
        contract = {
            'join_lines': ["- facttable_with_expenses.custkey -> customermap.custkey"],
            'required_aliases': {'customermap': 'cmap'},
            'warnings': ['OMITTED_UNSAFE_JOIN: x -> y'],
        }
        metrics = compute_join_contract_coverage(sql, contract)
        self.assertIn('joinContractCoverage', metrics)
        self.assertEqual(metrics['totalContractPaths'], 1)
        self.assertEqual(metrics['joinedContractPaths'], 1)
        self.assertTrue(metrics['omittedUnsafeJoins'])

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


class TestOneShotBenchmarkShape(unittest.TestCase):
    def test_multi_script_benchmark_metrics_shape(self):
        scripts = [
            "FactTable:\nLOAD CustKey, YYYYMM FROM [lib://FactTable.qvd] (qvd);",
            "Sales:\nLOAD MonthlyRegionKey, Account FROM [lib://Sales.qvd] (qvd);\n"
            "Expenses:\nCONCATENATE LOAD MonthlyRegionKey, Account, ExpenseActual FROM [lib://Expenses.qvd] (qvd);",
            "CustomerMap:\nLOAD CustKey, CustKeyAR FROM [lib://CustomerMap.qvd] (qvd);\n"
            "ARSummary:\nLOAD CustKeyAR, ARAge FROM [lib://ARSummary.qvd] (qvd);",
        ]
        rows = []
        for script in scripts:
            parsed = parse_migration_response("### SQL\nSELECT 1\n### DESCRIPTION\nok")
            sql = parsed.get('sql') or 'SELECT 1'
            issues = validate_generated_sql(sql, dialect='dbt')
            compile_blockers = [i for i in issues if validation_issue_category(i) == 'compile_error']
            semantic_blockers = [i for i in issues if validation_issue_category(i) == 'semantic_error']
            row = {
                'one_shot_compile_pass_rate': 0.0 if compile_blockers else 1.0,
                'semantic_blockers': len(semantic_blockers),
                'repair_triggered_pct': 1.0 if needs_sql_repair(issues) else 0.0,
                'loop_triggered_pct': 1.0 if compile_blockers else 0.0,
                'average_tokens': max(1, len(sql) // 4),
            }
            rows.append(row)
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertIn('one_shot_compile_pass_rate', row)
            self.assertIn('semantic_blockers', row)
            self.assertIn('repair_triggered_pct', row)
            self.assertIn('loop_triggered_pct', row)
            self.assertIn('average_tokens', row)


class TestAppSourceNameCleanup(unittest.TestCase):
    def test_source_name_cleanup_strips_qvd_and_extra_quote(self):
        try:
            import flask  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest('Flask is not installed in this test environment')
        import backend.app as app_mod
        item = {'source_tables': ["'FactTable.qvd''"], 'source': '', 'table': 'FactTable'}
        self.assertEqual(app_mod._source_name_from_plan_item(item), 'FactTable')

    def test_chat_finalize_calls_pass_cached_plan_context(self):
        try:
            import flask  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest('Flask is not installed in this test environment')
        import backend.app as app_mod
        source = inspect.getsource(app_mod.chat_refine)
        self.assertIn("plan=cached_plan.get('plan')", source)
        self.assertIn('qvs_script=combined_scripts', source)

    def test_chat_stream_finalize_calls_pass_cached_plan_context(self):
        try:
            import flask  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest('Flask is not installed in this test environment')
        import backend.app as app_mod
        source = inspect.getsource(app_mod.chat_refine_stream)
        self.assertIn("plan=cached_plan.get('plan')", source)
        self.assertIn('qvs_script=combined_scripts', source)
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
