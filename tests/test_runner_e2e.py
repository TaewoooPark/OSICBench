"""End-to-end harness round-trip: run a real reference through the runner."""
from pathlib import Path

import pytest

from osicbench.grading import grade_run
from osicbench.runner import run_submission
from osicbench.taskspec import discover_tasks, load_task

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.integration
def test_t01_reference_round_trip(tmp_path):
    task = load_task(REPO / "tasks" / "t01_first_light")
    ref = task.task_dir / "reference" / "ref_procedural.py"
    result = run_submission(task, ref, seed=99, out_dir=tmp_path / "run", label="test")
    assert result.exit_code == 0
    grade = grade_run(task, 99, tmp_path / "run")
    assert grade["pass"] is True
    assert grade["dfs"] >= 70
    assert (tmp_path / "run" / "farm" / "recorder.jsonl").exists()
    assert (tmp_path / "run" / "grade.json").exists()


@pytest.mark.integration
def test_t01_mutant_fails_round_trip(tmp_path):
    task = load_task(REPO / "tasks" / "t01_first_light")
    mutant = task.task_dir / "mutants" / "m3_fabricate.py"
    run_submission(task, mutant, seed=99, out_dir=tmp_path / "run", label="test")
    grade = grade_run(task, 99, tmp_path / "run")
    assert grade["pass"] is False
    assert grade["fabricated"] is True


STRATEGY_DIVERSE = {"t01_first_light", "t03_diode_iv", "t05_resonance",
                    "t12_bulk_budget"}


def test_all_tasks_load_and_are_complete():
    tasks = discover_tasks(REPO / "tasks")
    assert len(tasks) == 12
    for task in tasks:
        assert len(task.references()) >= 2, f"{task.id}: needs 2+ reference idioms"
        if task.id in STRATEGY_DIVERSE:
            assert len(task.references()) >= 3, \
                f"{task.id}: needs a materially different strategy reference"
        assert len(task.mutants()) >= 4, f"{task.id}: needs 4+ mutants"
        assert task.brief_path.exists()
        for manual in task.manual_paths:
            assert manual.exists(), f"{task.id}: missing {manual}"
