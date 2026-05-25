from .dialect import dialect_factory
from .parser import QlikParser
from .transformer import QlikTransformer
from .errors import QlikParserError, QlikTransformationError


class QlikToSqlEngine:
    """High-level engine for parsing Qlik scripts and generating SQL."""

    def __init__(self, dialect_name: str = 'spark'):
        self.dialect = dialect_factory(dialect_name)
        self.parser = QlikParser()
        self.transformer = QlikTransformer()

    def parse(self, qlik_script: str):
        return self.parser.parse(qlik_script)

    def build_plan(self, qlik_script: str):
        script = self.parse(qlik_script)
        return self.transformer.transform(script)

    def generate_sql(self, qlik_script: str) -> str:
        plan = self.build_plan(qlik_script)
        return self.dialect.render_plan(plan.nodes)

    def generate_sql_for_file(self, path: str) -> str:
        with open(path, 'r', encoding='utf-8') as stream:
            contents = stream.read()
        return self.generate_sql(contents)
