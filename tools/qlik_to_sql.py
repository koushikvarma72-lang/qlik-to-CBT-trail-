from backend.extraction.qvf_runtime import extract_model_from_script
from backend.migration.sql_generation import extract_sql_generation_plan
import re


def _safe_name(name):
    return re.sub(r'[^A-Za-z0-9_]', '_', (name or '').lower()) or 'model'


def convert_qvs_to_sql(qvs_script):
    """Simple, deterministic Qlik->SQL converter for basic LOAD/RESIDENT patterns.

    This is a conservative fallback generator (no LLM). It produces readable
    CTE-based SQL from the parsed script model and the extracted plan.
    """
    if not qvs_script:
        return ''

    plan = extract_sql_generation_plan(qvs_script)
    model = extract_model_from_script(qvs_script)

    ctes = []
    cte_names = []

    # Build CTEs from plan items when possible for stable ordering
    if plan:
        for idx, item in enumerate(plan):
            table = item.get('table') or f'generated_{idx}'
            cte_name = _safe_name(table)
            cte_names.append(cte_name)

            fields = item.get('fields') or []
            # If fields were not in plan, try model tables
            if not fields:
                mt = next((t for t in model.get('tables', []) if t.get('name', '').lower() == table.lower()), None)
                if mt:
                    fields = [f.get('name') for f in mt.get('fields', [])]

            select_list = '*'
            if fields:
                safe_fields = []
                for f in fields:
                    fname = f or ''
                    safe = re.sub(r'[^A-Za-z0-9_]', '_', fname)
                    safe_fields.append(f"{fname} as {safe}")
                select_list = ', '.join(safe_fields)

            sources = item.get('source_tables') or []
            if sources:
                source = sources[0]
                source_ref = f"{{{{ source('raw', '{source}') }}}}" if '[' not in source and '.' not in source else source
                from_clause = f"FROM {source_ref}"
            else:
                from_clause = "-- SOURCE UNKNOWN: review and replace with actual source table"

            cte_sql = f"{cte_name} as (\n    select {select_list} \n    {from_clause}\n)"
            ctes.append(cte_sql)

    else:
        # Fallback: create single CTE per model table
        for t in model.get('tables', []):
            name = t.get('name') or 'extracted'
            cte_name = _safe_name(name)
            cte_names.append(cte_name)
            fields = [f.get('name') for f in t.get('fields', [])]
            select_list = '*'
            if fields:
                select_list = ', '.join(fields)
            from_clause = t.get('sourcePath') or '-- SOURCE UNKNOWN'
            ctes.append(f"{cte_name} as (\n    select {select_list} \n    from {from_clause}\n)")

    if not ctes:
        return ''

    final_cte = ctes[-1].split()[0]
    sql = 'with\n' + ',\n'.join(ctes) + f"\nselect * from {final_cte};"
    return sql


if __name__ == '__main__':
    sample = '''
SalesTemp:
LOAD StoreID, SalesAmount, Quantity FROM [lib://data.qvd];

SalesSummary:
LOAD StoreID, Sum(SalesAmount) AS TotalSales RESIDENT SalesTemp GROUP BY StoreID;
'''
    print(convert_qvs_to_sql(sample))
