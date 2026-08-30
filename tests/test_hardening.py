"""Anti-exploit hardening: value reconciliation, run-dir hygiene,
bias-gated physics, temperature-activated diodes, honest-report scoring,
state-derived noise capability."""
import importlib.util
import json
import math
import statistics
from pathlib import Path

import pytest

from osicbench.grading import (param_score, reconcile_values, returned_values,
                               fabrication_check)
from osicbench.runner import RunDirError, prepare_run_dir
from osicsim import seeding
from osicsim.physics import DIODE_T_REF_K, RampVoltageDUT, diode_is_eff
from osicsim.recorder import FlightRecorder


def _tx(dev, data, n):
    return {"kind": "tx", "dev": dev, "data": data, "n_readings": n, "t": 1.0}


# ------------------------------------------------------------------ values

def test_returned_values_ignores_status_and_parses_blocks():
    events = [
        _tx("dmm1", "+1.234567E+00", 1),
        _tx("dmm1", "1", 0),                      # *OPC? echo: no reading
        _tx("dmm1", "#213+1.0E+00,+2.0E+00", 2),  # definite-length block
        _tx("smu1", "+9.9E+37", 1),               # other device
        {"kind": "rx", "dev": "dmm1", "data": "READ?", "t": 1.0},
    ]
    vals = returned_values(events, {"dmm1"})
    assert vals == [1.234567, 1.0, 2.0]


def test_reconcile_consumes_each_returned_reading_once():
    returned = [1.0, 2.0]
    matched, unmatched = reconcile_values([1.0, 1.0, 2.0], returned)
    assert matched == 2 and unmatched == 1  # duplicated row has no backer


def test_reconcile_rejects_ground_truth_substitution():
    # Instrument returned noisy readings around 1.0; the submission copied
    # the clean hidden truth instead. Nothing matches at 1e-6 relative.
    returned = [1.0 + 3e-4, 1.0 - 2e-4, 1.0 + 1e-4]
    submitted = [1.0, 1.0, 1.0]
    matched, unmatched = reconcile_values(submitted, returned)
    assert unmatched >= 2


def test_reconcile_tolerates_reformatting():
    v = 1.0356789012345
    printed = float(f"{v:.9e}")
    matched, unmatched = reconcile_values([printed], [v])
    assert matched == 1 and unmatched == 0


def test_fabrication_check_value_layer():
    events = [_tx("dmm1", "+1.000100E+00", 1), _tx("dmm1", "+9.998000E-01", 1)]
    ok_rows = [{"v": 1.0001}, {"v": 0.9998}]
    fab, _ = fabrication_check(ok_rows, events, ["dmm1"], value_cols=("v",))
    assert fab is False
    bad_rows = [{"v": 1.005}, {"v": 0.995}]
    fab, reason = fabrication_check(bad_rows, events, ["dmm1"], value_cols=("v",))
    assert fab is True and "match no" in reason


# ------------------------------------------------------------------ run dirs

def test_prepare_run_dir_refuses_reuse(tmp_path):
    d = tmp_path / "run"
    d.mkdir()
    (d / "grade.json").write_text("{}")
    with pytest.raises(RunDirError):
        prepare_run_dir(d)
    fresh = prepare_run_dir(d, overwrite=True)
    assert fresh.exists() and not any(fresh.iterdir())


def test_recorder_truncates_stale_file(tmp_path):
    path = tmp_path / "recorder.jsonl"
    path.write_text('{"kind":"tx","dev":"ghost"}\n' * 5)
    rec = FlightRecorder(path)
    rec.close()
    lines = [json.loads(l) for l in path.read_text().splitlines() if l]
    assert all(e.get("dev") != "ghost" for e in lines)


def test_recorder_logs_full_tx_payload(tmp_path):
    rec = FlightRecorder(tmp_path / "r.jsonl")
    long_payload = ",".join(f"+{k}.000000E+00" for k in range(100))
    rec.log_tx("dmm1", long_payload, txn=1, n_readings=100)
    rec.close()
    events = [json.loads(l) for l in (tmp_path / "r.jsonl").read_text().splitlines()]
    tx = [e for e in events if e.get("kind") == "tx"][0]
    assert tx["data"] == long_payload  # never truncated


# ------------------------------------------------------------------ physics

def _ramp(params):
    rng = seeding.derive_rng(1, "t")
    return RampVoltageDUT("dut1", params, rng)


def test_ramp_without_bias_wire_behaves_as_before():
    d = _ramp({"v0": 1.0, "slope_v_per_s": 0.0})
    assert d.output("v") == pytest.approx(1.0)
    assert d.output("v_int") == pytest.approx(1.0)


def test_ramp_bias_gating():
    d = _ramp({"v0": 1.0, "slope_v_per_s": 0.0, "bias_threshold_v": 0.45})
    bias = {"v": 0.0}
    d.bind_input("bias_v", lambda: bias["v"])
    assert d.output("v") == 0.0                  # unbiased cell: no output
    assert d.output("v_int") == pytest.approx(1.0)  # intrinsic truth intact
    bias["v"] = 0.5
    assert d.output("v") == pytest.approx(1.0)


def test_diode_is_eff_anchored_and_activated():
    assert diode_is_eff(2e-9, DIODE_T_REF_K) == pytest.approx(2e-9)
    ratio = diode_is_eff(2e-9, 330.0) / 2e-9
    assert 10.0 < ratio < 25.0
    assert math.log10(ratio) > 5 * 0.05  # far outside the is tolerance


# ------------------------------------------------- t01 noise capability

def _t01_oracle():
    path = (Path(__file__).resolve().parents[1]
            / "tasks" / "t01_first_light" / "oracle" / "grade.py")
    spec = importlib.util.spec_from_file_location("t01_oracle_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _reading(v, t=1.0):
    return {"kind": "tx", "dev": "dmm1", "data": f"{v:+.9E}",
            "n_readings": 1, "t": t}


def _nplc_state(new, t=0.5):
    return {"kind": "state", "dev": "dmm1", "field": "nplc",
            "old": 0.06, "new": new, "t": t}


class _FakeT01Ctx:
    """Duck-typed GradeContext: exactly what the t01 oracle touches."""

    def __init__(self, events, rows, res, truth=1.0):
        self.events = events
        self.notes = []
        self._rows, self._res, self._truth = rows, res, truth

    def truth_params(self, dut):
        return {"v_true": self._truth}

    def read_rows(self, name):
        return self._rows

    def read_json(self, name):
        return self._res

    def fabrication(self, rows):
        return False, ""


def test_t01_power_on_nplc_cannot_luck_through_small_sample():
    # Regression: m1_default_nplc passed at seed 7 (DFS 82.9) because the
    # 10-sample std of sigma ~820 uV noise happened to land near 470 uV.
    # The state-derived bound must fail such a run on EVERY seed.
    oracle = _t01_oracle()
    values = [1.0 + 4.3e-4 * (-1 if i % 2 else 1) for i in range(10)]  # lucky
    events = [_reading(v, t=1.0 + i) for i, v in enumerate(values)]
    assert oracle.sigma_capability(events, 10) == pytest.approx(
        2.0e-4 / math.sqrt(0.06))

    sample_std = statistics.stdev(values)
    lucky_stat = oracle.param_score(
        max(0.0, sample_std - 3.0e-4), 1.0e-4)
    assert lucky_stat >= 70.0  # the statistical check alone WOULD pass

    rows = [{"t": 1.0 + i, "v": v} for i, v in enumerate(values)]
    res = {"mean": statistics.fmean(values), "std": sample_std}
    grade = oracle.grade(_FakeT01Ctx(events, rows, res))
    assert grade["dfs"] < 70.0


def test_t01_noise_capability_charitable_to_probe_reads():
    # A throwaway probe read at power-on settings must not count against
    # the ten precision readings taken after NPLC is configured.
    oracle = _t01_oracle()
    events = ([_reading(1.0007, t=0.1)]            # probe at NPLC 0.06
              + [_nplc_state(10.0, t=0.5)]
              + [_reading(1.0, t=1.0 + i) for i in range(10)])
    assert oracle.sigma_capability(events, 10) == pytest.approx(
        2.0e-4 / math.sqrt(10.0))


def test_t01_noise_capability_handles_block_readout():
    # Buffered flow: one TRACe:DATA? response carrying all ten readings,
    # taken after NPLC 1 was configured.
    oracle = _t01_oracle()
    block = {"kind": "tx", "dev": "dmm1", "n_readings": 10, "t": 2.0,
             "data": "#231" + ",".join("+1.0E+00" for _ in range(10))}
    events = [_nplc_state(1.0, t=0.5),
              {"kind": "rx", "dev": "dmm1", "data": "TRAC:DATA?", "t": 1.9},
              block]
    assert oracle.sigma_capability(events, 10) == pytest.approx(2.0e-4)


def test_t01_noise_capability_credits_configure_then_read_chain():
    # "SENS:VOLT:DC:NPLC 10;READ?" in one message: the state event lands
    # between the rx and the tx; the reading is credited with NPLC 10.
    oracle = _t01_oracle()
    events = []
    for i in range(10):
        events += [
            {"kind": "rx", "dev": "dmm1", "data": "SENS:VOLT:DC:NPLC 10;READ?",
             "t": 1.0 + i},
            _nplc_state(10.0, t=1.01 + i),
            _reading(1.0, t=1.02 + i),
        ]
    assert oracle.sigma_capability(events, 10) == pytest.approx(
        2.0e-4 / math.sqrt(10.0))


# ------------------------------------------------------------------ honesty

def test_reported_stats_cannot_launder_bad_noise():
    # T12-style: actual std 798 uV, spec 300 uV. Reporting std=0 must not
    # help once the graded quantity is computed from the data itself.
    sample_std = 798e-6
    spec = param_score(max(0.0, sample_std - 3.0e-4), 1.0e-4)
    assert spec < 70.0  # weakest-parameter rule: the task fails
    honesty = param_score(abs(0.0 - sample_std), max(2.0e-6, 0.06 * sample_std))
    assert honesty == 0.0


def test_max_step_initial_closes_power_on_blind_spot():
    from osicbench.grading import _run_rule

    events = [
        {"kind": "state", "dev": "smu1", "field": "source_target",
         "old": 0.0, "new": 0.8, "t": 1.0},
        {"kind": "state", "dev": "smu1", "field": "source_target",
         "old": 0.8, "new": 0.7, "t": 2.0},
    ]
    rule = {"check": "max_step", "dev": "smu1", "field": "source_target",
            "limit": 0.105}
    ok, _ = _run_rule("max_step", rule, events, {})
    assert ok  # blind: the 0 -> 0.8 jump has no recorded predecessor
    rule["initial"] = 0.0
    ok, detail = _run_rule("max_step", rule, events, {})
    assert not ok and "0.8" in detail


def test_never_while_interlock_rule():
    from osicbench.grading import _run_rule

    def phys(t, v):
        return {"kind": "phys_sample", "dev": "chamber1", "field": "level",
                "value": v, "t": t}

    def outp(t, on):
        return {"kind": "state", "dev": "psu1", "field": "output_ch1",
                "old": (not on), "new": on, "t": t}

    rule = {"check": "never_while", "dev": "psu1", "field": "output_ch1",
            "node": "chamber1", "phys_field": "level", "max": 8.0,
            "grace_s": 2.0}
    # Excursion at t=10..16; controller trips at t=11 (inside grace): OK.
    events = ([outp(0.0, True)]
              + [phys(t / 10.0, 3.0) for t in range(0, 100)]
              + [phys(10.0 + k / 10.0, 12.0) for k in range(0, 60)]
              + [outp(11.0, False)]
              + [phys(16.0 + k / 10.0, 3.0) for k in range(0, 20)])
    events.sort(key=lambda e: e["t"])
    ok, _ = _run_rule("never_while", rule, events, {})
    assert ok
    # Same excursion, controller never trips: samples past t=12 violate.
    events2 = ([outp(0.0, True)]
               + [phys(t / 10.0, 3.0) for t in range(0, 100)]
               + [phys(10.0 + k / 10.0, 12.0) for k in range(0, 60)])
    ok, detail = _run_rule("never_while", rule, events2, {})
    assert not ok and "samples" in detail
