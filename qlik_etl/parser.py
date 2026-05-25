import re
from typing import List, Optional

from .ast import (
    ApplyMapStatement,
    DropFieldsStatement,
    LoadStatement,
    MappingLoadStatement,
    OtherStatement,
    QlikScript,
    SqlPassThroughStatement,
    Statement,
    VariableStatement,
)
from .errors import QlikParserError


def _split_fields(field_text: str) -> List[str]:
    fields = []
    buffer = []
    depth = 0
    in_single = False
    in_double = False

    for char in field_text:
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if char == '(':
                depth += 1
            elif char == ')' and depth > 0:
                depth -= 1
            elif char == ',' and depth == 0:
                item = ''.join(buffer).strip()
                if item:
                    fields.append(item)
                buffer = []
                continue
        buffer.append(char)

    tail = ''.join(buffer).strip()
    if tail:
        fields.append(tail)
    return fields


def _extract_clause(raw_text: str, clause: str) -> Optional[str]:
    if not raw_text or not clause:
        return None
    pattern = rf'\b{clause}\b(.*?)(?=\bWHERE\b|\bGROUP\b|\bORDER\b|\bCONCATENATE\b|\bJOIN\b|;|$)'
    match = re.search(pattern, raw_text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    return match.group(1).strip()


def _cleanup_identifier(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    value = value.strip()
    if value.startswith('[') and value.endswith(']'):
        return value[1:-1].strip()
    return value


class QlikParser:
    """A generic Qlik load script parser that builds an AST of statements."""

    def parse(self, qlik_script: str) -> QlikScript:
        if qlik_script is None:
            raise QlikParserError('Script content cannot be None.')

        script = QlikScript(raw_script=qlik_script)
        statements = self._split_statements(qlik_script)

        for line_number, raw_stmt in statements:
            stmt = self._parse_statement(raw_stmt, line_number)
            if stmt:
                script.statements.append(stmt)

        script.variables = self._extract_variables(script.statements)
        return script

    def _split_statements(self, text: str) -> List[tuple[int, str]]:
        statements = []
        buffer = []
        line_number = 1
        current_line = 1
        in_single = False
        in_double = False
        in_block_comment = False
        i = 0

        while i < len(text):
            char = text[i]
            buffer.append(char)

            if char == '\n':
                current_line += 1

            if in_block_comment:
                if text.startswith('*/', i):
                    in_block_comment = False
                    buffer.append(text[i + 1])
                    i += 2
                    continue
                i += 1
                continue

            if not in_single and not in_double and text.startswith('/*', i):
                in_block_comment = True
                i += 2
                continue

            if not in_single and not in_double and text.startswith('//', i):
                end = text.find('\n', i)
                if end == -1:
                    buffer.extend(text[i + 2:])
                    i = len(text)
                    continue
                buffer.extend(text[i + 2:end])
                i = end
                continue

            if char == '"' and not in_single:
                in_double = not in_double
            elif char == "'" and not in_double:
                in_single = not in_single

            if char == ';' and not in_single and not in_double and not in_block_comment:
                raw_statement = ''.join(buffer).strip()
                if raw_statement:
                    statements.append((line_number, raw_statement))
                buffer = []
                line_number = current_line + 1

            i += 1

        trailing = ''.join(buffer).strip()
        if trailing:
            statements.append((line_number, trailing))
        return statements

    def _parse_statement(self, raw_statement: str, line_number: int) -> Optional[Statement]:
        normalized = raw_statement.strip()
        if not normalized:
            return None

        first_token = normalized.split()[0].upper()
        if first_token in {'SET', 'LET'}:
            return self._parse_variable_statement(normalized, line_number, first_token)
        if first_token == 'SQL':
            return self._parse_sql_statement(normalized, line_number)
        if first_token == 'APPLYMAP':
            return self._parse_applymap_statement(normalized, line_number)
        if first_token == 'DROP':
            return self._parse_drop_fields_statement(normalized, line_number)
        if normalized.upper().startswith('MAPPING LOAD') or normalized.upper().startswith('MAPPING'):  # support label:MAPPING LOAD
            return self._parse_mapping_load_statement(normalized, line_number)
        if re.match(r'^(?:CONCATENATE|NOCONCATENATE|LEFT JOIN|RIGHT JOIN|INNER JOIN|FULL JOIN|OUTER JOIN|JOIN|KEEP)\b', normalized, flags=re.IGNORECASE) or 'LOAD' in normalized.upper():
            return self._parse_load_statement(normalized, line_number)

        return OtherStatement(type='OTHER', raw=normalized, line_number=line_number, keyword=first_token)

    def _parse_variable_statement(self, raw_statement: str, line_number: int, assignment_type: str) -> VariableStatement:
        match = re.match(r'^(SET|LET)\s+([A-Za-z_][A-Za-z0-9_.]*)\s*=\s*(.*)$', raw_statement, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            raise QlikParserError(f'Invalid SET/LET statement at line {line_number}: {raw_statement}')
        return VariableStatement(
            type=assignment_type,
            raw=raw_statement,
            line_number=line_number,
            assignment_type=match.group(1).upper(),
            variable_name=match.group(2).strip(),
            expression=match.group(3).strip(),
        )

    def _parse_sql_statement(self, raw_statement: str, line_number: int) -> SqlPassThroughStatement:
        sql_text = raw_statement[3:].strip()
        return SqlPassThroughStatement(type='SQL', raw=raw_statement, line_number=line_number, sql_text=sql_text)

    def _parse_applymap_statement(self, raw_statement: str, line_number: int) -> ApplyMapStatement:
        match = re.match(r'APPLYMAP\s*\(\s*([^,]+)\s*,\s*([^,]+)\s*(?:,\s*(.+?))?\s*\)\s*(?:AS\s+(.*))?$', raw_statement, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            return ApplyMapStatement(type='APPLYMAP', raw=raw_statement, line_number=line_number)
        return ApplyMapStatement(
            type='APPLYMAP',
            raw=raw_statement,
            line_number=line_number,
            map_name=match.group(1).strip(),
            source_field=match.group(2).strip(),
            default_value=match.group(3).strip() if match.group(3) else None,
            target_field=match.group(4).strip() if match.group(4) else None,
        )

    def _parse_mapping_load_statement(self, raw_statement: str, line_number: int) -> MappingLoadStatement:
        label = None
        mapping_name = None
        match = re.match(r'^(?:\[([^\]]+)\]|([A-Za-z_][A-Za-z0-9_\$]*))\s*:\s*(MAPPING\s+LOAD.*)$', raw_statement, flags=re.IGNORECASE | re.DOTALL)
        body = raw_statement
        if match:
            label = match.group(1) or match.group(2)
            body = match.group(3)
        mapping_name = label
        stmt = self._parse_load_statement(body, line_number)
        if isinstance(stmt, LoadStatement):
            return MappingLoadStatement(
                type='MAPPING LOAD',
                raw=raw_statement,
                line_number=line_number,
                label=label,
                prefix=stmt.prefix,
                target_table=stmt.target_table,
                source=stmt.source,
                source_type=stmt.source_type,
                fields=stmt.fields,
                filters=stmt.filters,
                group_by=stmt.group_by,
                order_by=stmt.order_by,
                join_type=stmt.join_type,
                join_target=stmt.join_target,
                is_mapping=True,
                raw_fields=stmt.raw_fields,
                mapping_name=mapping_name,
            )
        return MappingLoadStatement(type='MAPPING LOAD', raw=raw_statement, line_number=line_number, mapping_name=mapping_name)

    def _parse_drop_fields_statement(self, raw_statement: str, line_number: int) -> DropFieldsStatement:
        match = re.match(r'^DROP\s+FIELDS?\s+(.+?)\s+FROM\s+(.+)$', raw_statement, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            return DropFieldsStatement(type='DROP', raw=raw_statement, line_number=line_number)
        fields_text = match.group(1).strip().rstrip(';')
        target = _cleanup_identifier(match.group(2).strip().rstrip(';'))
        fields = [field.strip() for field in fields_text.split(',') if field.strip()]
        return DropFieldsStatement(
            type='DROP_FIELDS',
            raw=raw_statement,
            line_number=line_number,
            fields=fields,
            target_table=target,
        )

    def _parse_load_statement(self, raw_statement: str, line_number: int) -> LoadStatement:
        label = None
        prefix = None
        body = raw_statement

        label_match = re.match(r'^(?:\[([^\]]+)\]|([A-Za-z_][A-Za-z0-9_\$]*))\s*:\s*(.*)$', raw_statement, flags=re.IGNORECASE | re.DOTALL)
        if label_match:
            label = label_match.group(1) or label_match.group(2)
            body = label_match.group(3).strip()

        prefix_match = re.match(r'^(CONCATENATE|NOCONCATENATE|LEFT\s+JOIN|RIGHT\s+JOIN|INNER\s+JOIN|FULL\s+JOIN|OUTER\s+JOIN|JOIN|KEEP)\b', body, flags=re.IGNORECASE)
        if prefix_match:
            prefix = prefix_match.group(1).upper()
            body = body[prefix_match.end():].strip()

        join_target = None
        join_match = re.match(r'^\(?\s*([A-Za-z_\$][A-Za-z0-9_\$\[\]]*)\s*\)?\s*', body, flags=re.IGNORECASE)
        if prefix and 'JOIN' in prefix and join_match:
            candidate = join_match.group(1).strip()
            if not candidate.upper().startswith('LOAD'):
                join_target = _cleanup_identifier(candidate)
                body = body[join_match.end():].strip()

        load_match = re.search(r'\bLOAD\b', body, flags=re.IGNORECASE)
        if not load_match:
            raise QlikParserError(f'Unable to locate LOAD keyword in statement at line {line_number}: {raw_statement}')

        content = body[load_match.end():].strip()
        source_type = 'from'
        source = None
        resident_table = None
        where_clause = []
        group_by = None
        order_by = None
        is_inline = 'INLINE' in content.upper()

        resident_match = re.search(r'\bRESIDENT\b\s*([A-Za-z_\$\[\]][A-Za-z0-9_\$\[\]]*)', content, flags=re.IGNORECASE)
        if resident_match:
            source_type = 'resident'
            resident_table = _cleanup_identifier(resident_match.group(1))
            source = resident_table
        else:
            source_match = re.search(r'\bFROM\b\s*(.+?)(?=\bWHERE\b|\bGROUP\b|\bORDER\b|;|$)', content, flags=re.IGNORECASE | re.DOTALL)
            if source_match:
                source = _cleanup_identifier(source_match.group(1).strip())

        fields_text = content
        for delimiter in ['WHERE', 'GROUP BY', 'ORDER BY', 'RESIDENT', 'FROM']:
            delim_pattern = re.compile(rf'\b{delimiter}\b', flags=re.IGNORECASE)
            match = delim_pattern.search(fields_text)
            if match:
                fields_text = fields_text[:match.start()]
                break

        field_entries = _split_fields(fields_text)
        raw_fields = [entry.strip() for entry in field_entries if entry.strip()]

        where_match = re.search(r'\bWHERE\b\s*(.+?)(?=\bGROUP\b|\bORDER\b|;|$)', content, flags=re.IGNORECASE | re.DOTALL)
        if where_match:
            where_clause = [where_match.group(1).strip()]

        group_by_match = re.search(r'\bGROUP\s+BY\b\s*(.+?)(?=\bORDER\b|;|$)', content, flags=re.IGNORECASE | re.DOTALL)
        if group_by_match:
            group_by = group_by_match.group(1).strip()

        order_by_match = re.search(r'\bORDER\s+BY\b\s*(.+?)(?=;|$)', content, flags=re.IGNORECASE | re.DOTALL)
        if order_by_match:
            order_by = order_by_match.group(1).strip()

        if prefix and prefix.upper() == 'CONCATENATE':
            join_type = 'CONCATENATE'
        elif prefix and 'JOIN' in prefix.upper():
            join_type = 'JOIN'
        else:
            join_type = None

        if not label and prefix and ('JOIN' in prefix.upper() or prefix.upper() == 'CONCATENATE'):
            target_table = join_target
        else:
            target_table = label or _cleanup_identifier(source) or None

        return LoadStatement(
            type='LOAD',
            raw=raw_statement,
            line_number=line_number,
            label=label,
            prefix=prefix,
            target_table=target_table,
            source=source,
            source_type=source_type,
            resident_table=resident_table,
            fields=raw_fields,
            filters=where_clause,
            group_by=group_by,
            order_by=order_by,
            join_type=join_type,
            join_target=join_target,
            is_inline=is_inline,
            raw_fields=raw_fields,
        )

    def _extract_variables(self, statements: List[Statement]) -> dict:
        variables = {}
        for stmt in statements:
            if isinstance(stmt, VariableStatement):
                variables[stmt.variable_name] = stmt.expression
        return variables
