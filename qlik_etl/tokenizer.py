from dataclasses import dataclass
from typing import List

KEYWORDS = {
    'LOAD', 'SELECT', 'STORE', 'FROM', 'RESIDENT', 'CONCATENATE', 'NOCONCATENATE',
    'JOIN', 'LEFT', 'RIGHT', 'INNER', 'FULL', 'OUTER', 'APPLYMAP', 'MAPPING',
    'SET', 'LET', 'SQL', 'WHERE', 'GROUP', 'ORDER', 'BY', 'AS', 'INLINE',
    'KEEP', 'IF', 'THEN', 'ELSE', 'END', 'FOR', 'NEXT', 'DO', 'LOOP', 'CALL',
    'INCLUDE', 'SUB', 'EXIT', 'DROP', 'FIELD', 'TABLE', 'DATE', 'ADDMONTHS',
}

PUNCTUATION = {';', ',', '(', ')', '[', ']', '{', '}', ':', '.', '='}
OPERATORS = {'+', '-', '*', '/', '&', '>', '<', '>=', '<=', '<>', '!='}


@dataclass(frozen=True)
class Token:
    kind: str
    text: str
    line: int
    column: int


class QlikTokenizer:
    """Minimal Qlik tokenizer for script AST construction."""

    @classmethod
    def tokenize(cls, text: str) -> List[Token]:
        tokens: List[Token] = []
        line = 1
        column = 1
        position = 0
        length = len(text)

        while position < length:
            char = text[position]

            if char == '\n':
                line += 1
                column = 1
                position += 1
                continue

            if char.isspace():
                position += 1
                column += 1
                continue

            if text.startswith('//', position):
                end = text.find('\n', position)
                if end == -1:
                    end = length
                tokens.append(Token('COMMENT', text[position:end], line, column))
                consumed = end - position
                position = end
                column += consumed
                continue

            if text.startswith('/*', position):
                end = text.find('*/', position + 2)
                if end == -1:
                    end = length
                else:
                    end += 2
                tokens.append(Token('COMMENT', text[position:end], line, column))
                consumed = end - position
                line += text[position:end].count('\n')
                position = end
                column = 1
                continue

            if char in ('"', "'"):
                quote = char
                start_col = column
                token_text = char
                position += 1
                column += 1
                while position < length:
                    curr = text[position]
                    token_text += curr
                    position += 1
                    column += 1
                    if curr == quote:
                        break
                    if curr == '\\' and position < length:
                        token_text += text[position]
                        position += 1
                        column += 1
                tokens.append(Token('STRING', token_text, line, start_col))
                continue

            if char in PUNCTUATION:
                tokens.append(Token('PUNCTUATION', char, line, column))
                position += 1
                column += 1
                continue

            match = None
            for op in sorted(OPERATORS, key=len, reverse=True):
                if text.startswith(op, position):
                    match = op
                    break
            if match:
                tokens.append(Token('OPERATOR', match, line, column))
                position += len(match)
                column += len(match)
                continue

            if char.isalpha() or char in '_$[':
                start = position
                start_col = column
                if char == '[':
                    position += 1
                    column += 1
                    while position < length and text[position] != ']':
                        position += 1
                        column += 1
                    if position < length:
                        position += 1
                        column += 1
                    tokens.append(Token('IDENTIFIER', text[start:position], line, start_col))
                    continue
                while position < length and (text[position].isalnum() or text[position] in '_$.'):
                    position += 1
                    column += 1
                token_text = text[start:position]
                kind = 'KEYWORD' if token_text.upper() in KEYWORDS else 'IDENTIFIER'
                tokens.append(Token(kind, token_text, line, start_col))
                continue

            tokens.append(Token('UNKNOWN', char, line, column))
            position += 1
            column += 1

        tokens.append(Token('EOF', '', line, column))
        return tokens
