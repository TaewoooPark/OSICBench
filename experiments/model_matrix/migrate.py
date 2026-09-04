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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--base-commit", default=DEFAULT_BASE)
    args = parser.parse_args()
    try:
        receipt = migrate_authoring(args.source, args.out, base_commit=args.base_commit)
    except (ValueError, OSError, KeyError, TypeError, SyntaxError) as exc:
        print(json.dumps({"migration_complete": False, "error": str(exc) if isinstance(exc, MigrationInvalid)
                          else "Evidence validation failed; inspect the private source without modifying it"}))
        return 1
    print(json.dumps({"migration_complete": True, "imported_records": len(receipt["imported_records"]),
                      "preserved_calls": len(receipt["preserved_calls"]),
                      "target_plan_sha256": receipt["target_plan_sha256"], "providers_paused": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
