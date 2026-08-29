"""Mode B session mechanics and report aggregation."""
import json
from pathlib import Path

import pytest

from osicbench.report import build_report, render_markdown
from osicbench.runner import LiveSession
from osicbench.taskspec import load_task

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.integration
def test_live_session_reset_budget_and_isolation(tmp_path):
    task = load_task(REPO / "tasks" / "t01_first_light")
    session = LiveSession(task, seed=7, out_dir=tmp_path / "live")
    endpoints = session.start()
    assert endpoints.name == "endpoints.json" and endpoints.parent.name == "io"
    # The live attempt tree must not expose the recorder while running.
    attempt = session.attempt_dir()
    assert not (attempt / "farm" / "recorder.jsonl").exists()
    for _ in range(task.mode_b_resets):
        session.reset()
    with pytest.raises(RuntimeError):
        session.reset()          # budget exhausted
    final = session.finish()
    assert (final / "farm" / "recorder.jsonl").exists()  # collected for grading
    assert json.loads((final / "meta.json").read_text())["mode"] == "b"


def _fake_run(root, label, task, seed, passed, dfs):
    d = root / label / f"{task}_s{seed}"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(
        {"task": task, "seed": seed, "label": label, "mode": "a"}))
    (d / "grade.json").write_text(json.dumps(
        {"pass": passed, "dfs": dfs, "hss": 100.0, "transactions": 10,
         "fabricated": False}))


def test_report_aggregates_and_pairs(tmp_path):
    for seed in (1, 2, 3):
        _fake_run(tmp_path, "bare", "t01", seed, seed == 1, 60.0)
        _fake_run(tmp_path, "skilled", "t01", seed, True, 95.0)
    report = build_report(tmp_path)
    assert report["runs"] == 6
    assert report["conditions"]["skilled"]["pass"]["passed"] == 3
    assert report["conditions"]["bare"]["pass"]["passed"] == 1
    pc = report["paired_comparison"]
    assert pc["b_pass_a_fail"] == 2 and pc["a_pass_b_fail"] == 0
    md = render_markdown(report)
    assert "skilled" in md and "McNemar" in md
