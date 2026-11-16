"""Calculator: exact multi-step arithmetic, financial functions, and refusal of anything else."""

import time

import pytest

from src.services.calculator import calculate


def results(program: str) -> dict:
    out = calculate(program)
    assert "error" not in out, out
    return out["results"]


def error(program: str) -> str:
    out = calculate(program)
    assert "error" in out, out
    return out["error"]


def test_multi_step_program_with_names():
    out = calculate(
        """
        # Amazon FY2017 days payable outstanding
        avg_payables = average(25309, 34616)
        cogs = 111934
        dpo = 365 * avg_payables / cogs
        """.replace("        ", "")
    )
    assert out["results"] == {"avg_payables": 29962.5, "cogs": 111934, "dpo": 97.703222}
    assert out["steps"][2] == "dpo = 365 * avg_payables / cogs = 97.703222"


def test_the_two_benchmark_slips_compute_exactly():
    assert results("dpo = 365 * average(29962.5, 29962.5) / 116520")["dpo"] == pytest.approx(93.857814, abs=1e-6)
    assert results("turnover = 6489 / average(253, 288)")["turnover"] == pytest.approx(23.988909, abs=1e-6)


@pytest.mark.parametrize(
    ("program", "name", "expected"),
    [
        ("g = pct_change(16434, 23601)", "g", 43.610807),
        ("g = pct_change(-200, -100)", "g", 50.0),
        ("c = cagr(16434, 23601, 3)", "c", 12.822497),
        ("v = npv(0.1, [-100, 60, 60])", "v", 4.132231),
        ("r = irr([-500, 120, 150, 180, 200])", "r", 0.104146),
        ("m = max(3, 9, 4) - min([1, 2])", "m", 8),
        ("s = sum([1.5, 2.5], 1)", "s", 5),
        ("x = round(2 / 3, 2)", "x", 0.67),
        ("x = sqrt(16) + abs(-2) + 2 ** 3", "x", 14),
        ("x = log(exp(2))", "x", 2),
        ("x = -(-5) + +1", "x", 6),
        ("x = 1e3 / 4", "x", 250),
    ],
)
def test_functions_and_operators(program, name, expected):
    assert results(program)[name] == pytest.approx(expected, abs=1e-6)


def test_unicode_operators_from_documents():
    assert results("x = 365 × 29962.5 ÷ 116520 − 1")["x"] == pytest.approx(92.857814, abs=1e-6)


def test_bare_expressions_are_named_result():
    assert results("2 + 2") == {"result": 4}
    assert results("a = 1\na * 3") == {"a": 1, "result_2": 3}


@pytest.mark.parametrize(
    ("program", "message"),
    [
        ("x = __import__('os')", "unknown function '__import__'"),
        ("x = __import__('os').system('ls')", "only numbers"),
        ("x = open('/etc/passwd').read()", "only numbers"),
        ("x = (1).__class__", "only numbers"),
        ("x = [i for i in range(10)]", "only numbers"),
        ("x = lambda: 1", "only numbers"),
        ("x = 'text'", "only numbers"),
        ("x = True", "only numbers"),
        ("import os", "name = expression"),
        ("x = y + 1", "'y' is not defined"),
        ("x = 1 / 0", "division by zero"),
        ("x = 10 ** 999999", "exponents above 100"),
        ("x = 10 ** 99", "too large"),
        ("x = 1e300 * 1e300", "finite"),
        ("x = 34,616 / 2", "thousands separators"),
        ("x = 50 % 3", "'%' is not supported"),
        ("x = pct_change(0, 5)", "old value is 0"),
        ("x = cagr(-1, 5, 2)", "must be positive"),
        ("x = irr([100, 100])", "do not change sign"),
        ("x = sqrt(-1)", "math domain error"),
        ("x = round(1, 2, 3)", "wrong arguments"),
        ("x = average()", "at least one number"),
        ("x = [1, 2]", "single number"),
        ("average = 3", "function name"),
        ("x = 1; y = 2", "one calculation per line"),
        ("x = = 2", "not a valid expression"),
        ("", "empty"),
    ],
)
def test_refusals_name_the_problem(program, message):
    assert message in error(program)


def test_error_names_the_failing_line():
    out = calculate("a = 10\nb = a / 0\nc = 3")
    assert out["line"] == 2 and out["error"].startswith("Line 2 (b = a / 0)")


def test_size_limits():
    assert "longer than" in error("x = 1 + " + "1 + " * 600 + "1")
    assert "at most 40 lines" in error("\n".join(f"x{i} = {i}" for i in range(41)))
    assert "too long" in error("x = " + " + ".join(["1"] * 250))


def test_hostile_programs_finish_quickly():
    started = time.monotonic()
    error("x = ((9 ** 9) ** 9) ** 9")
    assert results("x = " + "(" * 90 + "1" + ")" * 90) == {"x": 1}
    assert time.monotonic() - started < 1


def test_comments_and_blank_lines_are_ignored():
    assert results("# heading\n\na = 2  # two\n\nb = a * 2") == {"a": 2, "b": 4}
