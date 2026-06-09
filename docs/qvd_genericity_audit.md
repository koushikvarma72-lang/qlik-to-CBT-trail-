# QVD Genericity Audit

Date: 2026-06-07

## Scope Scanned

- `qvd_to_databricks/`
- `qvd_business_analysis/`
- `backend/integrations/qvd_routes.py`
- Frontend QVD files under `frontend/src/`, including QVD pages, components, store, and API client
- Tests and docs were scanned separately to distinguish production logic from examples/fixtures

Search terms included sample table names, sample file names, exact sales-oriented columns, fixed KPI names, fixed hierarchy labels, hardcoded session paths, and hardcoded target tables:

- `Sales_Sample`, `sales_sample`, `sales_table`
- `ActualSales`, `BudgetSales`, `ForecastSales`
- `Customer`, `Region`, `Country`, `Franchise`, `Calendar.Date`
- `Actual Sales`, `Budget Sales`, `Forecast Sales`
- `Geography`
- `source.qvd`
- local/session path markers such as `/Users/` and known session ids

## Findings

### Production QVD Logic

No hardcoded `Sales_Sample`, `sales_sample`, exact sample QVD file names, sample session ids, or fixed sample target table names were found in production QVD accelerator code after the audit fixes.

Production code still contains generic keyword lists, which are expected and allowed:

- Business measure hints such as `sales`, `amount`, `budget`, `forecast`, `revenue`, `cost`, `price`, `value`, `margin`, `quantity`, `units`
- Dimension/hierarchy hints such as `customer`, `product`, `category`, `region`, `country`, `state`, `area`, `tier`, `segment`, `channel`
- Schema-suggestion hints for numeric precision and Databricks types

These are metadata/profile-driven rules, not fixed outputs for one QVD.

### Production Issues Found And Fixed

1. `qvd_business_analysis/entity_discovery.py`

   Previous behavior used fixed hierarchy output names such as `Geography`, `Area Tier`, `Product Category`, and `Customer Grouping`.

   Fix applied:

   - Hierarchy detection now uses generic token sequences only.
   - The emitted hierarchy `business_name` is generated from the matched source fields, for example `Region > Country > State`.
   - Hierarchies are only emitted when at least two levels from a known sequence exist.

2. `qvd_business_analysis/lineage_generator.py`

   Previous fallback source label was `source.qvd`, which looked like a hardcoded example file.

   Fix applied:

   - Fallback label is now `uploaded_qvd`.
   - Real outputs still use the uploaded file name or table metadata when available.

## Remaining Acceptable Usages

Sample-specific names remain in tests and documentation examples only:

- Tests use sales-style fixtures such as `ActualSales`, `sales_table`, and `Sales_Sample_3_1.qvd` to verify previous user-requested behavior.
- Additional tests now use non-sales fixtures, including inventory and claims/patient-style metadata, to prove the accelerator is not sales-only.
- Documentation files include examples from prior migration runs and are not used as production rules.

No frontend QVD page or backend route hardcodes one session id, source file name, target table name, or sample-specific business output.

## Tests Added/Updated

Added or updated coverage for:

- Non-sales QVD-like metadata still produces business entities.
- Inventory-style metadata produces inventory measures and dimensions.
- Patient/claims-style metadata produces claims KPIs and does not produce sales KPIs unless sales-like columns exist.
- Hierarchy generation is rule-based and does not emit a hierarchy when only one matching level exists.
- Existing sales-style tests continue to verify expected generic keyword behavior.

## Audit Conclusion

The QVD accelerator production logic is generic after the fixes above. Business analysis, KPI catalog generation, lineage, reconciliation rules, schema suggestion, DDL generation, conversion, validation, packaging, and deployment paths all derive outputs from uploaded metadata, row/profile artifacts, approved mappings, and configurable defaults rather than from a single Sales Sample QVD.
