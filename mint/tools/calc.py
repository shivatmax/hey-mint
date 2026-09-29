"""Exact arithmetic for totals, averages and conversions, so the model never adds up in its head.

"calculate" takes an expression ("1200.50 + 85 + 310.49", "(1200 - 85) * 0.92", "round(49.99 * 0.79, 2)")
or a list of numbers to total. Only numbers, + - * / // % **, parentheses and round/abs/min/max/sum are
allowed - it is not a way to run code. Numbers may carry currency signs and thousands commas ("$1,200.50").
"""

from __future__ import annotations

import ast
import operator
import re
from decimal import Decimal, InvalidOperation

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
_FUNCS = {"round": round, "abs": abs, "min": min, "max": max, "sum": sum}


def _number(text: str) -> Decimal:
    cleaned = re.sub(r"[^\d.\-]", "", str(text).replace(",", ""))
    try:
        return Decimal(cleaned)
    except InvalidOperation as error:
        raise ValueError(f"not a number: {text!r}") from error


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return Decimal(str(node.value))
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 100:
            raise ValueError("exponent too large")
        return _OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = _eval(node.operand)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval(e) for e in node.elts]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS:
        args = [_eval(a) for a in node.args]
        if node.func.id == "round":
            places = int(args[1]) if len(args) > 1 else 0
            return args[0].quantize(Decimal(1).scaleb(-places))
        if node.func.id in ("min", "max", "sum") and len(args) == 1 and isinstance(args[0], list):
            args = args[0]
        return _FUNCS[node.func.id](args) if node.func.id in ("min", "max", "sum") else _FUNCS[node.func.id](*args)
    raise ValueError("only numbers, + - * / % **, parentheses and round/abs/min/max/sum are allowed")


def _show(value) -> str:
    if isinstance(value, list):
        return ", ".join(_show(v) for v in value)
    text = format(value.normalize(), "f") if isinstance(value, Decimal) else str(value)
    return text


def calculate(args: dict) -> str:
    numbers = args.get("numbers")
    expression = str(args.get("expression") or "").strip()
    try:
        if numbers:
            values = [_number(n) for n in (numbers if isinstance(numbers, list) else str(numbers).split("\n"))
                      if str(n).strip()]
            total = sum(values, Decimal(0))
            mean = total / len(values)
            return (f"{len(values)} numbers: total {_show(total)}, average {_show(round(mean, 4))}, "
                    f"min {_show(min(values))}, max {_show(max(values))}.")
        if not expression:
            return "FAILED: give `expression` or `numbers`."
        text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", expression).replace("$", "").replace("€", "").replace("£", "")
        text = text.replace("×", "*").replace("÷", "/").replace("^", "**")
        return f"{expression} = {_show(_eval(ast.parse(text, mode='eval')))}"
    except (ValueError, SyntaxError, ZeroDivisionError, InvalidOperation, TypeError) as error:
        return f"FAILED: {error}"


PROMPT = """Arithmetic: totals, averages, differences, currency conversions and percentages of more than a couple \
of numbers go through calculate (expression, or numbers to total) - never add up in your head."""


def declarations():
    from google.genai import types
    S = types.Type.STRING
    return [types.FunctionDeclaration(
        name="calculate",
        description=("Exact arithmetic: an expression ('1200.50 + 85 + 310.49', 'round(49.99 * 0.79, 2)', "
                     "'(3840 - 1200) / 4') or a list of numbers to total (gives total, average, min, max). "
                     "Currency signs and thousands commas are fine."),
        parameters=types.Schema(type=types.Type.OBJECT, properties={
            "expression": types.Schema(type=S, description="Arithmetic only."),
            "numbers": types.Schema(type=types.Type.ARRAY, items=types.Schema(type=S),
                                    description="Numbers to total, e.g. ['$1,200.50', '85', '310.49']")}))]


HANDLERS = {"calculate": calculate}
