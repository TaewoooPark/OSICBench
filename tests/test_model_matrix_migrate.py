"""No-network migration tests with sealed synthetic authoring evidence."""
import copy
import fcntl
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from experiments.model_matrix import claude_adapter, migrate, runner, runtime


def _read(path):
    return json.loads(path.read_text())


def _snapshot(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


def _audit(condition):
    audit = {"valid": True, "errors": [], "requested_model": condition["model"],
             "resolved_model": condition["model"], "requested_effort": condition["effort"]}
    if condition["provider"] == "openai":
        audit["resolved_effort"] = condition["effort"]
    else:
        audit.update(observed_efforts=[], effort_verification="launch_configuration_only")
    return audit


def _refusal(workspace, model):
    return [
        {"type": "system", "subtype": "init", "model": model, "cwd": str(workspace),
         "plugins": [], "skills": [], "mcp_servers": [], "slash_commands": [],
         "tools": list(claude_adapter.TOOLS), "apiKeySource": "none"},
        {"type": "system", "subtype": "model_refusal_no_fallback", "original_model": model,
         "api_refusal_category": "cyber"},
        {"type": "assistant", "is_api_error_message": True, "error": "invalid_request",
         "message": {"model": "<synthetic>", "stop_reason": "refusal", "content": [],
                     "stop_details": {"type": "refusal", "category": "cyber"}}},
        {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "isUsingOverage": False}},
        {"type": "result", "subtype": "success", "is_error": True, "stop_reason": "refusal",
         "terminal_reason": "api_error", "api_error_status": None, "modelUsage": {model: {"inputTokens": 2}}}]


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    task = repo / "tasks/t01"
    task.mkdir(parents=True)
    (task / "brief.md").write_text("Neutral task brief\n")
    (task / "task.yaml").write_text("manuals: []\n")
    conditions = [{"id": "alpha", "provider": "openai", "model": "gpt-5.6-sol", "effort": "low"},
                  {"id": "beta", "provider": "anthropic", "model": "claude-opus-5", "effort": "max"}]
    cases = {"schema_version": 1, "samples": 2, "seeds": [1, 2], "schedule_seed": 101,
             "conditions": conditions, "contrasts": [{"id": "alpha-beta", "a": "alpha", "b": "beta"}]}
    cases_path = repo / "experiments/model_matrix/cases.json"
    runtime.write_json(cases_path, cases)
    monkeypatch.setattr(runtime, "REPO", repo)
    monkeypatch.setattr(runtime, "source_hash", lambda: "old-source")
    monkeypatch.setattr(runner.matrix, "_benchmark_hash", lambda: "benchmark")
    monkeypatch.setattr(runner, "_commit", lambda: "a" * 40)
    monkeypatch.setattr(runner, "_environment", lambda: {"cli_versions": {"openai": "fake", "anthropic": "fake"}})
    monkeypatch.setattr(shutil, "which", lambda value: "/usr/local/bin/" + Path(value).name)
    for condition in conditions:
        record = {"condition": condition, "passed": True, "source_exact": True, "effective_effort_checked": True,
                  "probe": {"parent_read_blocked": True, "network_blocked": True, "effort": condition["effort"]},
                  "process": {"exit_code": 0, "timed_out": False}, "provider_audit": _audit(condition)}
        runtime.write_json(tmp_path / "probes/preflight" / condition["id"] / "probe-001/preflight.json", record)
    source, out = tmp_path / "v1", tmp_path / "v2"
    manifest = runner.plan_experiment(source, cases_path, tmp_path / "probes")
    (source / ".runner.lock").touch()
    proof = {"base_commit": "a" * 40, "current_commit": "b" * 40,
             "base_source_sha256": "old-source", "current_source_sha256": "new-source",
             "benchmark_sha256": "benchmark", "changed_files": sorted(migrate.AMENDED), "source_files": {},
             "invariants": {"native_command_builders": True}}
    monkeypatch.setattr(migrate, "_source_proof", lambda *args: proof)
    monkeypatch.setattr(runtime, "source_hash", lambda: "new-source")
    monkeypatch.setattr(runner, "_commit", lambda: "b" * 40)
    def forbidden(*args, **kwargs):
        pytest.fail("Migration must not call a provider, quota endpoint, or grader")
    monkeypatch.setattr(runtime, "execute", forbidden)
    monkeypatch.setattr(runner, "_subscription_check", forbidden)
    monkeypatch.setattr(runner.matrix, "_grade_one", forbidden)
    rows = manifest["schedule"][:2]
    for row in rows:
        condition = next(c for c in conditions if c["id"] == row["condition"])
        call = f"{row['label']}/{row['task']}/attempt-1"
        workspace, private_runtime = source / "private/workspaces" / call, source / "private/runtimes" / call
        logs = source / "private/logs" / call
        workspace.mkdir(parents=True)
        private_runtime.mkdir(parents=True)
        logs.mkdir(parents=True)
        (workspace / "brief.md").write_text("Neutral task brief\n")
        (private_runtime / "containment.sb").write_text("PRIVATE_PROFILE\n")
        if condition["provider"] == "openai":
            (workspace / "main.py").write_text("print('neutral submission')\n")
            credential = private_runtime / "codex_home/auth.json"
            credential.parent.mkdir()
            credential.write_text("PRIVATE_CREDENTIAL_NOT_TO_COPY\n")
            session = private_runtime / "codex_home/sessions/2026/09/04/rollout.jsonl"
            session.parent.mkdir(parents=True)
            session.write_text('{"type":"neutral_rollout"}\n')
            audit = _audit(condition)
            events = [{"type": "neutral_completed_turn"}]
        else:
            (private_runtime / claude_adapter.SETTINGS_NAME).write_text('{"sandbox":{"enabled":false}}')
            audit = dict(_audit(condition), valid=False, errors=["model_fallback_observed", "primary_model_mismatch", "completion_not_successful"])
            events = _refusal(workspace, condition["model"])
        audit["rate_limited"] = False
        command = runtime.provider_module(condition["provider"]).build_command(condition["model"], condition["effort"], private_runtime, workspace)
        command[0] = shutil.which(command[0]) or command[0]
        process = {"status": "completed", "exit_code": 0 if condition["provider"] == "openai" else 1,
                   "timed_out": False, "command": ["/usr/bin/sandbox-exec", "-f", str(private_runtime / "containment.sb"), *command],
                   "cwd": str(workspace), "started_epoch_s": 1000.0, "finished_epoch_s": 1003.0,
                   "wall_s": 3.0, "epoch_elapsed_s": 3.0, "clock_discontinuity": False, "pid": 999999}
        result = dict(process, provider_audit=copy.deepcopy(audit), artifact_present=(workspace / "main.py").is_file(),
                      artifact_sha256=runner.matrix._artifact_hash(workspace), transport_audit={"violations": []})
        raw_audit = copy.deepcopy(result)
        raw_audit["provider_audit"].pop("rate_limited")
        runtime.write_json(logs / "process.json", process)
        runtime.write_json(logs / "audit.json", raw_audit)
        (logs / "prompt.txt").write_text(manifest["prompt"])
        (logs / "stdout.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events))
        (logs / "stderr.log").write_text("")
        record = {k: row[k] for k in ("label", "condition", "sample", "task")}
        record.update(attempts=[dict(result, number=1, subscription_precheck={"allowed": True})],
                      artifact_present=result["artifact_present"], artifact_sha256=result["artifact_sha256"], wall_s=3.0,
                      provider_audit=audit, subscription_guard={"stop": False, "reason": None, "rate_limited": False})
        if condition["provider"] == "openai":
            record.update(status="completed", eligible_for_grading=True)
            runner._freeze(workspace, runner._artifact_path(source, row), result["artifact_sha256"])
        else:
            record.update(status="blocked", eligible_for_grading=False, stop_reason="invalid_provider_audit")
        runner._save_record(runner._record_path(source, row), record)
    return {"source": source, "out": out, "manifest": manifest, "rows": rows, "proof": proof, "repo": repo}


def _run(evidence):
    return migrate.migrate_authoring(evidence["source"], evidence["out"], base_commit="a" * 40)


def test_migration_preserves_calls_and_source_without_credentials_or_retries(evidence):
    source, out = evidence["source"], evidence["out"]
    before = _snapshot(source)
    receipt = _run(evidence)
    assert _snapshot(source) == before
    assert receipt["provider_calls_performed"] == receipt["grading_calls_performed"] == 0
    assert len(receipt["preserved_calls"]) == 2
    manifest = runner._verify_plan(out)
    assert manifest["schedule"] == evidence["manifest"]["schedule"]
    assert manifest["expected_runs"] == evidence["manifest"]["expected_runs"]
    assert manifest["preflight"] == evidence["manifest"]["preflight"]
    assert manifest["benchmark_commit"] == "b" * 40
    assert manifest["source_sha256"] == "new-source"
    assert all(p["status"] == "paused" for p in _read(out / "orchestration.json")["providers"].values())
    assert not (out / "control").exists() and not (out / "private/runtimes").exists()
    for row in evidence["rows"]:
        old = _read(runner._record_path(source, row))
        new = _read(runner._record_path(out, row))
        runner._validate_record(out, row, new)
        assert new["attempts"] == old["attempts"]
        assert new["wall_s"] == old["wall_s"]
        assert new["migration"]["source_record_sha256"] == old["record_sha256"]
        assert (out / "private/migration/authoring" / row["label"] / f"{row['task']}.json").read_bytes() == runner._record_path(source, row).read_bytes()
    refusal = _read(out / "authoring/beta/t01.json")
    assert refusal["status"] == "completed" and refusal["outcome"] == "provider_refusal"
    assert refusal["eligible_for_grading"] is True and refusal["artifact_sha256"] is None
    assert refusal["provider_audit"]["completion_successful"] is False
    assert refusal["attempts"][0]["exit_code"] == 1 and refusal["attempts"][0]["provider_audit"]["valid"] is False
    assert (out / "private/artifacts/beta/t01/brief.md").exists()
    assert not (out / "private/artifacts/beta/t01/main.py").exists()
    assert list((out / "private/migration/rollouts").rglob("*.jsonl"))
    assert not any(p.name in migrate.CREDENTIAL_NAMES for p in out.rglob("*"))
    assert "PRIVATE_CREDENTIAL_NOT_TO_COPY" not in "".join(p.read_text() for p in out.rglob("*") if p.is_file())
    public = json.dumps(receipt)
    assert str(source) not in public and str(out) not in public
    assert "PRIVATE_PROFILE" not in public and "999999" not in public
    assert runner.run_experiment(out, grade_completed=False)["authoring"]["completed"] == 2


@pytest.mark.parametrize("status", ["running", "retry_pending", "unknown"])
def test_unresolved_record_is_rejected_without_destination(evidence, status):
    path = runner._record_path(evidence["source"], evidence["rows"][0])
    record = _read(path)
    record["status"] = status
    runner._save_record(path, record)
    with pytest.raises(ValueError):
        _run(evidence)
    assert not evidence["out"].exists()


def test_active_runner_lock_is_rejected(evidence):
    with (evidence["source"] / ".runner.lock").open("rb") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(migrate.MigrationInvalid, match="still running"):
            _run(evidence)
    assert not evidence["out"].exists()


@pytest.mark.parametrize("change", ["manifest", "record", "artifact", "logs", "command", "process", "grade", "unknown_author", "unrecorded_call", "credential", "symlink"])
def test_changed_or_ambiguous_evidence_is_rejected(evidence, change):
    source = evidence["source"]
    record_path = source / "authoring/alpha/t01.json"
    log_dir = source / "private/logs/alpha/t01/attempt-1"
    if change == "manifest":
        path = source / "evaluation_manifest.json"
        value = _read(path); value["prompt"] += "changed"
        runtime.write_json(path, value)
    elif change in {"record", "command"}:
        value = _read(record_path)
        if change == "record":
            value["wall_s"] += 1
            runtime.write_json(record_path, value)
        else:
            value["attempts"][0]["command"].append("--changed")
            runner._save_record(record_path, value)
    elif change == "artifact":
        (source / "private/artifacts/alpha/t01/main.py").write_text("changed")
    elif change == "logs":
        (log_dir / "prompt.txt").write_text("changed")
    elif change == "process":
        value = _read(log_dir / "process.json"); value["status"] = "running"
        runtime.write_json(log_dir / "process.json", value)
    elif change == "grade":
        runtime.write_json(source / "alpha/t01_s1/grade.json", {"pass": True})
    elif change == "unknown_author":
        runtime.write_json(source / "authoring/unknown/t01.json", {})
    elif change == "unrecorded_call":
        runtime.write_json(source / "private/logs/alpha/t01/attempt-2/process.json", {})
    elif change == "credential":
        (log_dir / "auth.json").write_text("DO_NOT_COPY")
    else:
        (log_dir / "linked.json").symlink_to(log_dir / "process.json")
    with pytest.raises(ValueError):
        _run(evidence)
    assert not evidence["out"].exists()


@pytest.mark.parametrize("violation", ["wrong_model", "fallback", "timeout", "exit", "artifact", "clock", "other_blocked"])
def test_refusal_reclassification_fails_closed(evidence, violation):
    source = evidence["source"]
    path = source / "authoring/beta/t01.json"
    record = _read(path)
    logs = source / "private/logs/beta/t01/attempt-1"
    if violation in {"wrong_model", "fallback"}:
        events = [json.loads(line) for line in (logs / "stdout.jsonl").read_text().splitlines()]
        if violation == "wrong_model":
            events[0]["model"] = "claude-sonnet-5"
        else:
            events.append({"type": "model_fallback"})
        (logs / "stdout.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    elif violation == "artifact":
        (source / "private/workspaces/beta/t01/attempt-1/main.py").write_text("print('unexpected')")
    elif violation == "other_blocked":
        record["stop_reason"] = "quota_exhausted"
    else:
        field, value = {"timeout": ("timed_out", True), "exit": ("exit_code", 2), "clock": ("clock_discontinuity", True)}[violation]
        record["attempts"][0][field] = value
        for name in ("process.json", "audit.json"):
            payload = _read(logs / name); payload[field] = value
            runtime.write_json(logs / name, payload)
    runner._save_record(path, record)
    with pytest.raises(ValueError):
        _run(evidence)
    assert not evidence["out"].exists()


def test_existing_destination_is_never_overwritten(evidence):
    evidence["out"].mkdir()
    marker = evidence["out"] / "marker"
    marker.write_text("preserve")
    with pytest.raises(migrate.MigrationInvalid):
        _run(evidence)
    assert marker.read_text() == "preserve"


def test_inherited_plan_fields_cannot_be_changed(evidence):
    path = evidence["source"] / "evaluation_manifest.json"
    manifest = _read(path)
    manifest["preflight"]["alpha"]["record"]["probe"]["parent_read_blocked"] = False
    manifest.pop("plan_sha256")
    manifest["plan_sha256"] = runner._digest(manifest)
    runtime.write_json(path, manifest)
    with pytest.raises(migrate.MigrationInvalid, match="preflight"):
        _run(evidence)


def test_deferred_zero_call_record_is_preserved_without_inventing_artifact(evidence):
    row = evidence["manifest"]["schedule"][2]
    record = {k: row[k] for k in ("label", "condition", "sample", "task")}
    record.update(status="deferred", attempts=[], artifact_present=False, artifact_sha256=None,
                  eligible_for_grading=False, subscription_precheck={"allowed": True},
                  subscription_prechecks=[{"checked_at": "2026-09-04T00:00:00+00:00", "result": {"allowed": True}}],
                  stop_reason="user_requested_pause")
    runner._save_record(runner._record_path(evidence["source"], row), record)
    receipt = _run(evidence)
    assert len(receipt["imported_records"]) == 3 and len(receipt["preserved_calls"]) == 2
    imported = _read(runner._record_path(evidence["out"], row))
    assert imported["status"] == "deferred" and imported["attempts"] == []
    assert imported["subscription_prechecks"] == record["subscription_prechecks"]
    assert not runner._artifact_path(evidence["out"], row).exists()


def test_sealed_but_changed_prompt_is_not_inherited(evidence):
    path = evidence["source"] / "evaluation_manifest.json"
    manifest = _read(path)
    manifest["prompt"] += "changed"
    manifest["prompt_sha256"] = migrate._sha(manifest["prompt"].encode())
    manifest.pop("plan_sha256")
    manifest["plan_sha256"] = runner._digest(manifest)
    runtime.write_json(path, manifest)
    with pytest.raises(migrate.MigrationInvalid, match="prompt"):
        _run(evidence)


def test_incomplete_unrecorded_log_directory_is_rejected(evidence):
    directory = evidence["source"] / "private/logs/alpha/t01/attempt-2"
    directory.mkdir()
    (directory / "stdout.jsonl").write_text("{}\n")
    with pytest.raises(migrate.MigrationInvalid, match="Unrecorded"):
        _run(evidence)


def test_ast_guard_rejects_launch_changes_but_allows_only_named_audit_functions():
    before = b"TOOLS = ('Read',)\ndef prepare_runtime(): return TOOLS\ndef inspect_trace(): return False\n"
    after = b"TOOLS = ('Read',)\ndef prepare_runtime(): return TOOLS\ndef inspect_trace(): return True\ndef _native_refusal(): return True\n"
    assert migrate._ast_unchanged(before, after, {"inspect_trace", "_native_refusal"})
    assert not migrate._ast_unchanged(before, after.replace(b"('Read',)", b"('Bash',)"), {"inspect_trace", "_native_refusal"})


def test_real_source_proof_verifies_committed_two_file_amendment(tmp_path, monkeypatch):
    actual_repo = runtime.REPO
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args])
    git("init", "-q")
    git("config", "user.name", "Neutral Test")
    git("config", "user.email", "test@example.invalid")
    for prefix in migrate.CORE:
        directory = repo / prefix
        directory.mkdir()
        (directory / "fixture.py").write_text("VALUE = 1\n")
    for name in [*runtime.EXECUTION_SOURCES, "cases.json"]:
        path = repo / migrate.PREFIX / name
        path.parent.mkdir(parents=True, exist_ok=True)
        data = subprocess.check_output(["git", "-C", str(actual_repo), "show", f"{migrate.DEFAULT_BASE}:{migrate.PREFIX}{name}"])
        path.write_bytes(data)
    git("add", ".")
    git("commit", "-qm", "Baseline")
    base = git("rev-parse", "HEAD").decode().strip()
    for name in ("runner.py", "claude_adapter.py"):
        shutil.copy2(actual_repo / migrate.PREFIX / name, repo / migrate.PREFIX / name)
    git("add", ".")
    git("commit", "-qm", "Declared amendment")
    monkeypatch.setattr(runtime, "REPO", repo)
    monkeypatch.setattr(runner.matrix, "REPO", repo)
    proof = migrate._source_proof(repo, base)
    assert proof["changed_files"] == sorted(migrate.AMENDED)
    assert all(proof["invariants"].values())
    assert proof["current_source_sha256"] == runtime.source_hash()
    (repo / "osicbench/fixture.py").write_text("VALUE = 2\n")
    with pytest.raises(migrate.MigrationInvalid, match="committed"):
        migrate._source_proof(repo, base)
    git("add", ".")
    git("commit", "-qm", "Unexpected benchmark change")
    with pytest.raises(migrate.MigrationInvalid, match="Only the declared"):
        migrate._source_proof(repo, base)
