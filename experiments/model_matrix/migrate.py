"""Import sealed authoring evidence under one documented harness amendment.

No provider, subscription, authoring, or grading operation is invoked here.
Original records and logs remain private and unchanged. The destination starts
paused and requires explicit provider resumes through the normal runner.
"""
from __future__ import annotations

import argparse
import ast
import copy
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import claude_adapter, runner, runtime


DEFAULT_BASE = "bc4feac317ed009672ae1a20793e0cbb74ff5c4f"
AMENDMENT_ID = "native-provider-refusal-and-cooperative-pause-v1"
PREFIX = "experiments/model_matrix/"
CORE = ("osicbench", "osicsim", "tasks", "manuals", "adapters")
AMENDED = {PREFIX + "runner.py", PREFIX + "claude_adapter.py"}
PARSER_BASE = "4a28c7eb616b862b4622ec214a55f332487c99f8"
PARSER_AMENDMENT_ID = "repeated-native-refusal-parser-v1"
PARSER_CHANGED = {PREFIX + "claude_adapter.py"}
PARSER_TARGET = ("claude-opus5-max@s2", "t21_hostile_link")
SUSPEND_BASE = "b3c3ddb4a8b1e9dd04db163e1975238b9cc9f293"
SUSPEND_AMENDMENT_ID = "host-suspend-operational-failure-v1"
SUSPEND_CHANGED = set()
SUSPEND_TARGET = ("claude-sonnet5-max@s2", "t14_coil_park")
SUSPEND_REASON = "host_suspend_or_clock_discontinuity"
SUSPEND_PLAN = "82fa202f42c2a113f1bc57e963e80c9cc87ffdf0a8c04da4309fe72c99e36611"
SUSPEND_RECORD_FILE_SHA = "d4ebd5458147abb883a066fd582e4450a23595842c5eeed11ee0fe7a6e07b6e4"
SUSPEND_RECORD_SHA = "82d4230237809b2c289e5f01e17c914b6467aa52f5d13956549120ad3953d3b0"
SUSPEND_MANIFEST_FILE_SHA = "045b2575b021b05e417f1d1690f2d09d8ff20a4c21b86a0b4380c216ccd61ec5"
SUSPEND_STATE_FILE_SHA = "2f2c027e25e644271242b56dbf187dbd7482bd9dfb24f459d3cebcd76b0b88ae"
SUSPEND_INVENTORY = {"sha256": "01781d103d12f9059ec17530ba83e049dcfa494635ebf8da3b51553d84096f38",
                     "regular_files": 41513, "symlinks": 9, "entries": 41522,
                     "regular_file_bytes": 757465506}
SUSPEND_COUNTS = {"completed": 326, "blocked": 1, "deferred": 0,
                  "records": 327, "calls": 327, "artifacts": 326,
                  "grades": 1590, "failures": 40, "missing": 1010}
LOG_FILES = {"audit.json", "process.json", "prompt.txt", "stdout.jsonl", "stderr.log"}
CREDENTIAL_NAMES = {"auth.json", ".credentials.json", "credentials.json", ".env"}


class MigrationInvalid(ValueError):
    """A neutral, path-free rejection of incomplete or ambiguous evidence."""


def _require(value, message):
    if not value:
        raise MigrationInvalid(message)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _encoded(value):
    return (json.dumps(value, indent=2, allow_nan=False) + "\n").encode()


def _git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    _require(result.returncode == 0, "Required local Git evidence is unavailable")
    return result.stdout


def _plain(path):
    _require(not any(p.is_symlink() for p in (path, *path.parents)), "Symlinked evidence is not supported")
    _require(path.is_file(), "Required evidence file is missing")
    return path.read_bytes()


def _tree(path):
    _require(path.is_dir() and not path.is_symlink(), "Required evidence directory is missing")
    result = {}
    for file in sorted(path.rglob("*")):
        _require(not file.is_symlink(), "Symlinked evidence is not supported")
        if file.is_dir():
            continue
        _require(file.is_file(), "Nonregular evidence is not supported")
        _require(file.name not in CREDENTIAL_NAMES, "Credential files cannot be imported")
        result[file.relative_to(path).as_posix()] = _sha(_plain(file))
    return result


def _ast_unchanged(before, after, allowed, added_imports=()):
    def retained(data):
        tree = ast.parse(data)
        tree.body = [node for node in tree.body
                     if not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in allowed)
                     and not (isinstance(node, ast.Import) and all(alias.name in added_imports for alias in node.names))]
        return ast.dump(tree, include_attributes=False)
    return retained(before) == retained(after)


def _source_proof(repo, base):
    _require(re.fullmatch(r"[0-9a-f]{40}", base) is not None, "Invalid baseline commit")
    current_commit = _git(repo, "rev-parse", "HEAD").decode().strip()
    names = [*CORE, *(PREFIX + name for name in runtime.EXECUTION_SOURCES), PREFIX + "cases.json"]
    baseline = {}
    for entry in _git(repo, "ls-tree", "-r", "-z", base, "--", *names).split(b"\0"):
        if not entry:
            continue
        header, relative = entry.split(b"\t", 1)
        mode, kind, _oid = header.decode().split()
        name = relative.decode()
        _require(kind == "blob" and mode in {"100644", "100755"}, "Unsupported source file type")
        baseline[name] = _git(repo, "show", f"{base}:{name}")
    _require(AMENDED <= set(baseline), "Baseline amendment sources are missing")
    current = {}
    for name in baseline:
        current[name] = _plain(repo / name)
        _require(current[name] == _git(repo, "show", f"{current_commit}:{name}"),
                 "Execution sources must be committed before migration")
    actual_core = {file.relative_to(repo).as_posix() for prefix in CORE
                   for file in (repo / prefix).rglob("*") if file.is_file()
                   and "__pycache__" not in file.parts and file.suffix not in {".pyc", ".pyo"}
                   and file.name != ".DS_Store"}
    _require(actual_core == {name for name in baseline if name.split("/")[0] in CORE},
             "Benchmark file coverage changed")
    changed = {name for name in baseline if baseline[name] != current[name]}
    _require(changed == AMENDED, "Only the declared adapter and runner amendment is supported")
    _require(_ast_unchanged(baseline[PREFIX + "claude_adapter.py"], current[PREFIX + "claude_adapter.py"],
                            {"inspect_trace", "_native_refusal"}), "Claude launch or isolation policy changed")
    _require(_ast_unchanged(baseline[PREFIX + "runner.py"], current[PREFIX + "runner.py"],
                            {"_verify_plan", "_sealed_plan", "_stop_requests", "request_stop", "_stop_reason",
                             "_run_experiment", "status", "main"}, {"uuid"}),
             "Planner, budgets, environment, staging, or record policy changed")
    def benchmark(files):
        digest = hashlib.sha256()
        for prefix in CORE:
            for name in sorted(n for n in files if n.startswith(prefix + "/")
                               and Path(n).suffix in {".py", ".yaml", ".md", ".json", ".csv"}):
                digest.update(name.encode() + b"\0" + hashlib.sha256(files[name]).digest())
        return digest.hexdigest()
    def execution(files):
        digest = hashlib.sha256(benchmark(files).encode())
        for name in sorted(runtime.EXECUTION_SOURCES):
            digest.update(name.encode() + b"\0" + files[PREFIX + name])
        return digest.hexdigest()
    _require(benchmark(baseline) == benchmark(current) == runner.matrix._benchmark_hash(),
             "Benchmark hash is not unchanged")
    _require(execution(current) == runtime.source_hash(), "Current execution hash is inconsistent")
    proof = {"base_commit": base, "current_commit": current_commit,
             "base_source_sha256": execution(baseline), "current_source_sha256": execution(current),
             "benchmark_sha256": benchmark(current), "changed_files": sorted(changed),
             "source_files": {name: {"base_sha256": _sha(baseline[name]), "current_sha256": _sha(current[name])}
                              for name in sorted(baseline)},
             "invariants": {"benchmark_bytes": True, "native_command_builders": True,
                            "runtime_preparation": True, "containment_and_transport": True,
                            "preflight_implementation": True, "planner_and_budgets": True}}
    return proof


def _validate_plan(source, proof):
    manifest = runner._sealed_plan(source)
    _require("migration" not in manifest, "Repeated migrations are not supported")
    _require(manifest.get("benchmark_commit") == proof["base_commit"]
             and manifest.get("source_sha256") == proof["base_source_sha256"]
             and manifest.get("benchmark_sha256") == proof["benchmark_sha256"],
             "Original plan does not match the declared baseline")
    _require(manifest.get("environment") == runner._environment(), "CLI or evaluator environment changed")
    _require(manifest.get("prompt") == runner.matrix.PROMPT
             and manifest.get("prompt_sha256") == _sha(runner.matrix.PROMPT.encode()), "Authoring prompt changed")
    _require(manifest.get("transport_policy_sha256") == _sha(_plain(Path(runtime.__file__).with_name("transport.py"))),
             "Transport policy changed")
    cases_path = runtime.REPO / PREFIX / "cases.json"
    cases = runner._cases(json.loads(_plain(cases_path)))
    _require(manifest.get("cases_sha256") == _sha(_plain(cases_path))
             and all(manifest.get(k) == v for k, v in cases.items()), "Cases changed")
    _require(manifest.get("author_timeout_s", 0) > 0 and math.isfinite(manifest["author_timeout_s"]),
             "Invalid frozen authoring budget")
    rows = manifest.get("schedule", [])
    identities = {(row["label"], row["task"]): row for row in rows}
    expected = {(row["label"], row["task"]) for row in manifest["expected_runs"]}
    _require(len(identities) == len(rows) and set(identities) == expected
             and [r.get("ordinal") for r in rows] == list(range(len(rows))), "Invalid authoring schedule")
    conditions = {condition["id"]: condition for condition in manifest["conditions"]}
    for row in rows:
        _require(row.get("provider") == conditions[row["condition"]]["provider"], "Schedule provider mismatch")
    for condition in conditions.values():
        item = manifest.get("preflight", {}).get(condition["id"], {})
        record = item.get("record", {})
        audit, process, probe = record.get("provider_audit", {}), record.get("process", {}), record.get("probe", {})
        _require(item.get("sha256") == _sha(_encoded(record)) and record.get("condition") == condition
                 and record.get("passed") is True and record.get("source_exact") is True
                 and record.get("effective_effort_checked") is True and probe.get("parent_read_blocked") is True
                 and probe.get("network_blocked") is True and process.get("exit_code") == 0
                 and not process.get("timed_out") and not process.get("cleanup_error")
                 and audit.get("valid") is True and not audit.get("errors")
                 and audit.get("resolved_model") == condition["model"] and runner._effort_matches(audit, condition)
                 and (condition["provider"] != "anthropic" or probe.get("effort") == condition["effort"]),
                 "Inherited preflight is not fully verified")
    return manifest, identities, conditions


def _without_rate_flag(value):
    result = copy.deepcopy(value)
    if isinstance(result.get("provider_audit"), dict):
        result["provider_audit"].pop("rate_limited", None)
    return result


def _audit_attempt(source, row, condition, attempt, manifest):
    number = attempt.get("number")
    _require(type(number) is int and number in {1, 2} and attempt.get("status") == "completed",
             "Unresolved or invalid native attempt")
    call_id = f"{row['label']}/{row['task']}/attempt-{number}"
    logs = source / "private/logs" / call_id
    workspace, runtime_dir = source / "private/workspaces" / call_id, source / "private/runtimes" / call_id
    log_hashes = _tree(logs)
    _require(set(log_hashes) == LOG_FILES, "Native call logs are incomplete or contain unexpected files")
    process = json.loads(_plain(logs / "process.json"))
    raw_audit = json.loads(_plain(logs / "audit.json"))
    _require(process.get("status") == "completed" and raw_audit.get("status") == "completed",
             "Native process has not finished")
    _require(all(attempt.get(k) == v for k, v in process.items()), "Attempt differs from original process receipt")
    stripped = _without_rate_flag(attempt)
    _require(all(stripped.get(k) == v for k, v in _without_rate_flag(raw_audit).items()),
             "Attempt differs from original runtime audit")
    _require(_plain(logs / "prompt.txt") == manifest["prompt"].encode(), "Native prompt differs from frozen plan")
    _require(attempt.get("cwd") == str(workspace.resolve()), "Original workspace identity mismatch")
    module = runtime.provider_module(condition["provider"])
    command = module.build_command(condition["model"], condition["effort"], runtime_dir, workspace)
    command[0] = shutil.which(command[0]) or command[0]
    expected = ["/usr/bin/sandbox-exec", "-f", str(runtime_dir / "containment.sb"), *command]
    _require(attempt.get("command") == expected, "Native command differs from the inherited command policy")
    for field in ("started_epoch_s", "finished_epoch_s", "wall_s", "epoch_elapsed_s"):
        value = attempt.get(field)
        _require(type(value) in {int, float} and math.isfinite(value) and value >= 0, "Invalid native timing evidence")
    _require(attempt["finished_epoch_s"] >= attempt["started_epoch_s"]
             and attempt.get("clock_discontinuity") is False and not attempt.get("cleanup_error"),
             "Native timing or cleanup is unresolved")
    _require(not runtime.clock_discontinuity(attempt["finished_epoch_s"] - attempt["started_epoch_s"], attempt["wall_s"]),
             "Native timing is inconsistent")
    return {"call_id": call_id, "label": row["label"], "condition": row["condition"],
            "task": row["task"], "sample": row["sample"], "attempt": number,
            "started_epoch_s": attempt["started_epoch_s"], "finished_epoch_s": attempt["finished_epoch_s"],
            "log_sha256": log_hashes}, logs, workspace, runtime_dir


def _import_record(source, row, condition, manifest):
    path = runner._record_path(source, row)
    raw = _plain(path)
    record = json.loads(raw)
    runner._validate_record(source, row, record)
    _require("migration" not in record, "Repeated record migration is not supported")
    original = copy.deepcopy(record)
    attempts = record.get("attempts")
    _require(isinstance(attempts, list) and len(attempts) <= 2, "Invalid native attempt list")
    _require([a.get("number") for a in attempts] == list(range(1, len(attempts) + 1)), "Attempt coverage is incomplete")
    calls, sources = [], []
    for attempt in attempts:
        call, logs, workspace, runtime_dir = _audit_attempt(source, row, condition, attempt, manifest)
        calls.append(call)
        sources.append((logs, workspace, runtime_dir))
    artifact = runner._artifact_path(source, row)
    reclassified = False
    if record["status"] == "deferred":
        _require(not attempts and not artifact.exists(), "Deferred author has unexpected evidence")
        artifact = None
    else:
        _require(bool(attempts), "Finished author has no native attempt")
        last = attempts[-1]
        for earlier in attempts[:-1]:
            _require(earlier.get("launch_error") and not earlier.get("artifact_present")
                     and earlier.get("artifact_sha256") is None, "Multiple content attempts cannot be imported")
        _require(record.get("artifact_present") is last.get("artifact_present")
                 and record.get("artifact_sha256") == last.get("artifact_sha256")
                 and record.get("provider_audit") == last.get("provider_audit"), "Top-level author evidence is inconsistent")
        _require(record.get("wall_s") == sum(a.get("wall_s", 0) for a in attempts), "Authoring duration is inconsistent")
        if record["status"] == "blocked":
            _require(condition["provider"] == "anthropic" and record.get("stop_reason") == "invalid_provider_audit"
                     and len(attempts) == 1 and not last.get("artifact_present") and last.get("artifact_sha256") is None,
                     "Blocked author is not the declared no-artifact refusal case")
            logs, workspace, _runtime = sources[-1]
            audit = claude_adapter.inspect_trace(logs / "stdout.jsonl", logs / "stderr.log", condition["model"], condition["effort"])
            _require(audit.get("init_cwd") == str(workspace.resolve()), "Refusal workspace identity mismatch")
            audit["rate_limited"] = False
            result = dict(last, provider_audit=audit)
            guard = runner._guard(result)
            _require(audit.get("provider_refusal") is True and audit.get("completion_successful") is False
                     and runner._stop_reason(result, condition, guard) is None, "Native refusal could not be safely reclassified")
            _require(runner.matrix._artifact_hash(workspace) is None, "Refusal workspace contains a submission")
            record.update(status="completed", outcome="provider_refusal", eligible_for_grading=True,
                          provider_audit=audit, subscription_guard=guard)
            record.pop("stop_reason", None)
            artifact = artifact if artifact.exists() else workspace
            reclassified = True
        else:
            _require(record["status"] == "completed" and last.get("provider_audit", {}).get("valid") is True
                     and not last.get("provider_audit", {}).get("errors"), "Completed author audit is invalid")
        _require(artifact.is_dir() and runner.matrix._artifact_hash(artifact) == record.get("artifact_sha256")
                 and (artifact / "main.py").is_file() is record.get("artifact_present"), "Frozen artifact is inconsistent")
    migration = {"schema_version": 1, "source_plan_sha256": manifest["plan_sha256"],
                 "source_record_sha256": original["record_sha256"], "source_record_file_sha256": _sha(raw),
                 "original_status": original["status"], "outcome_reclassified": reclassified,
                 "call_ids": [call["call_id"] for call in calls]}
    record["migration"] = migration
    record.pop("record_sha256")
    record["record_sha256"] = runner._digest(record)
    return {"row": row, "raw": raw, "record": record, "artifact": artifact,
            "artifact_files": _tree(artifact) if artifact else {}, "calls": calls, "sources": sources}


def _migrate_locked(source, out, base_commit):
    proof = _source_proof(runtime.REPO, base_commit)
    manifest, identities, conditions = _validate_plan(source, proof)
    old_manifest = _plain(source / "evaluation_manifest.json")
    _require(json.loads(old_manifest) == manifest, "Original manifest changed during validation")
    old_state = _plain(source / "orchestration.json")
    state = json.loads(old_state)
    _require(state.get("plan_sha256") == manifest["plan_sha256"]
             and set(state.get("providers", {})) == {c["provider"] for c in conditions.values()},
             "Original orchestration state does not match its plan")
    _require(all(v.get("status") in {"ready", "paused", "stopped"} for v in state["providers"].values()),
             "Original provider state is unresolved")
    for row in manifest["expected_runs"]:
        _require(not (source / row["label"] / f"{row['task']}_s{row['seed']}").exists(),
                 "Migration requires an author-only plan with no grading attempts")
    _require(not list(source.glob("*/*/grade.json")) and not list(source.glob("*/*/failure.json")),
             "Unexpected grading evidence exists")
    record_paths = sorted((source / "authoring").glob("*/*.json"))
    imported = []
    for path in record_paths:
        identity = path.parent.name, path.stem
        _require(identity in identities, "Unexpected authoring record identity")
        row = identities[identity]
        imported.append(_import_record(source, row, conditions[row["condition"]], manifest))
    _require(bool(imported), "No sealed authoring records exist to import")
    expected_calls = {call["call_id"] for item in imported for call in item["calls"]}
    actual_calls = {p.relative_to(source / "private/logs").as_posix()
                    for p in (source / "private/logs").glob("*/*/*")}
    _require(actual_calls == expected_calls, "Unrecorded or missing native calls exist")
    expected_artifacts = {(item["row"]["label"], item["row"]["task"]) for item in imported
                          if runner._artifact_path(source, item["row"]).exists()}
    actual_artifacts = {(p.parent.name, p.name) for p in (source / "private/artifacts").glob("*/*")}
    _require(actual_artifacts == expected_artifacts, "Unrecorded frozen artifacts exist")
    migration = {"schema_version": 1, "amendment_id": AMENDMENT_ID,
                 "source_plan_sha256": manifest["plan_sha256"], "source_manifest_file_sha256": _sha(old_manifest)}
    revised = copy.deepcopy(manifest)
    revised.update(source_sha256=proof["current_source_sha256"], benchmark_commit=proof["current_commit"], migration=migration)
    revised.pop("plan_sha256")
    revised["plan_sha256"] = runner._digest(revised)
    unchanged = sorted(set(manifest) - {"source_sha256", "benchmark_commit", "plan_sha256"})
    _require(all(revised[k] == manifest[k] for k in unchanged), "An undeclared plan field changed")
    receipt = {"schema_version": 1, "amendment_id": AMENDMENT_ID, "migration_complete": True,
               "created_at": datetime.now(timezone.utc).isoformat(), **migration,
               "target_plan_sha256": revised["plan_sha256"], "source_proof": proof,
               "unchanged_plan_fields": unchanged, "source_orchestration_file_sha256": _sha(old_state),
               "preserved_calls": [call for item in imported for call in item["calls"]],
               "imported_records": [{**item["record"]["migration"],
                    **{k: item["row"][k] for k in ("label", "condition", "sample", "task")},
                    "target_record_sha256": item["record"]["record_sha256"],
                    "artifact_sha256": item["record"]["artifact_sha256"],
                    "artifact_file_count": len(item["artifact_files"])} for item in imported],
               "provider_calls_performed": 0, "grading_calls_performed": 0,
               "preflight_inherited": True, "original_controls_replayed": False,
               "credentials_copied": False, "destination_providers_initially_paused": True}
    with tempfile.TemporaryDirectory(prefix=".author-migration-", dir=out.parent) as temporary:
        staging = Path(temporary)
        staging.chmod(0o700)
        private = staging / "private/migration"
        private.mkdir(parents=True)
        (private / "source-manifest.json").write_bytes(old_manifest)
        (private / "source-orchestration.json").write_bytes(old_state)
        for item in imported:
            row = item["row"]
            target = private / "authoring" / row["label"] / f"{row['task']}.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(item["raw"])
            runner.runtime.write_json(runner._record_path(staging, row), item["record"])
            if item["artifact"] is not None:
                target = runner._artifact_path(staging, row)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(item["artifact"], target)
                _require(_tree(target) == item["artifact_files"] == _tree(item["artifact"]), "Artifact changed during migration")
            for call, (logs, _workspace, original_runtime) in zip(item["calls"], item["sources"]):
                target = staging / "private/logs" / call["call_id"]
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(logs, target)
                _require(_tree(target) == call["log_sha256"] == _tree(logs), "Logs changed during migration")
                # Codex rollout traces can corroborate identity; the runtime's
                # credential store and configuration trees are never copied.
                sessions = original_runtime / "codex_home/sessions"
                if sessions.exists():
                    hashes = _tree(sessions)
                    _require(all(name.endswith(".jsonl") for name in hashes), "Unexpected private rollout file")
                    target = private / "rollouts" / call["call_id"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(sessions, target)
                    _require(_tree(target) == hashes == _tree(sessions), "Rollouts changed during migration")
                    call["rollout_sha256"] = hashes
            _require(_plain(runner._record_path(source, row)) == item["raw"], "Original author record changed during migration")
        _require(_plain(source / "evaluation_manifest.json") == old_manifest
                 and _plain(source / "orchestration.json") == old_state, "Original plan or state changed during migration")
        _require(_source_proof(runtime.REPO, base_commit) == proof, "Execution sources changed during migration")
        runtime.write_json(staging / "migration_receipt.json", receipt)
        runtime.write_json(staging / "orchestration.json", {
            "schema_version": 1, "plan_sha256": revised["plan_sha256"],
            "providers": {provider: {"status": "paused", "reason": "migration_requires_explicit_resume"}
                          for provider in state["providers"]},
            "control_acknowledgements": {provider: [] for provider in state["providers"]}})
        runtime.write_json(staging / "evaluation_manifest.json", revised)
        runner._verify_plan(staging)
        for item in imported:
            runner._validate_record(staging, item["row"], item["record"])
        out.mkdir(mode=0o700, exist_ok=False)
        # Publish the sealed plan last; an incomplete directory cannot run.
        for child in sorted(staging.iterdir(), key=lambda p: p.name == "evaluation_manifest.json"):
            shutil.move(str(child), out / child.name)
    return receipt


def migrate_authoring(source: Path, out: Path, *, base_commit: str = DEFAULT_BASE) -> dict:
    """Verify and copy completed evidence without resuming either provider."""
    _require(not any(p.is_symlink() for p in (source, *source.parents, out, *out.parents)),
             "Symlinked experiment roots are not supported")
    source, out = source.resolve(), out.resolve()
    _require(source.is_dir() and out.parent.is_dir() and not out.exists(), "A fresh destination and existing source are required")
    _require(not source.is_relative_to(out) and not out.is_relative_to(source)
             and not out.is_relative_to(runtime.REPO.resolve()), "Experiment roots must be disjoint and outside the repository")
    lock = source / ".runner.lock"
    _plain(lock)
    with lock.open("rb") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MigrationInvalid("Original evaluator is still running") from exc
        return _migrate_locked(source, out, base_commit)


def _map_sha256(files: dict[str, str]) -> str:
    """Hash a relative-name to file-hash map without private root paths."""
    digest = hashlib.sha256()
    for name, value in sorted(files.items()):
        digest.update(name.encode() + b"\0" + bytes.fromhex(value))
    return digest.hexdigest()


def _parser_source_proof(repo: Path, base: str) -> dict:
    """Prove that a committed tree contains only the parser amendment."""
    _require(re.fullmatch(r"[0-9a-f]{40}", base) is not None, "Invalid parser baseline commit")
    current_commit = _git(repo, "rev-parse", "HEAD").decode().strip()
    selected = [*CORE, *(PREFIX + name for name in runtime.EXECUTION_SOURCES), PREFIX + "cases.json"]
    baseline = {}
    for entry in _git(repo, "ls-tree", "-r", "-z", base, "--", *selected).split(b"\0"):
        if not entry:
            continue
        header, relative = entry.split(b"\t", 1)
        mode, kind, _oid = header.decode().split()
        name = relative.decode()
        _require(kind == "blob" and mode in {"100644", "100755"}, "Unsupported source file type")
        baseline[name] = _git(repo, "show", f"{base}:{name}")
    current = {}
    for name in baseline:
        current[name] = _plain(repo / name)
        _require(current[name] == _git(repo, "show", f"{current_commit}:{name}"),
                 "Execution sources must be committed before parser migration")
    actual_core = {path.relative_to(repo).as_posix() for prefix in CORE
                   for path in (repo / prefix).rglob("*") if path.is_file()
                   and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}
                   and path.name != ".DS_Store"}
    _require(actual_core == {name for name in baseline if name.split("/")[0] in CORE},
             "Benchmark file coverage changed")
    changed = {name for name in baseline if baseline[name] != current[name]}
    _require(changed == PARSER_CHANGED, "Only the declared Claude parser amendment is supported")
    adapter = PREFIX + "claude_adapter.py"
    _require(_ast_unchanged(baseline[adapter], current[adapter], {"_native_refusal"}),
             "Claude launch, isolation, or outer audit policy changed")

    def benchmark(files):
        digest = hashlib.sha256()
        for prefix in CORE:
            for name in sorted(n for n in files if n.startswith(prefix + "/")
                               and Path(n).suffix in {".py", ".yaml", ".md", ".json", ".csv"}):
                digest.update(name.encode() + b"\0" + hashlib.sha256(files[name]).digest())
        return digest.hexdigest()

    def execution(files):
        digest = hashlib.sha256(benchmark(files).encode())
        for name in sorted(runtime.EXECUTION_SOURCES):
            digest.update(name.encode() + b"\0" + files[PREFIX + name])
        return digest.hexdigest()

    _require(benchmark(baseline) == benchmark(current) == runner.matrix._benchmark_hash(),
             "Benchmark hash is not unchanged")
    _require(execution(current) == runtime.source_hash(), "Current execution hash is inconsistent")
    tool_name = PREFIX + "migrate.py"
    tool = _plain(repo / tool_name)
    _require(tool == _git(repo, "show", f"{current_commit}:{tool_name}"),
             "Migration tool must be committed before use")
    return {
        "base_commit": base, "current_commit": current_commit,
        "base_source_sha256": execution(baseline), "current_source_sha256": execution(current),
        "benchmark_sha256": benchmark(current), "changed_files": sorted(changed),
        "migration_tool_sha256": _sha(tool),
        "source_files": {name: {"base_sha256": _sha(baseline[name]), "current_sha256": _sha(current[name])}
                         for name in sorted(baseline)},
        "invariants": {"benchmark_bytes": True, "native_command_builder": True,
                       "runtime_preparation": True, "containment_and_transport": True,
                       "preflight_implementation": True, "planner_runner_and_budgets": True,
                       "parser_function_only": True},
    }


def _source_inventory(root: Path) -> dict:
    """Hash every source entry except the advisory runner lock."""
    digest = hashlib.sha256()
    regular_files = symlinks = regular_file_bytes = 0
    entries = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if relative == ".runner.lock":
            continue
        if path.is_symlink():
            kind, data = b"L", os.readlink(path).encode()
            symlinks += 1
        elif path.is_dir():
            continue
        else:
            _require(path.is_file(), "Unsupported source evidence entry")
            kind, data = b"F", path.read_bytes()
            regular_files += 1
            regular_file_bytes += len(data)
        file_digest = _sha(data)
        digest.update(kind + b"\0" + relative.encode() + b"\0" + file_digest.encode() + b"\n")
        entries.append((relative, kind.decode(), file_digest))
    return {"sha256": digest.hexdigest(), "regular_files": regular_files,
            "symlinks": symlinks, "entries": regular_files + symlinks,
            "regular_file_bytes": regular_file_bytes, "items": entries}


def _suspend_source_proof(repo: Path, base: str) -> dict:
    """Prove that only terminal failure accounting changed execution code."""
    _require(re.fullmatch(r"[0-9a-f]{40}", base) is not None, "Invalid suspension baseline commit")
    current_commit = _git(repo, "rev-parse", "HEAD").decode().strip()
    selected = [*CORE, *(PREFIX + name for name in runtime.EXECUTION_SOURCES), PREFIX + "cases.json"]
    baseline = {}
    for entry in _git(repo, "ls-tree", "-r", "-z", base, "--", *selected).split(b"\0"):
        if not entry:
            continue
        header, relative = entry.split(b"\t", 1)
        mode, kind, _oid = header.decode().split()
        name = relative.decode()
        _require(kind == "blob" and mode in {"100644", "100755"}, "Unsupported source file type")
        baseline[name] = _git(repo, "show", f"{base}:{name}")
    current = {}
    for name in baseline:
        current[name] = _plain(repo / name)
        _require(current[name] == _git(repo, "show", f"{current_commit}:{name}"),
                 "Execution sources must be committed before suspension migration")
    actual_core = {path.relative_to(repo).as_posix() for prefix in CORE
                   for path in (repo / prefix).rglob("*") if path.is_file()
                   and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}
                   and path.name != ".DS_Store"}
    _require(actual_core == {name for name in baseline if name.split("/")[0] in CORE},
             "Benchmark file coverage changed")
    changed = {name for name in baseline if baseline[name] != current[name]}
    _require(changed == SUSPEND_CHANGED, "Execution sources changed during the accounting amendment")

    def benchmark(files):
        digest = hashlib.sha256()
        for prefix in CORE:
            for name in sorted(item for item in files if item.startswith(prefix + "/")
                               and Path(item).suffix in {".py", ".yaml", ".md", ".json", ".csv"}):
                digest.update(name.encode() + b"\0" + hashlib.sha256(files[name]).digest())
        return digest.hexdigest()

    def execution(files):
        digest = hashlib.sha256(benchmark(files).encode())
        for name in sorted(runtime.EXECUTION_SOURCES):
            digest.update(name.encode() + b"\0" + files[PREFIX + name])
        return digest.hexdigest()

    _require(benchmark(baseline) == benchmark(current) == runner.matrix._benchmark_hash(),
             "Benchmark hash is not unchanged")
    _require(execution(current) == runtime.source_hash(), "Current execution hash is inconsistent")
    committed_support = {}
    for name in (PREFIX + "migrate.py", PREFIX + "analyze.py", PREFIX + "publish.py"):
        data = _plain(repo / name)
        _require(data == _git(repo, "show", f"{current_commit}:{name}"),
                 "Migration and reporting sources must be committed before use")
        committed_support[name] = _sha(data)
    return {"base_commit": base, "current_commit": current_commit,
            "base_source_sha256": execution(baseline), "current_source_sha256": execution(current),
            "benchmark_sha256": benchmark(current), "changed_files": sorted(changed),
            "support_source_sha256": committed_support,
            "source_files": {name: {"base_sha256": _sha(baseline[name]),
                                     "current_sha256": _sha(current[name])}
                             for name in sorted(baseline)},
            "invariants": {"benchmark_bytes": True, "native_command_builders": True,
                           "runtime_preparation": True, "containment_and_transport": True,
                           "preflight_implementation": True, "planner_and_budgets": True,
                           "terminal_accounting_only": True}}


def _validate_prior_migration(source: Path, manifest: dict) -> tuple[dict, bytes, dict, dict]:
    """Verify the complete v1-to-v2 receipt chain rather than trusting a tag."""
    prior_raw = _plain(source / "migration_receipt.json")
    prior = json.loads(prior_raw)
    migration = manifest.get("migration")
    _require(isinstance(migration, dict) and migration.get("schema_version") == 1
             and migration.get("amendment_id") == AMENDMENT_ID,
             "Source plan lacks the required prior migration")
    _require(prior.get("migration_complete") is True and prior.get("amendment_id") == AMENDMENT_ID
             and prior.get("target_plan_sha256") == manifest["plan_sha256"]
             and prior.get("provider_calls_performed") == 0 and prior.get("grading_calls_performed") == 0
             and prior.get("credentials_copied") is False,
             "Prior migration receipt is inconsistent")
    for field in ("source_plan_sha256", "source_manifest_file_sha256"):
        _require(migration.get(field) == prior.get(field), "Prior manifest migration link is inconsistent")
    archived_manifest = _plain(source / "private/migration/source-manifest.json")
    archived_state = _plain(source / "private/migration/source-orchestration.json")
    _require(_sha(archived_manifest) == prior.get("source_manifest_file_sha256")
             and _sha(archived_state) == prior.get("source_orchestration_file_sha256"),
             "Archived prior plan evidence changed")
    rows = {}
    for item in prior.get("imported_records", []):
        key = item.get("label"), item.get("task")
        _require(all(isinstance(value, str) for value in key) and key not in rows,
                 "Prior imported record identities are invalid")
        current = json.loads(_plain(runner._record_path(source, {"label": key[0], "task": key[1]})))
        archived = _plain(source / "private/migration/authoring" / key[0] / f"{key[1]}.json")
        archived_record = json.loads(archived)
        _require(_sha(archived) == item.get("source_record_file_sha256")
                 and archived_record.get("record_sha256") == item.get("source_record_sha256")
                 and current.get("record_sha256") == item.get("target_record_sha256")
                 and current.get("migration", {}).get("source_record_file_sha256") == _sha(archived),
                 "Prior imported record hash chain changed")
        rows[key] = item
    calls = {}
    for item in prior.get("preserved_calls", []):
        call_id = item.get("call_id")
        _require(isinstance(call_id, str) and len(call_id.split("/")) == 3 and call_id not in calls,
                 "Prior preserved call identities are invalid")
        _require(_tree(source / "private/logs" / call_id) == item.get("log_sha256"),
                 "Prior preserved call logs changed")
        calls[call_id] = item
    _require(len(rows) == len(prior.get("imported_records", [])) > 0
             and len(calls) == len(prior.get("preserved_calls", [])) > 0,
             "Prior migration coverage is empty or duplicated")
    return prior, prior_raw, rows, calls


def _validate_parser_plan(source: Path, proof: dict) -> tuple[dict, dict, dict, bytes, bytes, dict]:
    manifest = runner._sealed_plan(source)
    raw = _plain(source / "evaluation_manifest.json")
    _require(json.loads(raw) == manifest, "Source manifest changed during validation")
    _require(manifest.get("benchmark_commit") == proof["base_commit"]
             and manifest.get("source_sha256") == proof["base_source_sha256"]
             and manifest.get("benchmark_sha256") == proof["benchmark_sha256"],
             "Source plan does not match the parser baseline")
    _require(manifest.get("environment") == runner._environment(), "CLI or evaluator environment changed")
    _require(manifest.get("prompt") == runner.matrix.PROMPT
             and manifest.get("prompt_sha256") == _sha(runner.matrix.PROMPT.encode()), "Authoring prompt changed")
    _require(manifest.get("transport_policy_sha256") == _sha(_plain(Path(runtime.__file__).with_name("transport.py"))),
             "Transport policy changed")
    cases_path = runtime.REPO / PREFIX / "cases.json"
    cases = runner._cases(json.loads(_plain(cases_path)))
    _require(manifest.get("cases_sha256") == _sha(_plain(cases_path))
             and all(manifest.get(key) == value for key, value in cases.items()), "Cases changed")
    _require(manifest.get("author_timeout_s", 0) > 0 and math.isfinite(manifest["author_timeout_s"]),
             "Invalid authoring budget")
    rows = manifest.get("schedule", [])
    expected = manifest.get("expected_runs", [])
    identities = {(row["label"], row["task"]): row for row in rows}
    expected_ids = {(row["label"], row["task"], row["seed"]): row for row in expected}
    _require(len(rows) == 528 and len(expected) == 2640 and len(identities) == 528
             and len(expected_ids) == 2640 and [row.get("ordinal") for row in rows] == list(range(528))
             and {(a, b) for a, b, _seed in expected_ids} == set(identities),
             "Parser migration requires the exact frozen 528 by 2640 plan")
    conditions = {item["id"]: item for item in manifest["conditions"]}
    for row in rows:
        _require(row.get("provider") == conditions[row["condition"]]["provider"], "Schedule provider mismatch")
    for condition in conditions.values():
        item = manifest.get("preflight", {}).get(condition["id"], {})
        record = item.get("record", {})
        audit, process, probe = record.get("provider_audit", {}), record.get("process", {}), record.get("probe", {})
        _require(item.get("sha256") == _sha(_encoded(record)) and record.get("condition") == condition
                 and record.get("passed") is True and record.get("source_exact") is True
                 and record.get("effective_effort_checked") is True
                 and probe.get("parent_read_blocked") is True and probe.get("network_blocked") is True
                 and process.get("exit_code") == 0 and not process.get("timed_out") and not process.get("cleanup_error")
                 and audit.get("valid") is True and not audit.get("errors")
                 and audit.get("resolved_model") == condition["model"] and runner._effort_matches(audit, condition)
                 and (condition["provider"] != "anthropic" or probe.get("effort") == condition["effort"]),
                 "Inherited preflight is not fully verified")
    state_raw = _plain(source / "orchestration.json")
    state = json.loads(state_raw)
    _require(state.get("plan_sha256") == manifest["plan_sha256"]
             and set(state.get("providers", {})) == {item["provider"] for item in conditions.values()}
             and all(item.get("status") in {"paused", "stopped"} for item in state["providers"].values()),
             "Source orchestration is not quiescent")
    requests = runner._stop_requests(source, manifest)
    acknowledgements = state.get("control_acknowledgements", {})
    _require(all(request["request_id"] in acknowledgements.get(provider, [])
                 for request in requests for provider in request["providers"]),
             "Source has an unacknowledged control request")
    prior = _validate_prior_migration(source, manifest)
    return manifest, identities, expected_ids, raw, state_raw, {
        "receipt": prior[0], "raw": prior[1], "records": prior[2], "calls": prior[3]}


def _parser_records(source: Path, manifest: dict, identities: dict, conditions: dict,
                    prior: dict) -> tuple[list[dict], dict, list[dict], list[dict]]:
    paths = sorted((source / "authoring").glob("*/*.json"))
    present, calls, blocked, deferred = [], {}, [], []
    for path in paths:
        key = path.parent.name, path.stem
        _require(key in identities, "Unexpected author record identity")
        row = identities[key]
        raw = _plain(path)
        record = json.loads(raw)
        try:
            runner._validate_record(source, row, record)
        except (KeyError, TypeError, ValueError) as exc:
            raise MigrationInvalid(f"Unresolved or invalid author record: {key[0]}/{key[1]}") from exc
        _require(record.get("status") not in {"running", "retry_pending"}, "Unresolved author record")
        attempts = record.get("attempts", [])
        _require(isinstance(attempts, list)
                 and [attempt.get("number") for attempt in attempts] == list(range(1, len(attempts) + 1)),
                 "Invalid author attempt coverage")
        item_record = {"row": row, "raw": raw, "record": record}
        if record.get("status") == "blocked":
            blocked.append(item_record)
        elif record.get("status") == "deferred":
            _require(not attempts and not runner._artifact_path(source, row).exists(),
                     "Deferred author has execution evidence")
            deferred.append(item_record)
        else:
            _require(record.get("status") == "completed", "Unsupported author state")
        prior_row = prior["records"].get(key)
        if prior_row is not None:
            _require(record.get("record_sha256") == prior_row.get("target_record_sha256"),
                     "Prior imported target record changed")
            for call_id in record.get("migration", {}).get("call_ids", []):
                item = prior["calls"].get(call_id)
                _require(item is not None and _tree(source / "private/logs" / call_id) == item.get("log_sha256"),
                         "Prior imported call evidence changed")
                calls[call_id] = {**item, "lineage": "prior_migration"}
        else:
            condition = conditions[row["condition"]]
            for attempt in attempts:
                item, logs, _workspace, runtime_dir = _audit_attempt(source, row, condition, attempt, manifest)
                _require(item["call_id"] not in calls, "Duplicate native call identity")
                sessions = runtime_dir / "codex_home/sessions"
                item["rollout_sha256"] = _tree(sessions) if sessions.exists() else {}
                _require(all(name.endswith(".jsonl") for name in item["rollout_sha256"]),
                         "Unexpected private rollout evidence")
                calls[item["call_id"]] = {**item, "lineage": "source_plan"}
        present.append(item_record)
    _require(len(blocked) == 1 and (blocked[0]["row"]["label"], blocked[0]["row"]["task"]) == PARSER_TARGET,
             "Parser migration requires exactly the declared blocked refusal")
    actual_calls = {path.relative_to(source / "private/logs").as_posix()
                    for path in (source / "private/logs").glob("*/*/*")}
    _require(actual_calls == set(calls), "Unrecorded or missing native calls exist")
    return present, blocked[0], deferred, list(calls.values())


def _parser_runs(source: Path, expected: dict, records: list[dict]) -> tuple[list[dict], dict]:
    authors = {(item["row"]["label"], item["row"]["task"]): item["record"] for item in records}
    outcome_dirs = {}
    for kind in ("grade", "failure"):
        for path in source.glob(f"*/*/{kind}.json"):
            relative = path.parent.relative_to(source)
            _require(len(relative.parts) == 2, "Nested or invalid run evidence")
            label, stem = relative.parts
            _require("_s" in stem, "Invalid run identity")
            task, seed_text = stem.rsplit("_s", 1)
            _require(seed_text.isdigit(), "Invalid run seed identity")
            key = label, task, int(seed_text)
            _require(key in expected and key not in outcome_dirs, "Unexpected or contradictory run evidence")
            outcome_dirs[key] = (kind, path.parent)
    allowed_labels = {key[0] for key in expected}
    actual_dirs = {path.relative_to(source).as_posix() for label in allowed_labels
                   if (source / label).is_dir() for path in (source / label).iterdir() if path.is_dir()}
    _require(actual_dirs == {directory.relative_to(source).as_posix() for _kind, directory in outcome_dirs.values()},
             "Partial or unexpected run directories exist")
    rows, grade_logs = [], {}
    for key, (kind, directory) in sorted(outcome_dirs.items()):
        label, task, seed = key
        author = authors.get((label, task))
        _require(author is not None, "Run has no sealed author record")
        tree = _tree(directory)
        if kind == "grade":
            _require("failure.json" not in tree and {"grade.json", "meta.json", "adapter.json"} <= set(tree),
                     "Cached grade evidence is incomplete")
            meta = json.loads(_plain(directory / "meta.json"))
            adapter = json.loads(_plain(directory / "adapter.json"))
            _require(all(meta.get(field) == value for field, value in (("label", label), ("task", task), ("seed", seed)))
                     and adapter.get("artifact_sha256") == author.get("artifact_sha256")
                     and not adapter.get("cleanup_error") and not adapter.get("timed_out") and not adapter.get("error"),
                     "Cached grade provenance is stale or unresolved")
            for suffix in (".log", ".stderr"):
                name = f"{label}/{task}_s{seed}{suffix}"
                path = source / "grading_logs" / name
                grade_logs[name] = _sha(_plain(path))
        else:
            _require(set(tree) == {"failure.json"}, "Cached author failure contains unexpected evidence")
            failure = json.loads(_plain(directory / "failure.json"))
            _require(failure == {"label": label, "task": task, "seed": seed,
                                 "stage": "authoring", "reason": "missing_author_artifact"}
                     and author.get("status") == "completed" and author.get("artifact_present") is False
                     and author.get("artifact_sha256") is None,
                     "Cached failure provenance is inconsistent")
        rows.append({"label": label, "task": task, "seed": seed, "kind": kind,
                     "tree_sha256": _map_sha256(tree), "file_count": len(tree), "directory": directory,
                     "files": tree})
    _require(_tree(source / "grading_logs") == grade_logs,
             "Grading logs are missing or contain unrecorded executions")
    return rows, grade_logs


def _reclassify_parser_refusal(source: Path, target: dict, manifest: dict,
                               conditions: dict) -> tuple[dict, Path, dict]:
    row, original = target["row"], target["record"]
    _require(original.get("status") == "blocked" and original.get("stop_reason") == "invalid_provider_audit"
             and original.get("artifact_present") is False and original.get("artifact_sha256") is None
             and original.get("eligible_for_grading") is False and len(original.get("attempts", [])) == 1,
             "Declared blocked record does not match the parser-only case")
    attempt = original["attempts"][0]
    _require(attempt.get("exit_code") == 1 and attempt.get("timed_out") is False
             and not attempt.get("cleanup_error") and not attempt.get("launch_error")
             and attempt.get("artifact_present") is False and attempt.get("artifact_sha256") is None
             and set(original.get("provider_audit", {}).get("errors", [])) == {
                 "native_provider_refusal_unverified", "primary_model_mismatch", "completion_not_successful"},
             "Blocked record has additional unresolved evidence")
    call_id = f"{row['label']}/{row['task']}/attempt-1"
    logs = source / "private/logs" / call_id
    workspace = source / "private/workspaces" / call_id
    condition = conditions[row["condition"]]
    audit = claude_adapter.inspect_trace(logs / "stdout.jsonl", logs / "stderr.log",
                                         condition["model"], condition["effort"])
    audit["rate_limited"] = False
    result = dict(attempt, provider_audit=audit)
    guard = runner._guard(result)
    _require(audit.get("provider_refusal") is True and audit.get("completion_successful") is False
             and audit.get("valid") is True and not audit.get("errors")
             and runner._stop_reason(result, condition, guard) is None,
             "Repeated native refusal did not pass the amended parser")
    events = [json.loads(line) for line in _plain(logs / "stdout.jsonl").splitlines() if line.strip()]
    refusal_events = sum(event.get("type") == "system"
                         and event.get("subtype") == "model_refusal_no_fallback" for event in events)
    synthetic_events = sum(event.get("type") == "assistant"
                           and isinstance(event.get("message"), dict)
                           and event["message"].get("model") == "<synthetic>" for event in events)
    _require(refusal_events == synthetic_events == 2,
             "Declared parser amendment requires exactly two matched refusal envelopes")
    _require(runner.matrix._artifact_hash(workspace) is None, "Blocked refusal workspace contains a submission")
    with tempfile.TemporaryDirectory(prefix=".expected-task-", dir=source.parent) as temporary:
        expected_workspace = Path(temporary) / "workspace"
        runner._stage(expected_workspace, row["task"])
        _require(_tree(workspace) == _tree(expected_workspace),
                 "Blocked refusal workspace differs from frozen task inputs")
    record = copy.deepcopy(original)
    record.update(status="completed", outcome="provider_refusal", eligible_for_grading=True,
                  provider_audit=audit, subscription_guard=guard,
                  migration={"schema_version": 1, "source_plan_sha256": manifest["plan_sha256"],
                             "source_record_sha256": original["record_sha256"],
                             "source_record_file_sha256": _sha(target["raw"]),
                             "original_status": "blocked", "outcome_reclassified": True,
                             "call_ids": [call_id]})
    record.pop("stop_reason", None)
    record.pop("record_sha256")
    record["record_sha256"] = runner._digest(record)
    return record, workspace, {"call_id": call_id,
        "native_refusal_envelopes": refusal_events,
        "source_record_sha256": original["record_sha256"], "target_record_sha256": record["record_sha256"],
        "source_record_file_sha256": _sha(target["raw"])}


def _copy_tree(source: Path, target: Path, expected: dict[str, str]):
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    _require(_tree(target) == expected == _tree(source), "Evidence changed while copying")


def _migrate_parser_locked(source: Path, out: Path, base_commit: str) -> dict:
    proof = _parser_source_proof(runtime.REPO, base_commit)
    manifest, identities, expected, manifest_raw, state_raw, prior = _validate_parser_plan(source, proof)
    conditions = {item["id"]: item for item in manifest["conditions"]}
    records, target, deferred, calls = _parser_records(source, manifest, identities, conditions, prior)
    ordinary = [item for item in records if item is not target]
    statuses = {name: sum(item["record"]["status"] == name for item in records)
                for name in ("completed", "blocked", "deferred")}
    absent = set(identities) - {(item["row"]["label"], item["row"]["task"]) for item in records}
    for label, task in absent:
        _require(not (source / "private/artifacts" / label / task).exists()
                 and not (source / "private/logs" / label / task).exists()
                 and not (source / "private/workspaces" / label / task).exists(),
                 "Never-invoked row has execution evidence")
    run_rows, grading_logs = _parser_runs(source, expected, records)
    artifact_maps = {}
    for item in records:
        row, record = item["row"], item["record"]
        artifact = runner._artifact_path(source, row)
        if record["status"] == "completed":
            artifact_maps[(row["label"], row["task"])] = _tree(artifact)
        else:
            _require(not artifact.exists(), "Noncompleted source record has a frozen artifact")
    actual_artifacts = {(path.parent.name, path.name) for path in (source / "private/artifacts").glob("*/*")}
    _require(actual_artifacts == set(artifact_maps), "Frozen artifact coverage is inconsistent")
    revised_target, target_workspace, target_receipt = _reclassify_parser_refusal(
        source, target, manifest, conditions)
    target_artifact_map = _tree(target_workspace)
    prior_tree = _tree(source / "private/migration")
    control_tree = _tree(source / "control") if (source / "control").exists() else {}
    source_logs = _tree(source / "private/logs")
    source_artifacts = _tree(source / "private/artifacts")
    run_digest = _map_sha256({f"{row['label']}/{row['task']}_s{row['seed']}/{name}": sha
                              for row in run_rows for name, sha in row["files"].items()})
    implicit_ids = [{"label": label, "task": task} for label, task in sorted(absent)]
    implicit_hash = _sha(json.dumps(implicit_ids, sort_keys=True, separators=(",", ":")).encode())
    migration = {"schema_version": 1, "amendment_id": PARSER_AMENDMENT_ID,
                 "source_plan_sha256": manifest["plan_sha256"],
                 "source_manifest_file_sha256": _sha(manifest_raw),
                 "source_migration_receipt_sha256": _sha(prior["raw"])}
    revised_manifest = copy.deepcopy(manifest)
    revised_manifest.update(source_sha256=proof["current_source_sha256"],
                            benchmark_commit=proof["current_commit"], migration=migration)
    revised_manifest.pop("plan_sha256")
    revised_manifest["plan_sha256"] = runner._digest(revised_manifest)
    unchanged = sorted(set(manifest) - {"source_sha256", "benchmark_commit", "migration", "plan_sha256"})
    _require(all(revised_manifest[field] == manifest[field] for field in unchanged),
             "An undeclared plan field changed")
    receipt = {"schema_version": 1, "amendment_id": PARSER_AMENDMENT_ID,
               "migration_complete": True, "created_at": datetime.now(timezone.utc).isoformat(), **migration,
               "target_plan_sha256": revised_manifest["plan_sha256"], "source_proof": proof,
               "unchanged_plan_fields": unchanged, "source_orchestration_file_sha256": _sha(state_raw),
               "prior_migration_tree_sha256": _map_sha256(prior_tree),
               "source_control_tree_sha256": _map_sha256(control_tree),
               "source_native_logs_sha256": _map_sha256(source_logs),
               "source_artifacts_sha256": _map_sha256(source_artifacts),
               "preserved_runs_sha256": run_digest, "grading_logs_sha256": _map_sha256(grading_logs),
               "schedule": {"planned_authors": len(identities), "planned_runs": len(expected),
                            "source_statuses": statuses, "ordinary_records_byte_preserved": len(ordinary),
                            "reclassified_records": 1, "explicit_deferred_records": len(deferred),
                            "implicit_deferred_records": len(absent),
                            "implicit_deferred_identities_sha256": implicit_hash,
                            "graded_runs_preserved": sum(row["kind"] == "grade" for row in run_rows),
                            "failure_runs_preserved": sum(row["kind"] == "failure" for row in run_rows),
                            "missing_runs_preserved": len(expected) - len(run_rows)},
               "reclassified_record": target_receipt,
               "native_calls_preserved": len(calls), "native_call_identities_sha256": _sha(
                    json.dumps(sorted(item["call_id"] for item in calls), separators=(",", ":")).encode()),
               "provider_calls_performed": 0, "subscription_checks_performed": 0,
               "grading_calls_performed": 0, "derived_failure_records_created": 0,
               "prior_controls_replayed": False, "credentials_copied": False,
               "destination_providers_initially_paused": True}
    with tempfile.TemporaryDirectory(prefix=".parser-migration-", dir=out.parent) as temporary:
        staging = Path(temporary)
        staging.chmod(0o700)
        archive = staging / "private/amendment"
        archive.mkdir(parents=True)
        (archive / "source-manifest.json").write_bytes(manifest_raw)
        (archive / "source-orchestration.json").write_bytes(state_raw)
        (archive / "source-migration-receipt.json").write_bytes(prior["raw"])
        _copy_tree(source / "private/migration", archive / "prior-migration", prior_tree)
        if control_tree:
            _copy_tree(source / "control", archive / "source-control", control_tree)
        for item in records:
            row = item["row"]
            archived = archive / "source-authoring" / row["label"] / f"{row['task']}.json"
            archived.parent.mkdir(parents=True, exist_ok=True)
            archived.write_bytes(item["raw"])
            target_path = runner._record_path(staging, row)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            if item is target:
                runtime.write_json(target_path, revised_target)
            else:
                target_path.write_bytes(item["raw"])
            if item["record"]["status"] == "completed":
                key = row["label"], row["task"]
                _copy_tree(runner._artifact_path(source, row), runner._artifact_path(staging, row), artifact_maps[key])
        _copy_tree(target_workspace, runner._artifact_path(staging, target["row"]), target_artifact_map)
        _copy_tree(source / "private/logs", staging / "private/logs", source_logs)
        for item in calls:
            if item.get("lineage") != "source_plan" or not item.get("rollout_sha256"):
                continue
            original = source / "private/runtimes" / item["call_id"] / "codex_home/sessions"
            _copy_tree(original, archive / "rollouts" / item["call_id"], item["rollout_sha256"])
        if grading_logs:
            _copy_tree(source / "grading_logs", staging / "grading_logs", grading_logs)
        for row in run_rows:
            destination = staging / row["label"] / f"{row['task']}_s{row['seed']}"
            _copy_tree(row["directory"], destination, row["files"])
        _require(_plain(source / "evaluation_manifest.json") == manifest_raw
                 and _plain(source / "orchestration.json") == state_raw
                 and _tree(source / "private/logs") == source_logs
                 and _tree(source / "private/artifacts") == source_artifacts
                 and _tree(source / "private/migration") == prior_tree
                 and (_tree(source / "control") if control_tree else {}) == control_tree,
                 "Source evidence changed during parser migration")
        for item in records:
            _require(_plain(runner._record_path(source, item["row"])) == item["raw"],
                     "Source author record changed during parser migration")
        _require(_parser_source_proof(runtime.REPO, base_commit) == proof,
                 "Committed parser sources changed during migration")
        runtime.write_json(staging / "migration_receipt.json", receipt)
        runtime.write_json(staging / "orchestration.json", {
            "schema_version": 1, "plan_sha256": revised_manifest["plan_sha256"],
            "providers": {provider: {"status": "paused", "reason": "migration_requires_explicit_resume"}
                          for provider in sorted({condition["provider"] for condition in conditions.values()})},
            "control_acknowledgements": {provider: [] for provider in
                                          sorted({condition["provider"] for condition in conditions.values()})}})
        runtime.write_json(staging / "evaluation_manifest.json", revised_manifest)
        runner._verify_plan(staging)
        for item in ordinary:
            runner._validate_record(staging, item["row"], item["record"])
        runner._validate_record(staging, target["row"], revised_target)
        out.mkdir(mode=0o700, exist_ok=False)
        for child in sorted(staging.iterdir(), key=lambda path: path.name == "evaluation_manifest.json"):
            shutil.move(str(child), out / child.name)
    return receipt


def migrate_parser_amendment(source: Path, out: Path, *, base_commit: str = PARSER_BASE) -> dict:
    """Migrate one parser-only refusal amendment without native or grading calls."""
    _require(not any(path.is_symlink() for path in (source, *source.parents, out, *out.parents)),
             "Symlinked experiment roots are not supported")
    source, out = source.resolve(), out.resolve()
    _require(source.is_dir() and out.parent.is_dir() and not out.exists(),
             "A fresh destination and existing source are required")
    _require(not source.is_relative_to(out) and not out.is_relative_to(source)
             and not out.is_relative_to(runtime.REPO.resolve()),
             "Experiment roots must be disjoint and outside the repository")
    lock = source / ".runner.lock"
    _plain(lock)
    with lock.open("rb") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MigrationInvalid("Source evaluator is still running") from exc
        return _migrate_parser_locked(source, out, base_commit)


def _validate_prior_parser_chain(source: Path, manifest: dict) -> tuple[dict, bytes]:
    raw = _plain(source / "migration_receipt.json")
    receipt = json.loads(raw)
    migration = manifest.get("migration")
    _require(isinstance(migration, dict) and migration.get("schema_version") == 1
             and migration.get("amendment_id") == PARSER_AMENDMENT_ID,
             "Source plan lacks the required parser migration")
    _require(receipt.get("migration_complete") is True
             and receipt.get("amendment_id") == PARSER_AMENDMENT_ID
             and receipt.get("target_plan_sha256") == manifest["plan_sha256"]
             and receipt.get("provider_calls_performed") == 0
             and receipt.get("subscription_checks_performed") == 0
             and receipt.get("grading_calls_performed") == 0
             and receipt.get("credentials_copied") is False,
             "Prior parser migration receipt is inconsistent")
    for field in ("source_plan_sha256", "source_manifest_file_sha256",
                  "source_migration_receipt_sha256"):
        _require(migration.get(field) == receipt.get(field),
                 "Prior parser manifest link is inconsistent")
    archive = source / "private/amendment"
    _require(_sha(_plain(archive / "source-manifest.json")) == receipt["source_manifest_file_sha256"]
             and _sha(_plain(archive / "source-orchestration.json")) == receipt["source_orchestration_file_sha256"]
             and _sha(_plain(archive / "source-migration-receipt.json")) == receipt["source_migration_receipt_sha256"]
             and _map_sha256(_tree(archive / "prior-migration")) == receipt["prior_migration_tree_sha256"],
             "Archived parser migration evidence changed")
    if receipt.get("source_control_tree_sha256"):
        _require(_map_sha256(_tree(archive / "source-control")) == receipt["source_control_tree_sha256"],
                 "Archived control evidence changed")
    item = receipt.get("reclassified_record", {})
    label, task = PARSER_TARGET
    current_raw = _plain(runner._record_path(source, {"label": label, "task": task}))
    current = json.loads(current_raw)
    archived = _plain(archive / "source-authoring" / label / f"{task}.json")
    _require(current.get("record_sha256") == item.get("target_record_sha256")
             and _sha(archived) == item.get("source_record_file_sha256")
             and json.loads(archived).get("record_sha256") == item.get("source_record_sha256")
             and current.get("migration", {}).get("source_record_file_sha256") == _sha(archived),
             "Prior parser target hash chain changed")
    return receipt, raw


def _validate_suspend_plan(source: Path, proof: dict) -> tuple[dict, dict, dict, bytes, bytes, dict, bytes]:
    manifest = runner._sealed_plan(source)
    manifest_raw = _plain(source / "evaluation_manifest.json")
    state_raw = _plain(source / "orchestration.json")
    _require(_sha(manifest_raw) == SUSPEND_MANIFEST_FILE_SHA
             and _sha(state_raw) == SUSPEND_STATE_FILE_SHA
             and json.loads(manifest_raw) == manifest
             and manifest.get("plan_sha256") == SUSPEND_PLAN,
             "Source plan or orchestration seal differs from the declared stopped run")
    _require(manifest.get("benchmark_commit") == proof["base_commit"]
             and manifest.get("source_sha256") == proof["base_source_sha256"]
             and manifest.get("benchmark_sha256") == proof["benchmark_sha256"],
             "Stopped plan does not match the declared suspension baseline")
    _require(manifest.get("environment") == runner._environment(), "CLI or evaluator environment changed")
    _require(manifest.get("prompt") == runner.matrix.PROMPT
             and manifest.get("prompt_sha256") == _sha(runner.matrix.PROMPT.encode()),
             "Authoring prompt changed")
    _require(manifest.get("transport_policy_sha256") ==
             _sha(_plain(Path(runtime.__file__).with_name("transport.py"))), "Transport policy changed")
    cases_path = runtime.REPO / PREFIX / "cases.json"
    cases = runner._cases(json.loads(_plain(cases_path)))
    _require(manifest.get("cases_sha256") == _sha(_plain(cases_path))
             and all(manifest.get(key) == value for key, value in cases.items()), "Cases changed")
    _require(manifest.get("author_timeout_s", 0) > 0 and math.isfinite(manifest["author_timeout_s"]),
             "Invalid authoring timeout")
    rows, expected = manifest.get("schedule", []), manifest.get("expected_runs", [])
    identities = {(row["label"], row["task"]): row for row in rows}
    expected_ids = {(row["label"], row["task"], row["seed"]): row for row in expected}
    _require(len(rows) == 528 and len(expected) == 2640 and len(identities) == 528
             and len(expected_ids) == 2640 and [row.get("ordinal") for row in rows] == list(range(528))
             and {(label, task) for label, task, _seed in expected_ids} == set(identities),
             "Suspension migration requires the exact frozen 528 by 2640 plan")
    conditions = {item["id"]: item for item in manifest["conditions"]}
    for row in rows:
        _require(row.get("provider") == conditions[row["condition"]]["provider"],
                 "Schedule provider mismatch")
    for condition in conditions.values():
        item = manifest.get("preflight", {}).get(condition["id"], {})
        record = item.get("record", {})
        audit, process, probe = record.get("provider_audit", {}), record.get("process", {}), record.get("probe", {})
        _require(item.get("sha256") == _sha(_encoded(record)) and record.get("condition") == condition
                 and record.get("passed") is True and record.get("source_exact") is True
                 and record.get("effective_effort_checked") is True
                 and probe.get("parent_read_blocked") is True and probe.get("network_blocked") is True
                 and process.get("exit_code") == 0 and not process.get("timed_out")
                 and not process.get("cleanup_error") and audit.get("valid") is True
                 and not audit.get("errors") and audit.get("resolved_model") == condition["model"]
                 and runner._effort_matches(audit, condition)
                 and (condition["provider"] != "anthropic" or probe.get("effort") == condition["effort"]),
                 "Inherited preflight is not fully verified")
    state = json.loads(state_raw)
    anthropic = state.get("providers", {}).get("anthropic", {})
    openai = state.get("providers", {}).get("openai", {})
    _require(state.get("plan_sha256") == manifest["plan_sha256"]
             and set(state.get("providers", {})) == {"anthropic", "openai"}
             and anthropic == {"status": "stopped", "reason": "invalid_provider_audit",
                               "condition": "claude-sonnet5-max", "task": SUSPEND_TARGET[1], "sample": 2}
             and openai.get("status") == "ready",
             "Source orchestration is not the declared quiescent suspension state")
    requests = runner._stop_requests(source, manifest)
    acknowledgements = state.get("control_acknowledgements", {})
    _require(all(request["request_id"] in acknowledgements.get(provider, [])
                 for request in requests for provider in request["providers"]),
             "Source has an unacknowledged control request")
    prior, prior_raw = _validate_prior_parser_chain(source, manifest)
    return manifest, identities, expected_ids, manifest_raw, state_raw, prior, prior_raw


def _sealed_suspend_call(source: Path, row: dict, condition: dict, attempt: dict,
                         manifest: dict, *, inherited: bool) -> dict:
    number = attempt.get("number")
    _require(type(number) is int and number in {1, 2} and attempt.get("status") == "completed",
             "Unresolved or invalid native attempt")
    call_id = f"{row['label']}/{row['task']}/attempt-{number}"
    logs = source / "private/logs" / call_id
    hashes = _tree(logs)
    _require(set(hashes) == LOG_FILES, "Native call logs are incomplete or contain unexpected files")
    process = json.loads(_plain(logs / "process.json"))
    audited = json.loads(_plain(logs / "audit.json"))
    _require(process.get("status") == "completed" and audited.get("status") == "completed"
             and all(attempt.get(key) == value for key, value in process.items())
             and all(_without_rate_flag(attempt).get(key) == value
                     for key, value in _without_rate_flag(audited).items())
             and _plain(logs / "prompt.txt") == manifest["prompt"].encode(),
             "Native attempt differs from its sealed receipts")
    for field in ("started_epoch_s", "finished_epoch_s", "wall_s", "epoch_elapsed_s"):
        value = attempt.get(field)
        _require(type(value) in {int, float} and math.isfinite(value) and value >= 0,
                 "Invalid native timing evidence")
    _require(attempt["finished_epoch_s"] >= attempt["started_epoch_s"], "Native timestamps are reversed")
    if not inherited:
        workspace = source / "private/workspaces" / call_id
        runtime_dir = source / "private/runtimes" / call_id
        _require(attempt.get("cwd") == str(workspace.resolve()), "Native workspace identity mismatch")
        command = runtime.provider_module(condition["provider"]).build_command(
            condition["model"], condition["effort"], runtime_dir, workspace)
        actual = attempt.get("command")
        _require(isinstance(actual, list) and len(actual) == len(command) + 3
                 and actual[:3] == ["/usr/bin/sandbox-exec", "-f", str(runtime_dir / "containment.sb")]
                 and Path(actual[3]).is_absolute()
                 and Path(actual[3]).name == Path(command[0]).name
                 and actual[4:] == command[1:], "Native command differs from the frozen launch policy")
    return {"call_id": call_id, "log_sha256": hashes, "inherited": inherited}


def _validate_suspend_target(source: Path, target: dict, manifest: dict,
                             condition: dict, call: dict) -> dict:
    row, record, raw = target["row"], target["record"], target["raw"]
    _require((row["label"], row["task"]) == SUSPEND_TARGET
             and row.get("sample") == 2 and condition == {
                 "id": "claude-sonnet5-max", "provider": "anthropic",
                 "model": "claude-sonnet-5", "effort": "max"},
             "Suspension target identity changed")
    _require(_sha(raw) == SUSPEND_RECORD_FILE_SHA
             and record.get("record_sha256") == SUSPEND_RECORD_SHA
             and record.get("status") == "blocked" and record.get("stop_reason") == "invalid_provider_audit"
             and record.get("eligible_for_grading") is False
             and record.get("artifact_present") is False and record.get("artifact_sha256") is None
             and record.get("outcome") is None and len(record.get("attempts", [])) == 1
             and not runner._artifact_path(source, row).exists(),
             "Blocked source record is not the declared no-artifact suspension case")
    attempt = record["attempts"][0]
    audit = record.get("provider_audit", {})
    _require(attempt.get("timed_out") is True and attempt.get("exit_code") is None
             and attempt.get("clock_discontinuity") is True
             and not attempt.get("launch_error") and not attempt.get("cleanup_error")
             and attempt.get("artifact_present") is False and attempt.get("artifact_sha256") is None
             and attempt.get("provider_audit") == audit
             and audit.get("valid") is False and audit.get("errors") == [SUSPEND_REASON]
             and audit.get("provider_refusal") is False and audit.get("completion_successful") is False
             and audit.get("completion_status") == "incomplete_or_error"
             and audit.get("resolved_model") == condition["model"]
             and runner._effort_matches(audit, condition),
             "Suspension attempt has conflicting provider or process evidence")
    active = attempt["wall_s"]
    epoch = attempt["finished_epoch_s"] - attempt["started_epoch_s"]
    _require(manifest["author_timeout_s"] <= active <= manifest["author_timeout_s"] + 5
             and record.get("wall_s") == active
             and attempt.get("epoch_elapsed_s") == round(epoch, 3)
             and abs(epoch - active) > 2 and runtime.clock_discontinuity(epoch, active),
             "Suspension timing does not independently prove a clock discontinuity")
    subscription = record.get("subscription_precheck", {})
    history = record.get("subscription_prechecks")
    guard = record.get("subscription_guard", {})
    rate_events = audit.get("rate_limit", {}).get("events", [])
    _require(subscription.get("allowed") is True and subscription.get("paid_usage_enabled") is False
             and isinstance(history, list) and len(history) == 1
             and isinstance(history[0], dict) and set(history[0]) == {"checked_at", "result"}
             and isinstance(history[0].get("checked_at"), str)
             and history[0].get("result") == subscription
             and attempt.get("subscription_precheck") == subscription
             and guard == {"stop": False, "reason": None, "rate_limited": False}
             and audit.get("rate_limited") is False
             and audit.get("rate_limit", {}).get("using_overage") is False
             and isinstance(rate_events, list) and bool(rate_events)
             and all(event.get("status") == "allowed" and event.get("isUsingOverage") is False
                     for event in rate_events),
             "Suspension attempt contains quota or paid-usage evidence")
    logs = source / "private/logs" / call["call_id"]
    reconstructed = claude_adapter.inspect_trace(logs / "stdout.jsonl", logs / "stderr.log",
                                                  condition["model"], condition["effort"],
                                                  allow_incomplete=True)
    _require(reconstructed.get("valid") is True and not reconstructed.get("errors")
             and reconstructed.get("completion_status") == "incomplete_or_error"
             and reconstructed.get("provider_refusal") is False,
             "Native trace has an unresolved error independent of suspension")
    reconstructed["errors"].append(SUSPEND_REASON)
    reconstructed["valid"] = False
    raw_audit = json.loads(_plain(logs / "audit.json"))["provider_audit"]
    _require(reconstructed == raw_audit, "Suspension audit is not reproducible from the native trace")
    workspace = source / "private/workspaces" / call["call_id"]
    _require(runner.matrix._artifact_hash(workspace) is None and not (workspace / "main.py").exists(),
             "Suspension workspace contains a submission")
    with tempfile.TemporaryDirectory(prefix=".expected-suspend-task-", dir=source.parent) as temporary:
        expected = Path(temporary) / "workspace"
        runner._stage(expected, row["task"])
        _require(_tree(workspace) == _tree(expected), "Suspension workspace differs from staged task inputs")
    return {"label": row["label"], "condition": row["condition"], "sample": row["sample"],
            "task": row["task"], "call_id": call["call_id"], "reason": SUSPEND_REASON,
            "source_record_sha256": record["record_sha256"],
            "source_record_file_sha256": _sha(raw), "active_elapsed_s": active,
            "epoch_elapsed_s": attempt["epoch_elapsed_s"],
            "elapsed_discontinuity_s": round(epoch - active, 3),
            "workspace_sha256": _map_sha256(_tree(workspace)), "non_graded_evidence": True}


def _suspend_records(source: Path, manifest: dict, identities: dict,
                     conditions: dict, prior: dict) -> tuple[list[dict], dict, list[dict]]:
    records, blocked, calls = [], [], {}
    inherited_paths = list((source / "private/amendment/source-authoring").glob("*/*.json"))
    inherited_keys = {(path.parent.name, path.stem) for path in inherited_paths}
    inherited_calls = sorted(
        f"{path.parent.name}/{path.stem}/attempt-{attempt['number']}"
        for path in inherited_paths for attempt in json.loads(_plain(path)).get("attempts", []))
    _require(len(inherited_calls) == prior.get("native_calls_preserved")
             and _sha(json.dumps(inherited_calls, separators=(",", ":")).encode()) ==
             prior.get("native_call_identities_sha256"),
             "Prior author snapshot coverage changed")
    for path in sorted((source / "authoring").glob("*/*.json")):
        key = path.parent.name, path.stem
        _require(key in identities, "Unexpected author record identity")
        row, raw = identities[key], _plain(path)
        record = json.loads(raw)
        try:
            runner._validate_record(source, row, record)
        except (KeyError, TypeError, ValueError) as exc:
            raise MigrationInvalid(f"Unresolved or invalid author record: {key[0]}/{key[1]}") from exc
        attempts = record.get("attempts", [])
        _require(record.get("status") not in {"running", "retry_pending"}
                 and isinstance(attempts, list)
                 and [attempt.get("number") for attempt in attempts] == list(range(1, len(attempts) + 1)),
                 "Invalid author attempt coverage")
        if record.get("status") == "blocked":
            blocked.append({"row": row, "raw": raw, "record": record})
        elif record.get("status") == "deferred":
            _require(not attempts and not runner._artifact_path(source, row).exists(),
                     "Deferred author has execution evidence")
        else:
            reclassified = (record.get("outcome") == "provider_refusal"
                            and record.get("migration", {}).get("outcome_reclassified") is True
                            and record.get("provider_audit", {}).get("valid") is True
                            and record.get("provider_audit", {}).get("provider_refusal") is True)
            _require(record.get("status") == "completed" and attempts
                     and (record.get("provider_audit") == attempts[-1].get("provider_audit") or reclassified)
                     and record.get("wall_s") == sum(attempt.get("wall_s", 0) for attempt in attempts),
                     "Completed author evidence is inconsistent")
        migration_ids = record.get("migration", {}).get("call_ids", [])
        _require(not migration_ids or migration_ids == [
            f"{row['label']}/{row['task']}/attempt-{attempt['number']}" for attempt in attempts],
            "Imported call lineage differs from attempt coverage")
        inherited = key in inherited_keys
        if inherited:
            archived = _plain(source / "private/amendment/source-authoring" /
                              row["label"] / f"{row['task']}.json")
            if key == PARSER_TARGET:
                item = prior.get("reclassified_record", {})
                _require(_sha(archived) == item.get("source_record_file_sha256")
                         and record.get("record_sha256") == item.get("target_record_sha256"),
                         "Reclassified prior record changed")
            else:
                _require(archived == raw, "Ordinary prior record changed after parser migration")
        condition = conditions[row["condition"]]
        for attempt in attempts:
            item = _sealed_suspend_call(source, row, condition, attempt, manifest,
                                        inherited=inherited)
            _require(item["call_id"] not in calls, "Duplicate native call identity")
            calls[item["call_id"]] = item
        records.append({"row": row, "raw": raw, "record": record})
    statuses = {name: sum(item["record"]["status"] == name for item in records)
                for name in ("completed", "blocked", "deferred")}
    _require(len(records) == SUSPEND_COUNTS["records"] and statuses == {
                 key: SUSPEND_COUNTS[key] for key in ("completed", "blocked", "deferred")}
             and len(calls) == SUSPEND_COUNTS["calls"] and len(blocked) == 1
             and (blocked[0]["row"]["label"], blocked[0]["row"]["task"]) == SUSPEND_TARGET,
             "Author and call coverage differs from the declared stopped run")
    actual_calls = {path.relative_to(source / "private/logs").as_posix()
                    for path in (source / "private/logs").glob("*/*/*")}
    _require(actual_calls == set(calls), "Unrecorded or missing native calls exist")
    inherited = sorted(call_id for call_id, item in calls.items() if item["inherited"])
    _require(inherited == inherited_calls,
             "Prior native-call identity chain changed")
    return records, blocked[0], list(calls.values())


def _migrate_suspend_locked(source: Path, out: Path, base_commit: str) -> dict:
    source_inventory = _source_inventory(source)
    summary = {key: value for key, value in source_inventory.items() if key != "items"}
    _require(summary == SUSPEND_INVENTORY, "Stopped source inventory differs from the declared immutable baseline")
    proof = _suspend_source_proof(runtime.REPO, base_commit)
    manifest, identities, expected, manifest_raw, state_raw, prior, prior_raw = \
        _validate_suspend_plan(source, proof)
    conditions = {item["id"]: item for item in manifest["conditions"]}
    records, target, calls = _suspend_records(source, manifest, identities, conditions, prior)
    target_call = next(item for item in calls if item["call_id"] ==
                       f"{SUSPEND_TARGET[0]}/{SUSPEND_TARGET[1]}/attempt-1")
    target_receipt = _validate_suspend_target(
        source, target, manifest, conditions[target["row"]["condition"]], target_call)
    present = {(item["row"]["label"], item["row"]["task"]) for item in records}
    absent = set(identities) - present
    _require(len(absent) == 528 - SUSPEND_COUNTS["records"], "Unexpected uninvoked author coverage")
    for label, task in absent:
        _require(not (source / "private/artifacts" / label / task).exists()
                 and not (source / "private/logs" / label / task).exists()
                 and not (source / "private/workspaces" / label / task).exists(),
                 "Never-invoked row has execution evidence")
    run_rows, grading_logs = _parser_runs(source, expected, records)
    _require(sum(row["kind"] == "grade" for row in run_rows) == SUSPEND_COUNTS["grades"]
             and sum(row["kind"] == "failure" for row in run_rows) == SUSPEND_COUNTS["failures"]
             and len(expected) - len(run_rows) == SUSPEND_COUNTS["missing"],
             "Stopped run accounting differs from the declared baseline")
    target_keys = {(target["row"]["label"], target["row"]["task"], seed)
                   for seed in manifest["seeds"]}
    _require(not target_keys.intersection({(row["label"], row["task"], row["seed"])
                                           for row in run_rows}),
             "Suspension target already has run evidence")
    artifact_maps = {}
    for item in records:
        row, record = item["row"], item["record"]
        artifact = runner._artifact_path(source, row)
        if record["status"] == "completed":
            artifact_maps[(row["label"], row["task"])] = _tree(artifact)
        else:
            _require(not artifact.exists(), "Noncompleted source record has a frozen artifact")
    actual_artifacts = {(path.parent.name, path.name)
                        for path in (source / "private/artifacts").glob("*/*")}
    _require(len(actual_artifacts) == SUSPEND_COUNTS["artifacts"]
             and actual_artifacts == set(artifact_maps), "Frozen artifact coverage is inconsistent")
    source_logs = _tree(source / "private/logs")
    source_artifacts = _tree(source / "private/artifacts")
    prior_amendment = _tree(source / "private/amendment")
    controls = _tree(source / "control") if (source / "control").exists() else {}
    workspace = source / "private/workspaces" / target_call["call_id"]
    workspace_tree = _tree(workspace)
    run_digest = _map_sha256({f"{row['label']}/{row['task']}_s{row['seed']}/{name}": sha
                              for row in run_rows for name, sha in row["files"].items()})
    implicit = [{"label": label, "task": task} for label, task in sorted(absent)]
    migration = {"schema_version": 1, "amendment_id": SUSPEND_AMENDMENT_ID,
                 "source_plan_sha256": manifest["plan_sha256"],
                 "source_manifest_file_sha256": _sha(manifest_raw),
                 "source_migration_receipt_sha256": _sha(prior_raw)}
    revised = copy.deepcopy(manifest)
    revised.update(benchmark_commit=proof["current_commit"], migration=migration)
    revised.pop("plan_sha256")
    revised["plan_sha256"] = runner._digest(revised)
    unchanged = sorted(set(manifest) - {"benchmark_commit", "migration", "plan_sha256"})
    _require(all(revised[field] == manifest[field] for field in unchanged),
             "An undeclared plan field changed")
    receipt = {"schema_version": 1, "amendment_id": SUSPEND_AMENDMENT_ID,
               "migration_complete": True, "created_at": datetime.now(timezone.utc).isoformat(), **migration,
               "target_plan_sha256": revised["plan_sha256"], "source_proof": proof,
               "unchanged_plan_fields": unchanged,
               "source_orchestration_file_sha256": _sha(state_raw),
               "source_inventory": summary,
               "prior_amendment_tree_sha256": _map_sha256(prior_amendment),
               "source_control_tree_sha256": _map_sha256(controls),
               "source_native_logs_sha256": _map_sha256(source_logs),
               "source_artifacts_sha256": _map_sha256(source_artifacts),
               "preserved_runs_sha256": run_digest,
               "grading_logs_sha256": _map_sha256(grading_logs),
               "schedule": {"planned_authors": len(identities), "planned_runs": len(expected),
                            "completed_records_preserved": SUSPEND_COUNTS["completed"],
                            "blocked_records_preserved": SUSPEND_COUNTS["blocked"],
                            "ordinary_records_byte_preserved": len(records) - 1,
                            "target_record_byte_preserved": True,
                            "implicit_deferred_records": len(absent),
                            "implicit_deferred_identities_sha256": _sha(json.dumps(
                                implicit, sort_keys=True, separators=(",", ":")).encode()),
                            "graded_runs_preserved": SUSPEND_COUNTS["grades"],
                            "failure_runs_preserved": SUSPEND_COUNTS["failures"],
                            "derived_failure_records_created": len(manifest["seeds"]),
                            "missing_runs_after_migration": SUSPEND_COUNTS["missing"] - len(manifest["seeds"])},
               "operational_failure": target_receipt,
               "native_calls_preserved": len(calls),
               "native_call_identities_sha256": _sha(json.dumps(
                   sorted(item["call_id"] for item in calls), separators=(",", ":")).encode()),
               "provider_calls_performed": 0, "subscription_checks_performed": 0,
               "grading_calls_performed": 0, "credentials_copied": False,
               "prior_controls_replayed": False, "destination_providers_initially_paused": True}
    with tempfile.TemporaryDirectory(prefix=".suspend-migration-", dir=out.parent) as temporary:
        staging = Path(temporary)
        staging.chmod(0o700)
        archive = staging / "private/amendment"
        archive.mkdir(parents=True)
        (archive / "source-manifest.json").write_bytes(manifest_raw)
        (archive / "source-orchestration.json").write_bytes(state_raw)
        (archive / "source-migration-receipt.json").write_bytes(prior_raw)
        _copy_tree(source / "private/amendment", archive / "prior-amendment", prior_amendment)
        if controls:
            _copy_tree(source / "control", archive / "source-control", controls)
        for item in records:
            row = item["row"]
            archived = archive / "source-authoring" / row["label"] / f"{row['task']}.json"
            archived.parent.mkdir(parents=True, exist_ok=True)
            archived.write_bytes(item["raw"])
            destination = runner._record_path(staging, row)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(item["raw"])
            if item["record"]["status"] == "completed":
                _copy_tree(runner._artifact_path(source, row), runner._artifact_path(staging, row),
                           artifact_maps[(row["label"], row["task"])])
        _copy_tree(workspace, archive / "non-graded-target-workspace", workspace_tree)
        _copy_tree(source / "private/logs", staging / "private/logs", source_logs)
        if grading_logs:
            _copy_tree(source / "grading_logs", staging / "grading_logs", grading_logs)
        for row in run_rows:
            destination = staging / row["label"] / f"{row['task']}_s{row['seed']}"
            _copy_tree(row["directory"], destination, row["files"])
        for seed in manifest["seeds"]:
            failure = {"label": target["row"]["label"], "task": target["row"]["task"],
                       "seed": seed, "stage": "authoring", "reason": SUSPEND_REASON}
            runtime.write_json(staging / target["row"]["label"] /
                               f"{target['row']['task']}_s{seed}" / "failure.json", failure)
        _require(_source_inventory(source) == source_inventory,
                 "Source evidence changed during suspension migration")
        _require(_suspend_source_proof(runtime.REPO, base_commit) == proof,
                 "Committed sources changed during suspension migration")
        runtime.write_json(staging / "migration_receipt.json", receipt)
        runtime.write_json(staging / "orchestration.json", {
            "schema_version": 1, "plan_sha256": revised["plan_sha256"],
            "providers": {provider: {"status": "paused", "reason": "migration_requires_explicit_resume"}
                          for provider in sorted({condition["provider"] for condition in conditions.values()})},
            "control_acknowledgements": {provider: [] for provider in
                                          sorted({condition["provider"] for condition in conditions.values()})}})
        runtime.write_json(staging / "evaluation_manifest.json", revised)
        runner._verify_plan(staging)
        for item in records:
            runner._validate_record(staging, item["row"], item["record"])
        _require(sum(1 for seed in manifest["seeds"]
                     if (staging / target["row"]["label"] /
                         f"{target['row']['task']}_s{seed}" / "failure.json").is_file()) == len(manifest["seeds"]),
                 "Derived operational failures are incomplete")
        staging.rename(out)
    return receipt


def migrate_suspend_amendment(source: Path, out: Path, *, base_commit: str = SUSPEND_BASE) -> dict:
    """Account for one sealed host discontinuity without model or grader calls."""
    _require(not any(path.is_symlink() for path in (source, *source.parents, out, *out.parents)),
             "Symlinked experiment roots are not supported")
    source, out = source.resolve(), out.resolve()
    _require(source.is_dir() and out.parent.is_dir() and not out.exists(),
             "A fresh destination and existing source are required")
    _require(not source.is_relative_to(out) and not out.is_relative_to(source)
             and not out.is_relative_to(runtime.REPO.resolve()),
             "Experiment roots must be disjoint and outside the repository")
    lock = source / ".runner.lock"
    _plain(lock)
    with lock.open("rb") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MigrationInvalid("Source evaluator is still running") from exc
        return _migrate_suspend_locked(source, out, base_commit)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--base-commit")
    parser.add_argument("--amendment", choices=("initial", "repeated-refusal-parser", "host-suspend"),
                        default="initial")
    args = parser.parse_args()
    try:
        if args.amendment == "host-suspend":
            receipt = migrate_suspend_amendment(args.source, args.out,
                                                base_commit=args.base_commit or SUSPEND_BASE)
        elif args.amendment == "repeated-refusal-parser":
            receipt = migrate_parser_amendment(args.source, args.out,
                                               base_commit=args.base_commit or PARSER_BASE)
        else:
            receipt = migrate_authoring(args.source, args.out,
                                        base_commit=args.base_commit or DEFAULT_BASE)
    except (ValueError, OSError, KeyError, TypeError, SyntaxError) as exc:
        print(json.dumps({"migration_complete": False, "error": str(exc) if isinstance(exc, MigrationInvalid)
                          else "Evidence validation failed; inspect the private source without modifying it"}))
        return 1
    print(json.dumps({"migration_complete": True,
                      "imported_records": len(receipt.get("imported_records", [])),
                      "preserved_calls": len(receipt.get("preserved_calls", [])),
                      "native_calls_preserved": receipt.get("native_calls_preserved"),
                      "target_plan_sha256": receipt["target_plan_sha256"], "providers_paused": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
