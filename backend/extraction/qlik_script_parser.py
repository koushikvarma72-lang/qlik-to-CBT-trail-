# -*- coding: utf-8 -*-
"""
Comprehensive Qlik Load Script Parser
=====================================

Complete extraction and analysis of Qlik load scripts:
- LOAD/SELECT statements with data source tracking
- Variable definitions (SET, LET) with resolution
- All statement types (STORE, CONCATENATE, JOIN, KEEP, etc.)
- Comment and documentation preservation
- Formatting and structure preservation
- Error and warning detection
- Circular reference detection
"""

import re
from typing import Dict, List, Any, Tuple, Optional
from collections import defaultdict


def _split_sql_like_fields(field_text: str) -> List[str]:
    """Split a LOAD field list without breaking commas inside expressions."""
    if not field_text:
        return []

    fields = []
    token = []
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
                item = ''.join(token).strip()
                if item:
                    fields.append(item)
                token = []
                continue

        token.append(char)

    tail = ''.join(token).strip()
    if tail:
        fields.append(tail)

    return fields


class LoadScriptParser:
    """Parse and analyze complete Qlik load scripts."""
    
    # Keywords that start major statements
    MAJOR_KEYWORDS = {
        'LOAD', 'SELECT', 'STORE', 'CONCATENATE', 'JOIN', 'KEEP',
        'APPLYMAP', 'INLINE', 'SET', 'LET', 'INCLUDE', 'IF',
        'CALL', 'FOR', 'WHILE', 'DO', 'LOOP', 'NEXT'
    }
    
    # Statement modifiers
    STATEMENT_MODIFIERS = {
        'NOCONCATENATE', 'DISTINCT', 'WHERE', 'ORDER BY', 'GROUP BY',
        'INNER', 'LEFT', 'RIGHT', 'FULL', 'OUTER', 'AUTO', 'RESIDENT'
    }
    
    def __init__(self):
        self.statements = []
        self.variables = {}
        self.sources = []
        self.errors = []
        self.warnings = []
    
    def parse_complete_script(self, script_text: str) -> Dict[str, Any]:
        """
        Parse complete Qlik script preserving all details.
        
        Returns:
        {
            'statements': [...],          # All parsed statements
            'variables': {...},           # Variable definitions
            'dataSources': [...],         # External data sources
            'tables': [...],              # Logical tables created
            'associations': [...],        # Detected associations
            'circularReferences': [...],  # Circular dependencies
            'issues': [...],              # Errors and warnings
            'comments': [...],            # Documentation
            'formatting': {...},          # Structure info
        }
        """
        if not script_text:
            return self._empty_result()
        
        result = {
            'statements': [],
            'loadBlocks': [],
            'variables': {},
            'dataSources': [],
            'tables': [],
            'associations': [],
            'circularReferences': [],
            'issues': [],
            'comments': [],
            'formatting': self._analyze_formatting(script_text),
            'controlFlow': [],
            'subroutines': [],
            'includes': [],
            'sqlBlocks': [],
            'statementTypes': {},
            'rawScript': script_text,
        }
        
        # Split into lines for processing
        lines = script_text.split('\n')
        current_statement = []
        in_block_comment = False
        statement_start_line = 1
        
        for line_no, line in enumerate(lines, 1):
            stripped = line.strip()
            
            # Handle block comments /* ... */
            if '/*' in line:
                in_block_comment = True
                result['comments'].append({
                    'lineNumber': line_no,
                    'content': stripped,
                    'type': 'block_comment_start'
                })
            
            if in_block_comment:
                if '*/' in line:
                    in_block_comment = False
                    result['comments'].append({
                        'lineNumber': line_no,
                        'content': stripped,
                        'type': 'block_comment_end'
                    })
                continue
            
            # Handle line comments //
            if stripped.startswith('//'):
                result['comments'].append({
                    'lineNumber': line_no,
                    'content': stripped,
                    'type': 'line_comment'
                })
                continue
            
            if not stripped:
                continue
            
            current_statement.append((line_no, line))
            
            # Check if statement is complete (ends with ;)
            if ';' in stripped:
                stmt = self._parse_statement(current_statement, statement_start_line)
                if stmt:
                    result['statements'].append(stmt)
                current_statement = []
                statement_start_line = line_no + 1
        
        # Extract higher-level information
        result['variables'] = self._extract_variables(result['statements'])
        result['dataSources'] = self._extract_data_sources(result['statements'])
        result['tables'] = self._extract_table_definitions(result['statements'])
        result['associations'] = self._extract_associations(result['statements'])
        result['loadBlocks'] = [stmt for stmt in result['statements'] if stmt.get('type') == 'LOAD']
        result['circularReferences'] = self._detect_circular_references(result['statements'])
        result['controlFlow'] = self._extract_control_flow(result['statements'])
        result['subroutines'] = self._extract_subroutines(result['statements'])
        result['includes'] = [stmt for stmt in result['statements'] if stmt.get('type') == 'INCLUDE']
        result['sqlBlocks'] = [stmt for stmt in result['statements'] if stmt.get('type') == 'SQL']
        result['statementTypes'] = self._count_statement_types(result['statements'])
        result['issues'] = self.warnings + self.errors

        return result

    def _parse_statement(self, lines: List[Tuple[int, str]], start_line: int) -> Optional[Dict[str, Any]]:
        """Parse a single complete statement."""
        if not lines:
            return None
        
        full_text = '\n'.join(line[1] for line in lines)
        first_line_no = lines[0][0]
        first_line = lines[0][1].strip()
        
        keyword = self._detect_statement_keyword(full_text)
        if not keyword:
            return None

        if keyword == 'LOAD':
            return self._parse_load_statement(full_text, first_line_no)
        elif keyword == 'SELECT':
            return self._parse_select_statement(full_text, first_line_no)
        elif keyword == 'SQL':
            return self._parse_sql_passthrough_statement(full_text, first_line_no)
        elif keyword == 'SET' or keyword == 'LET':
            return self._parse_variable_statement(full_text, first_line_no, keyword)
        elif keyword == 'STORE':
            return self._parse_store_statement(full_text, first_line_no)
        elif keyword == 'CONCATENATE':
            if re.search(r'\bLOAD\b', full_text, re.IGNORECASE):
                return self._parse_load_statement(full_text, first_line_no, load_prefix='CONCATENATE')
            return self._parse_concatenate_statement(full_text, first_line_no)
        elif keyword == 'JOIN':
            if re.search(r'\bLOAD\b', full_text, re.IGNORECASE):
                return self._parse_load_statement(full_text, first_line_no, load_prefix='JOIN')
            return self._parse_join_statement(full_text, first_line_no)
        elif keyword == 'KEEP':
            if re.search(r'\bLOAD\b', full_text, re.IGNORECASE):
                return self._parse_load_statement(full_text, first_line_no, load_prefix='KEEP')
            return self._parse_keep_statement(full_text, first_line_no)
        elif keyword == 'MAPPING LOAD':
            return self._parse_mapping_load_statement(full_text, first_line_no)
        elif keyword == 'APPLYMAP':
            return self._parse_applymap_statement(full_text, first_line_no)
        elif keyword == 'INCLUDE':
            return self._parse_include_statement(full_text, first_line_no)
        elif keyword == 'CALL':
            return self._parse_call_statement(full_text, first_line_no)
        elif keyword in {'FOR', 'NEXT', 'DO', 'LOOP', 'WHILE', 'IF', 'SUB', 'END SUB', 'EXIT SUB'}:
            return self._parse_control_statement(full_text, first_line_no, keyword)
        else:
            return {
                'type': 'other',
                'keyword': keyword,
                'lineNumber': first_line_no,
                'content': full_text,
                'rawText': full_text
            }

    def _detect_statement_keyword(self, text: str) -> Optional[str]:
        normalized = (text or '').strip()
        if not normalized:
            return None

        patterns = [
            (r'^\s*(?:\[[^\]]+\]|[A-Za-z0-9_\$]+)\s*:\s*MAPPING\s+LOAD\b', 'MAPPING LOAD'),
            (r'^\s*MAPPING\s+LOAD\b', 'MAPPING LOAD'),
            (r'^\s*(?:\[[^\]]+\]|[A-Za-z0-9_\$]+)\s*:\s*LOAD\b', 'LOAD'),
            (r'^\s*(?:LEFT|RIGHT|INNER|OUTER|FULL)\s+JOIN\b', 'JOIN'),
            (r'^\s*JOIN\b', 'JOIN'),
            (r'^\s*(?:LEFT|RIGHT|INNER|OUTER|FULL)\s+KEEP\b', 'KEEP'),
            (r'^\s*KEEP\b', 'KEEP'),
            (r'^\s*NOCONCATENATE\b', 'CONCATENATE'),
            (r'^\s*CONCATENATE\b', 'CONCATENATE'),
            (r'^\s*SQL\b', 'SQL'),
            (r'^\s*SELECT\b', 'SELECT'),
            (r'^\s*LOAD\b', 'LOAD'),
            (r'^\s*APPLYMAP\b', 'APPLYMAP'),
            (r'^\s*SET\b', 'SET'),
            (r'^\s*LET\b', 'LET'),
            (r'^\s*\$\((?:must_)?include\s*=', 'INCLUDE'),
            (r'^\s*INCLUDE\b', 'INCLUDE'),
            (r'^\s*STORE\b', 'STORE'),
            (r'^\s*CALL\b', 'CALL'),
            (r'^\s*SUB\b', 'SUB'),
            (r'^\s*END\s+SUB\b', 'END SUB'),
            (r'^\s*EXIT\s+SUB\b', 'EXIT SUB'),
            (r'^\s*FOR(?:\s+EACH)?\b', 'FOR'),
            (r'^\s*NEXT\b', 'NEXT'),
            (r'^\s*DO\b', 'DO'),
            (r'^\s*LOOP\b', 'LOOP'),
            (r'^\s*WHILE\b', 'WHILE'),
            (r'^\s*IF\b', 'IF'),
        ]
        for pattern, keyword in patterns:
            if re.search(pattern, normalized, re.IGNORECASE):
                return keyword
        return None
    
    def _parse_load_statement(self, text: str, line_no: int, load_prefix: Optional[str] = None) -> Dict[str, Any]:
        """Parse LOAD statement."""
        original_text = text
        cleaned_text = text
        prefix_target = None
        prefix_label = None

        prefix_match = re.match(
            r'^\s*(?:(?P<prefix>(?:(?:LEFT|RIGHT|INNER|OUTER|FULL)\s+)?(?:JOIN|KEEP)|CONCATENATE|NOCONCATENATE|MAPPING)\s*(?:\(\s*(?P<prefix_target>\[[^\]]+\]|[A-Za-z0-9_\$]+)\s*\))?\s*)?(?:(?P<label>\[[^\]]+\]|[A-Za-z0-9_\$]+)\s*:\s*)?(?P<load>(?:MAPPING\s+)?LOAD\b[\s\S]*)$',
            text,
            re.IGNORECASE,
        )
        if prefix_match:
            cleaned_text = prefix_match.group('load') or text
            prefix_target = prefix_match.group('prefix_target')
            prefix_label = prefix_match.group('label')
            load_prefix = load_prefix or prefix_match.group('prefix')

        stmt = {
            'type': 'LOAD',
            'lineNumber': line_no,
            'content': original_text,
            'rawText': original_text,
            'fields': [],
            'fieldExpressions': [],
            'source': None,
            'sourceType': None,
            'residentTable': None,
            'modifiers': [],
            'conditions': []
        }

        if load_prefix:
            stmt['prefix'] = str(load_prefix).upper()
        if prefix_target:
            stmt['prefixTarget'] = prefix_target.strip('[]')
        if prefix_label:
            stmt['label'] = prefix_label.strip('[]')

        # Extract modifiers
        for modifier in ['DISTINCT', 'RESIDENT', 'INLINE']:
            if modifier in cleaned_text.upper():
                stmt['modifiers'].append(modifier)

        # Extract fields (simplified)
        load_match = re.search(r'LOAD\s+(.*?)\s+(?:FROM|RESIDENT|INLINE|;|WHERE|JOIN|KEEP|CONCATENATE|NOCONCATENATE)', cleaned_text, re.IGNORECASE | re.DOTALL)
        if load_match:
            fields_text = load_match.group(1)
            stmt['fieldExpressions'] = [f.strip() for f in _split_sql_like_fields(fields_text) if f.strip()]
            stmt['fields'] = list(stmt['fieldExpressions'])

        # Extract FROM clause
        from_match = re.search(r'FROM\s+([^;]+?)(?:WHERE|GROUP\s+BY|ORDER\s+BY|JOIN|KEEP|CONCATENATE|;)', cleaned_text, re.IGNORECASE | re.DOTALL)
        if from_match:
            stmt['source'] = from_match.group(1).strip()
            stmt['sourceType'] = 'from'
        resident_match = re.search(r'RESIDENT\s+([A-Za-z_][A-Za-z0-9_\$]*)', cleaned_text, re.IGNORECASE)
        if resident_match:
            stmt['residentTable'] = resident_match.group(1).strip()
            stmt['source'] = stmt['residentTable']
            stmt['sourceType'] = 'resident'

        # Extract WHERE clause
        where_match = re.search(r'WHERE\s+([^;]+?)(?:GROUP\s+BY|ORDER\s+BY|;)', cleaned_text, re.IGNORECASE | re.DOTALL)
        if where_match:
            stmt['conditions'].append(where_match.group(1).strip())
        
        return stmt
    
    def _parse_select_statement(self, text: str, line_no: int) -> Dict[str, Any]:
        """Parse SELECT (SQL) statement."""
        stmt = {
            'type': 'SELECT',
            'lineNumber': line_no,
            'content': text,
            'sqlStatement': text,
            'fields': [],
            'tables': [],
            'joins': [],
            'conditions': []
        }
        
        # Extract SELECT fields
        select_match = re.search(r'SELECT\s+(.*?)\s+FROM', text, re.IGNORECASE | re.DOTALL)
        if select_match:
            fields_text = select_match.group(1)
            stmt['fields'] = [f.strip() for f in re.split(r'[,]', fields_text) if f.strip()]
        
        # Extract FROM tables
        from_match = re.search(r'FROM\s+([^;]+?)(?:WHERE|;|JOIN|LEFT|RIGHT|INNER|OUTER)', text, re.IGNORECASE)
        if from_match:
            table_text = from_match.group(1)
            stmt['tables'] = [t.strip() for t in table_text.split(',') if t.strip()]
        
        # Extract JOINs
        join_pattern = r'(?:INNER|LEFT|RIGHT|FULL|OUTER)\s+JOIN\s+(\S+)\s+ON\s+([^;]+?)(?=WHERE|;|JOIN)'
        for match in re.finditer(join_pattern, text, re.IGNORECASE | re.DOTALL):
            stmt['joins'].append({
                'type': match.group(0).split()[0],
                'table': match.group(1),
                'condition': match.group(2).strip()
            })
        
        # Extract WHERE conditions
        where_match = re.search(r'WHERE\s+([^;]+?);', text, re.IGNORECASE)
        if where_match:
            stmt['conditions'].append(where_match.group(1).strip())
        
        return stmt

    def _parse_sql_passthrough_statement(self, text: str, line_no: int) -> Dict[str, Any]:
        sql_text = re.sub(r'^\s*SQL\s*', '', text, flags=re.IGNORECASE)
        stmt = self._parse_select_statement(sql_text, line_no)
        stmt['type'] = 'SQL'
        stmt['content'] = text
        stmt['rawText'] = text
        stmt['sqlStatement'] = sql_text
        return stmt
    
    def _parse_variable_statement(self, text: str, line_no: int, keyword: str) -> Dict[str, Any]:
        """Parse SET/LET statement."""
        pattern = rf'{keyword}\s+(\w+)\s*=\s*(.*?);'
        match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        
        stmt = {
            'type': keyword,
            'lineNumber': line_no,
            'content': text,
            'variableName': None,
            'value': None,
            'references': []
        }
        
        if match:
            var_name = match.group(1)
            var_value = match.group(2).strip()
            
            stmt['variableName'] = var_name
            stmt['value'] = var_value
            
            # Extract references to other variables
            ref_pattern = r'\$\((\w+)\)'
            stmt['references'] = re.findall(ref_pattern, var_value)
        
        return stmt
    
    def _parse_store_statement(self, text: str, line_no: int) -> Dict[str, Any]:
        """Parse STORE statement."""
        stmt = {
            'type': 'STORE',
            'lineNumber': line_no,
            'content': text,
            'tableName': None,
            'destination': None,
            'format': None
        }
        
        # Extract table name and destination
        match = re.search(r'STORE\s+(\w+)\s+INTO\s+([^\s;]+)', text, re.IGNORECASE)
        if match:
            stmt['tableName'] = match.group(1)
            stmt['destination'] = match.group(2)
            
            # Infer format from destination
            dest = match.group(2).lower()
            if '.qvd' in dest:
                stmt['format'] = 'QVD'
            elif '.csv' in dest or '.txt' in dest:
                stmt['format'] = 'CSV'
            elif '.xlsx' in dest or '.xls' in dest:
                stmt['format'] = 'EXCEL'
        
        return stmt
    
    def _parse_concatenate_statement(self, text: str, line_no: int) -> Dict[str, Any]:
        """Parse CONCATENATE statement."""
        stmt = {
            'type': 'CONCATENATE',
            'lineNumber': line_no,
            'content': text,
            'targetTable': None,
            'sourceTable': None,
            'isNoConcatenate': 'NOCONCATENATE' in text.upper()
        }
        
        # Extract target and source tables
        match = re.search(r'(?:NOCONCATENATE\s+)?(?:CONCATENATE\s*\(([^)]*)\))?\s*LOAD', text, re.IGNORECASE)
        if match and match.group(1):
            stmt['targetTable'] = match.group(1).strip()
        
        return stmt
    
    def _parse_join_statement(self, text: str, line_no: int) -> Dict[str, Any]:
        """Parse JOIN statement."""
        stmt = {
            'type': 'JOIN',
            'lineNumber': line_no,
            'content': text,
            'joinType': 'INNER',
            'leftTable': None,
            'rightTable': None
        }
        
        # Detect join type
        if 'LEFT' in text.upper():
            stmt['joinType'] = 'LEFT'
        elif 'RIGHT' in text.upper():
            stmt['joinType'] = 'RIGHT'
        elif 'FULL' in text.upper():
            stmt['joinType'] = 'FULL'
        
        return stmt
    
    def _parse_keep_statement(self, text: str, line_no: int) -> Dict[str, Any]:
        """Parse KEEP statement."""
        stmt = {
            'type': 'KEEP',
            'lineNumber': line_no,
            'content': text,
            'tables': []
        }
        
        # Extract table references
        keep_match = re.search(r'KEEP\s*\(([^)]+)\)', text, re.IGNORECASE)
        if keep_match:
            tables_text = keep_match.group(1)
            stmt['tables'] = [t.strip() for t in tables_text.split(',')]
        
        return stmt
    
    def _parse_include_statement(self, text: str, line_no: int) -> Dict[str, Any]:
        """Parse INCLUDE statement."""
        stmt = {
            'type': 'INCLUDE',
            'lineNumber': line_no,
            'content': text,
            'filePath': None,
            'isVariable': False
        }
        
        # Extract file path
        match = re.search(r'INCLUDE\s+([^\s;]+)', text, re.IGNORECASE)
        if not match:
            match = re.search(r'\$\((?:must_)?include\s*=\s*([^)]+)\)', text, re.IGNORECASE)
        if match:
            path = match.group(1)
            stmt['filePath'] = path.strip("'\"")
            stmt['isVariable'] = '$(' in path or '${' in path

        return stmt
    
    def _parse_call_statement(self, text: str, line_no: int) -> Dict[str, Any]:
        """Parse CALL statement."""
        stmt = {
            'type': 'CALL',
            'lineNumber': line_no,
            'content': text,
            'subroutine': None,
            'parameters': []
        }
        
        # Extract subroutine name and parameters
        match = re.search(r'CALL\s+(\w+)\s*\(([^)]*)\)', text, re.IGNORECASE)
        if match:
            stmt['subroutine'] = match.group(1)
            params = match.group(2).strip()
            stmt['parameters'] = [p.strip() for p in params.split(',') if p.strip()]
        
        return stmt

    def _parse_mapping_load_statement(self, text: str, line_no: int) -> Dict[str, Any]:
        stmt = self._parse_load_statement(text, line_no)
        stmt['type'] = 'MAPPING LOAD'
        stmt['isMappingLoad'] = True
        return stmt

    def _parse_applymap_statement(self, text: str, line_no: int) -> Dict[str, Any]:
        match = re.search(r'APPLYMAP\s*\(\s*[\'"]([^\'"]+)[\'"]\s*,\s*(.*?)\s*(?:,\s*(.*?))?\)', text, re.IGNORECASE | re.DOTALL)
        return {
            'type': 'APPLYMAP',
            'lineNumber': line_no,
            'content': text,
            'mappingTable': match.group(1) if match else None,
            'lookupExpression': match.group(2).strip() if match and match.group(2) else None,
            'defaultValue': match.group(3).strip() if match and match.group(3) else None,
            'rawText': text,
        }

    def _parse_control_statement(self, text: str, line_no: int, keyword: str) -> Dict[str, Any]:
        return {
            'type': keyword,
            'lineNumber': line_no,
            'content': text,
            'rawText': text,
            'keyword': keyword,
        }
    
    def _extract_variables(self, statements: List[Dict[str, Any]]) -> Dict[str, str]:
        """Extract all variable definitions with values."""
        variables = {}
        for stmt in statements:
            if stmt['type'] in ('SET', 'LET'):
                var_name = stmt.get('variableName')
                var_value = stmt.get('value')
                if var_name and var_value:
                    variables[var_name] = var_value
        return variables
    
    def _extract_data_sources(self, statements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Extract all external data sources."""
        sources = []
        seen = set()
        
        for stmt in statements:
            if stmt['type'] == 'LOAD':
                source = stmt.get('source')
                if source and source not in seen:
                    sources.append({
                        'type': 'LOAD_SOURCE',
                        'source': source,
                        'statement_line': stmt['lineNumber']
                    })
                    seen.add(source)
            elif stmt['type'] == 'SELECT':
                for table in stmt.get('tables', []):
                    if table not in seen:
                        sources.append({
                            'type': 'SQL_TABLE',
                            'source': table,
                            'statement_line': stmt['lineNumber']
                        })
                        seen.add(table)
            elif stmt['type'] == 'INCLUDE':
                filepath = stmt.get('filePath')
                if filepath and filepath not in seen:
                    sources.append({
                        'type': 'INCLUDE_FILE',
                        'source': filepath,
                        'statement_line': stmt['lineNumber'],
                        'isVariable': stmt.get('isVariable', False)
                    })
                    seen.add(filepath)
        
        return sources
    
    def _extract_table_definitions(self, statements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Extract logical table definitions."""
        tables = []
        load_counter = 0
        
        for stmt in statements:
            if stmt['type'] == 'LOAD':
                load_counter += 1
                table_obj = {
                    'tableNumber': load_counter,
                    'fields': stmt.get('fields', []),
                    'source': stmt.get('source'),
                    'modifiers': stmt.get('modifiers', []),
                    'conditions': stmt.get('conditions', []),
                    'lineNumber': stmt['lineNumber']
                }
                tables.append(table_obj)
        
        return tables
    
    def _extract_associations(self, statements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Detect table associations from CONCATENATE, JOIN, KEEP statements."""
        associations = []
        
        last_load_table = None
        for i, stmt in enumerate(statements):
            if stmt['type'] == 'LOAD':
                last_load_table = f"Table_{i}"
            elif stmt['type'] == 'CONCATENATE' and last_load_table:
                associations.append({
                    'type': 'CONCATENATE',
                    'fromTable': last_load_table,
                    'toTable': f"Table_{i}",
                    'lineNumber': stmt['lineNumber']
                })
            elif stmt['type'] == 'JOIN' and last_load_table:
                associations.append({
                    'type': 'JOIN',
                    'joinType': stmt.get('joinType', 'INNER'),
                    'leftTable': last_load_table,
                    'rightTable': f"Table_{i}",
                    'lineNumber': stmt['lineNumber']
                })
        
        return associations

    def _extract_control_flow(self, statements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            {
                'type': stmt.get('type'),
                'lineNumber': stmt.get('lineNumber'),
                'content': stmt.get('content'),
            }
            for stmt in statements
            if stmt.get('type') in {'FOR', 'NEXT', 'DO', 'LOOP', 'WHILE', 'IF'}
        ]

    def _extract_subroutines(self, statements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        routines = []
        current = None
        for stmt in statements:
            stmt_type = stmt.get('type')
            content = stmt.get('content', '')
            if stmt_type == 'SUB':
                match = re.search(r'SUB\s+([A-Za-z_][A-Za-z0-9_]*)', content, re.IGNORECASE)
                current = {
                    'name': match.group(1) if match else None,
                    'startLine': stmt.get('lineNumber'),
                    'statements': [content],
                }
            elif stmt_type == 'END SUB' and current:
                current['endLine'] = stmt.get('lineNumber')
                current['statements'].append(content)
                routines.append(current)
                current = None
            elif current:
                current['statements'].append(content)
        return routines

    def _count_statement_types(self, statements: List[Dict[str, Any]]) -> Dict[str, int]:
        counts = defaultdict(int)
        for stmt in statements:
            counts[stmt.get('type') or 'unknown'] += 1
        return dict(counts)
    
    def _detect_circular_references(self, statements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Detect circular variable references."""
        circulars = []
        variables = self._extract_variables(statements)
        
        def has_circular(var_name, visited=None):
            if visited is None:
                visited = set()
            
            if var_name in visited:
                return True
            
            if var_name not in variables:
                return False
            
            visited.add(var_name)
            value = variables[var_name]
            
            # Find references in value
            refs = re.findall(r'\$\((\w+)\)', value)
            for ref in refs:
                if has_circular(ref, visited.copy()):
                    return True
            
            return False
        
        for var_name in variables:
            if has_circular(var_name):
                circulars.append({
                    'variable': var_name,
                    'type': 'circular_reference'
                })
        
        return circulars
    
    def _analyze_formatting(self, script_text: str) -> Dict[str, Any]:
        """Analyze script formatting and structure."""
        lines = script_text.split('\n')
        return {
            'lineCount': len(lines),
            'characterCount': len(script_text),
            'averageLineLength': len(script_text) / len(lines) if lines else 0,
            'hasMultilineStatements': any('\n' in line for line in lines),
            'indentationStyle': self._detect_indentation(script_text),
            'commentRatio': self._calculate_comment_ratio(script_text)
        }
    
    def _detect_indentation(self, text: str) -> str:
        """Detect whether script uses spaces or tabs."""
        space_count = text.count('    ')  # 4 spaces
        tab_count = text.count('\t')
        
        if tab_count > space_count:
            return 'tabs'
        elif space_count > tab_count:
            return 'spaces'
        else:
            return 'mixed'
    
    def _calculate_comment_ratio(self, text: str) -> float:
        """Calculate ratio of comment lines to total lines."""
        lines = text.split('\n')
        comment_lines = sum(1 for line in lines if line.strip().startswith('//'))
        return (comment_lines / len(lines)) * 100 if lines else 0
    
    def _empty_result(self) -> Dict[str, Any]:
        """Return empty result structure."""
        return {
            'statements': [],
            'variables': {},
            'dataSources': [],
            'tables': [],
            'associations': [],
            'loadBlocks': [],
            'circularReferences': [],
            'issues': [],
            'comments': [],
            'controlFlow': [],
            'subroutines': [],
            'includes': [],
            'sqlBlocks': [],
            'statementTypes': {},
            'rawScript': '',
            'formatting': {
                'lineCount': 0,
                'characterCount': 0,
                'averageLineLength': 0
            }
        }


def parse_qlik_load_script(script_text: str) -> Dict[str, Any]:
    """Main entry point for comprehensive script parsing."""
    parser = LoadScriptParser()
    return parser.parse_complete_script(script_text)
