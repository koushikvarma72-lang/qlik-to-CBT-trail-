# Qlik to dbt Migration Architecture

## Target Flow

1. Extract Qlik script from QVF/QVS input.
2. Parse the script into structured load blocks.
3. Build a migration IR with sources, fields, keys, grain, joins, and warnings.
4. Generate deterministic SQL wherever rules are known.
5. Use AI only for missing or ambiguous transformation logic.
6. Apply deterministic post-processing.
7. Validate compile, semantic, and metadata issues.
8. Repair only failed sections.
9. Return final dbt SQL with score, issues, warnings, and assumptions.

## Execution Modes

- `one_shot`: generate once, validate, return SQL and issues.
- `loop`: skip fast generation and run targeted validation/repair.
- `auto`: try one-shot first, then enter repair only for compile or semantic blockers.

## Deterministic Rules

These must stay in code, not prompts:

- dbt config formatting
- `source()` syntax
- `UNION ALL` column alignment
- `SELECT *` removal inside union CTEs
- `Account` propagation
- `MonthlyRegionKey` calculation
- Qlik `AddMonths` translation
- source-name normalization
- join-key existence validation
- product bridge joins
- `ARSummary-1` source preservation

## AI Scope

AI should only assist with:

- complex load logic interpretation
- ambiguous transformation explanation
- join-intent suggestions
- semantic mismatch repair
- documenting assumptions

## Repair Rule

Repair must be targeted. It must not regenerate the full model when only one section is broken, and it must not delete previously valid joins, schemas, or CTEs.

## Physical Layout

- `backend/extraction/`: input adapters and Qlik parsing. This layer knows about QVF/QVS files, binary sections, load scripts, and decoded metadata.
- `backend/migration/`: migration domain logic. This layer owns IR contracts, deterministic SQL rendering, post-processing, validation, scoring, and repair policy.
- `backend/integrations/`: external service boundaries. This layer owns OpenRouter calls and dbt/dbt Cloud routes.
- `backend/app.py`: Flask composition root. It wires extraction, migration, and integrations together.
New code should import from the package that owns the behavior, for example `backend.migration.sql_generation`.
