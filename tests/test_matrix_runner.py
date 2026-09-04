"""Matrix accounting and subprocess regressions without external model calls."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters import matrix_runner as matrix
from osicbench.report import build_report


@pytest.fixture
def planned(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    for task in ("t01", "t02"):
        root = repo / "tasks" / task
        root.mkdir(parents=True)
        (root / "task.yaml").write_text("manuals: []\n")
        (root / "brief.md").write_text("Write main.py.\n")
    monkeypatch.setattr(matrix, "REPO", repo)
    args = SimpleNamespace(workdir=str(tmp_path / "work"), runs_dir=str(tmp_path / "runs"),
                           seeds="101,102", samples=3, sample=1, jobs=1,
                           agent="bare", force=False, skip_done=False)
    agents = {name: {"command": [sys.executable, "-c", "pass"]}
              for name in ("bare", "skill")}
    matrix.plan(args, agents)
    return args, agents


def _artifact(args, task="t01", text="pass\n"):
    ws = matrix.ws_dir(Path(args.workdir), args.agent, task, args.sample)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "main.py").write_text(text)
    matrix._write_json(Path(args.runs_dir) / "authoring" / args.agent / f"{task}.json",
                       {"artifact_sha256": matrix._artifact_hash(ws), "attempts": []})
    return ws


def test_plan_freezes_entire_matrix_before_calls(planned):
    args, agents = planned
    report = build_report(Path(args.runs_dir))
    assert report["runs"] == 24
    assert report["coverage"]["missing_run"] == 24
    assert report["conditions"]["bare"]["dfs_mean"] is None
    assert len(report["paired_comparison"]["per_sample"]) == 3
    matrix.plan(args, agents)
    args.samples = 2
    with pytest.raises(ValueError, match="frozen"):
        matrix.plan(args, agents)


@pytest.mark.parametrize("change", ["seeds", "command", "task", "duplicate", "prompt"])
def test_plan_drift_is_rejected(planned, change, monkeypatch):
    args, agents = planned
    if change == "seeds":
        args.seeds = "101"
    elif change == "command":
        agents["bare"]["command"] = ["another-cli"]
    elif change == "task":
        (matrix.REPO / "tasks/t01/brief.md").write_text("Changed task.\n")
    elif change == "prompt":
        monkeypatch.setattr(matrix, "PROMPT", "Changed prompt")
    else:
        path = Path(args.runs_dir) / "evaluation_manifest.json"
        payload = json.loads(path.read_text())
        payload["expected_runs"].append(payload["expected_runs"][0])
        path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        matrix._planned_rows(args, agents)


def test_missing_artifacts_stay_in_denominator(planned):
    args, agents = planned
    matrix.grade(args, agents)
    report = build_report(Path(args.runs_dir))
    condition = report["conditions"]["bare"]
    assert condition["pass"]["total"] == 12
    assert condition["coverage"]["failure_reasons"] == {"missing_author_artifact": 4}
    assert condition["coverage"]["graded_runs"] == 0
    assert condition["hss_mean"] is None


def test_skip_done_rejects_unrecorded_artifact(planned):
    args, agents = planned
    matrix.prep(args, agents)
    ws = matrix.ws_dir(Path(args.workdir), args.agent, "t01", 1)
    (ws / "main.py").write_text("pass\n")
    args.skip_done = True
    with pytest.raises(ValueError, match="unrecorded"):
        matrix.author(args, agents)
    with pytest.raises(ValueError, match="provenance"):
        matrix._grade_one("bare", ws, "t01", 101, Path(args.runs_dir))


def test_completed_failure_is_not_an_extra_author_retry(planned, monkeypatch):
    args, agents = planned
    for task in ("t01", "t02"):
        matrix._write_json(Path(args.runs_dir) / "authoring/bare" / f"{task}.json",
                           {"artifact_sha256": None, "attempts": [{"exit_code": 1}]})
    monkeypatch.setattr(matrix, "_author_one", lambda *a: pytest.fail("extra retry"))
    args.skip_done = True
    matrix.author(args, agents)
    args.force = True
    with pytest.raises(ValueError, match="frozen"):
        matrix.prep(args, agents)


@pytest.mark.parametrize("status", ["running", "retry_pending"])
def test_interrupted_author_attempt_requires_inspection(planned, status):
    args, agents = planned
    ws = _artifact(args)
    matrix._write_json(Path(args.runs_dir) / "authoring/bare/t01.json",
                       {"status": status, "artifact_sha256": matrix._artifact_hash(ws)})
    args.skip_done = True
    with pytest.raises(ValueError, match="unresolved"):
        matrix.author(args, agents)
    with pytest.raises(ValueError, match="unresolved"):
        matrix._grade_one("bare", ws, "t01", 101, Path(args.runs_dir))


@pytest.mark.parametrize("label", ["evaluation_manifest.json", "authoring", "grading_logs", "_report"])
def test_reserved_labels_rejected(planned, label):
    args, agents = planned
    agents["bare"]["label"] = label
    with pytest.raises(ValueError, match="reserved"):
        matrix.plan(args, agents)


def test_inside_repository_workspace_rejected(planned):
    args, agents = planned
    args.workdir = str(matrix.REPO / "workspace")
    with pytest.raises(ValueError, match="outside"):
        matrix.plan(args, agents)


def test_new_plan_cannot_adopt_old_workspace(planned):
    args, agents = planned
    _artifact(args)
    args.runs_dir += "-new"
    with pytest.raises(ValueError, match="fresh agent workspaces"):
        matrix.plan(args, agents)


@pytest.mark.parametrize("directory", [False, True])
def test_artifact_cannot_import_unhashed_symlink_content(planned, tmp_path, directory):
    args, _ = planned
    ws = _artifact(args)
    external = tmp_path / "external"
    external.mkdir()
    helper = external / "helper.py"
    helper.write_text("value = 1\n")
    (ws / "linked").symlink_to(external if directory else helper, target_is_directory=directory)
    with pytest.raises(ValueError, match="symlinks"):
        matrix._artifact_hash(ws)
    helper.write_text("value = 2\n")
    with pytest.raises(ValueError, match="symlinks"):
        matrix._grade_one("bare", ws, "t01", 101, Path(args.runs_dir))


@pytest.mark.parametrize("exit_code,timed_out", [(0, False), (1, False), (None, True)])
def test_author_artifact_is_kept_at_exit_or_cap(tmp_path, monkeypatch, exit_code, timed_out):
    work = tmp_path / "work"
    ws = matrix.ws_dir(work, "bare", "t01", 1)
    ws.mkdir(parents=True)
    calls = []

    def fake_run(*a, **kw):
        calls.append(kw)
        started = json.loads((tmp_path / "runs/authoring/bare/t01.json").read_text())
        assert started["status"] == "running"
        assert started["attempts"][0]["status"] == "running"
        (ws / "main.py").write_text("pass\n")
        return dict(exit_code=exit_code, timed_out=timed_out, wall_s=1)

    monkeypatch.setattr(matrix, "_run_logged", fake_run)
    result = matrix._author_one("bare", {"command": ["fake"]}, "t01", work, 1, tmp_path / "runs")
    assert result[1] is True
    assert len(calls) == 1
    record = json.loads((tmp_path / "runs/authoring/bare/t01.json").read_text())
    assert record["attempts"][0]["timed_out"] is timed_out
    assert record["artifact_sha256"] == matrix._artifact_hash(ws)
    assert record["status"] == "completed"


def test_author_retry_preserves_each_log_and_failure(tmp_path, monkeypatch):
    work = tmp_path / "work"
    matrix.ws_dir(work, "bare", "t01", 1).mkdir(parents=True)
    monkeypatch.setattr(matrix.time, "sleep", lambda _: None)
    result = matrix._author_one("bare", {"command": ["/not/a/real/cli"]},
                                "t01", work, 1, tmp_path / "runs")
    assert result[1] is False
    logs = tmp_path / "runs/authoring/bare"
    assert len(list(logs.glob("*.log"))) == 2
    record = json.loads((logs / "t01.json").read_text())
    assert len(record["attempts"]) == 2
    assert all(r.get("error") for r in record["attempts"])


@pytest.mark.parametrize("timeout", [False, True])
def test_grade_infrastructure_failure_is_durable(planned, monkeypatch, timeout):
    args, _ = planned
    ws = _artifact(args)
    monkeypatch.setattr(matrix, "_run_logged", lambda *a, **kw:
                        dict(exit_code=None if timeout else 2, timed_out=timeout, wall_s=1))
    result = matrix._grade_one("bare", ws, "t01", 101, Path(args.runs_dir))
    assert result[2] is None
    report = build_report(Path(args.runs_dir))
    reason = "grading_timeout" if timeout else "grading_error"
    assert report["coverage"]["failure_reasons"] == {reason: 1}


def test_cached_grade_requires_same_artifact_and_successful_harness(planned, monkeypatch):
    args, _ = planned
    ws = _artifact(args)
    root = Path(args.runs_dir)
    out = root / "bare/t01_s101"

    def fake_run(*a, **kw):
        matrix._write_json(out / "meta.json", {"label": "bare", "task": "t01", "seed": 101})
        matrix._write_json(out / "grade.json", {"pass": False, "dfs": 0, "hss": 100})
        # A normally graded failure uses CLI exit 1, not an infrastructure error.
        return dict(exit_code=1, timed_out=False, wall_s=1)

    monkeypatch.setattr(matrix, "_run_logged", fake_run)
    assert matrix._grade_one("bare", ws, "t01", 101, root)[2]["pass"] is False
    monkeypatch.setattr(matrix, "_run_logged", lambda *a, **kw: pytest.fail("cached grade rerun"))
    assert matrix._grade_one("bare", ws, "t01", 101, root)[2]["pass"] is False
    (ws / "main.py").write_text("print('changed')\n")
    with pytest.raises(ValueError, match="changed"):
        matrix._grade_one("bare", ws, "t01", 101, root)


def test_cleanup_failure_cannot_be_reported_as_pass(planned, monkeypatch):
    args, _ = planned
    ws = _artifact(args)
    root = Path(args.runs_dir)
    out = root / "bare/t01_s101"

    def fake_run(*a, **kw):
        matrix._write_json(out / "meta.json", {"label": "bare", "task": "t01", "seed": 101})
        matrix._write_json(out / "grade.json", {"pass": True})
        return dict(exit_code=0, timed_out=False, wall_s=1, cleanup_error="permission denied")

    monkeypatch.setattr(matrix, "_run_logged", fake_run)
    with pytest.raises(RuntimeError, match="infrastructure"):
        matrix._grade_one("bare", ws, "t01", 101, root)
    with pytest.raises(ValueError, match="both grade and failure"):
        build_report(root)
    with pytest.raises(ValueError):
        matrix._grade_one("bare", ws, "t01", 101, root)


def test_logged_timeout_reaps_group_without_workspace_regex(tmp_path):
    sibling = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)",
                                str(tmp_path / "task__s2")], start_new_session=True)
    try:
        result = matrix._run_logged(
            [sys.executable, "-c", "import time; time.sleep(30)", str(tmp_path / "task")],
            cwd=tmp_path, env=None, stdout=tmp_path / "out.log", stderr=tmp_path / "err.log",
            timeout=0.1)
        assert result["timed_out"]
        assert not result.get("cleanup_error")
        assert sibling.poll() is None
    finally:
        os.killpg(sibling.pid, signal.SIGKILL)
        sibling.wait(timeout=5)


def test_timeout_reaps_separate_session_descendant(tmp_path):
    child_file = tmp_path / "child.pid"
    script = ("import pathlib, subprocess, sys, time; "
              "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],"
              "start_new_session=True); pathlib.Path(sys.argv[1]).write_text(str(p.pid)); "
              "time.sleep(30)")
    result = matrix._run_logged([sys.executable, "-c", script, str(child_file)],
                               cwd=tmp_path, env=None, stdout=tmp_path / "out.log",
                               stderr=tmp_path / "err.log", timeout=0.5)
    assert result["timed_out"]
    child_pid = int(child_file.read_text())
    # A killed descendant may briefly be a zombie pending its system reaper.
    for _ in range(50):
        status = subprocess.run(["ps", "-p", str(child_pid), "-o", "stat="],
                                capture_output=True, text=True).stdout.strip()
        if not status or status.startswith("Z"):
            break
        time.sleep(0.02)
    else:
        os.kill(child_pid, signal.SIGKILL)
        pytest.fail("separate-session descendant survived timeout")


@pytest.mark.integration
def test_real_cli_grade_and_cached_report_round_trip(tmp_path):
    task_id = "t01_first_light"
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "main.py").write_text(
        (matrix.REPO / "tasks" / task_id / "reference/ref_procedural.py").read_text())
    root = tmp_path / "runs"
    matrix._write_json(root / "evaluation_manifest.json", {
        "schema_version": 1, "expected_runs": [
            {"label": "reference", "condition": "reference", "sample": 1,
             "task": task_id, "seed": 99}]})
    matrix._write_json(root / "authoring/reference" / f"{task_id}.json", {
        "status": "completed", "attempts": [],
        "artifact_sha256": matrix._artifact_hash(ws)})
    first = matrix._grade_one("reference", ws, task_id, 99, root)
    assert first[2]["pass"] is True
    assert matrix._grade_one("reference", ws, task_id, 99, root) == first
    report = build_report(root)
    assert report["coverage"]["verified"] is True
    assert report["coverage"]["graded_runs"] == 1
    assert report["conditions"]["reference"]["task_pass"]["rate"] == 1.0


def test_main_resolves_relative_roots(tmp_path, monkeypatch):
    config = tmp_path / "agents.json"
    config.write_text(json.dumps({"bare": {"command": ["fake"]}}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["matrix_runner.py", "plan", "--agents", str(config),
                                     "--workdir", "work", "--runs-dir", "runs"])
    captured = []
    monkeypatch.setattr(matrix, "plan", lambda args, agents: captured.append(args))
    matrix.main()
    assert captured[0].workdir == str((tmp_path / "work").resolve())
    assert captured[0].runs_dir == str((tmp_path / "runs").resolve())
