"""Anti-exploit hardening: value reconciliation, run-dir hygiene,
bias-gated physics, temperature-activated diodes, honest-report scoring."""
import json
import math
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


# ------------------------------------------------------------------ honesty

def test_reported_stats_cannot_launder_bad_noise():
    # T12-style: actual std 798 uV, spec 300 uV. Reporting std=0 must not
    # help once the graded quantity is computed from the data itself.
    sample_std = 798e-6
    spec = param_score(max(0.0, sample_std - 3.0e-4), 1.0e-4)
    assert spec < 70.0  # weakest-parameter rule: the task fails
    honesty = param_score(abs(0.0 - sample_std), max(2.0e-6, 0.06 * sample_std))
    assert honesty == 0.0
