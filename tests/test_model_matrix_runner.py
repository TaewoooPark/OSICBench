"""Frozen orchestration tests; every provider invocation is a local fake."""
import json
import fcntl
import threading
import time
from collections import Counter

import pytest

from experiments.model_matrix import runner


def _audit(condition):
    value = {"valid": True, "errors": [], "requested_model": condition["model"],
             "requested_effort": condition["effort"], "resolved_model": condition["model"]}
    if condition["provider"] == "openai":
        value["resolved_effort"] = condition["effort"]
    else:
        value.update(observed_efforts=[], effort_verification="launch_configuration_only")
    return value


@pytest.fixture
def experiment(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    for name in ("t01", "t02"):
        task = repo / "tasks" / name
        task.mkdir(parents=True)
        (task / "task.yaml").write_text("manuals: []\n")
        (task / "brief.md").write_text(f"Task {name}\n")
    monkeypatch.setattr(runner.runtime, "REPO", repo)
    monkeypatch.setattr(runner.runtime, "source_hash", lambda: "sources")
    monkeypatch.setattr(runner.matrix, "_benchmark_hash", lambda: "benchmark")
    monkeypatch.setattr(runner, "_commit", lambda: "commit")
    monkeypatch.setattr(runner, "_environment", lambda: {"cli_versions": {"openai": "fake", "anthropic": "fake"}})
    conditions = [{"id": "alpha", "provider": "openai", "model": "gpt-test", "effort": "low"},
                  {"id": "beta", "provider": "anthropic", "model": "claude-test", "effort": "max"}]
    cases = {"schema_version": 1, "samples": 2, "seeds": [1, 2], "schedule_seed": 101,
             "conditions": conditions, "contrasts": [{"id": "a-b", "a": "alpha", "b": "beta"}]}
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(json.dumps(cases))
    preflight = tmp_path / "probes"
    for condition in conditions:
        record = {"condition": condition, "passed": True, "source_exact": True,
                  "effective_effort_checked": True,
                  "probe": {"parent_read_blocked": True, "network_blocked": True,
                            "effort": condition["effort"]},
                  "process": {"exit_code": 0, "timed_out": False},
                  "provider_audit": _audit(condition)}
        runner.runtime.write_json(preflight / "preflight" / condition["id"] / "probe-001" /
                                  "preflight.json", record)
    calls, grades, prechecks = [], [], []

    def precheck(condition):
        prechecks.append(condition["id"])
        return {"allowed": True, "remaining_percent": 50}

    def guard(result):
        limited = result.get("provider_audit", {}).get("rate_limit", {}).get("detected", False)
        return {"stop": limited, "reason": "quota_exhausted" if limited else None,
                "rate_limited": limited}

    def execute(condition, **kwargs):
        workspace = kwargs["workspace"]
        calls.append((condition["id"], workspace))
        assert (workspace / "brief.md").is_file()
        assert not kwargs["runtime_dir"].exists()
        kwargs["runtime_dir"].mkdir(parents=True)
        record = runner._read(kwargs["evaluation_root"] / "authoring" /
                              workspace.parts[-3] / f"{workspace.parts[-2]}.json")
        assert record["status"] == "running"
        (workspace / "main.py").write_text("print('submission')\n")
        return {"provider_audit": _audit(condition), "artifact_present": True,
                "artifact_sha256": runner.matrix._artifact_hash(workspace), "wall_s": 1.0,
                "exit_code": 0, "timed_out": False}

    actual_grade = runner.matrix._grade_one

    def grade(label, workspace, task, seed, out):
        grades.append((label, task, seed))
        assert "artifacts" in workspace.parts
        fingerprint = runner.matrix._artifact_hash(workspace)
        if fingerprint is None:
            return actual_grade(label, workspace, task, seed, out)
        destination = out / label / f"{task}_s{seed}"
        identity = {"label": label, "task": task, "seed": seed}
        runner.runtime.write_json(destination / "meta.json", identity)
        runner.runtime.write_json(destination / "adapter.json", {"artifact_sha256": fingerprint})
        runner.runtime.write_json(destination / "grade.json", {"pass": True, "dfs": 100, "hss": 100})
        return task, seed, {"pass": True}

    monkeypatch.setattr(runner, "_subscription_check", precheck)
    monkeypatch.setattr(runner, "_guard", guard)
    monkeypatch.setattr(runner.runtime, "execute", execute)
    monkeypatch.setattr(runner.matrix, "_grade_one", grade)
    return {"out": tmp_path / "scoring", "cases": cases_path, "preflight": preflight,
            "calls": calls, "grades": grades, "prechecks": prechecks,
            "execute": execute, "grade": grade, "conditions": conditions}


def _plan(fixture):
    return runner.plan_experiment(fixture["out"], fixture["cases"], fixture["preflight"])


def test_plan_freezes_full_denominator_schedule_and_preflight(experiment):
    plan = _plan(experiment)
    assert len(plan["schedule"]) == 8
    assert len(plan["expected_runs"]) == 16
    assert [r["sample"] for r in plan["schedule"]] == [1] * 4 + [2] * 4
    assert [r["condition"] for r in plan["schedule"][:4]] == ["alpha", "beta", "beta", "alpha"]
    assert plan["benchmark_sha256"] == "benchmark"
    assert all(value["sha256"] for value in plan["preflight"].values())
    assert experiment["calls"] == []
    with pytest.raises(ValueError, match="already exists"):
        _plan(experiment)


@pytest.mark.parametrize("change", ["missing", "model", "effort", "canary"])
def test_invalid_preflight_is_rejected_before_output_or_calls(experiment, change):
    path = experiment["preflight"] / "preflight" / "beta" / "probe-001" / "preflight.json"
    record = runner._read(path)
    if change == "missing":
        path.unlink()
    else:
        if change == "model":
            record["provider_audit"]["resolved_model"] = "other"
        elif change == "effort":
            record["probe"]["effort"] = "low"
        else:
            record["probe"]["network_blocked"] = False
        runner.runtime.write_json(path, record)
    with pytest.raises(ValueError, match="no passed"):
        _plan(experiment)
    assert not experiment["out"].exists()
    assert not experiment["calls"]


def test_failed_newer_preflight_does_not_replace_passed_evidence(experiment):
    base = experiment["preflight"] / "preflight" / "alpha"
    failed = runner._read(base / "probe-001" / "preflight.json")
    failed["passed"] = False
    runner.runtime.write_json(base / "probe-002" / "preflight.json", failed)
    assert _plan(experiment)["preflight"]["alpha"]["attempt"] == "probe-001"


@pytest.mark.parametrize("change", ["plan", "source", "environment"])
def test_changed_plan_source_or_cli_is_rejected(experiment, monkeypatch, change):
    _plan(experiment)
    if change == "plan":
        path = experiment["out"] / "evaluation_manifest.json"
        record = runner._read(path)
        record["prompt"] += "changed"
        runner.runtime.write_json(path, record)
    elif change == "source":
        monkeypatch.setattr(runner.runtime, "source_hash", lambda: "changed")
    else:
        monkeypatch.setattr(runner, "_environment", lambda: {"cli_versions": "changed"})
    with pytest.raises(ValueError, match="changed"):
        runner.run_experiment(experiment["out"])
    assert not experiment["calls"]


def test_success_freezes_all_seeds_and_resume_never_reauthors(experiment):
    _plan(experiment)
    result = runner.run_experiment(experiment["out"])
    assert result["authoring"] == {"completed": 8}
    assert result["grading"] == {"graded": 16}
    assert len(experiment["calls"]) == len(experiment["prechecks"]) == 8
    for _, workspace in experiment["calls"]:
        (workspace / "main.py").write_text("original writable workspace changed\n")
    runner.run_experiment(experiment["out"])
    assert len(experiment["calls"]) == 8
    assert len(set(experiment["grades"])) == 16


def test_author_only_freezes_without_grading_then_run_grades_without_reauthoring(experiment):
    manifest = _plan(experiment)
    result = runner.run_experiment(experiment["out"], grade_completed=False)
    assert result["authoring"] == {"completed": 8}
    assert result["grading"] == {"missing": 16}
    assert experiment["grades"] == []
    assert len(experiment["calls"]) == 8
    assert runner._read(experiment["out"] / "evaluation_manifest.json") == manifest
    result = runner.run_experiment(experiment["out"])
    assert result["grading"] == {"graded": 16}
    assert len(experiment["calls"]) == 8
    assert len(experiment["grades"]) == 16


def test_stopped_provider_does_not_block_grading_existing_later_wave(experiment, monkeypatch):
    _plan(experiment)

    def execute(condition, **kwargs):
        result = experiment["execute"](condition, **kwargs)
        if condition["provider"] == "openai" and "@s2" in kwargs["workspace"].parts[-3]:
            result["provider_audit"]["rate_limit"] = {"detected": True}
        return result

    monkeypatch.setattr(runner.runtime, "execute", execute)
    authored = runner.run_experiment(experiment["out"], grade_completed=False)
    assert authored["authoring"] == {"completed": 6, "blocked": 1, "deferred": 1}
    assert experiment["grades"] == []
    calls = len(experiment["calls"])
    graded = runner.run_experiment(experiment["out"])
    assert graded["grading"] == {"graded": 12, "missing": 4}
    assert len(experiment["calls"]) == calls
    assert graded["providers"]["openai"]["status"] == "stopped"


@pytest.mark.parametrize("exit_code,timed_out", [(None, True), (7, False)])
def test_valid_artifact_survives_timeout_or_nonzero_exit(experiment, monkeypatch, exit_code, timed_out):
    _plan(experiment)

    def execute(condition, **kwargs):
        result = experiment["execute"](condition, **kwargs)
        result.update(exit_code=exit_code, timed_out=timed_out)
        return result

    monkeypatch.setattr(runner.runtime, "execute", execute)
    result = runner.run_experiment(experiment["out"])
    assert result["grading"] == {"graded": 16}


@pytest.mark.parametrize("timed_out", [False, True])
def test_no_artifact_records_each_seed_without_content_retry(experiment, monkeypatch, timed_out):
    _plan(experiment)

    def execute(condition, **kwargs):
        result = experiment["execute"](condition, **kwargs)
        (kwargs["workspace"] / "main.py").unlink()
        result.update(artifact_present=False, artifact_sha256=None, timed_out=timed_out,
                      exit_code=None if timed_out else 0)
        return result

    monkeypatch.setattr(runner.runtime, "execute", execute)
    result = runner.run_experiment(experiment["out"])
    assert result["authoring"] == {"completed": 8}
    assert result["grading"] == {"failure": 16}
    assert len(experiment["calls"]) == 8


@pytest.mark.parametrize("violation", ["quota", "model", "effort", "cleanup", "process"])
def test_stopped_provider_keeps_missing_rows_and_blocks_later_samples(experiment, monkeypatch, violation):
    _plan(experiment)

    def execute(condition, **kwargs):
        result = experiment["execute"](condition, **kwargs)
        if condition["id"] == "alpha":
            if violation == "quota":
                result["provider_audit"]["rate_limit"] = {"detected": True}
            elif violation == "model":
                result["provider_audit"]["resolved_model"] = "other-model"
            elif violation == "effort":
                result["provider_audit"]["resolved_effort"] = "other-effort"
            elif violation == "cleanup":
                result["cleanup_error"] = "PermissionError"
            else:
                (kwargs["workspace"] / "main.py").unlink()
                result.update(artifact_present=False, artifact_sha256=None, exit_code=1)
        return result

    monkeypatch.setattr(runner.runtime, "execute", execute)
    result = runner.run_experiment(experiment["out"])
    assert result["providers"]["openai"]["status"] == "stopped"
    assert result["providers"]["anthropic"]["status"] == "ready"
    assert Counter(c[0] for c in experiment["calls"]) == {"alpha": 1, "beta": 2}
    assert result["grading"] == {"missing": 12, "graded": 4}
    runner.run_experiment(experiment["out"])
    assert len(experiment["calls"]) == 3


def test_precheck_denial_prevents_provider_call(experiment, monkeypatch):
    manifest = _plan(experiment)
    monkeypatch.setattr(runner, "_subscription_check", lambda c: {
        "allowed": c["provider"] != "openai", "reason": "paid_usage_enabled"})
    result = runner.run_experiment(experiment["out"])
    assert result["providers"]["openai"]["reason"] == "paid_usage_enabled"
    assert {c[0] for c in experiment["calls"]} == {"beta"}
    row = next(row for row in manifest["schedule"] if row["provider"] == "openai")
    assert runner._read(runner._record_path(experiment["out"], row))["attempts"] == []


def test_explicit_resume_invokes_precheck_deferred_row_once_and_keeps_history(experiment, monkeypatch):
    manifest = _plan(experiment)
    monkeypatch.setattr(runner, "_subscription_check", lambda c: {
        "allowed": c["provider"] != "openai", "reason": "quota_headroom_low"})
    runner.run_experiment(experiment["out"], grade_completed=False)
    row = next(row for row in manifest["schedule"] if row["provider"] == "openai")
    path = runner._record_path(experiment["out"], row)
    denied = runner._read(path)
    assert denied["status"] == "deferred" and denied["attempts"] == []
    for directory in ("workspaces", "runtimes", "logs"):
        assert not (experiment["out"] / "private" / directory / row["label"] / row["task"]).exists()
    monkeypatch.setattr(runner, "_subscription_check", lambda c: {"allowed": True})
    runner.run_experiment(experiment["out"], grade_completed=False)
    assert {condition for condition, _ in experiment["calls"]} == {"beta"}
    completed = runner.run_experiment(experiment["out"], ("openai",), grade_completed=False)
    assert completed["authoring"] == {"completed": 8}
    assert len(experiment["calls"]) == 8
    record = runner._read(path)
    assert len(record["attempts"]) == 1
    assert len(record["subscription_prechecks"]) == 2
    assert record["subscription_prechecks"][0] == denied["subscription_prechecks"][0]
    assert all(entry["checked_at"] for entry in record["subscription_prechecks"])


def test_soft_headroom_stop_preserves_completed_artifact(experiment, monkeypatch):
    _plan(experiment)
    monkeypatch.setattr(runner, "_guard", lambda result: {
        "stop": result["provider_audit"]["requested_model"] == "gpt-test",
        "reason": "quota_headroom_low", "rate_limited": True})
    result = runner.run_experiment(experiment["out"])
    assert result["providers"]["openai"]["reason"] == "quota_headroom_low"
    assert result["authoring"] == {"completed": 3, "deferred": 5}
    assert result["grading"] == {"graded": 6, "missing": 10}


def test_explicit_resume_does_not_repeat_a_blocked_model_call(experiment, monkeypatch):
    _plan(experiment)
    monkeypatch.setattr(runner, "_guard", lambda result: {
        "stop": result["provider_audit"]["requested_model"] == "gpt-test",
        "reason": "quota_exhausted", "rate_limited": True})
    runner.run_experiment(experiment["out"])
    first_alpha = next(path for condition, path in experiment["calls"] if condition == "alpha")
    monkeypatch.setattr(runner, "_guard", lambda result: {"stop": False, "rate_limited": False})
    result = runner.run_experiment(experiment["out"], ("openai",))
    assert result["providers"]["openai"]["status"] == "ready"
    assert Counter(path for _, path in experiment["calls"])[first_alpha] == 1
    assert result["authoring"] == {"blocked": 1, "completed": 7}
    assert result["grading"] == {"missing": 2, "graded": 14}


@pytest.mark.parametrize("always_fail", [False, True])
def test_launch_failure_has_at_most_one_fresh_noartifact_retry(experiment, monkeypatch, always_fail):
    _plan(experiment)
    failures = []

    def execute(condition, **kwargs):
        if condition["id"] == "alpha" and (always_fail or not failures):
            failures.append(kwargs["workspace"])
            return {"launch_error": "FileNotFoundError", "exit_code": None, "timed_out": False,
                    "artifact_present": False, "artifact_sha256": None, "wall_s": 0,
                    "provider_audit": {"valid": False, "errors": ["model_unverified", "no_completed_turn"]}}
        return experiment["execute"](condition, **kwargs)

    monkeypatch.setattr(runner.runtime, "execute", execute)
    result = runner.run_experiment(experiment["out"])
    assert len(failures) == (2 if always_fail else 1)
    if always_fail:
        assert failures[0] != failures[1]
        assert result["providers"]["openai"]["status"] == "stopped"
    else:
        assert result["grading"] == {"graded": 16}


@pytest.mark.parametrize("state", ["running", "retry_pending"])
def test_unresolved_attempt_refuses_resume(experiment, state):
    manifest = _plan(experiment)
    row = manifest["schedule"][0]
    record = {key: row[key] for key in ("label", "condition", "sample", "task")}
    runner.runtime.write_json(runner._record_path(experiment["out"], row), dict(record, status=state))
    with pytest.raises(ValueError, match="unresolved running"):
        runner.run_experiment(experiment["out"])
    assert not experiment["calls"]


def test_snapshot_and_record_mutations_refuse_resume(experiment):
    manifest = _plan(experiment)
    runner.run_experiment(experiment["out"])
    row = manifest["schedule"][0]
    artifact = runner._artifact_path(experiment["out"], row) / "main.py"
    artifact.write_text("changed\n")
    with pytest.raises(ValueError, match="artifact changed"):
        runner.run_experiment(experiment["out"])
    assert len(experiment["calls"]) == 8


def test_one_author_per_provider_and_sample_barrier(experiment, monkeypatch):
    _plan(experiment)
    active, max_active, completed = Counter(), Counter(), Counter()
    lock = threading.Lock()

    def execute(condition, **kwargs):
        label = kwargs["workspace"].parts[-3]
        sample = 2 if "@s2" in label else 1
        with lock:
            if sample == 2:
                assert completed[1] == 4
            active[condition["provider"]] += 1
            max_active[condition["provider"]] = max(max_active[condition["provider"]], active[condition["provider"]])
        time.sleep(0.005)
        result = experiment["execute"](condition, **kwargs)
        with lock:
            active[condition["provider"]] -= 1
            completed[sample] += 1
        return result

    monkeypatch.setattr(runner.runtime, "execute", execute)
    runner.run_experiment(experiment["out"])
    assert max_active == {"openai": 1, "anthropic": 1}
    assert completed == {1: 4, 2: 4}


def test_concurrent_evaluator_is_rejected_before_calls(experiment):
    _plan(experiment)
    with (experiment["out"] / ".runner.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="another evaluator"):
            runner.run_experiment(experiment["out"])
    assert experiment["calls"] == []


def test_grading_integrity_failure_stops_later_waves(experiment, monkeypatch):
    _plan(experiment)

    def grade(label, *args):
        if label == "alpha":
            raise ValueError("stale grade")
        return experiment["grade"](label, *args)

    monkeypatch.setattr(runner.matrix, "_grade_one", grade)
    result = runner.run_experiment(experiment["out"])
    assert result["providers"]["openai"]["reason"] == "grading_integrity_failure"
    assert not any("@s2" in path.parts[-3] for _, path in experiment["calls"])


def test_runner_uses_real_nested_quota_guard_without_model_calls(experiment, monkeypatch):
    from experiments.model_matrix.subscription_guard import runtime_stop_reason
    _plan(experiment)

    def execute(condition, **kwargs):
        result = experiment["execute"](condition, **kwargs)
        if condition["provider"] == "openai":
            result["provider_audit"]["rate_limit"] = {
                "observed": [{"primary": {"used_percent": 100}}]}
        return result

    def guard(result):
        reason = runtime_stop_reason(result)
        return {"stop": reason is not None, "reason": reason, "rate_limited": reason is not None}

    monkeypatch.setattr(runner.runtime, "execute", execute)
    monkeypatch.setattr(runner, "_guard", guard)
    result = runner.run_experiment(experiment["out"])
    assert result["providers"]["openai"]["reason"] == "quota_exhausted"
    assert result["grading"] == {"missing": 12, "graded": 4}


@pytest.mark.parametrize("rejected", [False, True])
def test_completed_response_at_last_subscription_capacity(experiment, monkeypatch, rejected):
    _plan(experiment)

    def execute(condition, **kwargs):
        result = experiment["execute"](condition, **kwargs)
        if condition["provider"] == "openai":
            result["provider_audit"]["trace"] = {"completed": True}
            result["provider_audit"]["rate_limit"] = {
                "observed": [{"primary": {"used_percent": 100}}], "detected": rejected}
        return result

    monkeypatch.setattr(runner.runtime, "execute", execute)
    monkeypatch.setattr(runner, "_guard", lambda result: {
        "stop": result["provider_audit"]["requested_model"] == "gpt-test",
        "reason": "quota_exhausted", "rate_limited": True})
    result = runner.run_experiment(experiment["out"])
    assert result["providers"]["openai"]["reason"] == "quota_exhausted"
    assert result["grading"]["graded"] == (4 if rejected else 6)
    assert result["authoring"].get("blocked", 0) == int(rejected)
