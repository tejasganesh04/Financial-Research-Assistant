"""Exact arithmetic for answer models: a whole multi-step calculation in one call.

A program is a few lines of `name = expression`. Later lines can use earlier names. Only
numbers, + - * / **, brackets and a fixed list of financial functions are allowed, so nothing
else in Python can run. Errors name the line, so a model can correct it and try again.

    revenue_growth = pct_change(16434, 23601)
    dpo = 365 * average(25309, 34616) / 111934
"""

from __future__ import annotations

import ast
import math
import operator
import statistics

MAX_PROGRAM_CHARS = 2000
MAX_LINES = 40
MAX_NODES = 400
MAX_EXPONENT = 100
MAX_LIST_ITEMS = 100
MAX_MAGNITUDE = 1e18
DECIMALS = 6

BINARY = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}
UNARY = {ast.USub: operator.neg, ast.UAdd: operator.pos}


class CalculationError(ValueError):
    """A problem in the program, phrased so the caller can fix it."""


def _numbers(values) -> list[float]:
    flat = []
    for value in values:
        flat.extend(value if isinstance(value, list) else [value])
    if not flat:
        raise CalculationError("needs at least one number")
    return flat


def npv(rate: float, cashflows: list[float]) -> float:
    """Net present value; the first cash flow is at time 0."""
    return math.fsum(cf / (1 + rate) ** t for t, cf in enumerate(cashflows))


def irr(cashflows: list[float]) -> float:
    """Internal rate of return (as a fraction) by bisection between -99% and 1000%."""
    low, high = -0.99, 10.0
    if npv(low, cashflows) * npv(high, cashflows) > 0:
        raise CalculationError("irr: the cash flows do not change sign, so there is no rate of return")
    for _ in range(200):
        mid = (low + high) / 2
        if npv(low, cashflows) * npv(mid, cashflows) <= 0:
            high = mid
        else:
            low = mid
    return (low + high) / 2


def pct_change(old: float, new: float) -> float:
    """Percentage change from old to new, in percent."""
    if old == 0:
        raise CalculationError("pct_change: the old value is 0")
    return (new - old) / abs(old) * 100


def cagr(start: float, end: float, years: float) -> float:
    """Compound annual growth rate, in percent."""
    if start <= 0 or end <= 0 or years <= 0:
        raise CalculationError("cagr: start, end and years must be positive")
    return ((end / start) ** (1 / years) - 1) * 100


def _round(value: float, digits: float = 0) -> float:
    return round(value, int(digits))


FUNCTIONS = {
    "average": lambda *xs: statistics.fmean(_numbers(xs)),
    "sum": lambda *xs: math.fsum(_numbers(xs)),
    "min": lambda *xs: min(_numbers(xs)),
    "max": lambda *xs: max(_numbers(xs)),
    "abs": abs,
    "round": _round,
    "sqrt": math.sqrt,
    "log": math.log,
    "exp": math.exp,
    "pct_change": pct_change,
    "cagr": cagr,
    "npv": npv,
    "irr": irr,
}
FUNCTION_HELP = (
    "average(...), sum(...), min(...), max(...), abs(x), round(x, digits), sqrt(x), log(x), exp(x), "
    "pct_change(old, new) in percent, cagr(start, end, years) in percent, npv(rate, [cash flows]), "
    "irr([cash flows]) as a fraction"
)


def _evaluate(node: ast.AST, names: dict[str, float]):
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in names:
            raise CalculationError(f"'{node.id}' is not defined on an earlier line")
        return names[node.id]
    if isinstance(node, ast.BinOp) and type(node.op) in BINARY:
        return BINARY[type(node.op)](_evaluate(node.left, names), _evaluate(node.right, names))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
        base, exponent = _evaluate(node.left, names), _evaluate(node.right, names)
        if abs(exponent) > MAX_EXPONENT:
            raise CalculationError(f"exponents above {MAX_EXPONENT} are not allowed")
        if base != 0 and exponent > 0 and exponent * math.log10(abs(base)) > math.log10(MAX_MAGNITUDE):
            raise CalculationError("the result is too large")
        return base**exponent
    if isinstance(node, ast.UnaryOp) and type(node.op) in UNARY:
        return UNARY[type(node.op)](_evaluate(node.operand, names))
    if isinstance(node, ast.List):
        if len(node.elts) > MAX_LIST_ITEMS:
            raise CalculationError(f"lists may hold at most {MAX_LIST_ITEMS} numbers")
        return [_evaluate(item, names) for item in node.elts]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
        if node.func.id not in FUNCTIONS:
            raise CalculationError(f"unknown function '{node.func.id}'. Available: {FUNCTION_HELP}")
        try:
            return FUNCTIONS[node.func.id](*[_evaluate(arg, names) for arg in node.args])
        except CalculationError:
            raise
        except TypeError as error:
            raise CalculationError(f"wrong arguments for {node.func.id}: {error}") from error
    if isinstance(node, ast.Tuple):
        raise CalculationError("write numbers without thousands separators, for example 34616 not 34,616")
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        raise CalculationError("'%' is not supported; write percentages as fractions or multiply by 100")
    raise CalculationError("only numbers, + - * / **, brackets, earlier names and listed functions are allowed")


def _format(value: float) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CalculationError("each line must produce a single number")
    if math.isnan(value) or math.isinf(value) or abs(value) > MAX_MAGNITUDE:
        raise CalculationError("the result is not a finite number of reasonable size")
    rounded = round(float(value), DECIMALS)
    return int(rounded) if rounded.is_integer() and abs(rounded) < 1e15 else rounded


def calculate(program: str) -> dict:
    """Run the program; return each named result in order, or an error naming the failing line.

    Result: {"results": {name: value}, "steps": ["name = expression = value", ...]}
    or {"error": "...", "line": n}.
    """
    text = (program or "").replace("×", "*").replace("÷", "/").replace("−", "-").replace("–", "-")
    if not text.strip():
        return {"error": "The program is empty. Write lines such as: margin = 120 / 480 * 100"}
    if len(text) > MAX_PROGRAM_CHARS:
        return {"error": f"The program is longer than {MAX_PROGRAM_CHARS} characters; split it into calls."}
    lines = [line for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    if len(lines) > MAX_LINES:
        return {"error": f"Use at most {MAX_LINES} lines per call."}
    names: dict[str, float] = {}
    steps: list[str] = []
    for number, line in enumerate(lines, 1):
        source = line.split("#", 1)[0].strip()
        try:
            try:
                tree = ast.parse(source, mode="exec")
            except SyntaxError as error:
                raise CalculationError(f"not a valid expression ({error.msg})") from error
            if sum(1 for _ in ast.walk(tree)) > MAX_NODES:
                raise CalculationError("the expression is too long; split it into named steps")
            if len(tree.body) != 1:
                raise CalculationError("write one calculation per line")
            statement = tree.body[0]
            if (
                isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
            ):
                name, expression = statement.targets[0].id, statement.value
            elif isinstance(statement, ast.Expr):
                name, expression = f"result_{number}" if len(lines) > 1 else "result", statement.value
            else:
                raise CalculationError("write each line as name = expression")
            if name in FUNCTIONS:
                raise CalculationError(f"'{name}' is a function name; choose another name")
            value = _format(_evaluate(expression, names))
        except CalculationError as error:
            return {"error": f"Line {number} ({source}): {error}", "line": number}
        except ZeroDivisionError:
            return {"error": f"Line {number} ({source}): division by zero", "line": number}
        except (OverflowError, ValueError) as error:
            return {"error": f"Line {number} ({source}): {error}", "line": number}
        names[name] = value
        steps.append(f"{name} = {ast.unparse(expression)} = {value}")
    return {"results": dict(names), "steps": steps}
