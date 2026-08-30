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
                    "t12_bulk_budget", "t15_dead_leg", "t18_esr_sync"}


def test_all_tasks_load_and_are_complete():
    tasks = discover_tasks(REPO / "tasks")
    assert len(tasks) >= 12
    for task in tasks:
        assert len(task.references()) >= 2, f"{task.id}: needs 2+ reference idioms"
        if task.id in STRATEGY_DIVERSE:
            assert len(task.references()) >= 3, \
                f"{task.id}: needs a materially different strategy reference"
        assert len(task.mutants()) >= 4, f"{task.id}: needs 4+ mutants"
        assert task.brief_path.exists()
        for manual in task.manual_paths:
            assert manual.exists(), f"{task.id}: missing {manual}"


@pytest.mark.integration
def test_restart_after_kill_runs_the_submission_twice(tmp_path):
    import textwrap
    import yaml

    task_dir = tmp_path / "t_restart"
    (task_dir / "oracle").mkdir(parents=True)
    (task_dir / "task.yaml").write_text(yaml.safe_dump({
        "id": "t_restart_probe",
        "wall_clock_limit_s": 12,
        "post_exit_grace_s": 0.2,
        "sigkill_at_s": 3,
        "restart_after_kill_s": 2,
        "farm": {
            "devices": {"dmm1": {"type": "mer_d610"}},
            "duts": {"dut1": {"model": "const_voltage",
                              "params": {"v_true": 1.0}}},
            "wiring": [{"src": "dut1.v", "dst": "dmm1.input_v"}],
        },
    }))
    (task_dir / "brief.md").write_text("probe\n")
    (task_dir / "oracle" / "grade.py").write_text(
        "def grade(ctx):\n    return {'dfs': 0.0, 'fabricated': False}\n")
    submission = tmp_path / "probe.py"
    submission.write_text(textwrap.dedent("""\
        import os, time
        with open(os.path.join(os.environ["OSIC_RESULTS_DIR"], "pids.txt"),
                  "a") as fh:
            fh.write(f"{os.getpid()}\\n")
            fh.flush()
            time.sleep(30)
    """))
    task = load_task(task_dir)
    result = run_submission(task, submission, seed=5, out_dir=tmp_path / "run",
                            label="probe")
    assert result.sigkilled
    pids = (tmp_path / "run" / "results" / "pids.txt").read_text().split()
    assert len(pids) == 2 and pids[0] != pids[1]  # two distinct lives
    import json as _json
    meta = _json.loads((tmp_path / "run" / "meta.json").read_text())
    assert meta["restarted"] is True
