"""Safe, small evaluator for the TongDaXin formula subset used by the tool.

This is deliberately an expression evaluator, not Python execution.  Formula
definitions are stored as text and are parsed into a limited AST supporting the
usual market-series functions (REF, MA, HHV, LLV, SUM, COUNT, EVERY, CROSS,
IF and FINANCE).  Unsupported functions fail with a clear message instead of
silently running a different strategy.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Any


class FormulaSyntaxError(ValueError):
    """Raised when a saved formula cannot be parsed or evaluated safely."""


Token = tuple[str, str]
Node = tuple[Any, ...]
Scalar = float | None
Value = Scalar | list[Scalar]

_IDENT_RE = re.compile(r"[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]*")
_NUMBER_RE = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_OPERATORS = (":=", ">=", "<=", "<>", "!=", "==", "&&", "||", "+", "-", "*", "/", "%", "^", ">", "<", "=", ":", "&", "|", "!", "(", ")", ",", ";")
_SUPPORTED_FUNCTIONS = frozenset(
    {
        "ABS",
        "ATAN",
        "BARSLAST",
        "BARSCOUNT",
        "CROSS",
        "COUNT",
        "EVERY",
        "EXIST",
        "EMA",
        "EXP",
        "FINANCE",
        "HHV",
        "IF",
        "LLV",
        "LOG",
        "MA",
        "MAX",
        "MIN",
        "POW",
        "REF",
        "SIN",
        "SMA",
        "SQRT",
        "STD",
        "SUM",
    }
)


def _name_key(value: object) -> str:
    return str(value or "").strip().upper()


def _tokenize(formula: str) -> list[Token]:
    text = str(formula or "").replace("\\_", "_")
    text = (
        text.replace("；", ";")
        .replace("，", ",")
        .replace("（", "(")
        .replace("）", ")")
        .replace("：", ":")
    )
    text = re.sub(r"\{.*?\}", "", text, flags=re.DOTALL)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//[^\r\n]*", "", text)
    # TongDaXin users commonly copy logical operators as C-style symbols.
    # Keep the original AND/OR spelling supported as well.
    tokens: list[Token] = []
    index = 0
    while index < len(text):
        if text[index].isspace():
            index += 1
            continue
        number = _NUMBER_RE.match(text, index)
        if number:
            tokens.append(("NUMBER", number.group(0)))
            index = number.end()
            continue
        identifier = _IDENT_RE.match(text, index)
        if identifier:
            tokens.append(("IDENT", identifier.group(0)))
            index = identifier.end()
            continue
        operator = next((value for value in _OPERATORS if text.startswith(value, index)), None)
        if operator is not None:
            tokens.append(("OP", operator))
            index += len(operator)
            continue
        raise FormulaSyntaxError(f"公式包含无法识别的字符：{text[index]!r}")
    tokens.append(("EOF", ""))
    return tokens


@dataclass
class _TokenStream:
    tokens: list[Token]
    index: int = 0

    def peek(self, offset: int = 0) -> Token:
        position = min(self.index + offset, len(self.tokens) - 1)
        return self.tokens[position]

    def take(self) -> Token:
        token = self.peek()
        self.index += 1
        return token

    def accept(self, value: str) -> bool:
        if self.peek()[1] == value:
            self.take()
            return True
        return False


class _Parser:
    def __init__(self, formula: str):
        self.stream = _TokenStream(_tokenize(formula))

    def parse(self) -> tuple[dict[str, Node], Node, set[str]]:
        assignments: dict[str, Node] = {}
        final: Node | None = None
        calls: set[str] = set()
        while self.stream.peek()[0] != "EOF":
            self.stream.accept(";")
            if self.stream.peek()[0] == "EOF":
                break
            if (
                self.stream.peek()[0] == "IDENT"
                and self.stream.peek(1)[0] == "OP"
                and self.stream.peek(1)[1] in {":=", ":", "="}
            ):
                name = self.stream.take()[1]
                self.stream.take()
                expression = self.parse_or()
                assignments[_name_key(name)] = expression
                if _name_key(name) == "选股":
                    final = expression
                elif final is None:
                    final = expression
            else:
                final = self.parse_or()
            if self.stream.peek()[1] not in {";", ""}:
                token = self.stream.peek()
                raise FormulaSyntaxError(f"公式在 {token[1]!r} 附近缺少分号或运算符")
            self.stream.accept(";")
        if final is None:
            raise FormulaSyntaxError("公式没有最终选股条件")
        _collect_calls(final, calls)
        for expression in assignments.values():
            _collect_calls(expression, calls)
        return assignments, final, calls

    def parse_or(self) -> Node:
        node = self.parse_and()
        while True:
            token = self.stream.peek()
            if token[1] in {"|", "||"} or (token[0] == "IDENT" and _name_key(token[1]) == "OR"):
                self.stream.take()
                node = ("binary", "OR", node, self.parse_and())
            else:
                return node

    def parse_and(self) -> Node:
        node = self.parse_compare()
        while True:
            token = self.stream.peek()
            if token[1] in {"&", "&&"} or (token[0] == "IDENT" and _name_key(token[1]) == "AND"):
                self.stream.take()
                node = ("binary", "AND", node, self.parse_compare())
            else:
                return node

    def parse_compare(self) -> Node:
        node = self.parse_add()
        while self.stream.peek()[1] in {">", "<", ">=", "<=", "<>", "!=", "==", "="}:
            operator = self.stream.take()[1]
            node = ("binary", operator, node, self.parse_add())
        return node

    def parse_add(self) -> Node:
        node = self.parse_mul()
        while self.stream.peek()[1] in {"+", "-"}:
            operator = self.stream.take()[1]
            node = ("binary", operator, node, self.parse_mul())
        return node

    def parse_mul(self) -> Node:
        node = self.parse_unary()
        while self.stream.peek()[1] in {"*", "/", "%", "^"}:
            operator = self.stream.take()[1]
            node = ("binary", operator, node, self.parse_unary())
        return node

    def parse_unary(self) -> Node:
        token = self.stream.peek()
        if token[1] in {"+", "-"}:
            self.stream.take()
            return ("unary", token[1], self.parse_unary())
        if token[1] == "!" or (token[0] == "IDENT" and _name_key(token[1]) == "NOT"):
            self.stream.take()
            return ("unary", "NOT", self.parse_unary())
        return self.parse_primary()

    def parse_primary(self) -> Node:
        kind, value = self.stream.peek()
        if kind == "NUMBER":
            self.stream.take()
            return ("number", float(value))
        if kind == "IDENT":
            self.stream.take()
            if self.stream.accept("("):
                args: list[Node] = []
                if not self.stream.accept(")"):
                    while True:
                        args.append(self.parse_or())
                        if self.stream.accept(")"):
                            break
                        if not self.stream.accept(","):
                            raise FormulaSyntaxError(f"函数 {value} 的参数之间缺少逗号")
                return ("call", _name_key(value), tuple(args))
            return ("var", _name_key(value))
        if self.stream.accept("("):
            expression = self.parse_or()
            if not self.stream.accept(")"):
                raise FormulaSyntaxError("公式括号不匹配")
            return expression
        raise FormulaSyntaxError(f"公式在 {value!r} 附近缺少表达式")


def _collect_calls(node: Node, result: set[str]) -> None:
    if node[0] == "call":
        result.add(str(node[1]))
        for child in node[2]:
            _collect_calls(child, result)
    elif node[0] in {"binary", "unary"}:
        for child in node[2:]:
            _collect_calls(child, result)


def compile_formula(formula: str) -> tuple[dict[str, Node], Node, set[str]]:
    """Parse a formula and return assignments, final condition and functions."""
    assignments, final, calls = _Parser(formula).parse()
    if "DYNAINFO" in calls:
        raise FormulaSyntaxError("DYNAINFO是实时行情函数，本地日线/周线/分钟文件不能提供该动态值；已停止执行，不再用收盘价或VOL/MA(VOL,5)近似替代。请使用本地K线支持的条件。")
    unknown = sorted(calls - _SUPPORTED_FUNCTIONS)
    if unknown:
        raise FormulaSyntaxError("暂不支持这些函数：" + "、".join(unknown))
    return assignments, final, calls


def validate_formula(formula: str) -> list[str]:
    try:
        compile_formula(formula)
    except FormulaSyntaxError as exc:
        return [str(exc)]
    return []


def _as_series(value: Value, length: int) -> list[Scalar]:
    if isinstance(value, list):
        if len(value) != length:
            raise FormulaSyntaxError("公式内部序列长度不一致")
        return value
    return [value] * length


def _item(value: Value, index: int) -> Scalar:
    return value[index] if isinstance(value, list) else value


def _scalar(value: Value) -> Scalar:
    if isinstance(value, list):
        for item in value:
            if item is not None:
                return item
        return None
    return value


def _number(value: object) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _truth(value: Scalar) -> bool | None:
    if value is None:
        return None
    return abs(float(value)) > 1e-12


def _binary_value(operator: str, left: Scalar, right: Scalar) -> Scalar:
    if operator in {"AND", "OR"}:
        ltruth = _truth(left)
        rtruth = _truth(right)
        if operator == "AND":
            if ltruth is False or rtruth is False:
                return 0.0
            if ltruth is True and rtruth is True:
                return 1.0
            return None
        if ltruth is True or rtruth is True:
            return 1.0
        if ltruth is False and rtruth is False:
            return 0.0
        return None
    if left is None or right is None:
        return None
    a = float(left)
    b = float(right)
    if operator in {">", "<", ">=", "<=", "==", "=", "<>", "!="}:
        result = {
            ">": a > b,
            "<": a < b,
            ">=": a >= b,
            "<=": a <= b,
            "==": a == b,
            "=": a == b,
            "<>": a != b,
            "!=": a != b,
        }[operator]
        return 1.0 if result else 0.0
    try:
        if operator == "+":
            return a + b
        if operator == "-":
            return a - b
        if operator == "*":
            return a * b
        if operator == "/":
            return None if b == 0 else a / b
        if operator == "%":
            return None if b == 0 else a % b
        if operator == "^":
            return a**b
    except (ArithmeticError, ValueError, OverflowError):
        return None
    raise FormulaSyntaxError(f"不支持的运算符：{operator}")


def _binary(operator: str, left: Value, right: Value, length: int) -> list[Scalar]:
    return [
        _binary_value(operator, _item(left, index), _item(right, index))
        for index in range(length)
    ]


def _unary(operator: str, value: Value, length: int) -> list[Scalar]:
    result: list[Scalar] = []
    for index in range(length):
        item = _item(value, index)
        if item is None:
            result.append(None)
        elif operator == "+":
            result.append(float(item))
        elif operator == "-":
            result.append(-float(item))
        elif operator == "NOT":
            result.append(0.0 if _truth(item) else 1.0)
        else:
            raise FormulaSyntaxError(f"不支持的一元运算符：{operator}")
    return result


def _period(value: Value, default: int = 1) -> int:
    number = _scalar(value)
    if number is None:
        return default
    period = int(number)
    if period < 0:
        raise FormulaSyntaxError("REF、MA、SUM 等函数的周期不能为负数")
    return period


def _rolling(values: list[Scalar], period: int, mode: str) -> list[Scalar]:
    result: list[Scalar] = []
    if period == 0:
        period = len(values)
    for index in range(len(values)):
        if index < period - 1:
            result.append(None)
            continue
        window = values[index - period + 1 : index + 1]
        if any(value is None for value in window):
            result.append(None)
            continue
        numbers = [float(value) for value in window if value is not None]
        if mode == "sum":
            result.append(sum(numbers))
        elif mode == "mean":
            result.append(sum(numbers) / period)
        elif mode == "max":
            result.append(max(numbers))
        elif mode == "min":
            result.append(min(numbers))
        elif mode == "count":
            result.append(float(sum(1 for value in numbers if abs(value) > 1e-12)))
        elif mode == "every":
            result.append(1.0 if all(abs(value) > 1e-12 for value in numbers) else 0.0)
        elif mode == "std":
            average = sum(numbers) / period
            result.append(math.sqrt(sum((value - average) ** 2 for value in numbers) / period))
        else:
            raise FormulaSyntaxError(f"不支持的滚动函数：{mode}")
    return result


def _call(name: str, args: list[Value], length: int, finance: dict[str, float | None]) -> Value:
    if name == "FINANCE":
        key = _scalar(args[0]) if args else None
        if key is None:
            return None
        return finance.get(str(int(float(key))))
    if name == "IF":
        if len(args) != 3:
            raise FormulaSyntaxError("IF 需要 3 个参数")
        condition, yes, no = (_as_series(value, length) for value in args)
        result: list[Scalar] = []
        for index in range(length):
            truth = _truth(condition[index])
            result.append(yes[index] if truth is True else no[index] if truth is False else None)
        return result
    if name == "REF":
        if len(args) != 2:
            raise FormulaSyntaxError("REF 需要 2 个参数")
        values = _as_series(args[0], length)
        period = _period(args[1])
        return [values[index - period] if index >= period else None for index in range(length)]
    if name in {"SUM", "MA", "EMA", "HHV", "LLV", "COUNT", "EVERY", "STD", "EXIST"}:
        if len(args) != 2:
            raise FormulaSyntaxError(f"{name} 需要 2 个参数")
        values = _as_series(args[0], length)
        period = _period(args[1], 0 if name == "SUM" else 1)
        if name == "SUM" and period == 0:
            result: list[Scalar] = []
            total = 0.0
            for value in values:
                if value is None:
                    result.append(None)
                else:
                    total += float(value)
                    result.append(total)
            return result
        if name == "HHV":
            return _rolling(values, period, "max")
        if name == "LLV":
            return _rolling(values, period, "min")
        if name == "COUNT":
            return _rolling(values, period, "count")
        if name == "EVERY":
            return _rolling(values, period, "every")
        if name == "STD":
            return _rolling(values, period, "std")
        if name == "EXIST":
            counts = _rolling(values, period, "count")
            return [None if value is None else 1.0 if value > 0 else 0.0 for value in counts]
        if name == "EMA":
            result: list[Scalar] = []
            alpha = 2.0 / (period + 1) if period > 0 else 1.0
            previous: float | None = None
            for value in values:
                if value is None:
                    result.append(None)
                    continue
                current = float(value)
                previous = current if previous is None else alpha * current + (1 - alpha) * previous
                result.append(previous)
            return result
        return _rolling(values, period, "mean")
    if name == "SMA":
        if len(args) != 3:
            raise FormulaSyntaxError("SMA 需要 3 个参数")
        values = _as_series(args[0], length)
        period = _period(args[1], 1)
        weight = _scalar(args[2])
        if weight is None or period <= 0:
            raise FormulaSyntaxError("SMA 的周期和权重必须有效")
        weight = max(0.0, min(float(weight), float(period)))
        result: list[Scalar] = []
        previous: float | None = None
        for value in values:
            if value is None:
                result.append(None)
                continue
            current = float(value)
            previous = current if previous is None else (weight * current + (period - weight) * previous) / period
            result.append(previous)
        return result
    if name == "BARSCOUNT":
        if len(args) != 1:
            raise FormulaSyntaxError("BARSCOUNT 需要 1 个参数")
        values = _as_series(args[0], length)
        count = 0
        result: list[Scalar] = []
        for value in values:
            if value is not None:
                count += 1
                result.append(float(count))
            else:
                result.append(None)
        return result
    if name == "CROSS":
        if len(args) != 2:
            raise FormulaSyntaxError("CROSS 需要 2 个参数")
        left = _as_series(args[0], length)
        right = _as_series(args[1], length)
        result: list[Scalar] = [None]
        for index in range(1, length):
            if any(value is None for value in (left[index], right[index], left[index - 1], right[index - 1])):
                result.append(None)
            else:
                result.append(1.0 if left[index] > right[index] and left[index - 1] <= right[index - 1] else 0.0)
        return result
    if name == "BARSLAST":
        if len(args) != 1:
            raise FormulaSyntaxError("BARSLAST 需要 1 个参数")
        values = _as_series(args[0], length)
        result: list[Scalar] = []
        since = None
        for index, value in enumerate(values):
            if _truth(value) is True:
                since = 0
            elif since is not None:
                since += 1
            result.append(float(since) if since is not None else None)
        return result
    if name in {"MAX", "MIN"}:
        if len(args) != 2:
            raise FormulaSyntaxError(f"{name} 需要 2 个参数")
        left = _as_series(args[0], length)
        right = _as_series(args[1], length)
        return [
            None if left[index] is None or right[index] is None else (max(left[index], right[index]) if name == "MAX" else min(left[index], right[index]))
            for index in range(length)
        ]
    if name in {"ABS", "ATAN", "EXP", "LOG", "SIN", "SQRT"}:
        if len(args) != 1:
            raise FormulaSyntaxError(f"{name} 需要 1 个参数")
        values = _as_series(args[0], length)
        result: list[Scalar] = []
        for value in values:
            if value is None:
                result.append(None)
                continue
            try:
                result.append(
                    {
                        "ABS": abs,
                        "ATAN": math.atan,
                        "EXP": math.exp,
                        "LOG": math.log,
                        "SIN": math.sin,
                        "SQRT": math.sqrt,
                    }[name](float(value))
                )
            except (ArithmeticError, ValueError, OverflowError):
                result.append(None)
        return result
    if name == "POW":
        if len(args) != 2:
            raise FormulaSyntaxError("POW 需要 2 个参数")
        return _binary("^", args[0], args[1], length)
    raise FormulaSyntaxError(f"暂不支持函数：{name}")


def _evaluate_node(node: Node, env: dict[str, Value], length: int, finance: dict[str, float | None]) -> Value:
    kind = node[0]
    if kind == "number":
        return float(node[1])
    if kind == "var":
        name = str(node[1])
        if name in env:
            return env[name]
        if name in {"PI", "Π"}:
            return math.pi
        raise FormulaSyntaxError(f"公式引用了未知变量：{name}")
    if kind == "unary":
        return _unary(str(node[1]), _evaluate_node(node[2], env, length, finance), length)
    if kind == "binary":
        return _binary(
            str(node[1]),
            _evaluate_node(node[2], env, length, finance),
            _evaluate_node(node[3], env, length, finance),
            length,
        )
    if kind == "call":
        return _call(
            str(node[1]),
            [_evaluate_node(child, env, length, finance) for child in node[2]],
            length,
            finance,
        )
    raise FormulaSyntaxError(f"未知的公式节点：{kind}")


def evaluate_formula(
    bars: list[dict[str, Any]],
    formula: str,
    finance: dict[str, float | None] | None = None,
) -> dict[str, Any]:
    """Evaluate a parsed formula over OHLCV bars.

    ``finance`` is optional.  When omitted, FINANCE() returns unknown values;
    callers can use that to collect technical candidates before loading the
    slower per-stock financial data.
    """
    assignments, final, calls = compile_formula(formula)
    length = len(bars)
    if length <= 0:
        return {"final": [], "series": {}, "calls": calls}
    env: dict[str, Value] = {}
    fields = {
        "OPEN": "open",
        "HIGH": "high",
        "LOW": "low",
        "CLOSE": "close",
        "VOL": "volume",
        "VOLUME": "volume",
        "AMOUNT": "amount",
    }
    for name, field in fields.items():
        env[name] = [float(item[field]) if item.get(field) is not None else None for item in bars]
    finance_values: dict[str, float | None] = {}
    for key, value in (finance or {}).items():
        normalized_key = str(key).strip().upper()
        number = _number(value)
        finance_values[normalized_key] = number
        if normalized_key.startswith("FINANCE_"):
            finance_values[normalized_key.removeprefix("FINANCE_")] = number
    # TongDaXin formula aliases used in stock-selection formulas.
    env["O"] = env["OPEN"]
    env["H"] = env["HIGH"]
    env["L"] = env["LOW"]
    env["C"] = env["CLOSE"]
    env["V"] = env["VOL"]
    capital = finance_values.get("CAPITAL")
    if capital is None:
        capital = finance_values.get("7") or finance_values.get("FINANCE_7")
    env["CAPITAL"] = [capital] * length
    for name, expression in assignments.items():
        env[name] = _evaluate_node(expression, env, length, finance_values)
    final_value = _evaluate_node(final, env, length, finance_values)
    return {
        "final": _as_series(final_value, length),
        "series": env,
        "calls": calls,
    }


def uses_finance(formula: str) -> bool:
    try:
        _assignments, _final, calls = compile_formula(formula)
    except FormulaSyntaxError:
        text = str(formula or "").upper()
        return "FINANCE(" in text or re.search(r"\bCAPITAL\b", text) is not None
    return "FINANCE" in calls or re.search(r"\bCAPITAL\b", str(formula or "").upper()) is not None
