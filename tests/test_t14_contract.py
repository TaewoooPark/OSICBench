"""T14 requires a parked source and genuinely precision raw conversions."""
import importlib.util
import math
import random
import statistics
from pathlib import Path

import pytest

from osicbench.grading import evaluate_hss
from osicbench.taskspec import load_task
from osicsim.instruments.mer_d610 import MerD610
from osicsim.recorder import FlightRecorder, load_events


TASK = Path(__file__).resolve().parents[1] / "tasks" / "t14_coil_park"


def _events(final_target, output=False):
    levels = [i / 10 for i in range(1, 9)]
    if abs(final_target) <= 0.001:
        levels += [i / 10 for i in range(7, -1, -1)]
        levels.append(final_target)
    events = [
        {"kind": "state", "dev": "smu1", "field": "source_target",
         "new": level, "t": i + 1.0}
        for i, level in enumerate(levels)
    ]
    events.append({"kind": "snapshot", "states": {
        "smu1": {"source_target": final_target, "output": output}}})
    return events


def test_output_off_does_not_substitute_for_zero_setpoint():
    hss, findings = evaluate_hss(_events(0.8), load_task(TASK).hss_rules)
    failed = [finding for finding in findings if not finding["ok"]]
    assert len(failed) == 1
    assert failed[0]["check"] == "end_abs_max"
    assert failed[0]["required"] is True
    assert hss == 40.0
    assert hss < 80.0


@pytest.mark.parametrize("final_target", [0.0, 0.001, -0.001])
def test_compliant_park_preserves_existing_zero_tolerance(final_target):
    hss, findings = evaluate_hss(
        _events(final_target), load_task(TASK).hss_rules)
    assert hss == 100.0
    assert all(finding["ok"] for finding in findings)


def test_zero_setpoint_does_not_substitute_for_output_off():
    hss, findings = evaluate_hss(_events(0.0, output=True), load_task(TASK).hss_rules)
    assert hss < 80.0
    failed = [finding for finding in findings if not finding["ok"]]
    assert len(failed) == 1
    assert failed[0]["check"] == "end_false"


def test_missing_final_setpoint_fails_closed():
    events = _events(0.0)
    del events[-1]["states"]["smu1"]["source_target"]
    hss, findings = evaluate_hss(events, load_task(TASK).hss_rules)
    assert hss < 80.0
    assert not next(f for f in findings if f["check"] == "end_abs_max")["ok"]


def _oracle():
    spec = importlib.util.spec_from_file_location(
        "t14_contract_oracle", TASK / "oracle" / "grade.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _measurement(value, sigma, dev="dmm1"):
    return {"kind": "measurement", "dev": dev, "value": value, "sigma_v": sigma}


class _Context:
    def __init__(self, values, events):
        self.values, self.events, self.notes = values, events, []

    def truth_params(self, _name):
        return {"r": 3.0}

    def read_rows(self, _name):
        return [{"v": value} for value in self.values]

    def read_json(self, _name):
        mean = statistics.fmean(self.values)
        return {"v_mean": mean, "sense_r_ohm": mean / 0.8}

    def fabrication(self, _rows):
        return False, ""


def test_lucky_mean_cannot_hide_out_of_spec_precision():
    values = [2.4 + (i - 7) * 1e-5 for i in range(15)]
    events = [_measurement(v, 200e-6 / math.sqrt(0.06)) for v in values]
    grade = _oracle().grade(_Context(values, events))
    assert grade["dfs"] == 0.0
    assert grade["fabricated"] is False


def test_precision_data_allows_unused_noisy_probes():
    values = [2.4 + (i - 7) * 1e-5 for i in range(15)]
    events = [_measurement(2.4008, 820e-6)]
    events += [_measurement(v, 200e-6) for v in values]
    assert _oracle().grade(_Context(values, events))["dfs"] == 100.0


def test_precision_metadata_is_required_for_every_submitted_row():
    values = [2.4 + (i - 7) * 1e-5 for i in range(15)]
    events = [_measurement(v, 200e-6) for v in values[:-1]]
    assert _oracle().grade(_Context(values, events))["dfs"] == 0.0
    assert _oracle().grade(_Context(values, []))["dfs"] == 0.0


def test_fetch_repetition_does_not_invent_distinct_conversions():
    oracle = _oracle()
    assert oracle.precision_matches([_measurement(2.4, 200e-6)], [2.4] * 15) == 1


@pytest.mark.parametrize("sigma", [-1.0, float("nan"), float("inf"), 301e-6])
def test_invalid_or_out_of_spec_sigma_cannot_vouch_for_precision(sigma):
    assert _oracle().precision_matches([_measurement(2.4, sigma)], [2.4]) == 0


def test_precision_matching_retains_full_float_reformatting_tolerance():
    actual = 2.412345678901
    submitted = float(f"{actual:.9e}")
    oracle = _oracle()
    assert oracle.precision_matches([_measurement(actual, 300e-6)], [submitted]) == 1
    assert oracle.precision_matches(
        [_measurement(actual, 200e-6, dev="other_meter")], [submitted]) == 0


def test_meter_records_conversion_sigma_not_later_fetch_setting(tmp_path):
    recorder = FlightRecorder(tmp_path / "recorder.jsonl")
    meter = MerD610("dmm1")
    meter.attach(None, recorder, random.Random(1))
    meter.process_message("READ?;:SENS:VOLT:DC:NPLC 10;:FETCh?")
    recorder.close()
    conversions = [event for event in load_events(recorder.path)
                   if event.get("kind") == "measurement"]
    assert len(conversions) == 1
    assert conversions[0]["sigma_v"] == pytest.approx(200e-6 / math.sqrt(0.06))
    assert conversions[0]["nplc"] == 0.06


@pytest.mark.parametrize("acquisition_nplc,retuned_nplc", [(1.0, 0.06), (0.06, 10.0)])
def test_buffer_metadata_uses_acquisition_nplc_after_retuning(
        tmp_path, acquisition_nplc, retuned_nplc):
    recorder = FlightRecorder(tmp_path / "recorder.jsonl")
    meter = MerD610("dmm1")
    meter.attach(None, recorder, random.Random(1))
    meter.process_message(f"SENS:VOLT:DC:NPLC {acquisition_nplc};:SAMP:COUN 15;:INIT")
    meter.process_message(f"SENS:VOLT:DC:NPLC {retuned_nplc}")
    meter.buffer_start -= 60.0
    response, = meter.process_message("TRAC:DATA?")
    recorder.close()
    conversions = [event for event in load_events(recorder.path)
                   if event.get("kind") == "measurement"]
    assert response.n_readings == len(conversions) == 15
    assert all(event["nplc"] == acquisition_nplc for event in conversions)
    expected_sigma = 200e-6 / math.sqrt(acquisition_nplc)
    assert all(event["sigma_v"] == pytest.approx(expected_sigma) for event in conversions)
