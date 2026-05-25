from dataclasses import dataclass, field
from typing import Dict, List, Optional
import re

from .ast import ApplyMapStatement, DropFieldsStatement, LoadStatement, QlikScript, Statement
from .errors import QlikTransformationError


@dataclass
class TransformationNode:
    name: str
    operation: str
    statement: Statement
    source_names: List[str] = field(default_factory=list)
    fields: List[str] = field(default_factory=list)
    filters: List[str] = field(default_factory=list)
    group_by: Optional[str] = None
    order_by: Optional[str] = None
    join_type: Optional[str] = None
    prefix: Optional[str] = None
    join_target: Optional[str] = None
    source_type: Optional[str] = None
    drop_fields: List[str] = field(default_factory=list)
    join_keys: List[str] = field(default_factory=list)
    raw: str = ''


@dataclass
class TransformationPlan:
    nodes: List[TransformationNode] = field(default_factory=list)
    dependency_graph: Dict[str, List[str]] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)


class QlikTransformer:
    """Build an execution plan from Qlik AST objects."""

    def transform(self, script: QlikScript) -> 'TransformationPlan':
        plan = TransformationPlan()
        seen_fields: Dict[str, set] = {}
        for statement in script.statements:
            if isinstance(statement, LoadStatement):
                node = self._transform_load(statement)
                plan.nodes.append(node)
                # record dependencies but exclude self-references to avoid artificial cycles
                deps = [s for s in node.source_names if s != node.name]
                existing = plan.dependency_graph.get(node.name, [])
                # preserve order and uniqueness
                merged = existing + [d for d in deps if d not in existing]
                plan.dependency_graph[node.name] = merged
                # extract simple field names for tracking
                simple_names = self._extract_simple_field_names(statement.fields)
                # if concatenate into existing table, merge fields
                if node.name:
                    existing = seen_fields.get(node.name, set())
                    seen_fields[node.name] = existing.union(simple_names)
                # if join, infer join keys from target's seen fields
                if node.operation == 'JOIN' and node.join_target:
                    target_fields = seen_fields.get(node.join_target, set())
                    node.join_keys = sorted(list(target_fields.intersection(simple_names)))
                # record fields for new nodes
                if node.name and node.name not in seen_fields:
                    seen_fields[node.name] = set(simple_names)
            elif isinstance(statement, DropFieldsStatement):
                node = self._transform_drop_fields(statement)
                plan.nodes.append(node)
                deps = [s for s in node.source_names if s != node.name]
                existing = plan.dependency_graph.get(node.name, [])
                merged = existing + [d for d in deps if d not in existing]
                plan.dependency_graph[node.name] = merged
            elif isinstance(statement, ApplyMapStatement):
                plan.warnings.append('ApplyMap expressions are preserved as statement metadata; explicit join keys may require manual review.')
            else:
                continue

        plan.nodes = self._order_nodes(plan.nodes, plan.dependency_graph)
        return plan

    @staticmethod
    def _extract_simple_field_names(fields: List[str]) -> set:
        names = set()
        if not fields:
            return names
        for f in fields:
            if not f:
                continue
            s = f.strip()
            # handle AS alias (bracketed, quoted, or bare)
            as_match = re.search(r"\bAS\b\s*(?:\[(?P<br>[^\]]+)\]|\"(?P<dq>[^\"]+)\"|'(?P<sq>[^']+)'|(?P<plain>\w+))\s*$", s, flags=re.IGNORECASE)
            if as_match:
                alias = as_match.group('br') or as_match.group('dq') or as_match.group('sq') or as_match.group('plain')
                if alias:
                    names.add(alias.strip())
                    continue
            # bracketed identifier elsewhere
            br = re.search(r'\[([^\]]+)\]', s)
            if br:
                names.add(br.group(1).strip())
                continue
            # quoted identifier
            dq = re.search(r'"([^"]+)"', s)
            if dq:
                names.add(dq.group(1).strip())
                continue
            # plain identifier: take leading token before space or function paren
            plain = re.match(r'([A-Za-z_][A-Za-z0-9_]*)', s)
            if plain:
                names.add(plain.group(1))
                continue
        return names

    def _transform_load(self, stmt: LoadStatement) -> TransformationNode:
        if not stmt.target_table and not stmt.join_target:
            raise QlikTransformationError(f'LOAD statement at line {stmt.line_number} has no target table name.')

        operation = 'LOAD'
        if stmt.prefix and stmt.prefix.upper() == 'CONCATENATE':
            operation = 'CONCATENATE'
        elif stmt.prefix and 'JOIN' in stmt.prefix.upper():
            operation = 'JOIN'

        source_names: List[str] = []
        if operation in {'CONCATENATE', 'JOIN'} and stmt.join_target:
            source_names.append(stmt.join_target)
        # avoid recording the target table as its own dependency (e.g., FROM Customers into Customers)
        target_name = stmt.target_table or stmt.join_target
        if stmt.source and stmt.source not in source_names and stmt.source != target_name:
            source_names.append(stmt.source)
        if stmt.resident_table and stmt.resident_table not in source_names and stmt.resident_table != target_name:
            source_names.append(stmt.resident_table)
        # do not set self as source for simple LOADs (external sources have no dependency)

        return TransformationNode(
            name=stmt.target_table or stmt.join_target,
            operation=operation,
            statement=stmt,
            source_names=source_names,
            fields=stmt.fields,
            filters=stmt.filters,
            group_by=stmt.group_by,
            order_by=stmt.order_by,
            join_type=stmt.join_type,
            prefix=stmt.prefix,
            join_target=stmt.join_target,
            source_type=stmt.source_type,
            raw=stmt.raw,
        )

    def _transform_drop_fields(self, stmt: DropFieldsStatement) -> TransformationNode:
        if not stmt.target_table:
            raise QlikTransformationError(f'DROP FIELDS statement at line {stmt.line_number} has no target table.')

        return TransformationNode(
            name=stmt.target_table,
            operation='DROP_FIELDS',
            statement=stmt,
            source_names=[stmt.target_table],
            drop_fields=stmt.fields,
            raw=stmt.raw,
        )

    @staticmethod
    def _order_nodes(nodes: List[TransformationNode], dependency_graph: Dict[str, List[str]]) -> List[TransformationNode]:
        ordered = []
        visited = {}

        def visit(node_name: str):
            if visited.get(node_name) == 'temporary':
                raise QlikTransformationError(f'Circular dependency detected: {node_name}')
            if visited.get(node_name) == 'permanent':
                return
            visited[node_name] = 'temporary'
            for dependency in dependency_graph.get(node_name, []):
                visit(dependency)
            visited[node_name] = 'permanent'
            # append all nodes that share this name (preserve original ordering)
            for n in nodes:
                if n.name == node_name and n not in ordered:
                    ordered.append(n)

        for node in nodes:
            visit(node.name)
        return ordered
