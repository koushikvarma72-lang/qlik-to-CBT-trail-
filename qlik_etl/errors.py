class QlikParserError(Exception):
    """Raised when the Qlik parser encounters invalid syntax or ambiguity."""


class QlikTransformationError(Exception):
    """Raised when a Qlik AST cannot be transformed into a SQL plan."""
