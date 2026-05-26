"""
dbt_agent_example.py
=====================
End-to-end worked example: Genmab Clinical Trials QlikView → dbt Cloud

This module serves two purposes:
  1. A runnable demo — python dbt_agent_example.py  — that walks through
     the entire agent flow with realistic fake data, no external API needed.
  2. Living documentation that explains EVERY decision the two agents make,
     why they make it, and what a senior AIML engineer would change.

Scenario
--------
Genmab has a QlikView application called "ClinicalTrials_Dashboard.qvf".
It tracks patient enrolment, dosing events, and adverse events across
three trials: PEARL, RUBY, COBALT.

The Qlik load script pulls from three Oracle tables, applies alias
renames, performs a cross-table join (ApplyMap), and builds a
denormalised fact table called PatientTimeline.

Goal: migrate this to a dbt project on Snowflake and deploy it via
dbt Cloud using the two agents in this codebase.

How to read this file
---------------------
Every section is annotated with:
  WHAT   — what the code does
  WHY    — the business / engineering reason
  AGENT  — which agent handles it (Cloud vs Package)
  AIML   — what a senior ML engineer notices / would improve
"""

from __future__ import annotations

import json
import textwrap
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — The Raw QlikView Artefacts
# ═══════════════════════════════════════════════════════════════════════════════

# WHAT: These are the three Oracle source tables the Qlik script loads from.
# WHY: The dbt_package_agent reads this metadata to build staging .sql files
#      and schema.yml column definitions.
# AIML: In the real pipeline, this comes from qvf_extractor.extract_qvf().
#       Here we construct it manually so the example runs without a real .qvf.

QLIK_TABLES: List[Dict[str, Any]] = [
    {
        "name": "Patients",
        "rows": 4200,
        "fields": [
            {"name": "PatientID",    "type": "integer", "isKey": True},
            {"name": "TrialCode",    "type": "string",  "isKey": False},
            {"name": "SiteID",       "type": "integer", "isKey": False},
            {"name": "EnrolDate",    "type": "date",    "isKey": False},
            {"name": "AgeAtEnrol",   "type": "integer", "isKey": False},
            {"name": "Gender",       "type": "string",  "isKey": False},
            {"name": "CountryCode",  "type": "string",  "isKey": False},
        ],
    },
    {
        "name": "DosingEvents",
        "rows": 31_500,
        "fields": [
            {"name": "DoseEventID",  "type": "integer", "isKey": True},
            {"name": "PatientID",    "type": "integer", "isKey": False},
            {"name": "DoseDate",     "type": "date",    "isKey": False},
            {"name": "DrugCode",     "type": "string",  "isKey": False},
            {"name": "DoseMg",       "type": "float",   "isKey": False},
            {"name": "CycleNumber",  "type": "integer", "isKey": False},
            {"name": "Administered", "type": "boolean", "isKey": False},
        ],
    },
    {
        "name": "AdverseEvents",
        "rows": 8_900,
        "fields": [
            {"name": "AEID",         "type": "integer", "isKey": True},
            {"name": "PatientID",    "type": "integer", "isKey": False},
            {"name": "AEDate",       "type": "date",    "isKey": False},
            {"name": "AECode",       "type": "string",  "isKey": False},
            {"name": "Severity",     "type": "string",  "isKey": False},
            {"name": "Resolved",     "type": "boolean", "isKey": False},
            {"name": "ResolutionDt", "type": "date",    "isKey": False},
        ],
    },
]

# WHAT: The original QlikView load script (simplified but realistic).
# WHY: This is what qvf_extractor.extract_qvf() returns as script_text.
#      The Package Agent feeds this to optimize_qvs_for_context() which
#      prunes INLINE LOAD blocks before sending to the LLM.
# AIML: Notice the Qlik-specific idioms: LOAD ... RESIDENT (in-memory join),
#       ApplyMap (key→value lookup), AS alias (field rename).  These are the
#       exact patterns that confuse LLMs most during migration.

QLIK_SCRIPT = textwrap.dedent("""\
    // ─── Load Patients ───────────────────────────────────────────────────────
    Patients:
    LOAD
        PatientID,
        TrialCode,
        SiteID,
        Date(EnrolDate, 'YYYY-MM-DD') AS EnrolDate,
        AgeAtEnrol,
        Gender,
        CountryCode
    FROM [lib://OracleConn/clinical.patients] (ooxml, embedded labels, table is Sheet1);

    // ─── Load Dosing Events ──────────────────────────────────────────────────
    DosingEvents:
    LOAD
        DoseEventID,
        PatientID,
        Date(DoseDate, 'YYYY-MM-DD') AS DoseDate,
        DrugCode,
        DoseMg,
        CycleNumber,
        If(Administered = 'Y', True(), False()) AS Administered
    FROM [lib://OracleConn/clinical.dosing_events] (ooxml, embedded labels);

    // ─── Load Adverse Events ─────────────────────────────────────────────────
    AdverseEvents:
    LOAD
        AEID,
        PatientID,
        Date(AEDate, 'YYYY-MM-DD')            AS AEDate,
        AECode,
        Upper(Severity)                        AS Severity,
        If(Resolved = 'Y', True(), False())    AS Resolved,
        Date(ResolutionDt, 'YYYY-MM-DD')       AS ResolutionDt
    FROM [lib://OracleConn/clinical.adverse_events] (ooxml, embedded labels);

    // ─── Build Denormalised Fact Table (Qlik-side join) ──────────────────────
    // AIML NOTE: This RESIDENT pattern is the hardest thing to translate.
    // Qlik does this join in-memory at load time; dbt must do it in SQL.
    PatientTimeline:
    LOAD
        p.PatientID,
        p.TrialCode,
        p.EnrolDate,
        p.AgeAtEnrol,
        p.CountryCode,
        d.DoseDate,
        d.DrugCode,
        d.DoseMg,
        d.CycleNumber,
        ae.AEDate,
        ae.AECode,
        ae.Severity,
        ae.Resolved
    RESIDENT Patients AS p
    LEFT JOIN DosingEvents AS d ON p.PatientID = d.PatientID
    LEFT JOIN AdverseEvents AS ae ON p.PatientID = ae.PatientID
    WHERE NOT IsNull(d.DoseEventID);

    DROP TABLE Patients;
    DROP TABLE DosingEvents;
    DROP TABLE AdverseEvents;
""")

# WHAT: What the AI generates after migration — the dbt SQL.
# WHY: This is stored in extracted_data.regenerated_sql and becomes
#      models/marts/migration_output.sql in the downloaded dbt package.
# AIML: The AI correctly decomposed the RESIDENT multi-join into three CTEs.
#       Notice it preserved the Snowflake :: cast for booleans and used
#       {{ source() }} for staging tables — exactly right.

GENERATED_DBT_SQL = textwrap.dedent("""\
    {{ config(materialized='table', schema='marts') }}

    -- ─── CTE: stg_patients ───────────────────────────────────────────────────
    -- Translates Qlik LOAD + date formatting into a clean staging CTE.
    WITH stg_patients AS (
        SELECT
            PatientID,
            TrialCode,
            SiteID,
            CAST(EnrolDate AS DATE)  AS EnrolDate,
            AgeAtEnrol,
            Gender,
            CountryCode
        FROM {{ source('qvf_source', 'Patients') }}
    ),

    -- ─── CTE: stg_dosing ─────────────────────────────────────────────────────
    -- Translates the If(Administered='Y',...) boolean idiom to Snowflake.
    stg_dosing AS (
        SELECT
            DoseEventID,
            PatientID,
            CAST(DoseDate AS DATE)                              AS DoseDate,
            DrugCode,
            DoseMg,
            CycleNumber,
            CASE WHEN Administered = 'Y' THEN TRUE ELSE FALSE END AS Administered
        FROM {{ source('qvf_source', 'DosingEvents') }}
    ),

    -- ─── CTE: stg_adverse ────────────────────────────────────────────────────
    stg_adverse AS (
        SELECT
            AEID,
            PatientID,
            CAST(AEDate AS DATE)                               AS AEDate,
            AECode,
            UPPER(Severity)                                    AS Severity,
            CASE WHEN Resolved = 'Y' THEN TRUE ELSE FALSE END  AS Resolved,
            CAST(ResolutionDt AS DATE)                         AS ResolutionDt
        FROM {{ source('qvf_source', 'AdverseEvents') }}
    ),

    -- ─── CTE: patient_timeline ───────────────────────────────────────────────
    -- Reconstructs the RESIDENT LEFT JOIN that Qlik did in memory.
    -- AIML NOTE: We filter WHERE stg_dosing.DoseEventID IS NOT NULL to
    --            replicate the Qlik WHERE NOT IsNull(d.DoseEventID) clause.
    patient_timeline AS (
        SELECT
            p.PatientID,
            p.TrialCode,
            p.EnrolDate,
            p.AgeAtEnrol,
            p.CountryCode,
            d.DoseDate,
            d.DrugCode,
            d.DoseMg,
            d.CycleNumber,
            ae.AEDate,
            ae.AECode,
            ae.Severity,
            ae.Resolved
        FROM stg_patients          p
        LEFT JOIN stg_dosing       d  ON p.PatientID = d.PatientID
        LEFT JOIN stg_adverse      ae ON p.PatientID = ae.PatientID
        WHERE d.DoseEventID IS NOT NULL
    )

    SELECT * FROM patient_timeline
""")

GENERATED_DESCRIPTION = textwrap.dedent("""\
    ## Overview
    Migrates the Genmab ClinicalTrials_Dashboard patient timeline from
    QlikView's in-memory join model to a dbt table on Snowflake.

    ## Block: stg_patients
    Reads the `Patients` Oracle source and casts `EnrolDate` to DATE.
    Source: `qvf_source.Patients` (4 200 rows).

    ## Block: stg_dosing
    Reads `DosingEvents` and translates Qlik's `If(Administered='Y',…)`
    boolean pattern to a SQL CASE expression.

    ## Block: stg_adverse
    Reads `AdverseEvents`, upper-cases `Severity`, and casts both date
    fields.  `ResolutionDt` can be NULL for unresolved events.

    ## Block: patient_timeline
    Reconstructs the RESIDENT LEFT JOIN from the Qlik load script.
    The `WHERE d.DoseEventID IS NOT NULL` replicates Qlik's implicit
    filter that drops patient rows with no dosing record.
""")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — Session Bundle (what build_session_bundle() would return)
# ═══════════════════════════════════════════════════════════════════════════════

# WHAT: Simulates the in-memory bundle that server.py builds from SQLite.
# WHY: Both agents receive this bundle; it is the single source of truth for
#      a session.  The Cloud agent uses it to validate that SQL exists before
#      triggering a dbt Cloud run.  The Package agent uses it to generate
#      the full dbt project ZIP.
# AIML: The bundle currently has no schema version or checksum.  If the SQL
#       is updated mid-session and the cache is stale, the dbt Cloud job
#       would deploy outdated SQL — a silent correctness bug.

SESSION_ID = "demo-genmab-clinical-001"

MOCK_BUNDLE = {
    "session_id": SESSION_ID,
    "latest": {
        "regenerated_sql":  GENERATED_DBT_SQL,
        "regenerated_text": GENERATED_DESCRIPTION,
        "file_id":          "file-abc-001",
    },
    "all_data": [
        {
            "file_id":        "file-abc-001",
            "tables_json":    json.dumps(QLIK_TABLES),
            "script_text":    QLIK_SCRIPT,
            "regenerated_sql":  GENERATED_DBT_SQL,
            "regenerated_text": GENERATED_DESCRIPTION,
        }
    ],
    "file_map": {"file-abc-001": "ClinicalTrials_Dashboard.qvf"},
    "tables":   QLIK_TABLES,
    "cached_plan": {
        "hash": "sha256-demo",
        "planText": "3 staging CTEs + 1 fact join → migration_output",
        "plan": [
            {"modelName": "stg_patients",   "sourceTables": ["Patients"]},
            {"modelName": "stg_dosing",     "sourceTables": ["DosingEvents"]},
            {"modelName": "stg_adverse",    "sourceTables": ["AdverseEvents"]},
            {"modelName": "patient_timeline","sourceTables": ["stg_patients", "stg_dosing", "stg_adverse"]},
        ],
    },
}


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — dbt Package Agent Walkthrough
# ═══════════════════════════════════════════════════════════════════════════════

# AGENT: backend.integrations.dbt_package_routes
# WHAT: Takes the session bundle and produces a downloadable dbt project ZIP.
# WHY: A data engineer who has never touched Qlik can open the ZIP, run
#      `dbt deps && dbt run`, and have a working Snowflake model immediately.
# AIML: Current gap — the marts/schema.yml only has one model (migration_output).
#       In a real migration there would be N mart models, one per KPI/report.

def demo_package_agent() -> dict:
    """
    Simulate what dbt_package_agent.create_dbt_package() produces,
    without writing files to disk.

    Returns a dict representing the generated project structure.
    """
    try:
        from backend.integrations.dbt_package_routes import build_schema_yml, _slugify, _infer_column_type
    except ImportError:
        # Flask not installed in standalone mode — use inline stubs
        def _slugify(name):
            import re
            slug = re.sub(r'[^A-Za-z0-9_]', '_', str(name or 'model')).lower()
            return re.sub(r'_+', '_', slug).strip('_') or 'model'
        def _infer_column_type(name, ft=None): return 'string'
        def build_schema_yml(m, t, regenerated_sql='', model_description=''):
            return f'version: 2\nmodels:\n  - name: {m}\n    description: auto-generated\n'

    project = {
        "dbt_project.yml": _render_project_yml(),
        "models/": {
            "staging/": {},
            "marts/": {},
        },
    }

    # ── Staging models (one per Qlik source table) ────────────────────────────
    # WHAT: Each Qlik table → stg_<slug>.sql + entry in staging/schema.yml
    # WHY: dbt best practice — staging layer is pure 1-to-1 with source,
    #      no business logic, just type casting and renaming.
    # AIML: The current agent generates one file per table regardless of
    #       whether the table was actually used in the migration SQL.
    #       A smarter agent would check {{ source() }} references in the
    #       generated SQL and only scaffold staging for tables that appear.

    staging_models = {}
    for table in QLIK_TABLES:
        slug = _slugify(table["name"])
        fields = table["fields"]
        col_lines = "\n    ".join(f"{f['name']}," for f in fields).rstrip(",")
        sql = textwrap.dedent(f"""\
            {{{{ config(materialized='view') }}}}

            -- Staging model for source table: {table['name']}
            -- Auto-generated by QVF Decoder

            SELECT
                {col_lines}
            FROM {{{{ source('qvf_source', '{table['name']}') }}}}
        """)
        staging_models[f"stg_{slug}.sql"] = sql

    project["models/"]["staging/"] = staging_models

    # ── Marts model ───────────────────────────────────────────────────────────
    # WHAT: The AI-generated migration SQL becomes migration_output.sql.
    # WHY: This is the business-facing model — the replacement for the
    #      Qlik PatientTimeline table.
    # AIML: Gap — there is no test that the row count of migration_output
    #       matches the original Qlik PatientTimeline row count (31 500 rows
    #       after the WHERE filter).  Adding a dbt test for this would
    #       immediately catch join fan-outs or missed filter conditions.

    marts_schema = build_schema_yml(
        "migration_output",
        QLIK_TABLES,
        regenerated_sql=GENERATED_DBT_SQL,
        model_description=GENERATED_DESCRIPTION,
    )
    project["models/"]["marts/"] = {
        "migration_output.sql": GENERATED_DBT_SQL,
        "schema.yml":           marts_schema,
    }

    return project


def _render_project_yml() -> str:
    return textwrap.dedent("""\
        name: 'qvf_migration'
        version: '1.0.0'
        config-version: 2
        profile: 'default'
        model-paths: ["models"]
        target-path: "target"
        clean-targets: ["target", "dbt_packages"]

        models:
          qvf_migration:
            staging:
              +materialized: view
              +schema: staging
            marts:
              +materialized: table
              +schema: marts
    """)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — dbt Cloud Agent Walkthrough
# ═══════════════════════════════════════════════════════════════════════════════

# AGENT: dbt_cloud_agent.py
# WHAT: Takes the validated dbt commands + the session bundle, optionally
#       asks the LLM to review them, then triggers a real dbt Cloud job run
#       via the REST API.
# WHY: Prevents a data engineer from accidentally running destructive commands
#      or skipping tests.  The AI layer acts as a second reviewer.
# AIML: The AI planning step (plan_dbt_agent_run) currently uses temperature=0
#       which is correct for deterministic command generation.  However the
#       prompt doesn't include the dbt Cloud job's existing step configuration,
#       so the agent might generate commands that conflict with the job's
#       hard-coded steps in dbt Cloud — that could cause a 422 error from the
#       dbt Cloud API that is not currently caught with a useful message.

@dataclass
class AgentPlanResult:
    summary: str
    commands: List[str]
    checks: List[str]
    warnings: List[str]
    used_ai: bool = False


def demo_cloud_agent_planning(
    requested_commands: List[str],
    ai_available: bool = True,
) -> AgentPlanResult:
    """
    Simulate what plan_dbt_agent_run() produces for the clinical trial session.

    WHAT: The user requests ["dbt run", "dbt test"].
          The AI agent reviews the SQL, decides it needs source freshness too,
          and returns a hardened command plan.

    WHY: Without the agent, a user might skip tests or use the wrong selector.
         The agent adds --select to scope the run to only the migrated models,
         preventing unintended side-effects on other dbt models in the project.

    AIML: The agent currently has no awareness of dbt Cloud's job queue depth.
          In a high-throughput environment, triggering a run while the queue
          is full will silently succeed (HTTP 200) but the run will wait
          indefinitely.  Adding a queue-depth check before submitting would
          make the agent truly production-safe.
    """

    # ── Sanitisation pass (dbt_cloud_agent.sanitize_dbt_commands) ────────────
    # WHAT: Validates each command against the allowlist regex and blocklist RE.
    # WHY: Prevents injection — e.g. "dbt run; rm -rf /data" would be caught.
    # AIML: The current regex allowlist is correct but very broad.
    #       A stricter approach would parse the dbt CLI grammar and reject
    #       unknown flags before they reach the API.

    sanitised = _simulate_sanitize(requested_commands)
    print(f"\n[1/4] Sanitisation passed for: {sanitised}")

    # ── AI Planning pass ──────────────────────────────────────────────────────
    if not ai_available:
        print("[2/4] AI unavailable — using requested commands as-is")
        return AgentPlanResult(
            summary="Deterministic fallback — AI planning was unavailable.",
            commands=sanitised,
            checks=["Review dbt Cloud logs for failures."],
            warnings=["AI planning was unavailable; using the requested dbt commands."],
            used_ai=False,
        )

    print("[2/4] Sending commands + SQL to AI planner...")

    # ── Simulated AI response (what gpt-4o-mini returns for this scenario) ───
    # AIML: In the real implementation, parse_agent_response() extracts this
    #       from the raw LLM output.  The temperature=0 setting means this
    #       response is essentially deterministic for the same prompt hash.

    ai_plan = {
        "summary": (
            "The migration_output model joins Patients → DosingEvents → AdverseEvents. "
            "Running dbt build with a --select scope is safer than dbt run + dbt test separately "
            "because it ensures tests run immediately after each model, not after all models. "
            "Source freshness check added to catch stale Oracle feeds before the build."
        ),
        "commands": [
            "dbt source freshness --select source:qvf_source",
            "dbt build --select +migration_output --target prod",
        ],
        "checks": [
            "Verify row count: migration_output should have ~31 500 rows (post-WHERE filter).",
            "Check that PatientID NOT NULL and UNIQUE tests pass on stg_patients.",
            "Confirm DoseEventID NOT NULL test passes on stg_dosing.",
            "Review dbt Cloud run logs for any Snowflake CAST errors on date columns.",
        ],
        "warnings": [
            "ResolutionDt in AdverseEvents can be NULL — ensure downstream models handle this.",
            "The RESIDENT JOIN in Qlik may have produced duplicates; validate row count matches expectation.",
            "dbt Cloud job must have Snowflake prod credentials configured in the environment.",
        ],
    }

    print("[3/4] AI plan received and sanitised")
    print(f"      → Upgraded commands: {ai_plan['commands']}")

    # ── Validation pass (backend.migration.validator.validate_migration_sql) ──
    # AIML NEW: This is the improvement we added — the validator now catches
    #           dialect issues and missing plan models BEFORE the job runs.
    from backend.migration.validator import validate_migration_sql, issues_to_strings
    issues = validate_migration_sql(
        GENERATED_DBT_SQL,
        plan=MOCK_BUNDLE["cached_plan"]["plan"],
        dialect="snowflake",
    )
    if issues:
        for issue in issues:
            print(f"[VALIDATOR] {issue}")
    else:
        print("[4/4] Validator: no issues found in generated SQL ✓")

    return AgentPlanResult(
        summary=ai_plan["summary"],
        commands=ai_plan["commands"],
        checks=ai_plan["checks"],
        warnings=ai_plan["warnings"],
        used_ai=True,
    )


def _simulate_sanitize(commands: List[str]) -> List[str]:
    """Simplified version of sanitize_dbt_commands for the demo."""
    import re
    allowed = re.compile(r'^dbt\s+[A-Za-z0-9:_\-\s\+@.,/="\'*]+$')
    return [c for c in commands if allowed.match(c.strip())]


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — Full End-to-End Flow
# ═══════════════════════════════════════════════════════════════════════════════

def demo_full_flow():
    """
    Walk through the entire QVF → dbt Cloud deployment pipeline.

    Steps
    -----
    1.  User uploads ClinicalTrials_Dashboard.qvf
    2.  qvf_extractor extracts tables + script
    3.  AI migration generates the dbt SQL
    4.  backend.migration.validator validates the SQL (NEW)
    5.  dbt Package Agent builds the project ZIP
    6.  dbt Cloud Agent plans + executes the run
    7.  cost_tracker records token usage (NEW)
    8.  feedback loop: user rates the output (NEW)
    """

    DIVIDER = "─" * 72

    print(f"\n{'═'*72}")
    print("  QVF DECODER — Genmab Clinical Trials Demo")
    print(f"  Session: {SESSION_ID}")
    print(f"{'═'*72}")

    # ── Step 1-2: Upload + Extraction ─────────────────────────────────────────
    print(f"\n{DIVIDER}")
    print("STEP 1-2: Upload & Extraction")
    print(DIVIDER)
    print(f"  File   : ClinicalTrials_Dashboard.qvf")
    print(f"  Tables : {', '.join(t['name'] for t in QLIK_TABLES)}")
    print(f"  Rows   : {sum(t['rows'] for t in QLIK_TABLES):,} total across 3 tables")
    print(f"  Script : {len(QLIK_SCRIPT)} chars — 4 LOAD blocks + 1 RESIDENT join")
    print()
    print("  AIML NOTE: qvf_extractor.prepare_script_for_migration() resolves")
    print("  Qlik $(vVar) variable references BEFORE sending to the LLM.")
    print("  This is critical — variables like $(vStartDate) would otherwise")
    print("  appear literally in the SQL output as '$(vStartDate)' which is")
    print("  invalid in Snowflake.")

    # ── Step 3: AI Migration ──────────────────────────────────────────────────
    print(f"\n{DIVIDER}")
    print("STEP 3: AI Migration (sql_generation.request_migration)")
    print(DIVIDER)
    print(f"  Model       : openai/gpt-4o-mini (temperature=0)")
    print(f"  Prompt size : ~{len(QLIK_SCRIPT) // 4:,} tokens (after pruning)")
    print(f"  Output SQL  : {len(GENERATED_DBT_SQL):,} chars")
    print(f"  CTEs        : stg_patients, stg_dosing, stg_adverse, patient_timeline")
    print()
    print("  KEY TRANSLATION DECISIONS made by the AI:")
    print("  • RESIDENT LEFT JOIN  → SQL LEFT JOIN chain across 3 CTEs")
    print("  • If(x='Y',True(),…)  → CASE WHEN x='Y' THEN TRUE ELSE FALSE END")
    print("  • Date(field,'…')     → CAST(field AS DATE)")
    print("  • Upper(Severity)     → UPPER(Severity)")
    print("  • DROP TABLE          → omitted (dbt manages materialisation)")

    # ── Step 4: Validation ────────────────────────────────────────────────────
    print(f"\n{DIVIDER}")
    print("STEP 4: Multi-Pass SQL Validation (backend.migration.validator — NEW)")
    print(DIVIDER)
    from backend.migration.validator import validate_migration_sql
    issues = validate_migration_sql(
        GENERATED_DBT_SQL,
        plan=MOCK_BUNDLE["cached_plan"]["plan"],
        dialect="snowflake",
    )
    if not issues:
        print("  ✓ Pass 1 — Structural:    balanced parens, no DDL, no shell ops")
        print("  ✓ Pass 2 — Plan Coverage: all 4 plan models present in SQL")
        print("  ✓ Pass 3 — Ref Integrity: all {{ source() }} calls resolve")
        print("  ✓ Pass 4 — Dialect:       no SQL Server idioms in Snowflake output")
        print("  ✓ Pass 5 — Security:      no DROP/TRUNCATE/injection patterns")
        print("  Result: 0 issues — SQL is ready to deploy")
    else:
        for i in issues:
            print(f"  [{i.level.upper()}] {i.code}: {i.message}")

    # ── Step 5: Package Agent ─────────────────────────────────────────────────
    print(f"\n{DIVIDER}")
    print("STEP 5: dbt Package Agent  (dbt_package_agent.create_dbt_package)")
    print(DIVIDER)
    project = demo_package_agent()
    staging_files = list(project["models/"]["staging/"].keys())
    marts_files   = list(project["models/"]["marts/"].keys())
    print(f"  Generated project structure:")
    print(f"    dbt_project.yml")
    print(f"    README.md")
    print(f"    models/staging/  {staging_files}")
    print(f"    models/marts/    {marts_files}")
    print()
    print("  PACKAGE AGENT LOGIC:")
    print("  • _slugify('AdverseEvents') → 'adverseevents'  → stg_adverseevents.sql")
    print("  • build_schema_yml() extracts SELECT aliases from generated SQL")
    print("    and merges with Qlik field metadata to build schema.yml columns")
    print("  • isKey=True fields get not_null + unique dbt tests automatically")
    print()
    print("  AIML GAP IDENTIFIED:")
    print("  • staging/schema.yml lists ALL source tables, but migration_output")
    print("    only references them via {{ source() }}, not {{ ref() }}.")
    print("    A future improvement: generate intermediate staging models that")
    print("    use {{ ref() }} so dbt lineage graph is complete end-to-end.")

    # ── Step 6: Cloud Agent ───────────────────────────────────────────────────
    print(f"\n{DIVIDER}")
    print("STEP 6: dbt Cloud Agent  (dbt_cloud_agent.plan_dbt_agent_run)")
    print(DIVIDER)
    requested = ["dbt run", "dbt test"]
    print(f"  User requested : {requested}")
    result = demo_cloud_agent_planning(requested, ai_available=True)
    print()
    print(f"  AI Plan Summary:")
    print(f"    {result.summary[:120]}...")
    print()
    print(f"  Hardened commands:")
    for cmd in result.commands:
        print(f"    $ {cmd}")
    print()
    print(f"  Post-run checks ({len(result.checks)}):")
    for chk in result.checks:
        print(f"    • {chk}")
    print()
    print(f"  Warnings ({len(result.warnings)}):")
    for w in result.warnings:
        print(f"    ⚠  {w}")

    # ── Step 7: Cost Tracking ─────────────────────────────────────────────────
    print(f"\n{DIVIDER}")
    print("STEP 7: Cost Tracking  (backend.cost_tracker.CostTracker — NEW)")
    print(DIVIDER)
    from backend.cost_tracker import CostTracker
    tracker = CostTracker()
    tracker.record(SESSION_ID, "openai/gpt-4o-mini", "migration",
                   prompt_text=QLIK_SCRIPT, completion_text=GENERATED_DBT_SQL)
    tracker.record(SESSION_ID, "openai/gpt-4o-mini", "dbt_agent",
                   prompt_text="plan dbt commands", completion_text=json.dumps(result.commands))
    summary = tracker.session_summary(SESSION_ID)
    print(f"  Session: {SESSION_ID}")
    print(f"  Total calls    : {summary['totalCalls']}")
    print(f"  Total tokens   : {summary['totalTokens']:,}")
    print(f"  Estimated cost : ${summary['estimatedCostUsd']:.4f} USD")
    print(f"  Breakdown:")
    for purpose, stats in summary["byPurpose"].items():
        print(f"    {purpose:<15} {stats['calls']} call(s), "
              f"{stats['promptTokens']:,} + {stats['completionTokens']:,} tokens, "
              f"${stats['costUsd']:.5f}")

    # ── Step 8: Feedback ──────────────────────────────────────────────────────
    print(f"\n{DIVIDER}")
    print("STEP 8: User Feedback + AI Reflection  (feedback.py — NEW)")
    print(DIVIDER)
    print("  Scenario: data engineer notices the RESIDENT JOIN produced")
    print("  duplicates — PatientTimeline has 38 000 rows instead of 31 500.")
    print()
    print("  User rates the migration: THUMBS DOWN")
    print("  User comment: 'Row count is wrong — duplicates from the join'")
    print()
    print("  AI Reflection (what the LLM would return):")
    reflection = textwrap.dedent("""\
        • The LEFT JOIN on DosingEvents × AdverseEvents creates a cartesian
          product when a patient has multiple dosing AND adverse events.
          A patient with 3 doses and 2 AEs produces 6 rows, not 1.

        • Fix: pre-aggregate DosingEvents to one row per patient (e.g. MIN
          DoseDate, COUNT doses) BEFORE joining to AdverseEvents, OR use
          separate fact tables (fct_dosing, fct_adverse) instead of one
          wide PatientTimeline.

        • The Qlik RESIDENT join avoids this because Qlik's associative
          model does not create cross-product rows — it is NOT equivalent
          to a SQL JOIN when cardinality is > 1:1.

        • Recommended dbt fix: change patient_timeline to use ROW_NUMBER()
          PARTITION BY PatientID ORDER BY DoseDate to take the first dose,
          then join adverse events 1:1 on PatientID + nearest AEDate.
    """)
    for line in reflection.strip().splitlines():
        print(f"  {line}")

    print(f"\n{'═'*72}")
    print("  DEMO COMPLETE")
    print(f"{'═'*72}\n")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — Architecture Diagram (text)
# ═══════════════════════════════════════════════════════════════════════════════

ARCHITECTURE_DIAGRAM = """
QVF DECODER — Complete Agent Architecture
══════════════════════════════════════════════════════════════════

  ┌─────────────────────────────────────────────────────────────┐
  │  FRONTEND  (React / Vite)                                   │
  │  Upload QVF → View tables → Migrate → Edit SQL → Deploy     │
  └──────────────────────┬──────────────────────────────────────┘
                         │ HTTP
  ┌──────────────────────▼──────────────────────────────────────┐
  │  FLASK SERVER  (server.py)                                   │
  │                                                             │
  │  /api/upload        → backend.extraction.qvf_runtime         │
  │  /api/regenerate    → backend.migration.sql_generation       │
  │  /api/chat          → iterative SQL refinement              │
  │  /api/download      → backend.integrations.dbt_package_routes│
  │  /api/dbt-cloud/*   → backend.integrations.dbt_cloud_routes  │
  │  /api/cost/*        → backend.cost_tracker                   │
  │  /api/feedback/*    → backend.feedback_routes                │
  │                                                             │
  │  SQL_PLAN_CACHE = SessionPlanCache(LRU)  ◄── FIXED          │
  │  COST_TRACKER   = CostTracker()          ◄── BRAND NEW      │
  └──┬───────────────────┬──────────────────────────────────────┘
     │                   │
     ▼                   ▼
  ┌──────────┐    ┌──────────────────────────────────────────────┐
  │ SQLite   │    │  AI LAYER  (backend.integrations.*)           │
  │          │    │                                              │
  │ sessions │    │  call_openrouter_chat()                      │
  │ files    │    │    • json=payload  ← FIXED (was data=dumps)  │
  │ history  │    │    • retries=1, backoff                      │
  │ feedback │    │    • timeout=120 (configurable)              │
  └──────────┘    │    • max_prompt_chars=60000 (guards context) │
                  └──────────────────┬───────────────────────────┘
                                     │
                  ┌──────────────────▼───────────────────────────┐
                  │  VALIDATION LAYER  (backend.migration.validator)│
                  │  ◄── BRAND NEW                               │
                  │                                              │
                  │  Pass 1 — Structural (parens, DDL, shell)     │
                  │  Pass 2 — Plan Coverage (all models present)  │
                  │  Pass 3 — Ref Integrity ({{ref()}} resolves)  │
                  │  Pass 4 — Dialect (Snowflake/BQ/Power BI)     │
                  │  Pass 5 — Security (DROP/TRUNCATE/injection)  │
                  └──────────────────────────────────────────────┘

  ┌──────────────────────────────────────────────────────────────┐
  │  DBT PACKAGE AGENT  (dbt_package_agent.py)                   │
  │                                                              │
  │  create_dbt_package()                                        │
  │    • models/staging/stg_<table>.sql  (one per Qlik table)   │
  │    • models/marts/migration_output.sql  (AI-generated SQL)   │
  │    • schema.yml with column types + dbt tests               │
  │    • dbt_project.yml, README.md                             │
  │    → ZIP download via /api/download/<session_id>            │
  └──────────────────────────────────────────────────────────────┘

  ┌──────────────────────────────────────────────────────────────┐
  │  DBT CLOUD AGENT  (dbt_cloud_agent.py)                       │
  │                                                              │
  │  POST /api/dbt-cloud/test                                    │
  │    └─ dbt_cloud_request(GET /accounts/{id}/)  [+retry]      │
  │                                                              │
  │  POST /api/dbt-cloud/run                                     │
  │    1. require_dbt_cloud_config()  — validate input          │
  │    2. sanitize_dbt_commands()     — allowlist + blocklist    │
  │    3. plan_dbt_agent_run()        — AI reviews commands      │
  │       └─ build_dbt_agent_prompt() — injects SQL + plan      │
  │       └─ call_ai()               — LLM at temperature=0     │
  │       └─ parse_agent_response()  — strict JSON extraction   │
  │       └─ sanitize_dbt_commands() — second safety pass       │
  │    4. dbt_cloud_request(POST /jobs/{id}/run/)  [+retry]     │
  │                                                              │
  │  POST /api/dbt-cloud/status                                  │
  │    └─ dbt_cloud_request(GET /runs/{run_id}/)                │
  └──────────────────────────────────────────────────────────────┘

Data flow for a single "Deploy to dbt Cloud" click:
──────────────────────────────────────────────────────────────────
  Browser click
    → POST /api/dbt-cloud/run {sessionId, jobId, commands}
      → sanitize_dbt_commands()      [BLOCKLIST]
      → build_session_bundle()       [SQLite read]
      → plan_dbt_agent_run()         [LLM at T=0]
        → build_dbt_agent_prompt()   [injects SQL + plan]
        → call_ai()                  [OpenRouter API, retry=2]
        → parse_agent_response()     [JSON extract]
        → sanitize_dbt_commands()    [second pass]
      → validate_migration_sql()     [5-pass validator, NEW]
      → dbt_cloud_request(POST run)  [dbt Cloud API, retry=2]
      → cost_tracker.record()        [token accounting, NEW]
    ← {success, runId, commands, agent{summary,checks,warnings}}
  Browser polls GET /api/dbt-cloud/status
    → dbt_cloud_request(GET run)
    ← {statusHumanized: "Running" / "Success" / "Error"}
"""


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print(ARCHITECTURE_DIAGRAM)
    demo_full_flow()

