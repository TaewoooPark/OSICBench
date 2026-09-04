"""T03's compliance mutant must produce inaccurate fits, not just a bad style."""
import ast
import math
from pathlib import Path

import pytest

from osicbench.grading import combine_min, param_score
from osicbench.taskspec import load_task
from osicsim.physics import resolve_params


TASK_DIR = Path(__file__).resolve().parents[1] / "tasks" / "t03_diode_iv"
MUTANT = TASK_DIR / "mutants" / "m3_compliance_blind.py"
VT = 8.617333262e-5 * 300.0


def _configured_compliance():
    """Read the actual negative control's literal configuration without I/O."""
    tree = ast.parse(MUTANT.read_text())
    commands = [node.args[1].value for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "w" and len(node.args) == 2
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
                and node.args[1].value.startswith("SENS:CURR:PROT ")]
    assert len(commands) == 1
    return float(commands[0].split()[1])


def _fit(i_s, ideality, limit):
    """The mutant's all-point log-linear fit, using noiseless clamped currents."""
    xs = [0.30 + 0.01 * index for index in range(26)]
    ideal = [i_s * math.expm1(voltage / (ideality * VT)) for voltage in xs]
    ys = [math.log(max(min(value, limit), 1e-15)) for value in ideal]
    count, sx, sy = len(xs), sum(xs), sum(ys)
    slope = (count * sum(x * y for x, y in zip(xs, ys)) - sx * sy) / (
        count * sum(x * x for x in xs) - sx * sx)
    fitted_n = 1 / (slope * VT) if abs(slope) > 1e-12 else math.inf
    fitted_is = math.exp((sy - slope * sx) / count)
    score = combine_min({
        "n": param_score(abs(fitted_n - ideality), 0.02),
        "log10_is": param_score(abs(math.log10(fitted_is) - math.log10(i_s)), 0.03),
    })
    return score, fitted_n, sum(value > limit for value in ideal)


def test_seed10_exposes_weak_default_limit_negative_control():
    task = load_task(TASK_DIR)
    truth = resolve_params(10, "dut1", task.config["farm"]["duts"]["dut1"]["params"])
    old_score, _, clamped = _fit(truth["i_s"], truth["n"], 105e-6)
    assert old_score == pytest.approx(100.0)
    assert clamped == 1
    corrected_score, _, clamped = _fit(truth["i_s"], truth["n"], _configured_compliance())
    assert corrected_score == 0.0
    assert clamped >= 22


def test_undersized_limit_is_discriminating_across_parameter_grid():
    task = load_task(TASK_DIR)
    parameters = task.config["farm"]["duts"]["dut1"]["params"]
    n_lo, n_hi = parameters["n"]["uniform"]
    is_lo, is_hi = map(math.log10, parameters["i_s"]["loguniform"])
    limit = _configured_compliance()
    assert limit == 1e-6
    # Include both boundary corners and the interior. This fast physical
    # check complements, rather than replaces, the live multi-seed gate.
    for ni in range(101):
        ideality = n_lo + (n_hi - n_lo) * ni / 100
        for ii in range(101):
            i_s = 10 ** (is_lo + (is_hi - is_lo) * ii / 100)
            score, fitted_n, clamped = _fit(i_s, ideality, limit)
            assert score == 0.0
            assert abs(fitted_n - ideality) > 20.0
            assert clamped >= 22
