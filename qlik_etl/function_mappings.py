FUNCTION_MAPPINGS = {
    'Sum': 'SUM',
    'Count': 'COUNT',
    'Avg': 'AVG',
    'Min': 'MIN',
    'Max': 'MAX',
    'If': 'CASE WHEN',
    'ApplyMap': 'MAP_VALUE',
    'Date': 'DATE',
    'AddMonths': 'ADD_MONTHS',
    'MonthStart': 'DATE_TRUNC',
    'Text': 'CAST',
    'Num': 'CAST',
    'Floor': 'FLOOR',
    'Ceil': 'CEIL',
    'Round': 'ROUND',
    'Len': 'LENGTH',
    'Upper': 'UPPER',
    'Lower': 'LOWER',
    # Month() returns abbreviated names in Qlik ("Jan", "Feb").
    # MONTHNAME() returns full names ("January") — use TO_CHAR with 'Mon' format instead.
    # NOTE: the dialect layer handles Month() specially via TO_CHAR(expr, 'Mon').
}
