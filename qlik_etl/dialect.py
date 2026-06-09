import re
from typing import Dict, List

from .function_mappings import FUNCTION_MAPPINGS
from .errors import QlikTransformationError


class SqlDialect:
    def quote_identifier(self, identifier: str) -> str:
        if not identifier:
            return identifier
        identifier = identifier.strip()
        if identifier.startswith('[') and identifier.endswith(']'):
            identifier = identifier[1:-1]
        escaped = identifier.replace('"', '""')
        return f'"{escaped}"'

    def translate_expression(self, expression: str) -> str:
        if not expression:
            return expression
        result = expression.strip()
        result = re.sub(r'\[([^\]]+)\]', lambda m: self.quote_identifier(m.group(1)), result)
        for qlik_name, sql_name in FUNCTION_MAPPINGS.items():
            result = re.sub(rf'\b{qlik_name}\s*\(', f'{sql_name}(', result, flags=re.IGNORECASE)
        result = re.sub(r'\s*&\s*', ' || ', result)
        if re.search(r'\bIF\s*\(', result, flags=re.IGNORECASE):
            result = self._translate_if_expression(result)
        return result

    def _translate_if_expression(self, expression: str) -> str:
        expression = re.sub(r'\bIF\s*\(\s*(.+?)\s*,\s*(.+?)\s*,\s*(.+?)\s*\)',
                            lambda m: f'CASE WHEN {m.group(1)} THEN {m.group(2)} ELSE {m.group(3)} END',
                            expression, flags=re.IGNORECASE | re.DOTALL)
        return expression
    def render_select(self, node: 'TransformationNode', alias_map: Dict[str, str]) -> str:
        if not node.fields:
            select_clause = 'SELECT *'
        else:
            rendered_fields = [self.translate_expression(field) for field in node.fields]
            select_clause = 'SELECT\n    ' + ',\n    '.join(rendered_fields)

        source_clause = ''
        if node.source_names:
            source_name = node.source_names[0]
            quoted = alias_map.get(source_name, source_name)
            source_clause = f'FROM {self.quote_identifier(quoted)}'
        clauses = [select_clause]
        if source_clause:
            clauses.append(source_clause)
        if node.filters:
            clauses.append('WHERE ' + ' AND '.join(node.filters))
        if node.group_by:
            clauses.append('GROUP BY ' + node.group_by)
        if node.order_by:
            clauses.append('ORDER BY ' + node.order_by)
        return '\n'.join(clauses)

    def render_plan(self, nodes: List['TransformationNode']) -> str:
        if not nodes:
            return ''
        bodies = []
        alias_map: Dict[str, str] = {}
        for index, node in enumerate(nodes, start=1):
            key = node.name or f'load_{index}'
            alias_map[key] = key

        for index, node in enumerate(nodes, start=1):
            alias = self.quote_identifier(node.name or f'load_{index}')
            if node.operation == 'CONCATENATE':
                body = self.render_concatenate(node, alias_map)
            elif node.operation == 'JOIN':
                body = self.render_join(node, alias_map)
            elif node.operation == 'DROP_FIELDS':
                body = self.render_drop_fields(node, alias_map)
            else:
                body = self.render_node(node, alias_map)
            bodies.append(f'{alias} AS (\n{body}\n)')

        final_name = nodes[-1].name or 'result'
        cte_block = 'WITH\n' + ',\n'.join(bodies)
        return f'{cte_block}\nSELECT *\nFROM {self.quote_identifier(final_name)}'

    def render_node(self, node: 'TransformationNode', alias_map: Dict[str, str]) -> str:
        return self.render_select(node, alias_map)

    def render_concatenate(self, node: 'TransformationNode', alias_map: Dict[str, str]) -> str:
        # CONCATENATE: append rows from append_source into target
        if not node.source_names:
            return self.render_select(node, alias_map)
        target = alias_map.get(node.source_names[0], node.source_names[0])
        append_source = alias_map.get(node.source_names[1], node.source_names[1]) if len(node.source_names) > 1 else node.source_names[0]
        select_target = f'SELECT *\nFROM {self.quote_identifier(target)}'
        if node.fields:
            append_fields = [self.translate_expression(f) for f in node.fields]
            append_select = 'SELECT\n    ' + ',\n    '.join(append_fields) + f'\nFROM {self.quote_identifier(append_source)}'
        else:
            append_select = f'SELECT *\nFROM {self.quote_identifier(append_source)}'
        return f'{select_target}\nUNION ALL\n{append_select}'

    def render_join(self, node: 'TransformationNode', alias_map: Dict[str, str]) -> str:
        # Best-effort join rendering; ideally we'd infer keys from expressions
        left = alias_map.get(node.source_names[0], node.source_names[0]) if node.source_names else None
        right = alias_map.get(node.source_names[1], node.source_names[1]) if len(node.source_names) > 1 else None
        if not left or not right:
            return self.render_select(node, alias_map)
        left_q = self.quote_identifier(left)
        right_q = self.quote_identifier(right)
        select_parts = ['SELECT', '    left.*']
        if node.fields:
            select_parts.append(',\n    ' + ',\n    '.join([self.translate_expression(f) for f in node.fields]))
        select_clause = '\n'.join(select_parts)
        # build ON clause from inferred join_keys when available
        on_clause = '1=1'
        if getattr(node, 'join_keys', None):
            pairs = []
            for col in node.join_keys:
                pairs.append(f"{left_q}.{self.quote_identifier(col)} = {right_q}.{self.quote_identifier(col)}")
            on_clause = ' AND '.join(pairs)

        join_clause = f'FROM {left_q}\nLEFT JOIN {right_q} ON {on_clause}'
        if node.filters:
            join_clause += '\nWHERE ' + ' AND '.join(node.filters)
        return select_clause + '\n' + join_clause

    def render_drop_fields(self, node: 'TransformationNode', alias_map: Dict[str, str]) -> str:
        # Implement DROP FIELDS as projection excluding dropped columns
        source = node.source_names[0] if node.source_names else None
        if not source:
            return '-- DROP_FIELDS: no source available'
        remaining = []
        if node.statement and hasattr(node.statement, 'fields'):
            remaining = [f for f in node.statement.fields if f not in getattr(node, 'drop_fields', [])]
        if not remaining:
            return f'SELECT *\nFROM {self.quote_identifier(alias_map.get(source, source))}'
        rendered_fields = [self.translate_expression(f) for f in remaining]
        return 'SELECT\n    ' + ',\n    '.join(rendered_fields) + f'\nFROM {self.quote_identifier(alias_map.get(source, source))}'


class SparkSqlDialect(SqlDialect):
    def translate_expression(self, expression: str) -> str:
        translated = super().translate_expression(expression)
        translated = re.sub(r'\bDATE\s*\(([^,]+?),\s*([^)]+?)\)', r"DATE_FORMAT(\1, \2)", translated, flags=re.IGNORECASE)
        translated = re.sub(r'\bMONTHSTART\s*\(([^)]+?)\)', r'DATE_TRUNC(\1, MONTH)', translated, flags=re.IGNORECASE)
        translated = re.sub(r'\bADDMONTHS\s*\(([^,]+?)\s*,\s*([^)]+?)\)', r'ADD_MONTHS(\1, \2)', translated, flags=re.IGNORECASE)
        return translated


def dialect_factory(name: str = 'spark') -> SqlDialect:
    if name.lower() == 'spark':
        return SparkSqlDialect()
    raise QlikTransformationError(f'Unsupported SQL dialect: {name}')
