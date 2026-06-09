"""
backend/migration/ir.py
=======================
Intermediate Representation (IR) for Qlik → dbt migrations.

Architecture
------------
Instead of generating SQL directly from the Qlik script, the IR system
forces an explicit data model to be built first:

  Qlik Script
      ↓
  extract_sql_generation_plan()   (existing)
      ↓
  build_migration_ir()            (THIS MODULE — new)
      ↓
  validate_ir()                   (THIS MODULE — checks join keys exist,
                                   cardinality flags, null-padding completeness)
      ↓
  render_ir_contract_comment()    (THIS MODULE — emits the schema contract
                                   block that the LLM must include verbatim)
      ↓
  SQL Generation (LLM or deterministic)
      ↓
  audit_sql_against_ir()          (THIS MODULE — post-generation check)

Key concepts
------------
- TableEntry        : one Qlik LOAD block → one IR table with grain + keys
- JoinSpec          : explicit join path with cardinality tag + required action
- UnionSpec         : CONCATENATE → UNION ALL with full null-padding manifest
- DateFieldEntry    : per-field type registry (integer_yyyymm vs date vs string)
- MigrationIR       : the complete model document; includes ambiguities list
                      (fields that need user clarification before generation)

Usage
-----
    from backend.migration.ir import build_migration_ir, validate_ir, render_ir_contract_comment

    plan = extract_sql_generation_plan(qvs_script)
    ir   = build_migration_ir(plan, qvs_script)
    issues = validate_ir(ir)
    contract_sql = render_ir_contract_comment(ir)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


# ─── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class DateFieldEntry:
    """How a date/time field is stored and what conversion is required."""
    field_name: str
    storage_type: str            # 'integer_yyyymm' | 'date' | 'string' | 'unknown'
    conversion_sql: str          # e.g. "TO_DATE(YYYYMM::varchar,'YYYYMM')"
    appears_in_addmonths: bool   # True → used in Addmonths() → check storage type
    is_ambiguous: bool           # True → needs user input before generation


@dataclass
class TableEntry:
    """One Qlik table (LOAD block) with its data model attributes."""
    name: str                              # exact Qlik table name
    cte_name: str                          # safe lowercase SQL CTE identifier
    source: str                            # raw source reference (QVD path, RESIDENT table)
    source_type: str                       # 'from' | 'resident' | 'inline'
    fields: List[str]                      # output column names (after AS alias)
    grain: str                             # human description of row granularity
    key_fields: List[str]                  # fields that join this table to others
    date_fields: Dict[str, DateFieldEntry] # field_name → DateFieldEntry
    is_concatenate: bool                   # True → UNION ALL into concat_target
    concat_target: Optional[str]           # name of target table for UNION ALL
    drop_fields: List[str]                 # columns to exclude after LOAD
    is_island_table: bool                  # True → no direct key to FactTable
    intentional_typos: Dict[str, str]      # {original_name: aliased_name} for typos
    filters: List[str]                     # WHERE conditions


@dataclass
class UnionBranch:
    """One branch of a UNION ALL (base or appended)."""
    source_table: str
    explicit_fields: List[str]   # columns this branch actually provides
    null_padded_fields: List[str] # columns padded with NULL (must match base schema)


@dataclass
class UnionSpec:
    """Describes a CONCATENATE → UNION ALL operation."""
    target_table: str
    base_branch: UnionBranch
    appended_branches: List[UnionBranch]
    all_columns: List[str]       # union of all columns across all branches (ordered)
    null_padding_required: bool  # True when branches have different schemas


@dataclass
class JoinSpec:
    """Explicit join relationship with cardinality and safety tags."""
    from_table: str
    to_table: str
    left_key: str
    right_key: str
    cardinality: str             # 'many-to-one' | 'one-to-many' | 'many-to-many' | 'unknown'
    safe: bool                   # False → row multiplication risk
    required_action: Optional[str]  # 'aggregate_before_join' | 'pivot_before_join' | None
    join_chain: List[str]        # intermediate tables (e.g. ItemBranchMaster between Fact and ItemMaster)
    note: str                    # human-readable explanation


@dataclass
class MigrationIR:
    """Complete intermediate representation for one migration job."""
    tables: Dict[str, TableEntry]          # table_name → TableEntry
    joins: List[JoinSpec]
    unions: List[UnionSpec]
    field_registry: Dict[str, List[str]]   # field_name (lower) → [table_names]
    date_registry: Dict[str, DateFieldEntry] # field_name (lower) → DateFieldEntry
    ambiguities: List[str]                 # questions that must be answered before generation
    warnings: List[str]                    # non-blocking issues
    island_tables: List[str]              # table names tagged as island tables


# ─── YYYYMM / date field heuristics ──────────────────────────────────────────

# Fields whose names suggest they hold YYYYMM-format integers (e.g. 201305)
_YYYYMM_PATTERN = re.compile(r'\byyyymm\b', re.IGNORECASE)

# Common date field suffixes
_DATE_SUFFIX_PATTERN = re.compile(
    r'(date|_dt|_at|timestamp|yyyymm|orderdate|createddate|modifieddate)$',
    re.IGNORECASE,
)

# Fields that are clearly surrogate/composite keys (not dates)
_KEY_SUFFIX_PATTERN = re.compile(r'(key|id|num|no|code)$', re.IGNORECASE)

# Fields that appear inside Addmonths() in the Qlik script
_ADDMONTHS_PATTERN = re.compile(r'Addmonths\s*\(\s*(\[?[A-Za-z0-9_\s]+?\]?)\s*,', re.IGNORECASE)

# Fields that appear inside TO_DATE calls in the script
_TO_DATE_PATTERN = re.compile(r"TO_DATE\s*\(\s*(\[?[A-Za-z0-9_\s]+?\]?)\s*::", re.IGNORECASE)


def _normalize_field_name(name: str) -> str:
    return re.sub(r'[\[\]"\s]', '', str(name or '')).strip()


def _safe_cte_name(name: str) -> str:
    value = str(name or '').strip().strip('[]')
    value = re.sub(r'[^A-Za-z0-9_]+', '_', value).strip('_').lower()
    if not value:
        value = 'load_block'
    if re.match(r'^\d', value):
        value = f'_{value}'
    return value


def _extract_output_field_names(fields: List[str]) -> List[str]:
    """Extract the output column name from 'expr AS alias' or bare 'expr'."""
    results = []
    for f in (fields or []):
        f = str(f).strip()
        # AS alias pattern
        m = re.search(
            r'(?:AS\s+)(\[[^\]]+\]|"[^"]+"|[A-Za-z0-9_$][A-Za-z0-9_$\s-]*)$',
            f, flags=re.IGNORECASE,
        )
        if m:
            alias = m.group(1).strip().strip('[]"')
            results.append(alias)
        else:
            # Plain field reference — strip brackets/quotes
            plain = re.sub(r'[\[\]"]', '', f).strip()
            # Only use simple identifiers, skip expressions
            if re.match(r'^[A-Za-z_][A-Za-z0-9_ -]*$', plain):
                results.append(plain)
    return results


def _detect_addmonths_fields(qvs_script: str) -> set:
    """Return the set of field names used as date inputs to Addmonths()."""
    fields = set()
    for m in _ADDMONTHS_PATTERN.finditer(qvs_script or ''):
        raw = _normalize_field_name(m.group(1))
        if raw:
            fields.add(raw.lower())
    return fields


def _infer_date_storage(field_name: str, addmonths_fields: set, qvs_script: str) -> DateFieldEntry:
    """Infer storage type and required conversion for a date field."""
    name_lower = field_name.lower()
    in_addmonths = name_lower in addmonths_fields

    # YYYYMM is almost always stored as an integer (201305 format)
    if _YYYYMM_PATTERN.search(name_lower):
        return DateFieldEntry(
            field_name=field_name,
            storage_type='integer_yyyymm',
            conversion_sql=f"TO_DATE({field_name}::varchar, 'YYYYMM')",
            appears_in_addmonths=in_addmonths,
            is_ambiguous=False,
        )

    # If it appears in Addmonths but name doesn't obviously say YYYYMM,
    # we can't be sure — flag as ambiguous
    if in_addmonths:
        return DateFieldEntry(
            field_name=field_name,
            storage_type='unknown',
            conversion_sql='',
            appears_in_addmonths=True,
            is_ambiguous=True,
        )

    # Standard date suffix → probably a real DATE type
    if _DATE_SUFFIX_PATTERN.search(name_lower):
        return DateFieldEntry(
            field_name=field_name,
            storage_type='date',
            conversion_sql=field_name,  # no conversion needed
            appears_in_addmonths=False,
            is_ambiguous=False,
        )

    return DateFieldEntry(
        field_name=field_name,
        storage_type='unknown',
        conversion_sql='',
        appears_in_addmonths=False,
        is_ambiguous=True,
    )


def _detect_intentional_typos(fields: List[str]) -> Dict[str, str]:
    """
    Detect field aliases that differ from their expression only in typo-like ways.
    e.g. 'ExpeenseBudget AS ExpenseBudget'
    Returns {raw_expression: alias} for cases that look like typo corrections.
    """
    typos = {}
    for f in (fields or []):
        f = str(f).strip()
        m = re.search(
            r'^(.+?)\s+AS\s+(\[[^\]]+\]|"[^"]+"|[A-Za-z0-9_$][A-Za-z0-9_$\s-]*)$',
            f, flags=re.IGNORECASE,
        )
        if not m:
            continue
        expr = m.group(1).strip().strip('[]"')
        alias = m.group(2).strip().strip('[]"')
        # Heuristic: both are identifiers and differ primarily in spelling
        if (
            re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', expr)
            and re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', alias)
            and expr.lower() != alias.lower()
            and len(expr) >= 4
        ):
            # Simple edit-distance proxy: shared prefix or suffix suggests typo
            common_prefix = len(re.match(r'(\w*)', expr.lower().rstrip()).group(1))
            if common_prefix >= 3:
                typos[expr] = alias
    return typos


def _infer_grain(table_name: str, fields: List[str], source_type: str) -> str:
    """Best-effort grain description from table name and field names."""
    name_lower = table_name.lower()
    field_names_lower = {f.lower() for f in fields}

    if 'fact' in name_lower:
        if any('orderid' in f or 'order_id' in f for f in field_names_lower):
            return 'one row per order line item'
        return 'transaction fact grain'
    if 'calendar' in name_lower:
        if 'yyyymm' in field_names_lower:
            return 'one row per YYYYMM period'
        return 'one row per date/period'
    if 'customer' in name_lower or 'cust' in name_lower:
        return 'one row per customer'
    if 'budget' in name_lower:
        return 'one row per Region + Month (not transaction level)'
    if 'expense' in name_lower:
        return 'one row per Region + Account + Month'
    if 'arsummary' in name_lower or 'ar_summary' in name_lower:
        if any('arage' in f for f in field_names_lower):
            return 'one row per CustKeyAR per ARAge bucket (up to 4 rows per customer)'
        return 'one row per CustKeyAR (snapshot)'
    if 'item' in name_lower or 'product' in name_lower:
        return 'one row per item/product'
    if 'account' in name_lower:
        return 'one row per account'
    if source_type == 'resident':
        return 'derived from resident table (same grain or aggregated)'
    return 'grain not determined — verify before joining'


def _infer_cardinality_and_safety(
    from_table: str, to_table: str, to_entry: Optional[TableEntry]
) -> tuple:
    """
    Heuristic: infer whether a join is safe.
    Returns (cardinality, safe, required_action, note).
    """
    if to_entry is None:
        return 'unknown', False, None, 'Target table not found in IR — cannot verify cardinality'

    to_grain = to_entry.grain.lower()
    from_lower = from_table.lower()

    # Dimension tables → typically safe many-to-one
    dimension_signals = ['customer', 'item', 'product', 'account', 'calendar',
                         'master', 'hierarchy', 'group', 'segment']
    if any(s in to_grain or s in to_entry.name.lower() for s in dimension_signals):
        return 'many-to-one', True, None, 'Dimension table — one row per key'

    # AR aging — explicitly multi-row per customer
    if 'arsummary' in to_entry.name.lower() or 'ar_summary' in to_entry.name.lower():
        if 'arage' in ' '.join(to_entry.key_fields).lower() or '4 rows' in to_grain:
            return (
                'one-to-many', False, 'pivot_before_join',
                'ARSummary-1 has up to 4 rows per CustKeyAR (one per ARAge bucket). '
                'Pivot to one row per customer before joining.',
            )
        return 'many-to-one', True, None, 'AR snapshot — one row per CustKeyAR'

    # Expense/budget tables — multi-row per MonthlyRegionKey
    if 'expense' in to_entry.name.lower() or 'budget' in to_entry.name.lower():
        return (
            'many-to-many', False, 'aggregate_before_join',
            f'{to_entry.name} has multiple rows per join key (grain: {to_entry.grain}). '
            'Aggregate to the join key grain before joining.',
        )

    return 'unknown', False, None, 'Cannot determine cardinality — verify before joining'


# ─── Core builder ─────────────────────────────────────────────────────────────

def build_migration_ir(plan: List[dict], qvs_script: str = '') -> MigrationIR:
    """
    Build a MigrationIR from the output of extract_sql_generation_plan().

    Args:
        plan:       List of plan dicts from extract_sql_generation_plan()
        qvs_script: Raw Qlik script text (used for addmonths detection etc.)

    Returns:
        MigrationIR ready for validate_ir() and render_ir_contract_comment()
    """
    tables: Dict[str, TableEntry] = {}
    unions: List[UnionSpec] = []
    ambiguities: List[str] = []
    warnings: List[str] = []

    # Pre-scan: find all fields used in Addmonths()
    addmonths_fields = _detect_addmonths_fields(qvs_script)

    # ── Pass 1: build TableEntry for every LOAD block ─────────────────────────
    for item in plan:
        if item.get('operation') == 'DROP_FIELDS':
            continue

        table_name = item.get('table') or 'unnamed'
        raw_fields = item.get('fields') or []
        output_fields = _extract_output_field_names(raw_fields)
        source_type = (item.get('source_type') or 'from').lower()

        # Date field registry for this table
        date_fields: Dict[str, DateFieldEntry] = {}
        for col in output_fields:
            col_lower = col.lower()
            if (_DATE_SUFFIX_PATTERN.search(col_lower)
                    or col_lower in addmonths_fields
                    or _YYYYMM_PATTERN.search(col_lower)):
                entry = _infer_date_storage(col, addmonths_fields, qvs_script)
                date_fields[col_lower] = entry
                if entry.is_ambiguous:
                    ambiguities.append(
                        f"DATE AMBIGUITY: field '{col}' in table '{table_name}' — "
                        f"cannot determine storage type (date vs integer_yyyymm). "
                        f"Check $tags in QVD metadata or Qlik script comments."
                    )

        # Typo detection
        typos = _detect_intentional_typos(raw_fields)

        # Grain inference
        grain = _infer_grain(table_name, output_fields, source_type)

        # Simple key field heuristic: fields that are used to join other tables
        key_fields = [
            f for f in output_fields
            if _KEY_SUFFIX_PATTERN.search(f.lower()) or 'regionkey' in f.lower()
        ]

        entry = TableEntry(
            name=table_name,
            cte_name=_safe_cte_name(table_name),
            source=item.get('source') or '',
            source_type=source_type,
            fields=output_fields,
            grain=grain,
            key_fields=key_fields,
            date_fields=date_fields,
            is_concatenate=bool(item.get('is_concatenate')),
            concat_target=item.get('concatenate_target'),
            drop_fields=item.get('drop_fields') or [],
            is_island_table=False,  # resolved in Pass 2
            intentional_typos=typos,
            filters=item.get('filters') or [],
        )
        # Don't overwrite a base entry with a concat entry
        if table_name not in tables:
            tables[table_name] = entry

    # ── Pass 2: build UNION specs from CONCATENATE blocks ─────────────────────
    concat_groups: Dict[str, List[dict]] = {}
    for item in plan:
        if item.get('is_concatenate') and item.get('concatenate_target'):
            target = item['concatenate_target']
            concat_groups.setdefault(target, []).append(item)

    for target_name, concat_items in concat_groups.items():
        base_entry = tables.get(target_name)
        if base_entry is None:
            warnings.append(
                f"UNION ALL: CONCATENATE target '{target_name}' not found as a base LOAD block."
            )
            continue

        base_fields = base_entry.fields
        appended_branches: List[UnionBranch] = []

        for concat_item in concat_items:
            branch_fields = _extract_output_field_names(concat_item.get('fields') or [])
            null_padded = [c for c in base_fields if c not in branch_fields]
            appended_branches.append(UnionBranch(
                source_table=concat_item.get('table') or '',
                explicit_fields=branch_fields,
                null_padded_fields=null_padded,
            ))

        base_branch = UnionBranch(
            source_table=target_name,
            explicit_fields=base_fields,
            null_padded_fields=[],
        )
        all_columns = list(base_fields)
        for branch in appended_branches:
            for f in branch.explicit_fields:
                if f not in all_columns:
                    all_columns.append(f)

        null_needed = any(len(b.null_padded_fields) > 0 for b in appended_branches)
        if null_needed:
            warnings.append(
                f"UNION ALL '{target_name}': appended branch(es) have fewer columns "
                f"than the base — null-padding is required. "
                f"Missing columns must be emitted as CAST(NULL AS type)."
            )

        unions.append(UnionSpec(
            target_table=target_name,
            base_branch=base_branch,
            appended_branches=appended_branches,
            all_columns=all_columns,
            null_padding_required=null_needed,
        ))

    # ── Pass 3: build field registry (field_name → tables) ───────────────────
    field_registry: Dict[str, List[str]] = {}
    for tname, tentry in tables.items():
        for col in tentry.fields:
            key = col.lower()
            field_registry.setdefault(key, []).append(tname)

    # ── Pass 4: detect island tables (no shared key with any 'fact' table) ────
    fact_tables = [n for n in tables if 'fact' in n.lower()]
    fact_fields: set = set()
    for ft in fact_tables:
        fact_fields.update(f.lower() for f in tables[ft].fields)

    island_tables: List[str] = []
    for tname, tentry in tables.items():
        if 'fact' in tname.lower():
            continue
        if tentry.is_concatenate:
            continue  # concat branches share grain with their target
        shared = {f.lower() for f in tentry.fields} & fact_fields
        if not shared:
            tentry.is_island_table = True
            island_tables.append(tname)

    # ── Pass 5: build join specs ──────────────────────────────────────────────
    # Basic join detection: tables that share a key field with the fact table
    joins: List[JoinSpec] = []
    for tname, tentry in tables.items():
        if 'fact' in tname.lower() or tentry.is_concatenate:
            continue
        for ft in fact_tables:
            ft_entry = tables[ft]
            shared_keys = (
                {k.lower() for k in tentry.key_fields}
                & {k.lower() for k in ft_entry.key_fields + ft_entry.fields}
            )
            if not shared_keys:
                continue
            key = next(iter(shared_keys))
            cardinality, safe, req_action, note = _infer_cardinality_and_safety(
                ft, tname, tentry
            )
            joins.append(JoinSpec(
                from_table=ft,
                to_table=tname,
                left_key=key,
                right_key=key,
                cardinality=cardinality,
                safe=safe,
                required_action=req_action,
                join_chain=[],
                note=note,
            ))

    # Build date registry across all tables
    date_registry: Dict[str, DateFieldEntry] = {}
    for tentry in tables.values():
        for fname, dentry in tentry.date_fields.items():
            if fname not in date_registry:
                date_registry[fname] = dentry
            elif date_registry[fname].storage_type != dentry.storage_type:
                ambiguities.append(
                    f"DATE CONSISTENCY: field '{fname}' has inconsistent storage types "
                    f"across tables — check that the same conversion is applied everywhere."
                )

    return MigrationIR(
        tables=tables,
        joins=joins,
        unions=unions,
        field_registry=field_registry,
        date_registry=date_registry,
        ambiguities=ambiguities,
        warnings=warnings,
        island_tables=island_tables,
    )


# ─── IR Validation ────────────────────────────────────────────────────────────

@dataclass
class IRIssue:
    level: str     # 'error' | 'warning' | 'ambiguity'
    code: str
    message: str
    suggestion: Optional[str] = None

    def __str__(self):
        parts = [f"[{self.level.upper()}] {self.code}: {self.message}"]
        if self.suggestion:
            parts.append(f"  → {self.suggestion}")
        return "\n".join(parts)


def validate_ir(ir: MigrationIR) -> List[IRIssue]:
    """
    Run validation checks on the IR before SQL generation begins.

    Checks:
    1. Every join left_key exists in the from_table's field list
    2. Every join right_key exists in the to_table's field list
    3. Every many-to-many join has a required_action
    4. Every UNION ALL with null_padding_required has explicit null_padded_fields
    5. Ambiguous date fields are flagged as metadata warnings
    6. Island tables are flagged with a warning
    """
    issues: List[IRIssue] = []

    # 1. Ambiguities → metadata warnings. They should be annotated/assumed, not
    # block SQL that is internally consistent.
    for amb in ir.ambiguities:
        issues.append(IRIssue(
            level='warning',
            code='IR_AMBIGUITY',
            message=amb,
            suggestion='Record the assumed type in generated SQL comments and verify with QVD metadata if available.',
        ))

    # 2. Join key existence checks
    for j in ir.joins:
        from_entry = ir.tables.get(j.from_table)
        to_entry = ir.tables.get(j.to_table)

        if from_entry and j.left_key:
            from_fields_lower = {f.lower() for f in from_entry.fields}
            if j.left_key.lower() not in from_fields_lower:
                issues.append(IRIssue(
                    level='error',
                    code='JOIN_KEY_MISSING_LEFT',
                    message=(
                        f"Join from '{j.from_table}' to '{j.to_table}': "
                        f"left key '{j.left_key}' does not exist in {j.from_table}'s field list."
                    ),
                    suggestion=(
                        f"Check the LOAD statement for '{j.from_table}'. "
                        f"Available fields: {', '.join(sorted(from_entry.fields)[:10])}"
                    ),
                ))

        if to_entry and j.right_key:
            to_fields_lower = {f.lower() for f in to_entry.fields}
            if j.right_key.lower() not in to_fields_lower:
                issues.append(IRIssue(
                    level='error',
                    code='JOIN_KEY_MISSING_RIGHT',
                    message=(
                        f"Join from '{j.from_table}' to '{j.to_table}': "
                        f"right key '{j.right_key}' does not exist in {j.to_table}'s field list."
                    ),
                    suggestion=(
                        f"Check the LOAD statement for '{j.to_table}'. "
                        f"Available fields: {', '.join(sorted(to_entry.fields)[:10])}. "
                        f"If the join requires an intermediate table, add it to join_chain."
                    ),
                ))

        # 3. Many-to-many joins must have a required_action
        if j.cardinality == 'many-to-many' and not j.required_action:
            issues.append(IRIssue(
                level='error',
                code='MANY_TO_MANY_NO_ACTION',
                message=(
                    f"Join from '{j.from_table}' to '{j.to_table}' is many-to-many "
                    f"but has no required_action specified."
                ),
                suggestion=(
                    "Set required_action to 'aggregate_before_join' or 'pivot_before_join'. "
                    "Many-to-many joins will multiply rows without pre-aggregation."
                ),
            ))

    # 4. UNION ALL null-padding completeness
    for u in ir.unions:
        for branch in u.appended_branches:
            missing = set(u.all_columns) - set(branch.explicit_fields) - set(branch.null_padded_fields)
            if missing:
                issues.append(IRIssue(
                    level='error',
                    code='UNION_NULL_PADDING_INCOMPLETE',
                    message=(
                        f"UNION ALL '{u.target_table}': appended branch "
                        f"'{branch.source_table}' is missing null-padding for "
                        f"columns: {', '.join(sorted(missing))}"
                    ),
                    suggestion=(
                        "Add CAST(NULL AS <type>) AS \"<col>\" for each missing column "
                        "in the appended UNION ALL branch."
                    ),
                ))

    # 5. Island table warnings
    for island in ir.island_tables:
        entry = ir.tables.get(island)
        grain = entry.grain if entry else 'unknown grain'
        issues.append(IRIssue(
            level='warning',
            code='ISLAND_TABLE',
            message=(
                f"Table '{island}' has no shared key with the fact table. "
                f"Grain: {grain}."
            ),
            suggestion=(
                f"'{island}' cannot be joined directly to the fact table. "
                "Either aggregate it to a matching grain first, or model it as a "
                "separate mart. Do not force-join it on an unrelated key."
            ),
        ))

    # 6. Date consistency across CTEs
    seen_conversions: Dict[str, str] = {}
    for tname, tentry in ir.tables.items():
        for fname, dentry in tentry.date_fields.items():
            key = fname.lower()
            if dentry.conversion_sql:
                if key in seen_conversions and seen_conversions[key] != dentry.conversion_sql:
                    issues.append(IRIssue(
                        level='warning',
                        code='DATE_CONVERSION_INCONSISTENT',
                        message=(
                            f"Field '{fname}' uses different conversions in different CTEs: "
                            f"'{seen_conversions[key]}' vs '{dentry.conversion_sql}'"
                        ),
                        suggestion=(
                            "Pick one conversion approach and apply it consistently. "
                            "If reading from a raw source, use TO_DATE(). "
                            "If reading from a CTE that already converted it, use the column directly."
                        ),
                    ))
                seen_conversions[key] = dentry.conversion_sql

    return issues


# ─── IR → SQL Contract Block ──────────────────────────────────────────────────

def render_ir_contract_comment(ir: MigrationIR) -> str:
    """
    Render the schema contract as a SQL comment block.

    This block is injected at the top of the generated SQL so the LLM and
    any human reviewer can see the full data model in one place.
    """
    lines = [
        "-- ═══════════════════════════════════════════════════════════════════",
        "-- MIGRATION SCHEMA CONTRACT (auto-generated by migration_ir.py)",
        "-- ═══════════════════════════════════════════════════════════════════",
        "--",
    ]

    # SOURCE FIELD REGISTRY
    lines.append("-- SOURCE FIELD REGISTRY")
    for tname, tentry in ir.tables.items():
        if tentry.is_concatenate:
            continue
        truncated = tentry.fields[:8]
        more = len(tentry.fields) - 8
        field_str = ', '.join(truncated) + (f', ... (+{more} more)' if more > 0 else '')
        lines.append(f"--   {tname}: [{field_str}]")
    lines.append("--")

    # SOURCE NAME MAP
    lines.append("-- SOURCE NAME MAP")
    for tname, tentry in ir.tables.items():
        raw_source = (tentry.source or '').strip()
        if not raw_source:
            continue
        lines.append(
            f"--   Qlik table {tname} reads raw source {raw_source}; "
            f"use source('raw', '<exact raw source name>') before aliasing to {tentry.cte_name}."
        )
    if not any((t.source or '').strip() for t in ir.tables.values()):
        lines.append("--   (no raw source names detected)")
    lines.append("--")

    # DATE FIELD TYPES
    lines.append("-- DATE FIELD TYPES")
    if ir.date_registry:
        for fname, dentry in ir.date_registry.items():
            storage = dentry.storage_type
            conv = dentry.conversion_sql or 'ambiguous — needs clarification'
            lines.append(f"--   {dentry.field_name}: storage={storage}, conversion={conv}")
    else:
        lines.append("--   (no date fields detected)")
    lines.append("--")

    # INTENTIONAL SOURCE TYPOS
    all_typos = {}
    for tentry in ir.tables.values():
        for orig, alias in tentry.intentional_typos.items():
            all_typos[orig] = alias
    if all_typos:
        lines.append("-- INTENTIONAL SOURCE TYPOS (do not correct)")
        for orig, alias in all_typos.items():
            lines.append(f"--   {orig} → aliased as {alias}")
        lines.append("--")

    # ISLAND TABLE GRAINS
    lines.append("-- ISLAND TABLE GRAINS")
    if ir.island_tables:
        for tname in ir.island_tables:
            entry = ir.tables.get(tname)
            grain = entry.grain if entry else 'unknown'
            lines.append(f"--   {tname}: {grain}")
    else:
        lines.append("--   (no island tables detected)")
    lines.append("--")

    # UNION ALL NULL PADDING MANIFEST
    if ir.unions:
        lines.append("-- UNION ALL NULL PADDING MANIFEST")
        for u in ir.unions:
            lines.append(f"--   Target: {u.target_table}")
            lines.append(f"--   Base columns: {', '.join(u.all_columns[:6])}")
            for branch in u.appended_branches:
                if branch.null_padded_fields:
                    lines.append(
                        f"--   Branch '{branch.source_table}' must NULL-pad: "
                        + ', '.join(branch.null_padded_fields[:8])
                    )
        lines.append("--")

    # JOIN MAP
    if ir.joins:
        lines.append("-- JOIN MAP")
        for j in ir.joins:
            safety_tag = "SAFE" if j.safe else f"UNSAFE ({j.required_action or 'no action specified'})"
            lines.append(
                f"--   {j.from_table}.{j.left_key} → {j.to_table}.{j.right_key} "
                f"[{j.cardinality}] [{safety_tag}]"
            )
            if j.note:
                lines.append(f"--     Note: {j.note}")
        lines.append("--")

    # AMBIGUITIES
    if ir.ambiguities:
        lines.append("-- CONTRACT QUESTIONS (must be resolved before SQL generation)")
        for i, amb in enumerate(ir.ambiguities, 1):
            lines.append(f"--   Q{i}: {amb}")
        lines.append("--")

    # WARNINGS
    if ir.warnings:
        lines.append("-- WARNINGS")
        for w in ir.warnings:
            lines.append(f"--   WARNING: {w}")
        lines.append("--")

    lines.append("-- ═══════════════════════════════════════════════════════════════════")

    return "\n".join(lines)


# ─── Post-generation SQL Audit ────────────────────────────────────────────────

@dataclass
class AuditIssue:
    level: str
    code: str
    message: str
    suggestion: Optional[str] = None

    def __str__(self):
        return f"[{self.level.upper()}] {self.code}: {self.message}"


def audit_sql_against_ir(sql: str, ir: MigrationIR) -> List[AuditIssue]:
    """
    Post-generation audit: check that the generated SQL matches what the IR describes.

    Checks:
    1. No SELECT * inside a UNION ALL branch (column mismatch risk)
    2. Every many-to-many join has a pre-aggregation CTE
    3. Every pivot-required table has a pivoted CTE
    4. No bare field reference that doesn't exist in the referenced table
    5. UNION ALL branches have consistent column counts (heuristic)
    """
    issues: List[AuditIssue] = []
    sql_upper = sql.upper()
    sql_lower = sql.lower()

    def _split_select_fields(field_text: str) -> List[str]:
        fields = []
        token = []
        depth = 0
        in_single = False
        in_double = False
        for ch in field_text or '':
            if ch == "'" and not in_double:
                in_single = not in_single
            elif ch == '"' and not in_single:
                in_double = not in_double
            elif not in_single and not in_double:
                if ch == '(':
                    depth += 1
                elif ch == ')' and depth > 0:
                    depth -= 1
                elif ch == ',' and depth == 0:
                    item = ''.join(token).strip()
                    if item:
                        fields.append(item)
                    token = []
                    continue
            token.append(ch)
        tail = ''.join(token).strip()
        if tail:
            fields.append(tail)
        return fields

    def _output_name(select_item: str) -> Optional[str]:
        item = (select_item or '').strip()
        m = re.search(
            r'\s+AS\s+(\[[^\]]+\]|"[^"]+"|[A-Za-z0-9_$][A-Za-z0-9_$\s-]*)$',
            item,
            flags=re.IGNORECASE,
        )
        if m:
            return _normalize_field_name(m.group(1))
        if re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', item):
            return item
        if re.match(r'^"[^"]+"$', item):
            return _normalize_field_name(item)
        return None

    def _cte_body(sql_text: str, cte_name: str) -> str:
        match = re.search(
            rf'\b{re.escape(cte_name)}\s+AS\s*\(',
            sql_text or '',
            flags=re.IGNORECASE,
        )
        if not match:
            return ''
        start = match.end()
        depth = 1
        i = start
        while i < len(sql_text):
            ch = sql_text[i]
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    return sql_text[start:i]
            i += 1
        return sql_text[start:]

    def _collect_cte_fields(sql_text: str) -> Dict[str, set]:
        cte_fields: Dict[str, set] = {}
        cte_sources: Dict[str, str] = {}
        cte_names = {
            m.group(1)
            for m in re.finditer(
                r'\b([A-Za-z_][A-Za-z0-9_]*)\s+AS\s*\(',
                sql_text or '',
                flags=re.IGNORECASE,
            )
        }
        for raw_cte_name in cte_names:
            cte_name = raw_cte_name.lower()
            body = _cte_body(sql_text, raw_cte_name)
            match = re.search(
                r'^\s*SELECT\s+(.*?)\s+FROM(?:\s+([A-Za-z_][A-Za-z0-9_]*))?\b',
                body or '',
                flags=re.IGNORECASE | re.DOTALL,
            )
            if not match:
                continue
            source_name = (match.group(2) or '').lower()
            fields = set()
            for item in _split_select_fields(match.group(1)):
                if item.strip() == '*':
                    fields.add('*')
                    continue
                name = _output_name(item)
                if name:
                    fields.add(name.lower())
            cte_fields[cte_name] = fields
            if fields == {'*'} and source_name:
                cte_sources[cte_name] = source_name

        for entry in ir.tables.values():
            cte_fields.setdefault(entry.cte_name.lower(), set())
            cte_fields[entry.cte_name.lower()].update(f.lower() for f in entry.fields)

        changed = True
        while changed:
            changed = False
            for cte_name, source_name in list(cte_sources.items()):
                source_fields = cte_fields.get(source_name)
                if source_fields and '*' not in source_fields and cte_fields.get(cte_name) == {'*'}:
                    cte_fields[cte_name] = set(source_fields)
                    changed = True
        return cte_fields

    def _collect_aliases(sql_text: str) -> Dict[str, str]:
        aliases = {}
        relation_pattern = re.compile(
            r'\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)\s+(?:AS\s+)?([A-Za-z_][A-Za-z0-9_]*)\b',
            re.IGNORECASE,
        )
        for rel, alias in relation_pattern.findall(sql_text or ''):
            if alias.upper() in {'ON', 'WHERE', 'LEFT', 'RIGHT', 'FULL', 'INNER', 'JOIN'}:
                continue
            aliases[alias.lower()] = rel.lower()
        return aliases

    def _collect_cte_output_styles(sql_text: str) -> Dict[str, Dict[str, bool]]:
        styles: Dict[str, Dict[str, bool]] = {}

        def output_style(select_item: str) -> Optional[Tuple[str, bool]]:
            item = (select_item or '').strip()
            m = re.search(
                r'\s+AS\s+(\[[^\]]+\]|"[^"]+"|[A-Za-z0-9_$][A-Za-z0-9_$\s-]*)$',
                item,
                flags=re.IGNORECASE,
            )
            if m:
                raw = m.group(1).strip()
                return _normalize_field_name(raw).lower(), raw.startswith('"')
            if re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', item):
                return item.lower(), False
            if re.match(r'^"[^"]+"$', item):
                return _normalize_field_name(item).lower(), True
            return None

        for raw_cte_name in _collect_cte_names(sql_text):
            body = _cte_body(sql_text, raw_cte_name)
            match = re.search(
                r'^\s*SELECT\s+(.*?)\s+FROM\b',
                body or '',
                flags=re.IGNORECASE | re.DOTALL,
            )
            if not match:
                continue
            cte_styles: Dict[str, bool] = {}
            for item in _split_select_fields(match.group(1)):
                style = output_style(item)
                if style:
                    column, quoted = style
                    cte_styles[column] = quoted
            styles[raw_cte_name.lower()] = cte_styles
        return styles

    def _collect_cte_names(sql_text: str) -> set:
        return {
            m.group(1).lower()
            for m in re.finditer(
                r'\b([A-Za-z_][A-Za-z0-9_]*)\s+AS\s*\(',
                sql_text or '',
                flags=re.IGNORECASE,
            )
        }

    def _is_key_field(name: str) -> bool:
        value = re.sub(r'[^a-z0-9]+', '', str(name or '').lower())
        return (
            value.endswith('key')
            or value.endswith('id')
            or value in {'custkey', 'custkeyar', 'itembranchkey', 'addressnumber'}
        )

    def _is_descriptive_field(name: str) -> bool:
        value = re.sub(r'[^a-z0-9]+', '', str(name or '').lower())
        return (
            value in {'customer', 'customername', 'shortname'}
            or value.endswith('name')
            or value.endswith('desc')
            or value.endswith('description')
            or value.endswith('text')
        )

    def _select_columns_from_branch(branch: str) -> List[str]:
        match = re.search(r'\bSELECT\b(.*?)\bFROM\b', branch or '', re.IGNORECASE | re.DOTALL)
        if not match:
            return []
        names = []
        for item in _split_select_fields(match.group(1)):
            name = _output_name(item)
            if name:
                names.append(name.lower())
        return names

    cte_fields = _collect_cte_fields(sql)
    aliases = _collect_aliases(sql)
    cte_names = _collect_cte_names(sql)
    cte_output_styles = _collect_cte_output_styles(sql)

    # 1. SELECT * inside a UNION ALL
    union_all_blocks = re.findall(
        r'UNION\s+ALL\s*\n\s*SELECT\s+\*',
        sql, flags=re.IGNORECASE
    )
    if union_all_blocks:
        issues.append(AuditIssue(
            level='error',
            code='UNION_SELECT_STAR',
            message=(
                f"Found {len(union_all_blocks)} UNION ALL branch(es) using SELECT *. "
                "This will fail at runtime if branches have different schemas."
            ),
            suggestion=(
                "Enumerate every column explicitly in each UNION ALL branch. "
                "Pad missing columns with CAST(NULL AS <type>) AS \"col\"."
            ),
        ))

    # 2. Many-to-many joins require aggregation CTE
    for j in ir.joins:
        if j.cardinality == 'many-to-many' and j.required_action == 'aggregate_before_join':
            to_cte = _safe_cte_name(j.to_table)
            agg_cte = f"{to_cte}_aggregated"
            # Check that an aggregation CTE exists before the join
            if agg_cte not in sql_lower:
                issues.append(AuditIssue(
                    level='error',
                    code='MISSING_AGGREGATION_CTE',
                    message=(
                        f"Join to '{j.to_table}' is many-to-many ({j.note}) "
                        f"but no aggregation CTE '{agg_cte}' was found in the SQL."
                    ),
                    suggestion=(
                        f"Add a CTE '{agg_cte}' that aggregates '{to_cte}' to the "
                        f"join key '{j.right_key}' grain before joining."
                    ),
                ))

    # 3. Pivot-required tables
    for j in ir.joins:
        if j.required_action == 'pivot_before_join':
            to_cte = _safe_cte_name(j.to_table)
            pivot_cte = f"{to_cte}_pivoted"
            if pivot_cte not in sql_lower:
                issues.append(AuditIssue(
                    level='error',
                    code='MISSING_PIVOT_CTE',
                    message=(
                        f"Join to '{j.to_table}' requires a pivot ({j.note}) "
                        f"but no pivot CTE '{pivot_cte}' was found."
                    ),
                    suggestion=(
                        f"Add a '{pivot_cte}' CTE that pivots '{to_cte}' so there "
                        f"is one row per {j.right_key} before joining."
                    ),
                ))

    # 4. Island tables should not appear in JOIN conditions directly
    for island in ir.island_tables:
        cte_name = _safe_cte_name(island)
        # Look for "JOIN <island_cte> ON" pattern
        if re.search(rf'\bJOIN\s+{re.escape(cte_name)}\s+\w+\s+ON\b', sql, re.IGNORECASE):
            issues.append(AuditIssue(
                level='warning',
                code='ISLAND_TABLE_JOINED_DIRECTLY',
                message=(
                    f"Island table '{island}' is joined directly in the SQL "
                    f"even though it has no shared key with the fact table."
                ),
                suggestion=(
                    f"Remove the direct JOIN to '{island}'. "
                    "Either aggregate it to a compatible grain first, or model it as a separate mart."
                ),
            ))

    # 5. Alias column ownership and JOIN key existence checks
    for alias, column in re.findall(
        r'\b([A-Za-z_][A-Za-z0-9_]*)\."?([A-Za-z_][A-Za-z0-9_ ]*)"?',
        sql or '',
        flags=re.IGNORECASE,
    ):
        rel = aliases.get(alias.lower())
        if not rel:
            continue
        fields = cte_fields.get(rel)
        if not fields or '*' in fields:
            continue
        if column.lower().strip() not in fields:
            issues.append(AuditIssue(
                level='error',
                code='COLUMN_OWNERSHIP_MISMATCH',
                message=(
                    f"Alias '{alias}' references column '{column}', but '{rel}' "
                    "does not expose that column."
                ),
                suggestion=(
                    "Add the column to the source/union CTE if it belongs there, "
                    "or select it from the CTE that actually owns it."
                ),
            ))

    for join_match in re.finditer(
        r'\bJOIN\s+[A-Za-z_][A-Za-z0-9_]*(?:\s+AS)?\s+([A-Za-z_][A-Za-z0-9_]*)\s+ON\s+(.*?)(?=\b(?:LEFT|RIGHT|FULL|INNER|CROSS)?\s*JOIN\b|\bWHERE\b|\bGROUP\s+BY\b|\bORDER\s+BY\b|\)|$)',
        sql or '',
        flags=re.IGNORECASE | re.DOTALL,
    ):
        condition = join_match.group(2)
        for alias, column in re.findall(r'\b([A-Za-z_][A-Za-z0-9_]*)\."?([A-Za-z_][A-Za-z0-9_ ]*)"?', condition):
            rel = aliases.get(alias.lower())
            fields = cte_fields.get(rel or '')
            if fields and '*' not in fields and column.lower().strip() not in fields:
                issues.append(AuditIssue(
                    level='error',
                    code='JOIN_KEY_MISSING',
                    message=(
                        f"JOIN condition references {alias}.{column}, but "
                        f"'{rel}' does not expose '{column}'."
                    ),
                    suggestion='Use only keys present on both sides of the join.',
                ))

        for left_alias, left_col, right_alias, right_col in re.findall(
            r'\b([A-Za-z_][A-Za-z0-9_]*)\."?([A-Za-z_][A-Za-z0-9_ -]*)"?\s*=\s*'
            r'\b([A-Za-z_][A-Za-z0-9_]*)\."?([A-Za-z_][A-Za-z0-9_ -]*)"?',
            condition,
            flags=re.IGNORECASE,
        ):
            if (
                (_is_key_field(left_col) and _is_descriptive_field(right_col))
                or (_is_key_field(right_col) and _is_descriptive_field(left_col))
            ):
                issues.append(AuditIssue(
                    level='error',
                    code='INVALID_KEY_TO_TEXT_JOIN',
                    message=(
                        f'Join condition uses incompatible fields: '
                        f'{left_alias}.{left_col} = {right_alias}.{right_col}.'
                    ),
                    suggestion='Use only validated Qlik association keys; do not join keys to descriptive text.',
                ))
            left_rel = aliases.get(left_alias.lower(), '')
            right_rel = aliases.get(right_alias.lower(), '')
            left_norm = re.sub(r'[^a-z0-9]+', '', left_col.lower())
            right_norm = re.sub(r'[^a-z0-9]+', '', right_col.lower())
            itemmaster_rels = {'itemmaster', 'item_master'}
            if (
                (right_rel in itemmaster_rels and right_norm == 'shortname' and left_norm == 'itembranchkey')
                or (left_rel in itemmaster_rels and left_norm == 'shortname' and right_norm == 'itembranchkey')
            ):
                issues.append(AuditIssue(
                    level='error',
                    code='WRONG_PRODUCT_JOIN_PATH',
                    message='Fact/Item-Branch Key is being joined directly to ItemMaster.Short Name.',
                    suggestion='Repair-lock: keep the bridge path facttable_with_expenses -> itembranchmaster -> itemmaster.',
                ))

    for alias, column in re.findall(r'\b([A-Za-z_][A-Za-z0-9_]*)\."([^"]+)"', sql or ''):
        rel = aliases.get(alias.lower())
        if not rel:
            continue
        output_styles = cte_output_styles.get(rel, {})
        emitted_quoted = output_styles.get(column.lower())
        if emitted_quoted is False and column != column.upper():
            issues.append(AuditIssue(
                level='error',
                code='QUOTED_CASE_MISMATCH',
                message=(
                    f'{alias}."{column}" references a column emitted unquoted by '
                    f"'{rel}'. Snowflake resolves the emitted column as uppercase."
                ),
                suggestion=f'Use {alias}.{column} or quote the column consistently where it is selected.',
            ))

    # 6. Known Qlik join-path and grain checks
    def _has_cte(*names: str) -> bool:
        return any(name.lower() in cte_names for name in names)

    def _has_join(*names: str) -> bool:
        return any(
            re.search(rf'\bJOIN\s+{re.escape(name)}\b', sql or '', re.IGNORECASE)
            for name in names
        )

    if _has_cte('itembranchmaster', 'item_branch_master') and not _has_join('itembranchmaster', 'item_branch_master'):
        issues.append(AuditIssue(
            level='error',
            code='MISSING_PRODUCT_BRIDGE_JOIN',
            message='itembranchmaster CTE is generated but not joined into the final product path.',
            suggestion='Join facttable_with_expenses."Item-Branch Key" to itembranchmaster."Item-Branch Key".',
        ))

    if _has_cte('itemmaster', 'item_master'):
        if not _has_cte('itembranchmaster', 'item_branch_master') or not _has_join('itembranchmaster', 'item_branch_master'):
            issues.append(AuditIssue(
                level='error',
                code='MISSING_PRODUCT_BRIDGE_JOIN',
                message='itemmaster is present without the required itembranchmaster bridge join.',
                suggestion='Route product joins through itembranchmaster before joining itemmaster by Short Name.',
            ))
        if not _has_join('itemmaster', 'item_master'):
            issues.append(AuditIssue(
                level='error',
                code='MISSING_PRODUCT_MASTER_JOIN',
                message='itemmaster CTE is generated but not joined into the final product path.',
                suggestion='Join itembranchmaster."Short Name" to itemmaster."Short Name".',
            ))

    for cte_name, label, left_key, right_key in (
        ('productgroupmaster', 'productgroupmaster', 'Product Group', 'Product Group'),
        ('product_group_master', 'product_group_master', 'Product Group', 'Product Group'),
        ('productsubgroupmaster', 'productsubgroupmaster', 'Product Sub Group', 'Product Sub Group'),
        ('product_subgroup_master', 'product_subgroup_master', 'Product Sub Group', 'Product Sub Group'),
        ('producttypemaster', 'producttypemaster', 'Product Type', 'Product Type'),
        ('product_type_master', 'product_type_master', 'Product Type', 'Product Type'),
    ):
        if cte_name in cte_names and not _has_join(cte_name):
            issues.append(AuditIssue(
                level='error',
                code='MISSING_PRODUCT_MASTER_JOIN',
                message=f'{label} CTE is generated but not joined.',
                suggestion=f'Join itemmaster."{left_key}" to {label}."{right_key}".',
            ))

    if 'arsummary_1' in cte_names and not _has_join('arsummary_1'):
        issues.append(AuditIssue(
            level='error',
            code='MISSING_ARSUMMARY_1_JOIN',
            message='arsummary_1 CTE is generated but not joined.',
            suggestion='Join arsummary_1 ar1 on the validated CustKeyAR path, e.g. cmap.CustKeyAR = ar1.CustKeyAR.',
        ))

    if _has_cte('accountmaster', 'account_master') and not _has_join('accountmaster', 'account_master'):
        issues.append(AuditIssue(
            level='error',
            code='UNUSED_ACCOUNT_MASTER',
            message='accountmaster CTE is generated but never joined.',
            suggestion='Join expenses/account rows on Account or remove the CTE.',
        ))

    if _has_cte('accountgroupmaster', 'account_group_master') and not _has_join('accountgroupmaster', 'account_group_master'):
        issues.append(AuditIssue(
            level='error',
            code='UNUSED_ACCOUNT_GROUP_MASTER',
            message='accountgroupmaster CTE is generated but never joined.',
            suggestion='Join accountmaster.AccountGroup to accountgroupmaster.AccountGroup or remove the CTE.',
        ))

    fact_expenses_body = _cte_body(sql, 'facttable_with_expenses')
    if fact_expenses_body and re.search(r'\bUNION\s+ALL\b', fact_expenses_body, re.IGNORECASE):
        branch_columns = [
            _select_columns_from_branch(branch)
            for branch in re.split(r'\bUNION\s+ALL\b', fact_expenses_body, flags=re.IGNORECASE)
        ]
        if branch_columns:
            first = branch_columns[0]
            for idx, columns in enumerate(branch_columns, start=1):
                if 'account' not in columns:
                    issues.append(AuditIssue(
                        level='error',
                        code='FACT_EXPENSES_ACCOUNT_MISSING',
                        message=f'facttable_with_expenses UNION branch {idx} does not emit Account.',
                        suggestion='Emit CAST(NULL AS VARCHAR) AS "Account" in facttable branch and "Account" in expenses branch.',
                    ))
                if columns != first:
                    issues.append(AuditIssue(
                        level='error',
                        code='UNION_COLUMN_ORDER_MISMATCH',
                        message='facttable_with_expenses UNION branches do not emit identical columns in identical order.',
                        suggestion='Render the same ordered column list in every UNION branch.',
                    ))

    if re.search(r'\bJOIN\s+(?:item_master|itemmaster)\b', sql or '', re.IGNORECASE):
        if not re.search(r'\bJOIN\s+(?:item_branch_master|itembranchmaster)\b', sql or '', re.IGNORECASE):
            issues.append(AuditIssue(
                level='error',
                code='WRONG_PRODUCT_JOIN_PATH',
                message=(
                    'ItemMaster is joined without ItemBranchMaster. FactTable must join '
                    'ItemBranchMaster by Item-Branch Key, then ItemBranchMaster joins '
                    'ItemMaster by Short Name.'
                ),
                suggestion=(
                    'Use: FactTable."Item-Branch Key" = ItemBranchMaster."Item-Branch Key" '
                    'and ItemBranchMaster."Short Name" = ItemMaster."Short Name".'
                ),
            ))
        if re.search(
            r'ON\s+[^;\n)]*(?:item[\w]*|im)\."?short name"?\s*=\s*[^;\n)]*"?(?:item-branch key)"?',
            sql or '',
            re.IGNORECASE,
        ) or re.search(
            r'ON\s+[^;\n)]*"?(?:item-branch key)"?\s*=\s*[^;\n)]*(?:item[\w]*|im)\."?short name"?',
            sql or '',
            re.IGNORECASE,
        ):
            issues.append(AuditIssue(
                level='error',
                code='WRONG_PRODUCT_JOIN_PATH',
                message='Fact/Item-Branch Key is being joined directly to ItemMaster.Short Name.',
                suggestion='Route through ItemBranchMaster before ItemMaster.',
            ))

    for join_match in re.finditer(
        r'\bJOIN\s+(?:expenses|int_expenses|expenses_aggregated|int_expenses_aggregated)\b(?:\s+AS)?\s+([A-Za-z_][A-Za-z0-9_]*)?\s+ON\s+(.*?)(?=\b(?:LEFT|RIGHT|FULL|INNER|CROSS)?\s*JOIN\b|\bWHERE\b|\bGROUP\s+BY\b|\bORDER\s+BY\b|\)|$)',
        sql or '',
        flags=re.IGNORECASE | re.DOTALL,
    ):
        condition = join_match.group(2).lower()
        if 'monthlyregionkey' in condition and 'account' not in condition:
            issues.append(AuditIssue(
                level='error',
                code='EXPENSES_GRAIN_JOIN_INCOMPLETE',
                message='Expenses is joined on MonthlyRegionKey without Account.',
                suggestion='Join Expenses at full grain: MonthlyRegionKey + Account.',
            ))

    if re.search(r'\barsummary[_-]?1?\b', sql or '', re.IGNORECASE):
        for join_match in re.finditer(
            r'\bJOIN\s+[A-Za-z_][A-Za-z0-9_]*(?:\s+AS)?\s+[A-Za-z_][A-Za-z0-9_]*\s+ON\s+(.*?)(?=\b(?:LEFT|RIGHT|FULL|INNER|CROSS)?\s*JOIN\b|\bWHERE\b|\bGROUP\s+BY\b|\bORDER\s+BY\b|\)|$)',
            sql or '',
            flags=re.IGNORECASE | re.DOTALL,
        ):
            condition = join_match.group(1).lower()
            if 'arsummary' in condition and 'custkeyar' in condition and 'arage' not in condition:
                issues.append(AuditIssue(
                    level='warning',
                    code='ARSUMMARY_GRAIN_RISK',
                    message='ARSummary appears joined at CustKeyAR only; ARAge buckets may multiply rows.',
                    suggestion='Pivot or aggregate ARSummary to one row per CustKeyAR before joining.',
                ))

    # 7. Unused generated CTEs should be removed or intentionally joined.
    for cte in sorted(cte_names):
        if cte in {'final', 'final_model', 'final_mart'}:
            continue
        references = len(re.findall(rf'\b{re.escape(cte)}\b', sql_lower))
        if references <= 1 and cte in {'accountmaster', 'accountgroupmaster', 'account_master', 'account_group_master'}:
            issues.append(AuditIssue(
                level='error',
                code='UNUSED_CTE',
                message=f'CTE "{cte}" is defined but never used.',
                suggestion='Join it intentionally on a validated key or remove it from the generated SQL.',
            ))

    # 8. Null padding check: union ALL branches should not have significantly
    #    different column counts (heuristic based on comma count in SELECT lists)
    union_sections = re.split(r'\bUNION\s+ALL\b', sql, flags=re.IGNORECASE)
    if len(union_sections) > 1:
        # Rough column count per branch via comma count in SELECT ... FROM
        col_counts = []
        for section in union_sections:
            sel_match = re.search(r'\bSELECT\b(.*?)\bFROM\b', section, re.IGNORECASE | re.DOTALL)
            if sel_match:
                body = sel_match.group(1)
                # Count at depth 0 only (ignore nested parens)
                depth = 0
                commas = 0
                for ch in body:
                    if ch == '(':
                        depth += 1
                    elif ch == ')':
                        depth -= 1
                    elif ch == ',' and depth == 0:
                        commas += 1
                col_counts.append(commas + 1)

        if col_counts and (max(col_counts) - min(col_counts)) > 0:
            issues.append(AuditIssue(
                level='warning',
                code='IR_UNION_BRANCH_COUNT_WARNING',
                message=(
                    f"IR observed differing union projection counts "
                    f"({col_counts}). This will fail at runtime."
                ),
                suggestion=(
                    "Pad all branches to the same column count using "
                    "CAST(NULL AS <type>) AS \"col\" for missing columns."
                ),
            ))

    return issues


# ─── IR → prompt injection ────────────────────────────────────────────────────

def format_ir_for_prompt(ir: MigrationIR, max_chars: int = 3000) -> str:
    """
    Return a compact, LLM-readable summary of the IR for injection into prompts.
    Stays within max_chars to avoid blowing the context budget.
    """
    lines = ["## Data Model IR (derived from Qlik script — treat as ground truth)"]

    # Tables
    lines.append("\n### Tables")
    for tname, tentry in list(ir.tables.items())[:12]:
        if tentry.is_concatenate:
            continue
        lines.append(
            f"- **{tname}** | grain: {tentry.grain} "
            f"| keys: {', '.join(tentry.key_fields[:4]) or 'none detected'}"
        )
        if tentry.source:
            lines.append(f"  source map: raw `{tentry.source}` → cte `{tentry.cte_name}`")
        if tentry.is_island_table:
            lines.append(f"  ⚠ ISLAND TABLE — do not join directly to fact table")
        if tentry.date_fields:
            for fn, de in tentry.date_fields.items():
                lines.append(
                    f"  📅 {de.field_name}: {de.storage_type} "
                    f"→ conversion: `{de.conversion_sql or 'AMBIGUOUS'}`"
                )
        if tentry.intentional_typos:
            for orig, alias in tentry.intentional_typos.items():
                lines.append(f"  ⚠ INTENTIONAL TYPO: '{orig}' aliased as '{alias}' — do not correct")

    # Joins
    if ir.joins:
        lines.append("\n### Join Map")
        for j in ir.joins:
            safety = "✓ safe" if j.safe else f"✗ UNSAFE — {j.required_action or 'no action'}"
            lines.append(
                f"- {j.from_table}.{j.left_key} → {j.to_table}.{j.right_key} "
                f"[{j.cardinality}] [{safety}]"
            )

    # UNION ALL
    if ir.unions:
        lines.append("\n### UNION ALL (CONCATENATE blocks)")
        for u in ir.unions:
            lines.append(f"- Target: **{u.target_table}** ({len(u.appended_branches)} appended branch(es))")
            for branch in u.appended_branches:
                if branch.null_padded_fields:
                    lines.append(
                        f"  Branch '{branch.source_table}' must NULL-pad: "
                        + ", ".join(f'"{c}"' for c in branch.null_padded_fields[:8])
                    )

    # Ambiguities
    if ir.ambiguities:
        lines.append("\n### ⚠ Unresolved Ambiguities (ask before generating)")
        for i, amb in enumerate(ir.ambiguities, 1):
            lines.append(f"{i}. {amb}")

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n... [IR truncated for prompt budget]"
    return text
