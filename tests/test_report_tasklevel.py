"""Task-level statistics: the primary comparison unit for conditions.

Runs cluster by task (one submission serves every seed), so run-level
intervals and McNemar overstate the evidence. build_report must expose
task-level pass rates and a task-level paired test alongside.
"""
import json
from pathlib import Path

from osicbench.report import build_report, render_markdown


def _write_run(root: Path, label: str, task: str, seed: int, passed: bool):
    d = root / label / f"{task}_s{seed}"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(
        {"task": task, "seed": seed, "label": label}))
    (d / "grade.json").write_text(json.dumps(
        {"pass": passed, "dfs": 100.0 if passed else 0.0, "hss": 100.0,
         "transactions": 10, "fabricated": False}))


def test_task_level_pass_requires_every_seed(tmp_path):
    # taskA passes both seeds; taskB passes one of two -> task-level 1/2.
    for seed, ok in ((1, True), (2, True)):
        _write_run(tmp_path, "cond", "taskA", seed, ok)
    for seed, ok in ((1, True), (2, False)):
        _write_run(tmp_path, "cond", "taskB", seed, ok)
    rep = build_report(tmp_path)
    c = rep["conditions"]["cond"]
    assert c["pass"]["passed"] == 3 and c["pass"]["total"] == 4
    assert c["task_pass"]["passed"] == 1 and c["task_pass"]["total"] == 2


def test_paired_comparison_reports_both_granularities(tmp_path):
    # Condition a passes both tasks on all seeds; condition b fails taskB
    # entirely -> run-level discordant 2/0, task-level discordant 1/0.
    for task in ("taskA", "taskB"):
        for seed in (1, 2):
            _write_run(tmp_path, "a", task, seed, True)
            _write_run(tmp_path, "b", task, seed, task == "taskA")
    rep = build_report(tmp_path)
    pc = rep["paired_comparison"]
    assert pc["a_pass_b_fail"] == 2 and pc["b_pass_a_fail"] == 0
    assert pc["task_a_pass_b_fail"] == 1 and pc["task_b_pass_a_fail"] == 0
    assert 0.0 < pc["mcnemar_p_tasks"] <= 1.0
    md = render_markdown(rep)
    assert "task level (primary)" in md
