"""Synthetic, no-provider tests for the graded parser-amendment migration."""
import copy
import fcntl
import hashlib
import json
import shutil
import uuid
from pathlib import Path

import pytest

from experiments.model_matrix import migrate, runner, runtime


def _read(path):
    return json.loads(path.read_text())


def _snapshot(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


def _audit(condition, *, refusal=False):
    value = {"valid": True, "errors": [], "requested_model": condition["model"],
             "resolved_model": condition["model"], "requested_effort": condition["effort"]}
    if condition["provider"] == "openai":
        value["resolved_effort"] = condition["effort"]
    else:
        value.update(observed_efforts=[], effort_verification="launch_configuration_only")
    if refusal:
        value.update(provider_refusal=True, completion_successful=False,
                     completion_status="completed_provider_refusal",
                     identity_evidence="init_and_refusal_metadata", refusal_category="cyber",
                     provider_error="invalid_request", provider_error_status=None,
                     provider_refusal_details={"category": "cyber", "provider_error": "invalid_request",
                         "provider_error_status": None, "terminal_reason": "api_error",
                         "system_subtype": "model_refusal_no_fallback", "original_model": condition["model"]},
                     rate_limit={"events": [{"status": "allowed", "isUsingOverage": False}],
                                 "using_overage": False})
    return value


def _refusal_events(workspace, model):
    events = [{"type": "system", "subtype": "init", "model": model, "cwd": str(workspace),
               "plugins": [], "skills": [], "mcp_servers": [], "slash_commands": [],
               "tools": ["Bash", "Edit", "Read", "Write"], "apiKeySource": "none"}]
    for _ in range(2):
        events.extend([
            {"type": "system", "subtype": "model_refusal_no_fallback",
             "original_model": model, "api_refusal_category": "cyber"},
            {"type": "assistant", "is_api_error_message": True, "error": "invalid_request",
             "message": {"model": "<synthetic>", "stop_reason": "refusal", "content": [],
                         "stop_details": {"type": "refusal", "category": "cyber"}}},
        ])
    events.append({"type": "result", "subtype": "success", "is_error": True,
                   "stop_reason": "refusal", "terminal_reason": "api_error",
                   "api_error_status": None, "modelUsage": {model: {"inputTokens": 2}}})
    return events


def _write_call(root, row, condition, workspace, private_runtime, audit, *, exit_code=0, events=None):
    call_id = f"{row['label']}/{row['task']}/attempt-1"
    logs = root / "private/logs" / call_id
    logs.mkdir(parents=True)
    private_runtime.mkdir(parents=True, exist_ok=True)
    (private_runtime / "containment.sb").write_text("PRIVATE CONTAINMENT RECEIPT\n")
    if condition["provider"] == "anthropic":
        (private_runtime / "claude-settings.json").write_text('{"sandbox":{"enabled":false}}')
    command = runtime.provider_module(condition["provider"]).build_command(
        condition["model"], condition["effort"], private_runtime, workspace)
    command[0] = shutil.which(command[0]) or command[0]
    process = {"status": "completed", "exit_code": exit_code, "timed_out": False,
               "command": ["/usr/bin/sandbox-exec", "-f", str(private_runtime / "containment.sb"), *command],
               "cwd": str(workspace), "started_epoch_s": 1000.0, "finished_epoch_s": 1003.0,
               "wall_s": 3.0, "epoch_elapsed_s": 3.0, "clock_discontinuity": False, "pid": 4242}
    artifact_hash = runner.matrix._artifact_hash(workspace)
    result = dict(process, provider_audit=copy.deepcopy(audit),
                  artifact_present=artifact_hash is not None, artifact_sha256=artifact_hash,
                  transport_audit={"violations": []})
    raw_result = copy.deepcopy(result)
    raw_result["provider_audit"].pop("rate_limited", None)
    runtime.write_json(logs / "process.json", process)
    runtime.write_json(logs / "audit.json", raw_result)
    (logs / "prompt.txt").write_text(runner.matrix.PROMPT)
    (logs / "stdout.jsonl").write_text("".join(json.dumps(event) + "\n" for event in (events or [{}])))
    (logs / "stderr.log").write_text("")
    return result


def _reseal_suspend_inventory(fixture, monkeypatch, *, record=False, state=False):
    source = fixture["source"]
    if record:
        raw = runner._record_path(source, fixture["target"]).read_bytes()
        monkeypatch.setattr(migrate, "SUSPEND_RECORD_FILE_SHA", migrate._sha(raw))
        monkeypatch.setattr(migrate, "SUSPEND_RECORD_SHA", json.loads(raw)["record_sha256"])
    if state:
        monkeypatch.setattr(
            migrate, "SUSPEND_STATE_FILE_SHA",
            migrate._sha((source / "orchestration.json").read_bytes()))
    inventory = migrate._source_inventory(source)
    monkeypatch.setattr(
        migrate, "SUSPEND_INVENTORY",
        {key: value for key, value in inventory.items() if key != "items"})


def _rewrite_target_receipts(source, target, field, value):
    call_id = f"{target['label']}/{target['task']}/attempt-1"
    for name in ("process.json", "audit.json"):
        path = source / "private/logs" / call_id / name
        receipt = _read(path)
        receipt[field] = value
        runtime.write_json(path, receipt)


@pytest.fixture
def amendment(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    for index in range(1, 23):
        task_name = "t21_hostile_link" if index == 21 else f"t{index:02d}"
        task = repo / "tasks" / task_name
        task.mkdir(parents=True)
        (task / "brief.md").write_text(f"Neutral task {index}\n")
        (task / "task.yaml").write_text("manuals: []\n")
    conditions = [
        {"id": "gpt56-sol-low", "provider": "openai", "model": "gpt-5.6-sol", "effort": "low"},
        {"id": "gpt56-sol-ultra", "provider": "openai", "model": "gpt-5.6-sol", "effort": "ultra"},
        {"id": "gpt56-terra-low", "provider": "openai", "model": "gpt-5.6-terra", "effort": "low"},
        {"id": "gpt56-terra-ultra", "provider": "openai", "model": "gpt-5.6-terra", "effort": "ultra"},
        {"id": "claude-opus5-low", "provider": "anthropic", "model": "claude-opus-5", "effort": "low"},
        {"id": "claude-opus5-max", "provider": "anthropic", "model": "claude-opus-5", "effort": "max"},
        {"id": "claude-sonnet5-low", "provider": "anthropic", "model": "claude-sonnet-5", "effort": "low"},
        {"id": "claude-sonnet5-max", "provider": "anthropic", "model": "claude-sonnet-5", "effort": "max"},
    ]
    cases = {"schema_version": 1, "samples": 3, "seeds": [19471, 38459, 62039, 87133, 105943],
             "schedule_seed": 7331, "conditions": conditions, "contrasts": []}
    cases_path = repo / "experiments/model_matrix/cases.json"
    runtime.write_json(cases_path, cases)
    monkeypatch.setattr(runtime, "REPO", repo)
    monkeypatch.setattr(runtime, "source_hash", lambda: "old-parser-source")
    monkeypatch.setattr(runner.matrix, "_benchmark_hash", lambda: "benchmark")
    monkeypatch.setattr(runner, "_commit", lambda: migrate.PARSER_BASE)
    environment = {"cli_versions": {"openai": "fake", "anthropic": "fake"}}
    monkeypatch.setattr(runner, "_environment", lambda: environment)
    monkeypatch.setattr(shutil, "which", lambda value: "/usr/local/bin/" + Path(value).name)
    preflights = tmp_path / "preflights"
    for condition in conditions:
        record = {"condition": condition, "passed": True, "source_exact": True,
                  "effective_effort_checked": True,
                  "probe": {"parent_read_blocked": True, "network_blocked": True,
                            "effort": condition["effort"]},
                  "process": {"exit_code": 0, "timed_out": False},
                  "provider_audit": _audit(condition)}
        runtime.write_json(preflights / "preflight" / condition["id"] / "probe-001/preflight.json", record)
    source, out = tmp_path / "scoring-v2", tmp_path / "scoring-v3"
    manifest = runner.plan_experiment(source, cases_path, preflights)
    (source / ".runner.lock").touch()
    original_manifest = b'{"schema_version":1,"historical":true}\n'
    original_state = b'{"schema_version":1,"historical":true}\n'
    archived = source / "private/migration"
    archived.mkdir(parents=True)
    (archived / "source-manifest.json").write_bytes(original_manifest)
    (archived / "source-orchestration.json").write_bytes(original_state)

    def find(condition_id, task, sample=1):
        return next(row for row in manifest["schedule"]
                    if row["condition"] == condition_id and row["task"] == task and row["sample"] == sample)

    imported = find("gpt56-sol-low", "t01")
    native = find("gpt56-terra-low", "t02")
    no_artifact = find("claude-sonnet5-low", "t03")
    blocked = find("claude-opus5-max", "t21_hostile_link", 2)
    deferred = find("gpt56-sol-ultra", "t04")
    selected = [imported, native, no_artifact, blocked, deferred]
    by_condition = {item["id"]: item for item in conditions}

    records = {}
    for row in selected[:-1]:
        condition = by_condition[row["condition"]]
        call_id = f"{row['label']}/{row['task']}/attempt-1"
        workspace = source / "private/workspaces" / call_id
        runner._stage(workspace, row["task"])
        if row in (imported, native):
            (workspace / "main.py").write_text("print('neutral submission')\n")
        old_refusal = row == blocked
        audit = (_audit(condition, refusal=True) if row == no_artifact else
                 dict(_audit(condition), valid=False,
                      errors=["native_provider_refusal_unverified", "primary_model_mismatch",
                              "completion_not_successful"]) if old_refusal else _audit(condition))
        audit["rate_limited"] = False
        private_runtime = source / "private/runtimes" / call_id
        events = _refusal_events(workspace, condition["model"]) if old_refusal else [{}]
        result = _write_call(source, row, condition, workspace, private_runtime, audit,
                             exit_code=1 if old_refusal else 0, events=events)
        record = {key: row[key] for key in ("label", "condition", "sample", "task")}
        record.update(attempts=[dict(result, number=1, status="completed",
                                    subscription_precheck={"allowed": True})], wall_s=3.0,
                      artifact_present=result["artifact_present"], artifact_sha256=result["artifact_sha256"],
                      provider_audit=audit, subscription_guard={"stop": False, "reason": None,
                                                               "rate_limited": False})
        if old_refusal:
            record.update(status="blocked", eligible_for_grading=False,
                          stop_reason="invalid_provider_audit")
        else:
            record.update(status="completed", eligible_for_grading=True)
            if row == no_artifact:
                record["outcome"] = "provider_refusal"
            runner._freeze(workspace, runner._artifact_path(source, row), result["artifact_sha256"])
        runner._save_record(runner._record_path(source, row), record)
        records[(row["label"], row["task"])] = _read(runner._record_path(source, row))
    deferred_record = {key: deferred[key] for key in ("label", "condition", "sample", "task")}
    deferred_record.update(status="deferred", attempts=[], artifact_present=False, artifact_sha256=None,
                           eligible_for_grading=False, subscription_prechecks=[], stop_reason="user_requested_pause")
    runner._save_record(runner._record_path(source, deferred), deferred_record)
    records[(deferred["label"], deferred["task"])] = _read(runner._record_path(source, deferred))

    imported_path = runner._record_path(source, imported)
    imported_record = _read(imported_path)
    historical = copy.deepcopy(imported_record)
    historical.pop("record_sha256")
    historical["status"] = "completed"
    historical["record_sha256"] = runner._digest(historical)
    historical_raw = migrate._encoded(historical)
    historical_path = archived / "authoring" / imported["label"] / f"{imported['task']}.json"
    historical_path.parent.mkdir(parents=True)
    historical_path.write_bytes(historical_raw)
    imported_record["migration"] = {
        "schema_version": 1, "source_plan_sha256": "1" * 64,
        "source_record_sha256": historical["record_sha256"],
        "source_record_file_sha256": migrate._sha(historical_raw),
        "original_status": "completed", "outcome_reclassified": False,
        "call_ids": [f"{imported['label']}/{imported['task']}/attempt-1"],
    }
    imported_record.pop("record_sha256")
    imported_record["record_sha256"] = runner._digest(imported_record)
    runtime.write_json(imported_path, imported_record)
    records[(imported["label"], imported["task"])] = imported_record
    imported_call = f"{imported['label']}/{imported['task']}/attempt-1"
    call_hashes = migrate._tree(source / "private/logs" / imported_call)
    prior = {"schema_version": 1, "amendment_id": migrate.AMENDMENT_ID,
             "migration_complete": True, "source_plan_sha256": "1" * 64,
             "source_manifest_file_sha256": migrate._sha(original_manifest),
             "source_orchestration_file_sha256": migrate._sha(original_state),
             "provider_calls_performed": 0, "grading_calls_performed": 0, "credentials_copied": False,
             "preserved_calls": [{"call_id": imported_call, "log_sha256": call_hashes}],
             "imported_records": [{"label": imported["label"], "task": imported["task"],
                 "source_record_file_sha256": migrate._sha(historical_raw),
                 "source_record_sha256": historical["record_sha256"],
                 "target_record_sha256": imported_record["record_sha256"]}],
    }
    manifest = _read(source / "evaluation_manifest.json")
    manifest["migration"] = {"schema_version": 1, "amendment_id": migrate.AMENDMENT_ID,
                             "source_plan_sha256": prior["source_plan_sha256"],
                             "source_manifest_file_sha256": prior["source_manifest_file_sha256"]}
    manifest.pop("plan_sha256")
    manifest["plan_sha256"] = runner._digest(manifest)
    prior["target_plan_sha256"] = manifest["plan_sha256"]
    runtime.write_json(source / "evaluation_manifest.json", manifest)
    runtime.write_json(source / "migration_receipt.json", prior)
    request_id = str(uuid.uuid4())
    request = {"schema_version": 1, "plan_sha256": manifest["plan_sha256"], "request_id": request_id,
               "requested_at": "2026-09-06T00:00:00+00:00", "action": "pause",
               "providers": ["openai", "anthropic"]}
    runtime.write_json(source / "control/stop-requests" / f"{request_id}.json", request)
    runtime.write_json(source / "orchestration.json", {
        "schema_version": 1, "plan_sha256": manifest["plan_sha256"],
        "providers": {"openai": {"status": "paused"}, "anthropic": {"status": "stopped"}},
        "control_acknowledgements": {"openai": [request_id], "anthropic": [request_id]}})

    grade_row = manifest["expected_runs"][next(index for index, value in enumerate(manifest["expected_runs"])
                                                if value["label"] == native["label"] and value["task"] == native["task"])]
    grade_dir = source / grade_row["label"] / f"{grade_row['task']}_s{grade_row['seed']}"
    runtime.write_json(grade_dir / "meta.json", {"label": grade_row["label"], "task": grade_row["task"],
                                                  "seed": grade_row["seed"]})
    runtime.write_json(grade_dir / "adapter.json", {"artifact_sha256": records[(native["label"], native["task"])]["artifact_sha256"],
                                                     "exit_code": 0, "timed_out": False})
    runtime.write_json(grade_dir / "grade.json", {"pass": True, "dfs": 100, "hss": 100,
                                                   "rs": 100, "budget_ok": True})
    (grade_dir / "submission").mkdir()
    (grade_dir / "submission/main.py").write_text("post-run mutated copy\n")
    log_stem = source / "grading_logs" / grade_row["label"] / f"{grade_row['task']}_s{grade_row['seed']}"
    log_stem.parent.mkdir(parents=True)
    log_stem.with_suffix(".log").write_text("neutral grade output\n")
    log_stem.with_suffix(".stderr").write_text("")
    failure_row = next(value for value in manifest["expected_runs"]
                       if value["label"] == no_artifact["label"] and value["task"] == no_artifact["task"])
    runtime.write_json(source / failure_row["label"] / f"{failure_row['task']}_s{failure_row['seed']}" / "failure.json",
                       {"label": failure_row["label"], "task": failure_row["task"], "seed": failure_row["seed"],
                        "stage": "authoring", "reason": "missing_author_artifact"})
    proof = {"base_commit": migrate.PARSER_BASE, "current_commit": "b" * 40,
             "base_source_sha256": "old-parser-source", "current_source_sha256": "new-parser-source",
             "benchmark_sha256": "benchmark", "changed_files": sorted(migrate.PARSER_CHANGED),
             "migration_tool_sha256": "2" * 64, "source_files": {},
             "invariants": {"parser_function_only": True}}
    monkeypatch.setattr(migrate, "_parser_source_proof", lambda *_args: copy.deepcopy(proof))
    monkeypatch.setattr(runtime, "source_hash", lambda: "new-parser-source")
    monkeypatch.setattr(runner, "_commit", lambda: "b" * 40)

    def inspect(stdout, _stderr, model, effort):
        events = [json.loads(line) for line in Path(stdout).read_text().splitlines()]
        pairs = sum(event.get("subtype") == "model_refusal_no_fallback" for event in events)
        condition = next(item for item in conditions if item["model"] == model and item["effort"] == effort)
        return _audit(condition, refusal=True) if pairs == 2 else {"valid": False, "errors": ["invalid"]}

    monkeypatch.setattr(migrate.claude_adapter, "inspect_trace", inspect)
    for forbidden_name in ("execute",):
        monkeypatch.setattr(runtime, forbidden_name,
                            lambda *_args, **_kwargs: pytest.fail("provider call during migration"))
    monkeypatch.setattr(runner, "_subscription_check",
                        lambda *_args, **_kwargs: pytest.fail("subscription call during migration"))
    monkeypatch.setattr(runner.matrix, "_grade_one",
                        lambda *_args, **_kwargs: pytest.fail("grader call during migration"))
    return {"source": source, "out": out, "manifest": manifest, "rows": selected,
            "target": blocked, "ordinary": [imported, native, no_artifact, deferred],
            "grade_row": grade_row, "failure_row": failure_row, "request_id": request_id}


def _migrate(fixture):
    return migrate.migrate_parser_amendment(fixture["source"], fixture["out"])


def test_parser_amendment_preserves_evidence_and_reclassifies_only_target(amendment):
    source, out = amendment["source"], amendment["out"]
    before = _snapshot(source)
    receipt = _migrate(amendment)
    assert _snapshot(source) == before
    assert receipt["provider_calls_performed"] == 0
    assert receipt["subscription_checks_performed"] == 0
    assert receipt["grading_calls_performed"] == 0
    assert receipt["derived_failure_records_created"] == 0
    assert receipt["schedule"] == {
        "planned_authors": 528, "planned_runs": 2640,
        "source_statuses": {"completed": 3, "blocked": 1, "deferred": 1},
        "ordinary_records_byte_preserved": 4, "reclassified_records": 1,
        "explicit_deferred_records": 1, "implicit_deferred_records": 523,
        "implicit_deferred_identities_sha256": receipt["schedule"]["implicit_deferred_identities_sha256"],
        "graded_runs_preserved": 1, "failure_runs_preserved": 1, "missing_runs_preserved": 2638}
    for row in amendment["ordinary"]:
        assert runner._record_path(out, row).read_bytes() == runner._record_path(source, row).read_bytes()
    target = amendment["target"]
    old, new = _read(runner._record_path(source, target)), _read(runner._record_path(out, target))
    assert new["attempts"] == old["attempts"]
    assert new["status"] == "completed" and new["outcome"] == "provider_refusal"
    assert new["eligible_for_grading"] is True and new["artifact_sha256"] is None
    assert new["migration"]["source_record_sha256"] == old["record_sha256"]
    assert receipt["reclassified_record"]["native_refusal_envelopes"] == 2
    runner._validate_record(out, target, new)
    assert (out / "private/artifacts" / target["label"] / target["task"]).is_dir()
    assert not (out / "private/artifacts" / target["label"] / target["task"] / "main.py").exists()
    grade = amendment["grade_row"]
    failure = amendment["failure_row"]
    for row in (grade, failure):
        relative = Path(row["label"]) / f"{row['task']}_s{row['seed']}"
        assert _snapshot(out / relative) == _snapshot(source / relative)
    assert (out / "private/amendment/prior-migration/source-manifest.json").is_file()
    assert (out / "private/amendment/source-control/stop-requests").is_dir()
    assert not (out / "control").exists() and not (out / "private/runtimes").exists()
    state = _read(out / "orchestration.json")
    assert all(value["status"] == "paused" for value in state["providers"].values())
    assert state["control_acknowledgements"] == {"anthropic": [], "openai": []}
    target_manifest = runner._verify_plan(out)
    assert target_manifest["schedule"] == amendment["manifest"]["schedule"]
    assert target_manifest["expected_runs"] == amendment["manifest"]["expected_runs"]
    assert target_manifest["migration"]["source_plan_sha256"] == amendment["manifest"]["plan_sha256"]


@pytest.mark.parametrize("status", ["running", "retry_pending"])
def test_unresolved_record_prevents_fresh_destination(amendment, status):
    row = amendment["ordinary"][1]
    path = runner._record_path(amendment["source"], row)
    record = _read(path)
    record["status"] = status
    runner._save_record(path, record)
    with pytest.raises(migrate.MigrationInvalid, match="Unresolved"):
        _migrate(amendment)
    assert not amendment["out"].exists()


def test_active_source_lock_prevents_migration(amendment):
    with (amendment["source"] / ".runner.lock").open("rb") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(migrate.MigrationInvalid, match="still running"):
            _migrate(amendment)
    assert not amendment["out"].exists()


@pytest.mark.parametrize("change", ["pending_control", "ready_provider", "prior_receipt", "extra_blocked",
                                     "target_trace", "target_artifact", "grade_adapter", "grade_meta",
                                     "failure", "grading_log", "unexpected_run", "unexpected_call",
                                     "artifact", "source_manifest"])
def test_parser_migration_rejects_ambiguous_or_changed_evidence(amendment, change):
    source = amendment["source"]
    if change in {"pending_control", "ready_provider"}:
        state = _read(source / "orchestration.json")
        if change == "pending_control":
            state["control_acknowledgements"]["openai"] = []
        else:
            state["providers"]["openai"] = {"status": "ready"}
        runtime.write_json(source / "orchestration.json", state)
    elif change == "prior_receipt":
        receipt = _read(source / "migration_receipt.json")
        receipt["imported_records"][0]["target_record_sha256"] = "0" * 64
        runtime.write_json(source / "migration_receipt.json", receipt)
    elif change == "extra_blocked":
        row = amendment["ordinary"][1]
        path = runner._record_path(source, row)
        record = _read(path)
        record.update(status="blocked", eligible_for_grading=False, stop_reason="invalid_provider_audit")
        runner._save_record(path, record)
    elif change == "target_trace":
        path = source / "private/logs" / amendment["target"]["label"] / amendment["target"]["task"] / "attempt-1/stdout.jsonl"
        events = [json.loads(line) for line in path.read_text().splitlines()]
        del events[3]
        path.write_text("".join(json.dumps(event) + "\n" for event in events))
    elif change == "target_artifact":
        workspace = source / "private/workspaces" / amendment["target"]["label"] / amendment["target"]["task"] / "attempt-1"
        (workspace / "extra.txt").write_text("unexpected")
    elif change in {"grade_adapter", "grade_meta"}:
        row = amendment["grade_row"]
        directory = source / row["label"] / f"{row['task']}_s{row['seed']}"
        name = "adapter.json" if change == "grade_adapter" else "meta.json"
        value = _read(directory / name)
        value["artifact_sha256" if change == "grade_adapter" else "seed"] = "0" * 64 if change == "grade_adapter" else 999
        runtime.write_json(directory / name, value)
    elif change == "failure":
        row = amendment["failure_row"]
        path = source / row["label"] / f"{row['task']}_s{row['seed']}" / "failure.json"
        value = _read(path); value["reason"] = "grading_error"
        runtime.write_json(path, value)
    elif change == "grading_log":
        (source / "grading_logs/unexpected.log").write_text("orphan")
    elif change == "unexpected_run":
        runtime.write_json(source / "gpt56-sol-low/unexpected_s1/grade.json", {})
    elif change == "unexpected_call":
        runtime.write_json(source / "private/logs/gpt56-sol-low/t22/attempt-1/process.json", {})
    elif change == "artifact":
        row = amendment["ordinary"][1]
        (runner._artifact_path(source, row) / "main.py").write_text("changed")
    else:
        manifest = _read(source / "evaluation_manifest.json")
        manifest["prompt"] += "changed"
        runtime.write_json(source / "evaluation_manifest.json", manifest)
    with pytest.raises((migrate.MigrationInvalid, ValueError, KeyError)):
        _migrate(amendment)
    assert not amendment["out"].exists()


def test_existing_destination_is_not_overwritten(amendment):
    amendment["out"].mkdir()
    marker = amendment["out"] / "marker"
    marker.write_text("preserve")
    with pytest.raises(migrate.MigrationInvalid):
        _migrate(amendment)
    assert marker.read_text() == "preserve"


def test_cli_default_remains_initial_migration(monkeypatch):
    seen = []
    monkeypatch.setattr(migrate, "migrate_authoring", lambda source, out, base_commit: seen.append((source, out, base_commit)) or
                        {"imported_records": [], "preserved_calls": [], "target_plan_sha256": "x"})
    monkeypatch.setattr("sys.argv", ["migrate", "--source", "old", "--out", "new"])
    assert migrate.main() == 0
    assert seen and seen[0][2] == migrate.DEFAULT_BASE


@pytest.fixture
def suspended(amendment, monkeypatch):
    migrate.migrate_parser_amendment(amendment["source"], amendment["out"])
    source = amendment["out"]
    manifest = _read(source / "evaluation_manifest.json")
    monkeypatch.setattr(migrate, "SUSPEND_TARGET", ("claude-sonnet5-max@s2", "t14"))
    target = next(row for row in manifest["schedule"]
                  if (row["label"], row["task"]) == migrate.SUSPEND_TARGET)
    condition = next(item for item in manifest["conditions"] if item["id"] == target["condition"])
    call_id = f"{target['label']}/{target['task']}/attempt-1"
    workspace = source / "private/workspaces" / call_id
    runner._stage(workspace, target["task"])
    private_runtime = source / "private/runtimes" / call_id
    reconstructed = _audit(condition)
    reconstructed.update(completion_successful=False, completion_status="incomplete_or_error",
                         provider_refusal=False, provider_error=None,
                         identity_evidence="trace_primary_models", warnings=["incomplete"],
                         rate_limit={"events": [{"status": "allowed", "isUsingOverage": False}],
                                     "using_overage": False})
    audited = copy.deepcopy(reconstructed)
    audited.update(valid=False, errors=[migrate.SUSPEND_REASON], rate_limited=False)
    result = _write_call(source, target, condition, workspace, private_runtime, audited)
    result.update(exit_code=None, timed_out=True, started_epoch_s=1000.0,
                  finished_epoch_s=3505.0, wall_s=2400.0,
                  epoch_elapsed_s=2505.0, clock_discontinuity=True)
    result["provider_audit"] = audited
    process = {key: value for key, value in result.items()
               if key in {"status", "exit_code", "timed_out", "command", "cwd", "started_epoch_s",
                          "finished_epoch_s", "wall_s", "epoch_elapsed_s", "clock_discontinuity", "pid"}}
    raw_result = copy.deepcopy(result)
    raw_result["provider_audit"].pop("rate_limited", None)
    runtime.write_json(source / "private/logs" / call_id / "process.json", process)
    runtime.write_json(source / "private/logs" / call_id / "audit.json", raw_result)
    subscription = {"allowed": True, "paid_usage_enabled": False}
    record = {key: target[key] for key in ("label", "condition", "sample", "task")}
    record.update(status="blocked", stop_reason="invalid_provider_audit", eligible_for_grading=False,
                  artifact_present=False, artifact_sha256=None, wall_s=2400.0,
                  attempts=[dict(result, number=1, status="completed",
                                 subscription_precheck=subscription)],
                  provider_audit=audited,
                  subscription_precheck=subscription,
                  subscription_prechecks=[{"checked_at": "2026-09-07T00:00:00+00:00",
                                           "result": subscription}],
                  subscription_guard={"stop": False, "reason": None, "rate_limited": False})
    runner._save_record(runner._record_path(source, target), record)
    (source / ".runner.lock").touch()
    runtime.write_json(source / "orchestration.json", {
        "schema_version": 1, "plan_sha256": manifest["plan_sha256"],
        "providers": {"anthropic": {"status": "stopped", "reason": "invalid_provider_audit",
                                      "condition": "claude-sonnet5-max", "task": target["task"],
                                      "sample": 2},
                      "openai": {"status": "ready"}},
        "control_acknowledgements": {"anthropic": [], "openai": []}})
    counts = {"completed": 4, "blocked": 1, "deferred": 1, "records": 6, "calls": 5,
              "artifacts": 4, "grades": 1, "failures": 1, "missing": 2638}
    monkeypatch.setattr(migrate, "SUSPEND_COUNTS", counts)
    monkeypatch.setattr(migrate, "SUSPEND_PLAN", manifest["plan_sha256"])
    target_raw = runner._record_path(source, target).read_bytes()
    monkeypatch.setattr(migrate, "SUSPEND_RECORD_FILE_SHA", migrate._sha(target_raw))
    monkeypatch.setattr(migrate, "SUSPEND_RECORD_SHA", _read(runner._record_path(source, target))["record_sha256"])
    monkeypatch.setattr(migrate, "SUSPEND_MANIFEST_FILE_SHA",
                        migrate._sha((source / "evaluation_manifest.json").read_bytes()))
    monkeypatch.setattr(migrate, "SUSPEND_STATE_FILE_SHA",
                        migrate._sha((source / "orchestration.json").read_bytes()))
    inventory = migrate._source_inventory(source)
    monkeypatch.setattr(migrate, "SUSPEND_INVENTORY",
                        {key: value for key, value in inventory.items() if key != "items"})
    proof = {"base_commit": "b" * 40, "current_commit": "c" * 40,
             "base_source_sha256": "new-parser-source",
             "current_source_sha256": "new-parser-source",
             "benchmark_sha256": "benchmark", "changed_files": [],
             "support_source_sha256": {}, "source_files": {},
             "invariants": {"terminal_accounting_only": True}}
    monkeypatch.setattr(migrate, "_suspend_source_proof", lambda *_args: copy.deepcopy(proof))
    monkeypatch.setattr(runner, "_commit", lambda: "c" * 40)

    def inspect(_stdout, _stderr, model, effort, allow_incomplete=False):
        assert model == condition["model"] and effort == condition["effort"] and allow_incomplete
        return copy.deepcopy(reconstructed)

    monkeypatch.setattr(migrate.claude_adapter, "inspect_trace", inspect)
    return {"source": source, "out": source.parent / "scoring-v4", "target": target,
            "manifest": manifest, "counts": counts}


def _migrate_suspended(fixture):
    return migrate.migrate_suspend_amendment(
        fixture["source"], fixture["out"], base_commit="b" * 40)


def test_suspend_migration_preserves_source_and_accounts_exact_seeds(suspended):
    source, out, target = suspended["source"], suspended["out"], suspended["target"]
    before = _snapshot(source)
    source_manifest_raw = (source / "evaluation_manifest.json").read_bytes()
    source_state_raw = (source / "orchestration.json").read_bytes()
    source_receipt_raw = (source / "migration_receipt.json").read_bytes()
    source_records = {
        path.relative_to(source / "authoring"): path.read_bytes()
        for path in (source / "authoring").glob("*/*.json")
    }
    source_call_ids = sorted(
        f"{record['label']}/{record['task']}/attempt-{attempt['number']}"
        for raw in source_records.values()
        for record in [json.loads(raw)]
        for attempt in record.get("attempts", []))
    source_artifacts = _snapshot(source / "private/artifacts")
    source_logs = _snapshot(source / "private/logs")
    source_grading_logs = _snapshot(source / "grading_logs")
    source_prior_amendment = _snapshot(source / "private/amendment")
    source_controls = _snapshot(source / "control")
    target_call = f"{target['label']}/{target['task']}/attempt-1"
    source_target_workspace = _snapshot(source / "private/workspaces" / target_call)
    source_runs = {
        path.parent.relative_to(source): _snapshot(path.parent)
        for outcome in ("grade.json", "failure.json")
        for path in source.glob(f"*/*/{outcome}")
    }
    source_inventory = migrate._source_inventory(source)
    source_inventory.pop("items")

    receipt = _migrate_suspended(suspended)

    assert _snapshot(source) == before
    for relative, raw in source_records.items():
        assert (out / "authoring" / relative).read_bytes() == raw
        assert (out / "private/amendment/source-authoring" / relative).read_bytes() == raw
    assert _snapshot(out / "private/artifacts") == source_artifacts
    assert _snapshot(out / "private/logs") == source_logs
    assert _snapshot(out / "grading_logs") == source_grading_logs
    for relative, tree in source_runs.items():
        assert _snapshot(out / relative) == tree
    archive = out / "private/amendment"
    assert (archive / "source-manifest.json").read_bytes() == source_manifest_raw
    assert (archive / "source-orchestration.json").read_bytes() == source_state_raw
    assert (archive / "source-migration-receipt.json").read_bytes() == source_receipt_raw
    assert _snapshot(archive / "prior-amendment") == source_prior_amendment
    assert _snapshot(archive / "source-control") == source_controls
    assert _snapshot(archive / "non-graded-target-workspace") == source_target_workspace
    assert receipt["provider_calls_performed"] == 0
    assert receipt["subscription_checks_performed"] == 0
    assert receipt["grading_calls_performed"] == 0
    assert receipt["credentials_copied"] is False
    assert receipt["prior_controls_replayed"] is False
    assert receipt["source_inventory"] == source_inventory
    assert receipt["source_manifest_file_sha256"] == migrate._sha(source_manifest_raw)
    assert receipt["source_orchestration_file_sha256"] == migrate._sha(source_state_raw)
    assert receipt["source_migration_receipt_sha256"] == migrate._sha(source_receipt_raw)
    assert receipt["prior_amendment_tree_sha256"] == migrate._map_sha256(source_prior_amendment)
    assert receipt["source_control_tree_sha256"] == migrate._map_sha256(source_controls)
    assert receipt["source_native_logs_sha256"] == migrate._map_sha256(source_logs)
    assert receipt["source_artifacts_sha256"] == migrate._map_sha256(source_artifacts)
    assert receipt["grading_logs_sha256"] == migrate._map_sha256(source_grading_logs)
    assert receipt["native_calls_preserved"] == len(source_call_ids)
    assert receipt["native_call_identities_sha256"] == migrate._sha(
        json.dumps(source_call_ids, separators=(",", ":")).encode())
    assert receipt["schedule"]["ordinary_records_byte_preserved"] == len(source_records) - 1
    assert receipt["schedule"]["target_record_byte_preserved"] is True
    preserved_runs = {
        f"{relative.as_posix()}/{name}": digest
        for relative, tree in source_runs.items()
        for name, digest in tree.items()
    }
    assert receipt["preserved_runs_sha256"] == migrate._map_sha256(preserved_runs)
    assert receipt["schedule"]["derived_failure_records_created"] == 5
    assert receipt["schedule"]["missing_runs_after_migration"] == 2633
    assert receipt["operational_failure"]["reason"] == migrate.SUSPEND_REASON
    assert receipt["operational_failure"]["non_graded_evidence"] is True
    target_raw = source_records[Path(target["label"]) / f"{target['task']}.json"]
    assert receipt["operational_failure"]["source_record_file_sha256"] == migrate._sha(target_raw)
    assert receipt["operational_failure"]["source_record_sha256"] == json.loads(target_raw)["record_sha256"]
    for seed in suspended["manifest"]["seeds"]:
        directory = out / target["label"] / f"{target['task']}_s{seed}"
        assert [path.name for path in directory.iterdir()] == ["failure.json"]
        assert _read(directory / "failure.json") == {
            "label": target["label"], "task": target["task"], "seed": seed,
            "stage": "authoring", "reason": migrate.SUSPEND_REASON}
    assert (out / "private/amendment/non-graded-target-workspace").is_dir()
    assert not (out / "private/artifacts" / target["label"] / target["task"]).exists()
    assert not (out / "private/runtimes").exists()
    state = _read(out / "orchestration.json")
    assert all(value["status"] == "paused" for value in state["providers"].values())
    assert state["control_acknowledgements"] == {"anthropic": [], "openai": []}
    target_manifest = runner._verify_plan(out)
    assert _read(out / "migration_receipt.json") == receipt
    assert receipt["source_plan_sha256"] == suspended["manifest"]["plan_sha256"]
    assert receipt["target_plan_sha256"] == target_manifest["plan_sha256"] == state["plan_sha256"]
    assert target_manifest["migration"] == {
        "schema_version": 1,
        "amendment_id": migrate.SUSPEND_AMENDMENT_ID,
        "source_plan_sha256": receipt["source_plan_sha256"],
        "source_manifest_file_sha256": receipt["source_manifest_file_sha256"],
        "source_migration_receipt_sha256": receipt["source_migration_receipt_sha256"],
    }
    for field in receipt["unchanged_plan_fields"]:
        assert target_manifest[field] == suspended["manifest"][field]
    status = runner.status(out)
    assert status["authoring"] == {"completed": 4, "blocked": 1, "deferred": 523}
    assert status["grading"] == {"graded": 1, "failure": 6, "missing": 2633}


@pytest.mark.parametrize("change", ["clock", "timeout", "audit", "artifact", "run", "state", "record"])
def test_suspend_migration_rejects_changed_target_or_state(suspended, monkeypatch, change):
    source, target = suspended["source"], suspended["target"]
    record_path = runner._record_path(source, target)
    record = _read(record_path)
    if change == "clock":
        record["attempts"][0]["clock_discontinuity"] = False
        runner._save_record(record_path, record)
        _rewrite_target_receipts(source, target, "clock_discontinuity", False)
    elif change == "timeout":
        record["attempts"][0]["timed_out"] = False
        runner._save_record(record_path, record)
        _rewrite_target_receipts(source, target, "timed_out", False)
    elif change == "audit":
        record["provider_audit"]["errors"].append("other")
        record["attempts"][0]["provider_audit"]["errors"].append("other")
        runner._save_record(record_path, record)
        audit_path = (source / "private/logs" / target["label"] / target["task"] /
                      "attempt-1/audit.json")
        audit_receipt = _read(audit_path)
        audit_receipt["provider_audit"]["errors"].append("other")
        runtime.write_json(audit_path, audit_receipt)
    elif change == "artifact":
        path = runner._artifact_path(source, target)
        path.mkdir(parents=True)
    elif change == "run":
        seed = suspended["manifest"]["seeds"][0]
        runtime.write_json(source / target["label"] / f"{target['task']}_s{seed}/failure.json",
                           {"label": target["label"], "task": target["task"], "seed": seed,
                            "stage": "authoring", "reason": migrate.SUSPEND_REASON})
    elif change == "state":
        state = _read(source / "orchestration.json")
        state["providers"]["anthropic"] = {"status": "ready"}
        runtime.write_json(source / "orchestration.json", state)
    else:
        record["wall_s"] += 1
        runner._save_record(record_path, record)
    _reseal_suspend_inventory(
        suspended, monkeypatch,
        record=change in {"clock", "timeout", "audit", "record"},
        state=change == "state")
    expected = {
        "clock": "Suspension attempt has conflicting provider or process evidence",
        "timeout": "Suspension attempt has conflicting provider or process evidence",
        "audit": "Suspension attempt has conflicting provider or process evidence",
        "artifact": "Blocked source record is not the declared no-artifact suspension case",
        "run": "Cached failure provenance is inconsistent",
        "state": "Source orchestration is not the declared quiescent suspension state",
        "record": "Suspension timing does not independently prove a clock discontinuity",
    }
    with pytest.raises(migrate.MigrationInvalid, match=expected[change]):
        _migrate_suspended(suspended)
    assert not suspended["out"].exists()


def test_suspend_migration_rejects_inventory_seal_change(suspended):
    (suspended["source"] / "private/unexpected-evidence.txt").write_text("changed\n")
    with pytest.raises(
            migrate.MigrationInvalid,
            match="Stopped source inventory differs from the declared immutable baseline"):
        _migrate_suspended(suspended)
    assert not suspended["out"].exists()


def test_suspend_migration_rejects_held_lock(suspended):
    with (suspended["source"] / ".runner.lock").open("rb") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(migrate.MigrationInvalid, match="still running"):
            _migrate_suspended(suspended)
    assert not suspended["out"].exists()
