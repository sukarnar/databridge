"""Excel-style formula language compiled to Polars expressions.

Examples
    [Cust ID]
    TRIM(PROPER([First Name])) & " " & [Last Name]
    IF([Amount] > 1000, "large", "small")
    ROUND(TO_NUMBER([Amount]) * 1.1, 2)
    DATEVALUE([Order Date], "%m/%d/%Y")

Grammar (lowest to highest precedence)
    comparison := concat (("=" | "<>" | "<" | ">" | "<=" | ">=") concat)?
    concat     := additive ("&" additive)*
    additive   := term (("+" | "-") term)*
    term       := unary (("*" | "/") unary)*
    unary      := "-" unary | primary
    primary    := NUMBER | STRING | [column] | TRUE | FALSE | NULL | NAME "(" args ")" | "(" expr ")"

Formulas are parsed, never evaluated as Python, so they are safe to accept from users.
"""

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

import polars as pl


class FormulaError(ValueError):
    def __init__(self, message: str, pos: int | None = None):
        super().__init__(message if pos is None else f"{message} (at position {pos + 1})")
        self.pos = pos


# ------------------------------------------------------------------ tokenizer

_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<column>\[[^\]]+\])
  | (?P<number>\d+\.\d*|\.\d+|\d+)
  | (?P<string>"(?:[^"]|"")*")
  | (?P<op><>|<=|>=|[=<>&+\-*/(),])
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
    """,
    re.VERBOSE,
)


@dataclass
class Token:
    kind: str
    value: str
    pos: int


def tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    i = 0
    while i < len(text):
        m = _TOKEN_RE.match(text, i)
        if not m:
            raise FormulaError(f"Unexpected character {text[i]!r}", i)
        kind = m.lastgroup or ""
        if kind != "ws":
            tokens.append(Token(kind, m.group(), i))
        i = m.end()
    tokens.append(Token("end", "", len(text)))
    return tokens


# ------------------------------------------------------------------ AST


@dataclass
class Node:
    kind: str  # num | str | col | bool | null | call | binop | neg
    value: Any = None
    args: list["Node"] | None = None
    pos: int = 0


class _Parser:
    def __init__(self, text: str):
        self.tokens = tokenize(text)
        self.i = 0

    def peek(self) -> Token:
        return self.tokens[self.i]

    def take(self, value: str | None = None, kind: str | None = None) -> Token:
        t = self.peek()
        if (value is not None and t.value != value) or (kind is not None and t.kind != kind):
            want = value or kind
            got = t.value or "end of formula"
            raise FormulaError(f"Expected {want!r} but found {got!r}", t.pos)
        self.i += 1
        return t

    def parse(self) -> Node:
        if self.peek().kind == "end":
            raise FormulaError("Formula is empty", 0)
        node = self.comparison()
        if self.peek().kind != "end":
            raise FormulaError(f"Unexpected {self.peek().value!r}", self.peek().pos)
        return node

    def comparison(self) -> Node:
        left = self.concat()
        t = self.peek()
        if t.kind == "op" and t.value in {"=", "<>", "<", ">", "<=", ">="}:
            self.i += 1
            right = self.concat()
            return Node("binop", t.value, [left, right], t.pos)
        return left

    def concat(self) -> Node:
        node = self.additive()
        while self.peek().value == "&":
            t = self.take("&")
            node = Node("binop", "&", [node, self.additive()], t.pos)
        return node

    def additive(self) -> Node:
        node = self.term()
        while self.peek().value in {"+", "-"} and self.peek().kind == "op":
            t = self.take()
            node = Node("binop", t.value, [node, self.term()], t.pos)
        return node

    def term(self) -> Node:
        node = self.unary()
        while self.peek().value in {"*", "/"} and self.peek().kind == "op":
            t = self.take()
            node = Node("binop", t.value, [node, self.unary()], t.pos)
        return node

    def unary(self) -> Node:
        if self.peek().value == "-" and self.peek().kind == "op":
            t = self.take("-")
            return Node("neg", None, [self.unary()], t.pos)
        return self.primary()

    def primary(self) -> Node:
        t = self.peek()
        if t.kind == "number":
            self.i += 1
            return Node("num", float(t.value) if "." in t.value else int(t.value), pos=t.pos)
        if t.kind == "string":
            self.i += 1
            return Node("str", t.value[1:-1].replace('""', '"'), pos=t.pos)
        if t.kind == "column":
            self.i += 1
            return Node("col", t.value[1:-1].strip(), pos=t.pos)
        if t.value == "(":
            self.take("(")
            node = self.comparison()
            self.take(")")
            return node
        if t.kind == "name":
            self.i += 1
            upper = t.value.upper()
            if upper in {"TRUE", "FALSE"} and self.peek().value != "(":
                return Node("bool", upper == "TRUE", pos=t.pos)
            if upper == "NULL" and self.peek().value != "(":
                return Node("null", pos=t.pos)
            self.take("(")
            args: list[Node] = []
            if self.peek().value != ")":
                args.append(self.comparison())
                while self.peek().value == ",":
                    self.take(",")
                    args.append(self.comparison())
            self.take(")")
            return Node("call", upper, args, t.pos)
        raise FormulaError(f"Unexpected {t.value or 'end of formula'!r}", t.pos)


def parse(text: str) -> Node:
    return _Parser(text).parse()


def referenced_columns(text: str) -> list[str]:
    """Columns a formula reads, in order of first use. Used to draw arrows."""
    try:
        tokens = tokenize(text)
    except FormulaError:
        return []
    seen: list[str] = []
    for t in tokens:
        if t.kind == "column":
            name = t.value[1:-1].strip()
            if name not in seen:
                seen.append(name)
    return seen


# ------------------------------------------------------------------ compiler


def _s(e: pl.Expr) -> pl.Expr:
    return e.cast(pl.String)


def _num(e: pl.Expr) -> pl.Expr:
    return (
        e.cast(pl.String)
        .str.replace_all(r"[,$€£ ]", "")
        .cast(pl.Float64, strict=False)
    )


def _bool(e: pl.Expr) -> pl.Expr:
    return e.cast(pl.Boolean, strict=False).fill_null(False)


def _literal_int(node: Node, fn: str) -> int:
    if node.kind != "num" or not isinstance(node.value, int):
        raise FormulaError(f"{fn} expects a whole-number literal here", node.pos)
    return node.value


def _literal_str(node: Node, fn: str) -> str:
    if node.kind != "str":
        raise FormulaError(f"{fn} expects a text literal in quotes here", node.pos)
    return node.value


def _date_expr(e: pl.Expr, fmt: str | None) -> pl.Expr:
    as_str = e.cast(pl.String)
    if fmt:
        return as_str.str.to_date(fmt, strict=False)
    return pl.coalesce(
        as_str.str.slice(0, 10).str.to_date("%Y-%m-%d", strict=False),  # dates, datetimes, ISO text
        as_str.str.to_date("%m/%d/%Y", strict=False),
        as_str.str.to_date("%d-%b-%Y", strict=False),
        as_str.str.to_date("%d/%m/%Y", strict=False),
    )


@dataclass
class FunctionSpec:
    min_args: int
    max_args: int | None
    doc: str
    build: Callable[[list[pl.Expr], list[Node]], pl.Expr]


def _proper(a: list[pl.Expr], _n: list[Node]) -> pl.Expr:
    return _s(a[0]).str.strip_chars().str.to_titlecase()


FUNCTIONS: dict[str, FunctionSpec] = {
    "TRIM": FunctionSpec(1, 1, "Remove leading/trailing spaces", lambda a, n: _s(a[0]).str.strip_chars()),
    "UPPER": FunctionSpec(1, 1, "Upper case", lambda a, n: _s(a[0]).str.to_uppercase()),
    "LOWER": FunctionSpec(1, 1, "Lower case", lambda a, n: _s(a[0]).str.to_lowercase()),
    "PROPER": FunctionSpec(1, 1, "Title Case", _proper),
    "LEN": FunctionSpec(1, 1, "Number of characters", lambda a, n: _s(a[0]).str.len_chars()),
    "LEFT": FunctionSpec(2, 2, "LEFT(text, n)", lambda a, n: _s(a[0]).str.head(_literal_int(n[1], "LEFT"))),
    "RIGHT": FunctionSpec(2, 2, "RIGHT(text, n)", lambda a, n: _s(a[0]).str.tail(_literal_int(n[1], "RIGHT"))),
    "MID": FunctionSpec(
        3, 3, "MID(text, start, length) - start is 1-based",
        lambda a, n: _s(a[0]).str.slice(_literal_int(n[1], "MID") - 1, _literal_int(n[2], "MID")),
    ),
    "CONCAT": FunctionSpec(
        1, None, "Join values as text; blanks are skipped",
        lambda a, n: pl.concat_str([_s(x) for x in a], ignore_nulls=True),
    ),
    "REPLACE": FunctionSpec(
        3, 3, "REPLACE(text, find, replace_with)",
        lambda a, n: _s(a[0]).str.replace_all(_literal_str(n[1], "REPLACE"), _literal_str(n[2], "REPLACE"), literal=True),
    ),
    "SPLIT": FunctionSpec(
        3, 3, "SPLIT(text, separator, part) - part is 1-based",
        lambda a, n: _s(a[0]).str.split(_literal_str(n[1], "SPLIT")).list.get(
            _literal_int(n[2], "SPLIT") - 1, null_on_oob=True),
    ),
    "TO_NUMBER": FunctionSpec(1, 1, "Parse text such as '1,200.50' or '$15' to a number", lambda a, n: _num(a[0])),
    "ROUND": FunctionSpec(
        1, 2, "ROUND(number, digits)",
        lambda a, n: _num(a[0]).round(_literal_int(n[1], "ROUND") if len(n) > 1 else 0),
    ),
    "ABS": FunctionSpec(1, 1, "Absolute value", lambda a, n: _num(a[0]).abs()),
    "DATEVALUE": FunctionSpec(
        1, 2, "DATEVALUE(text, format) e.g. \"%m/%d/%Y\"",
        lambda a, n: _date_expr(a[0], _literal_str(n[1], "DATEVALUE") if len(n) > 1 else None),
    ),
    "TEXT": FunctionSpec(
        2, 2, "TEXT(date, format) e.g. \"%Y-%m\"",
        lambda a, n: _date_expr(a[0], None).dt.strftime(_literal_str(n[1], "TEXT")),
    ),
    "YEAR": FunctionSpec(1, 1, "Year of a date", lambda a, n: _date_expr(a[0], None).dt.year()),
    "MONTH": FunctionSpec(1, 1, "Month of a date", lambda a, n: _date_expr(a[0], None).dt.month()),
    "DAY": FunctionSpec(1, 1, "Day of a date", lambda a, n: _date_expr(a[0], None).dt.day()),
    "TODAY": FunctionSpec(0, 0, "Today's date", lambda a, n: pl.lit(date.today())),
    "IF": FunctionSpec(
        2, 3, "IF(condition, then, else)",
        lambda a, n: pl.when(a[0]).then(a[1]).otherwise(a[2] if len(a) > 2 else pl.lit(None)),
    ),
    "COALESCE": FunctionSpec(1, None, "First non-blank value", lambda a, n: pl.coalesce(a)),
    "AND": FunctionSpec(1, None, "AND(condition, condition, ...) - true when all are true",
                        lambda a, n: pl.all_horizontal([_bool(x) for x in a])),
    "OR": FunctionSpec(1, None, "OR(condition, condition, ...) - true when any is true",
                       lambda a, n: pl.any_horizontal([_bool(x) for x in a])),
    "NOT": FunctionSpec(1, 1, "NOT(condition)", lambda a, n: ~_bool(a[0])),
    "CONTAINS": FunctionSpec(
        2, 2, "CONTAINS(text, find) - case-insensitive",
        lambda a, n: _s(a[0]).str.to_lowercase().str.contains(_literal_str(n[1], "CONTAINS").lower(), literal=True),
    ),
    "IN": FunctionSpec(2, None, "IN(value, option1, option2, ...)",
                       lambda a, n: _s(a[0]).is_in([str(x.value) for x in n[1:]])),
    "ISBLANK": FunctionSpec(
        1, 1, "True when blank or only spaces",
        lambda a, n: a[0].is_null() | (_s(a[0]).str.strip_chars() == ""),
    ),
}


def compile_formula(text: str, available: set[str] | None = None) -> pl.Expr:
    """Compiles formula text to a Polars expression. `available` validates column names."""
    return _compile(parse(text), available)


def _compile(node: Node, available: set[str] | None) -> pl.Expr:
    k = node.kind
    if k == "num":
        return pl.lit(node.value)
    if k == "str":
        return pl.lit(node.value, dtype=pl.String)
    if k == "bool":
        return pl.lit(node.value)
    if k == "null":
        return pl.lit(None)
    if k == "col":
        if available is not None and node.value not in available:
            raise FormulaError(f"Unknown column [{node.value}]", node.pos)
        return pl.col(node.value)
    if k == "neg":
        return -_numeric_operand(node.args[0], available)
    if k == "binop":
        left, right = node.args
        op = node.value
        if op == "&":
            return pl.concat_str(
                [_s(_compile(left, available)).fill_null(""), _s(_compile(right, available)).fill_null("")]
            )
        if op in {"+", "-", "*", "/"}:
            a, b = _numeric_operand(left, available), _numeric_operand(right, available)
            return {"+": a + b, "-": a - b, "*": a * b, "/": a / b}[op]
        a, b = _compile(left, available), _compile(right, available)
        if left.kind == "str" or right.kind == "str":
            a, b = _s(a), _s(b)
        elif left.kind == "num" or right.kind == "num":  # [col] >= 4 works for text columns holding numbers
            a = a if left.kind == "num" else _num(a)
            b = b if right.kind == "num" else _num(b)
        return {"=": a == b, "<>": a != b, "<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[op]
    if k == "call":
        spec = FUNCTIONS.get(node.value)
        if not spec:
            raise FormulaError(f"Unknown function {node.value}", node.pos)
        n = len(node.args or [])
        if n < spec.min_args or (spec.max_args is not None and n > spec.max_args):
            raise FormulaError(f"{node.value} takes {spec.doc}", node.pos)
        exprs = [_compile(a, available) for a in node.args or []]
        return spec.build(exprs, node.args or [])
    raise FormulaError(f"Cannot compile {k}", node.pos)


def _numeric_operand(node: Node, available: set[str] | None) -> pl.Expr:
    e = _compile(node, available)
    if node.kind == "num":
        return e
    return _num(e) if node.kind in {"col", "str"} else e
