# -*- coding: utf-8 -*-
"""
Advanced Expression & Visualization Extractor
==============================================

Specialized extraction for:
- Complex Qlik expressions (set analysis, aggregations, conditionals)
- Visualization properties and structure
- KPI definitions and calculations
- Alternate states and conditional logic
- Master dimensions and measures
- Extensions and custom objects
"""

import re
from typing import Dict, List, Any, Tuple, Set
from collections import defaultdict


class ExpressionPreserver:
    """Preserve and analyze Qlik expressions without modification."""
    
    # Qlik aggregation functions
    AGGREGATION_FUNCTIONS = {
        'Sum', 'Count', 'Avg', 'Min', 'Max', 'StDev', 'Variance',
        'Concat', 'StringConcat', 'Mode', 'Median', 'FirstSortedValue',
        'LastSortedValue', 'Correl', 'Covar', 'Skew', 'Kurtosis'
    }
    
    # Qlik functions that can be used in expressions
    QLIK_FUNCTIONS = {
        'Sum', 'Count', 'Avg', 'Min', 'Max', 'Upper', 'Lower', 'Len',
        'SubString', 'Index', 'Replace', 'Today', 'Now', 'Date',
        'Timestamp', 'Year', 'Month', 'Day', 'Hour', 'Minute', 'Second',
        'GetAlternateStateValue', 'GetMetadata', 'If', 'Match', 'WildMatch',
        'Keep', 'Drop', 'Exists', 'IsNull', 'IsEmpty', 'IsTextual',
        'IsNumeric', 'Peek', 'Previous', 'RowNo', 'FieldValue', 'RecNo'
    }
    
    def __init__(self):
        self.expressions_found = []
        self.set_analysis_expressions = []
        self.conditional_expressions = []
        self.color_expressions = []
        self.drill_expressions = []
    
    def extract_and_preserve_expressions(self, text: str) -> List[Dict[str, Any]]:
        """
        Extract all expressions from text while preserving exact syntax.
        
        Identifies:
        - Set analysis expressions: Sum({<Year={2025}>} Sales)
        - Aggregation expressions: Sum(Sales) / Count(Customers)
        - Conditional expressions: If(Status='Active', Qty, 0)
        - Color expressions: RGB rules and custom colors
        - Drill expressions: Multi-level drill-down definitions
        """
        if not text:
            return []
        
        expressions = []
        
        # Pattern for set analysis: {< ... >}
        set_analysis_pattern = r'\{\s*<[^>]*>\s*\}'
        for match in re.finditer(set_analysis_pattern, text):
            expr = match.group(0)
            expressions.append({
                'type': 'set_analysis',
                'expression': expr,
                'offset': match.start(),
                'length': len(expr),
                'components': self._parse_set_analysis(expr)
            })
        
        # Pattern for aggregation functions
        for func in self.AGGREGATION_FUNCTIONS:
            pattern = rf'{func}\s*\([^)]*\)'
            for match in re.finditer(pattern, text, re.IGNORECASE):
                expr = match.group(0)
                expressions.append({
                    'type': 'aggregation',
                    'function': func,
                    'expression': expr,
                    'offset': match.start()
                })
        
        # Pattern for If conditions
        if_pattern = r'If\s*\([^)]*[,;][^)]*[,;][^)]*\)'
        for match in re.finditer(if_pattern, text, re.IGNORECASE):
            expr = match.group(0)
            expressions.append({
                'type': 'conditional',
                'expression': expr,
                'offset': match.start(),
                'condition': self._extract_if_condition(expr)
            })
        
        return expressions
    
    def _parse_set_analysis(self, expr: str) -> Dict[str, Any]:
        """Parse set analysis expression: {< field1={val1}, field2={val2} >}"""
        result = {
            'type': 'set_analysis',
            'fields': [],
            'modifiers': []
        }
        
        # Extract field selections
        field_pattern = r'(\w+)\s*=\s*\{([^}]*)\}'
        for match in re.finditer(field_pattern, expr):
            field_name = match.group(1)
            values = match.group(2).split(',')
            result['fields'].append({
                'name': field_name,
                'values': [v.strip() for v in values if v.strip()]
            })
        
        # Extract modifiers ($1, $2, etc.)
        modifier_pattern = r'(\$\d+)'
        modifiers = re.findall(modifier_pattern, expr)
        result['modifiers'] = modifiers
        
        return result
    
    def _extract_if_condition(self, expr: str) -> Dict[str, Any]:
        """Extract If condition components."""
        result = {
            'condition': None,
            'true_value': None,
            'false_value': None
        }
        
        # Simple extraction - may need more sophisticated parsing
        parts = re.split(r'[,;]', expr[3:-1])  # Remove "If(" and ")"
        if len(parts) >= 3:
            result['condition'] = parts[0].strip()
            result['true_value'] = parts[1].strip()
            result['false_value'] = parts[2].strip()
        
        return result
    
    def categorize_expression(self, expression: str) -> str:
        """Determine the category/type of an expression."""
        expr_upper = expression.upper()
        
        if '{<' in expression:
            return 'set_analysis'
        elif any(f'SUM' in expr_upper or f'COUNT' in expr_upper or 
                f'AVG' in expr_upper for f in self.AGGREGATION_FUNCTIONS):
            return 'aggregation'
        elif 'IF(' in expr_upper:
            return 'conditional'
        elif 'RGB(' in expr_upper or 'HEX(' in expr_upper:
            return 'color'
        elif 'DRILLDOWN' in expr_upper or 'ALTERNATE' in expr_upper:
            return 'drill'
        else:
            return 'custom'
    
    def extract_field_references(self, expression: str) -> Set[str]:
        """
        Extract field references from an expression.
        Identifies [Field Name], [Field], FieldName patterns.
        """
        fields = set()
        
        # Pattern for bracketed field names: [Field Name]
        pattern1 = r'\[([^\]]+)\]'
        fields.update(re.findall(pattern1, expression))
        
        # Pattern for unquoted field names (word boundary)
        pattern2 = r'\b([A-Za-z_]\w*)\b'
        candidates = re.findall(pattern2, expression)
        for candidate in candidates:
            # Filter out Qlik functions
            if candidate not in self.QLIK_FUNCTIONS:
                fields.add(candidate)
        
        return fields
    
    def preserve_formatting(self, expression: str) -> Dict[str, Any]:
        """Preserve expression formatting details."""
        return {
            'expression': expression,
            'length': len(expression),
            'hasLineBreaks': '\n' in expression,
            'hasComments': '//' in expression or '/*' in expression,
            'indentation': len(expression) - len(expression.lstrip()),
            'spaces': expression.count(' '),
            'tabs': expression.count('\t')
        }


class VisualizationExtractor:
    """Extract complete visualization and sheet structure."""
    
    CHART_TYPES = {
        'bar', 'line', 'scatter', 'pie', 'table', 'pivot', 'gauge',
        'map', 'combo', 'area', 'waterfall', 'funnel', 'network', 'kpi'
    }
    
    def __init__(self):
        self.visualizations = []
        self.sheets = []
        self.containers = []
    
    def extract_visualizations_comprehensive(self, metadata: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Extract visualization objects with complete property preservation.
        """
        visualizations = []
        
        viz_data = metadata.get('visualizations', []) or []
        for viz in viz_data:
            viz_obj = {
                'id': viz.get('id', f"viz_{len(visualizations)}"),
                'type': self._infer_chart_type(viz.get('type', 'unknown')),
                'title': viz.get('title', ''),
                'description': viz.get('description', ''),
                'sheetId': viz.get('sheetId'),
                'visualizationGrid': self._extract_grid_properties(viz),
                'data': self._extract_data_properties(viz),
                'appearance': self._extract_appearance_properties(viz),
                'behavior': self._extract_behavior_properties(viz),
                'rawProperties': viz.get('qProperty', {}),
                'qHyperCubeDef': viz.get('qHyperCubeDef', {}),
                'qLayout': viz.get('qLayout', {}),
                'qMeta': viz.get('qMeta', {}),
                'qInfo': viz.get('qInfo', {})
            }
            visualizations.append(viz_obj)
        
        return visualizations
    
    def _infer_chart_type(self, chart_type: str) -> str:
        """Infer and normalize chart type."""
        if not chart_type:
            return 'unknown'
        
        chart_type_lower = chart_type.lower()
        for known_type in self.CHART_TYPES:
            if known_type in chart_type_lower:
                return known_type
        
        return chart_type
    
    def _extract_grid_properties(self, viz: Dict[str, Any]) -> Dict[str, Any]:
        """Extract layout grid properties."""
        return {
            'x': viz.get('x', 0),
            'y': viz.get('y', 0),
            'width': viz.get('width', 4),
            'height': viz.get('height', 4),
            'zOrder': viz.get('zOrder', 0),
            'isVisible': not viz.get('isHidden', False),
            'rotation': viz.get('rotation', 0)
        }
    
    def _extract_data_properties(self, viz: Dict[str, Any]) -> Dict[str, Any]:
        """Extract data definition properties."""
        return {
            'dimensions': viz.get('dimensions', []),
            'measures': viz.get('measures', []),
            'sortOrder': viz.get('sortOrder', []),
            'sorting': self._extract_sorting_rules(viz),
            'drilling': viz.get('drilling', []),
            'drillDownExpression': viz.get('drillDownExpression'),
            'alternateStates': viz.get('alternateStates', []),
            'dataBindings': viz.get('dataBindings', [])
        }
    
    def _extract_appearance_properties(self, viz: Dict[str, Any]) -> Dict[str, Any]:
        """Extract appearance/styling properties."""
        return {
            'colorExpression': viz.get('colorExpression'),
            'colorScheme': viz.get('colorScheme'),
            'labels': viz.get('labels', {}),
            'legend': viz.get('legend', {}),
            'conditionalVisibility': viz.get('showCondition'),
            'theme': viz.get('theme'),
            'customFormatting': viz.get('customFormatting', {})
        }
    
    def _extract_behavior_properties(self, viz: Dict[str, Any]) -> Dict[str, Any]:
        """Extract behavior/interaction properties."""
        return {
            'interactionMode': viz.get('interactionMode', 'normal'),
            'selections': viz.get('selections', []),
            'onSelect': viz.get('onSelect'),
            'hyperlinks': viz.get('hyperlinks', []),
            'drillTargets': viz.get('drillTargets', []),
            'bookmarkable': viz.get('bookmarkable', True)
        }
    
    def _extract_sorting_rules(self, viz: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract sorting rules in priority order."""
        sorting = []
        sort_order = viz.get('sortOrder', [])
        
        for i, sort_rule in enumerate(sort_order):
            if isinstance(sort_rule, dict):
                sorting.append({
                    'priority': i,
                    'field': sort_rule.get('field'),
                    'direction': sort_rule.get('direction', 'asc'),
                    'expression': sort_rule.get('expression')
                })
        
        return sorting
    
    def extract_sheets_comprehensive(self, metadata: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract sheet objects with complete structure."""
        sheets = []
        sheets_data = metadata.get('sheets', []) or []
        
        for sheet in sheets_data:
            sheet_obj = {
                'id': sheet.get('id', f"sheet_{len(sheets)}"),
                'title': sheet.get('title') or sheet.get('name', ''),
                'description': sheet.get('description', ''),
                'order': sheet.get('order', len(sheets)),
                'isHidden': sheet.get('isHidden', False),
                'layout': sheet.get('layout', 'grid'),
                'gridLayout': sheet.get('gridLayout', {}),
                'visualizationIds': sheet.get('visualizationIds', []),
                'visualizations': sheet.get('visualizations', []),
                'containers': self._extract_containers(sheet),
                'navigation': sheet.get('navigation', {}),
                'rawProperties': sheet.get('qProperty', {}),
                'qMeta': sheet.get('qMeta', {}),
                'qInfo': sheet.get('qInfo', {})
            }
            sheets.append(sheet_obj)
        
        return sheets
    
    def _extract_containers(self, sheet: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract container objects within sheet."""
        containers = []
        container_data = sheet.get('containers', [])
        
        for container in container_data:
            container_obj = {
                'id': container.get('id'),
                'type': container.get('type', 'unknown'),
                'title': container.get('title'),
                'items': container.get('items', []),
                'layout': container.get('layout', {}),
                'appearance': container.get('appearance', {})
            }
            containers.append(container_obj)
        
        return containers


class MasterObjectExtractor:
    """Extract master dimensions and measures."""
    
    def __init__(self):
        self.master_dimensions = []
        self.master_measures = []
    
    def extract_master_dimensions(self, metadata: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract master dimension library definitions."""
        dimensions = []
        dims_data = metadata.get('masterDimensions', []) or metadata.get('dimensions', [])
        
        for dim in dims_data:
            if not dim.get('qLibraryId'):  # Skip if not a master object
                continue
            
            dim_obj = {
                'id': dim.get('qLibraryId') or dim.get('id'),
                'name': dim.get('name', ''),
                'description': dim.get('description', ''),
                'expression': dim.get('expression', ''),
                'orderByExpression': dim.get('orderByExpression'),
                'isDefault': dim.get('isDefault', False),
                'isMaster': True,
                'rawProperties': dim.get('qProperty', {}),
                'usageCount': 0  # Could be calculated from visualizations
            }
            dimensions.append(dim_obj)
        
        return dimensions
    
    def extract_master_measures(self, metadata: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract master measure library definitions."""
        measures = []
        meas_data = metadata.get('masterMeasures', []) or metadata.get('measures', [])
        
        for meas in meas_data:
            if not meas.get('qLibraryId'):  # Skip if not a master object
                continue
            
            meas_obj = {
                'id': meas.get('qLibraryId') or meas.get('id'),
                'name': meas.get('name', ''),
                'description': meas.get('description', ''),
                'expression': meas.get('expression', ''),
                'aggregationFunction': meas.get('aggregationFunction'),
                'formatString': meas.get('formatString'),
                'colorExpression': meas.get('colorExpression'),
                'isDefault': meas.get('isDefault', False),
                'isMaster': True,
                'rawProperties': meas.get('qProperty', {}),
                'usageCount': 0  # Could be calculated from visualizations
            }
            measures.append(meas_obj)
        
        return measures


def extract_advanced_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """
    Main entry point for advanced metadata extraction.
    
    Returns structured data including:
    - Preserved expressions
    - Visualizations with full properties
    - Master dimensions and measures
    - KPI definitions
    - Alternate states
    """
    result = {
        'expressions': [],
        'visualizations': [],
        'sheets': [],
        'masterDimensions': [],
        'masterMeasures': [],
        'alternateStates': [],
        'kpis': [],
        'extensions': []
    }
    
    # Extract expressions
    expr_preserver = ExpressionPreserver()
    if metadata.get('expressions'):
        result['expressions'] = [
            {
                'id': f"expr_{i}",
                'content': expr,
                'preserved': expr_preserver.preserve_formatting(expr),
                'category': expr_preserver.categorize_expression(expr),
                'fieldReferences': list(expr_preserver.extract_field_references(expr))
            }
            for i, expr in enumerate(metadata.get('expressions', []))
        ]
    
    # Extract visualizations
    viz_extractor = VisualizationExtractor()
    result['visualizations'] = viz_extractor.extract_visualizations_comprehensive(metadata)
    result['sheets'] = viz_extractor.extract_sheets_comprehensive(metadata)
    
    # Extract master objects
    master_extractor = MasterObjectExtractor()
    result['masterDimensions'] = master_extractor.extract_master_dimensions(metadata)
    result['masterMeasures'] = master_extractor.extract_master_measures(metadata)
    
    # Extract alternate states
    result['alternateStates'] = metadata.get('alternateStates', [])
    
    # Extract KPIs
    result['kpis'] = metadata.get('kpis', [])
    
    # Extract extensions
    result['extensions'] = metadata.get('extensions', [])
    
    return result
