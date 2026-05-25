from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any


@dataclass
class SourceReference:
    name: str
    type: str
    raw: str


@dataclass
class Statement:
    type: str
    raw: str
    line_number: int
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class VariableStatement(Statement):
    variable_name: str = ''
    expression: str = ''
    assignment_type: str = 'SET'


@dataclass
class LoadStatement(Statement):
    label: Optional[str] = None
    prefix: Optional[str] = None
    target_table: Optional[str] = None
    source: Optional[str] = None
    source_type: Optional[str] = None
    resident_table: Optional[str] = None
    fields: List[str] = field(default_factory=list)
    filters: List[str] = field(default_factory=list)
    group_by: Optional[str] = None
    order_by: Optional[str] = None
    join_type: Optional[str] = None
    join_target: Optional[str] = None
    is_mapping: bool = False
    is_applymap: bool = False
    is_inline: bool = False
    raw_fields: List[str] = field(default_factory=list)


@dataclass
class ApplyMapStatement(Statement):
    map_name: str = ''
    source_field: str = ''
    default_value: Optional[str] = None
    target_field: Optional[str] = None


@dataclass
class DropFieldsStatement(Statement):
    fields: List[str] = field(default_factory=list)
    target_table: Optional[str] = None


@dataclass
class MappingLoadStatement(LoadStatement):
    mapping_name: Optional[str] = None


@dataclass
class SqlPassThroughStatement(Statement):
    sql_text: str = ''


@dataclass
class OtherStatement(Statement):
    keyword: str = ''


@dataclass
class QlikScript:
    statements: List[Statement] = field(default_factory=list)
    variables: Dict[str, str] = field(default_factory=dict)
    issues: List[Dict[str, Any]] = field(default_factory=list)
    raw_script: str = ''
