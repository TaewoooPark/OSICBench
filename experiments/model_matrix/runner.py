"""Frozen, resumable subscription-only authoring and independent grading.

The provider clients run only through runtime.execute's external containment.
Private runtime credentials, workspaces, and traces must never be published.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import fcntl
import hashlib
import json
import math
import platform
import random
import re
import shutil
import subprocess
import sys
import threading
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import yaml

from adapters import matrix_runner as matrix
from osicbench.report import validate_evaluation_manifest
from . import runtime


def _read(path: Path) -> dict:
    return json.loads(path.read_text())


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _commit() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=runtime.REPO,
                                   text=True).strip()


def _environment() -> dict:
    versions = {}
    for provider, command in (("openai", "codex"), ("anthropic", "claude")):
        versions[provider] = subprocess.check_output([command, "--version"],
                                                     text=True, timeout=15).strip()
    return {"cli_versions": versions, "os": platform.platform(),
            "python": sys.version, "author_workers_per_provider": 1,
            "grading_workers": 2, "grading_timeout_s": matrix.GRADE_TIMEOUT_S}


def _cases(payload: dict) -> dict:
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported cases schema")
    samples, seeds = payload.get("samples"), payload.get("seeds")
    if type(samples) is not int or samples < 1:
        raise ValueError("samples must be a positive integer")
    if not isinstance(seeds, list) or not seeds or any(type(s) is not int for s in seeds):
        raise ValueError("seeds must be a nonempty integer list")
    if len(set(seeds)) != len(seeds) or type(payload.get("schedule_seed")) is not int:
        raise ValueError("duplicate seeds or invalid schedule seed")
    conditions = payload.get("conditions")
    if not isinstance(conditions, list) or not conditions:
        raise ValueError("conditions must be nonempty")
    seen = set()
    for condition in conditions:
        identity = condition.get("id", "")
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", identity)
                or identity in {"authoring", "private", "grading_logs"}
                or identity in seen or condition.get("provider") not in runtime.PROVIDERS
                or any(not isinstance(condition.get(k), str) or not condition[k]
                       for k in ("model", "effort"))):
            raise ValueError("invalid or duplicate condition")
        seen.add(identity)
    contrast_ids = set()
    for contrast in payload.get("contrasts", []):
        if (not isinstance(contrast.get("id"), str) or contrast["id"] in contrast_ids
                or contrast.get("a") not in seen or contrast.get("b") not in seen
                or contrast["a"] == contrast["b"]):
            raise ValueError("invalid contrast")
        contrast_ids.add(contrast["id"])
    return payload


def _matching_preflight(root: Path, condition: dict) -> dict:
    matches = []
    for path in sorted((root / "preflight" / condition["id"]).glob("*/preflight.json")):
        record = _read(path)
        audit, process = record.get("provider_audit", {}), record.get("process", {})
        probe = record.get("probe", {})
        if (record.get("condition") == condition and record.get("passed") is True
                and record.get("source_exact") is True
                and record.get("effective_effort_checked") is True
                and probe.get("parent_read_blocked") is True
                and probe.get("network_blocked") is True
                and process.get("exit_code") == 0 and not process.get("timed_out")
                and not process.get("cleanup_error") and audit.get("valid") is True
                and not audit.get("errors")
                and audit.get("resolved_model") == condition["model"]
                and _effort_matches(audit, condition)
                and (condition["provider"] != "anthropic" or probe.get("effort") == condition["effort"])):
            matches.append({"record": record, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                            "attempt": path.parent.name})
    if not matches:
        raise ValueError(f"no passed identity-verified preflight for {condition['id']}")
    return matches[-1]


def _effort_matches(audit: dict, condition: dict) -> bool:
    if condition["provider"] == "openai":
        return audit.get("resolved_effort") == condition["effort"]
    # Claude does not usually report effective effort in stream-json. The
    # frozen smoke check verifies its tool environment, not a server snapshot.
    observed = audit.get("observed_efforts", [])
    if observed:
        return observed == [condition["effort"]]
    return (audit.get("requested_effort") == condition["effort"]
            and audit.get("effort_verification") == "launch_configuration_only")


def plan_experiment(out: Path, cases_path: Path, preflight_root: Path,
                    timeout_s: float = matrix.AUTHOR_TIMEOUT_S) -> dict:
    out = out.resolve()
    if out.is_relative_to(runtime.REPO.resolve()):
        raise ValueError("private scoring output must be outside the benchmark repository")
    if out.exists():
        raise ValueError("scoring output already exists; planning requires a new root")
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError("timeout must be positive and finite")
    cases = _cases(_read(cases_path))
    task_ids = [path.name for path in matrix.tasks(runtime.REPO / "tasks")]
    if not task_ids:
        raise ValueError("no benchmark tasks")
    preflight = {c["id"]: _matching_preflight(preflight_root.resolve(), c)
                 for c in cases["conditions"]}
    schedule, expected, rng = [], [], random.Random(cases["schedule_seed"])
    block = 0
    for sample in range(1, cases["samples"] + 1):
        ordered_tasks = task_ids.copy()
        rng.shuffle(ordered_tasks)
        for task in ordered_tasks:
            conditions = cases["conditions"]
            offset = block % len(conditions)
            for condition in conditions[offset:] + conditions[:offset]:
                label = condition["id"] + (f"@s{sample}" if sample > 1 else "")
                row = {"condition": condition["id"], "label": label,
                       "task": task, "sample": sample}
                schedule.append(dict(row, ordinal=len(schedule), provider=condition["provider"]))
                expected.extend(dict(row, seed=seed) for seed in cases["seeds"])
            block += 1
    manifest = dict(cases, expected_runs=expected, schedule=schedule, tasks=task_ids,
                    prompt=matrix.PROMPT, prompt_sha256=hashlib.sha256(matrix.PROMPT.encode()).hexdigest(),
                    source_sha256=runtime.source_hash(), benchmark_commit=_commit(),
                    benchmark_sha256=matrix._benchmark_hash(),
                    cases_sha256=hashlib.sha256(cases_path.read_bytes()).hexdigest(),
                    preflight=preflight, author_timeout_s=timeout_s, environment=_environment(),
                    transport_policy_sha256=hashlib.sha256(
                        Path(runtime.__file__).with_name("transport.py").read_bytes()).hexdigest())
    validate_evaluation_manifest(manifest)
    manifest["plan_sha256"] = _digest(manifest)
    out.mkdir(parents=True, exist_ok=False)
    out.chmod(0o700)
    runtime.write_json(out / "evaluation_manifest.json", manifest)
    runtime.write_json(out / "orchestration.json", {
        "schema_version": 1, "plan_sha256": manifest["plan_sha256"],
        "providers": {c["provider"]: {"status": "ready"} for c in cases["conditions"]}})
    return manifest


def _sealed_plan(out: Path) -> dict:
    manifest = _read(out / "evaluation_manifest.json")
    unsigned = {k: v for k, v in manifest.items() if k != "plan_sha256"}
    if manifest.get("plan_sha256") != _digest(unsigned):
        raise ValueError("frozen plan has changed")
    validate_evaluation_manifest(manifest)
    return manifest


def _verify_plan(out: Path) -> dict:
    manifest = _sealed_plan(out)
    if (manifest["source_sha256"] != runtime.source_hash()
            or manifest["benchmark_sha256"] != matrix._benchmark_hash()
            or manifest["benchmark_commit"] != _commit()):
        raise ValueError("benchmark or experiment implementation changed after planning")
    return manifest


def _stop_requests(out: Path, manifest: dict) -> list[dict]:
    """Read data-only control receipts; malformed requests fail closed."""
    providers = {condition["provider"] for condition in manifest["conditions"]}
    directory = out / "control" / "stop-requests"
    if directory.is_symlink() or directory.parent.is_symlink():
        raise ValueError("control request directories must not be symlinks")
    requests = []
    for path in sorted(directory.glob("*.json")):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
            raise ValueError("invalid stop request file")
        request = _read(path)
        fields = {"schema_version", "plan_sha256", "request_id", "requested_at", "action", "providers"}
        if not isinstance(request, dict) or set(request) != fields:
            raise ValueError("invalid stop request schema")
        targets = request["providers"]
        try:
            identifier = str(uuid.UUID(request["request_id"]))
            timestamp = datetime.fromisoformat(request["requested_at"])
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("invalid stop request identity or timestamp") from exc
        if (type(request["schema_version"]) is not int or request["schema_version"] != 1
                or request["plan_sha256"] != manifest["plan_sha256"] or request["action"] != "pause"
                or path.stem != identifier or request["request_id"] != identifier
                or timestamp.tzinfo is None or not isinstance(targets, list) or not targets
                or any(not isinstance(target, str) or target not in providers for target in targets)
                or len(set(targets)) != len(targets)):
            raise ValueError("stop request does not match the frozen experiment")
        requests.append(request)
    return requests


def request_stop(out: Path, providers: tuple[str, ...] = ()) -> dict:
    """Ask selected workers to pause after their active calls, without signals."""
    out = out.resolve()
    manifest = _sealed_plan(out)
    known = {condition["provider"] for condition in manifest["conditions"]}
    selected = sorted(set(providers) if providers else known)
    if not set(selected) <= known:
        raise ValueError("unknown provider requested for pause")
    _stop_requests(out, manifest)
    request = {"schema_version": 1, "plan_sha256": manifest["plan_sha256"],
               "request_id": str(uuid.uuid4()), "requested_at": datetime.now(timezone.utc).isoformat(),
               "action": "pause", "providers": selected}
    runtime.write_json(out / "control" / "stop-requests" / f"{request['request_id']}.json", request)
    return request


def _record_path(out: Path, row: dict) -> Path:
    return out / "authoring" / row["label"] / f"{row['task']}.json"


def _artifact_path(out: Path, row: dict) -> Path:
    return out / "private" / "artifacts" / row["label"] / row["task"]


def _stage(workspace: Path, task: str):
    source = runtime.REPO / "tasks" / task
    (workspace / "manuals").mkdir(parents=True, exist_ok=False)
    shutil.copy2(source / "brief.md", workspace / "brief.md")
    config = yaml.safe_load((source / "task.yaml").read_text())
    for manual in config.get("manuals", []):
        target = workspace / "manuals" / manual
        if not target.resolve().is_relative_to((workspace / "manuals").resolve()):
            raise ValueError("manual path escapes task workspace")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(runtime.REPO / "manuals" / manual, target)
    if (source / "rig").exists():
        shutil.copytree(source / "rig", workspace / "rig")


def _freeze(workspace: Path, target: Path, expected_hash: str | None) -> str | None:
    actual = matrix._artifact_hash(workspace)
    if actual != expected_hash:
        raise ValueError("artifact changed between runtime audit and freezing")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(workspace, target)
    if matrix._artifact_hash(target) != actual or matrix._artifact_hash(workspace) != actual:
        raise ValueError("artifact changed while freezing")
    return actual


def _validate_record(out: Path, row: dict, record: dict):
    if any(record.get(key) != row[key] for key in ("label", "condition", "sample", "task")):
        raise ValueError("authoring identity mismatch")
    if record.get("status") in {"running", "retry_pending"}:
        raise ValueError("unresolved running authoring attempt; inspect the retained process before resuming")
    if record.get("status") not in {"completed", "blocked", "deferred"}:
        raise ValueError("unrecognized authoring status")
    if record.get("record_sha256") != _digest({k: v for k, v in record.items() if k != "record_sha256"}):
        raise ValueError("frozen authoring record changed")
    if record.get("status") == "deferred" and (record.get("attempts") or record.get("artifact_present")
                                                or record.get("artifact_sha256") is not None):
        raise ValueError("deferred precheck record must not contain a provider attempt or artifact")
    artifact = _artifact_path(out, row)
    if artifact.exists() and matrix._artifact_hash(artifact) != record.get("artifact_sha256"):
        raise ValueError("frozen authoring artifact changed")
    if record.get("status") == "completed":
        if not artifact.is_dir() or matrix._artifact_hash(artifact) != record.get("artifact_sha256"):
            raise ValueError("frozen authoring artifact is missing or changed")
        if record.get("eligible_for_grading") is not True:
            raise ValueError("completed author record is not eligible for grading")


def _save_record(path: Path, record: dict):
    record.pop("record_sha256", None)
    record["record_sha256"] = _digest(record)
    runtime.write_json(path, record)


def _subscription_check(condition: dict) -> dict:
    from .subscription_guard import check_subscription
    return check_subscription(condition["provider"], condition["model"])


def _guard(result: dict) -> dict:
    from .subscription_guard import runtime_stop_reason
    reason = runtime_stop_reason(result)
    return {"stop": reason is not None, "reason": reason,
            "rate_limited": reason in {"quota_exhausted", "quota_headroom_low"}}


def _completed_before_exhaustion(result: dict, condition: dict) -> bool:
    """Post-generation usage telemetry does not invalidate a completed response."""
    audit = result.get("provider_audit", {})
    completed = (audit.get("trace", {}).get("completed") if condition["provider"] == "openai"
                 else audit.get("completion_successful"))
    if (completed is not True or result.get("exit_code") != 0 or result.get("timed_out")
            or not result.get("artifact_present")):
        return False
    numeric_exhaustion, rejected = False, False

    def visit(value):
        nonlocal numeric_exhaustion, rejected
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            rejected |= (value.get("detected") is True or value.get("limit_reached") is True
                         or value.get("allowed") is False or value.get("status") in {"rejected", "blocked"})
            for key, limit in (("used_percent", 100), ("usedPercent", 100), ("utilization", 1)):
                number = value.get(key)
                if type(number) in (int, float) and math.isfinite(number) and number >= limit:
                    numeric_exhaustion = True
            for item in value.values():
                if isinstance(item, (dict, list)):
                    visit(item)

    visit(audit.get("rate_limit", {}))
    return numeric_exhaustion and not rejected


def _stop_reason(result: dict, condition: dict, guard: dict) -> str | None:
    if result.get("cleanup_error"):
        return "cleanup_failure"
    soft_stop = (guard.get("reason") == "quota_headroom_low"
                 or (guard.get("reason") == "quota_exhausted"
                     and _completed_before_exhaustion(result, condition)))
    if guard.get("stop") and not soft_stop:
        return guard.get("reason", "subscription_guard")
    if result.get("launch_error"):
        return "launch_failure"
    audit = result.get("provider_audit", {})
    if (audit.get("valid") is not True or audit.get("errors")
            or audit.get("resolved_model") != condition["model"]
            or not _effort_matches(audit, condition)):
        return "invalid_provider_audit"
    if audit.get("provider_refusal") is True:
        if result.get("artifact_present"):
            return "refusal_artifact_conflict"
        if guard.get("stop"):
            return guard.get("reason", "subscription_guard")
        # Native Claude refusals can exit 1 despite a complete, audited terminal
        # refusal. This is a nonpass outcome, never a successful answer.
        if (type(result.get("exit_code")) is not int or result["exit_code"] not in {0, 1}
                or result.get("timed_out")):
            return "invalid_provider_refusal"
        return None
    if result.get("artifact_present") or result.get("timed_out"):
        return None
    if result.get("exit_code") != 0:
        return "author_process_failure"
    return None


def run_experiment(out: Path, resume_providers: tuple[str, ...] = (),
                   grade_completed: bool = True) -> dict:
    """Serialize evaluator launches; interrupted author records need manual review."""
    out = out.resolve()
    with (out / ".runner.lock").open("a") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("another evaluator is already running this plan") from exc
        return _run_experiment(out, resume_providers, grade_completed)


def _run_experiment(out: Path, resume_providers: tuple[str, ...], grade_completed: bool) -> dict:
    out = out.resolve()
    manifest = _verify_plan(out)
    if _environment() != manifest["environment"]:
        raise ValueError("CLI or evaluator environment changed after planning")
    state = _read(out / "orchestration.json")
    if state.get("plan_sha256") != manifest["plan_sha256"]:
        raise ValueError("orchestration state does not match the frozen plan")
    conditions = {c["id"]: c for c in manifest["conditions"]}
    requests = _stop_requests(out, manifest)
    acknowledged = state.setdefault("control_acknowledgements", {})
    for provider in state["providers"]:
        acknowledged.setdefault(provider, [])
    for row in manifest["schedule"]:
        path = _record_path(out, row)
        if path.exists():
            _validate_record(out, row, _read(path))
    for provider in resume_providers:
        if provider not in state["providers"]:
            raise ValueError("unknown provider requested for explicit resume")
        previous = state["providers"][provider]
        state["providers"][provider] = {"status": "ready", "explicitly_resumed_from": previous}
        acknowledged[provider] = sorted(set(acknowledged[provider]) |
                                        {r["request_id"] for r in requests if provider in r["providers"]})
    runtime.write_json(out / "orchestration.json", state)
    lock = threading.Lock()

    def stopped(provider):
        with lock:
            return state["providers"][provider]["status"] != "ready"

    def check_controls(provider):
        requests = _stop_requests(out, manifest)
        with lock:
            pending = [r["request_id"] for r in requests if provider in r["providers"]
                       and r["request_id"] not in acknowledged[provider]]
            if pending:
                acknowledged[provider].extend(pending)
                previous = state["providers"][provider]
                if previous["status"] == "ready":
                    state["providers"][provider] = {"status": "paused", "reason": "user_requested_pause",
                        "stop_requests": pending, "paused_at": datetime.now(timezone.utc).isoformat()}
                else:
                    previous["stop_requests"] = sorted(set(previous.get("stop_requests", [])) | set(pending))
                runtime.write_json(out / "orchestration.json", state)
            return state["providers"][provider]["status"] == "ready"

    def stop(provider, reason, row):
        with lock:
            state["providers"][provider] = {"status": "stopped", "reason": reason,
                                            "condition": row["condition"], "task": row["task"],
                                            "sample": row["sample"]}
            runtime.write_json(out / "orchestration.json", state)

    def grade(row, seed):
        try:
            artifact = _artifact_path(out, row)
            _validate_record(out, row, _read(_record_path(out, row)))
            result = matrix._grade_one(row["label"], artifact, row["task"], seed, out)
            _validate_record(out, row, _read(_record_path(out, row)))
            return result
        except Exception as exc:
            stop(row["provider"], "grading_integrity_failure", row)
            runtime.write_json(out / "private" / "grading_errors" / row["label"] /
                               f"{row['task']}_s{seed}.json", {"error_type": type(exc).__name__,
                                                              "error": str(exc)})
            return None

    def author(row):
        condition, path = conditions[row["condition"]], _record_path(out, row)
        previous = _read(path) if path.exists() else {}
        record = {k: row[k] for k in ("label", "condition", "sample", "task")}
        record.update(status="running", attempts=[], artifact_sha256=None,
                      artifact_present=False, eligible_for_grading=False,
                      subscription_prechecks=previous.get("subscription_prechecks", []))
        for number in (1, 2):
            _verify_plan(out)
            if not check_controls(row["provider"]):
                if record["attempts"]:
                    record.update(status="blocked", stop_reason="user_pause_after_launch_failure")
                    _save_record(path, record)
                return False
            base = Path(row["label"]) / row["task"] / f"attempt-{number}"
            workspace = out / "private" / "workspaces" / base
            runtime_dir = out / "private" / "runtimes" / base
            log_dir = out / "private" / "logs" / base
            record["status"] = "running"
            runtime.write_json(path, record)
            try:
                subscription = _subscription_check(condition)
                record["subscription_precheck"] = subscription
                record["subscription_prechecks"].append({"checked_at": datetime.now(timezone.utc).isoformat(),
                                                          "result": subscription})
                runtime.write_json(path, record)
                if subscription.get("allowed") is not True:
                    reason = subscription.get("reason", "subscription_precheck_failed")
                    record.update(status="deferred" if not record["attempts"] else "blocked", stop_reason=reason)
                    _save_record(path, record)
                    stop(row["provider"], reason, row)
                    return False
                if not check_controls(row["provider"]):
                    with lock:
                        reason = state["providers"][row["provider"]].get("reason", "provider_stopped")
                    record.update(status="blocked" if record["attempts"] else "deferred", stop_reason=reason)
                    _save_record(path, record)
                    return False
                _stage(workspace, row["task"])
                record["attempts"].append({"number": number, "status": "running",
                                            "subscription_precheck": subscription})
                runtime.write_json(path, record)
                result = runtime.execute(condition, workspace=workspace, runtime_dir=runtime_dir,
                                         log_dir=log_dir, evaluation_root=out,
                                         prompt=manifest["prompt"], timeout_s=manifest["author_timeout_s"])
                guard = _guard(result)
                audit = result.setdefault("provider_audit", {})
                audit["rate_limited"] = bool(guard.get("rate_limited"))
                record["provider_audit"] = audit
                record["subscription_guard"] = guard
                record["attempts"][-1].update(result, number=number, status="completed")
                record["wall_s"] = sum(a.get("wall_s", 0) for a in record["attempts"])
                present = (workspace / "main.py").is_file()
                if present != bool(result.get("artifact_present")):
                    raise ValueError("runtime artifact presence mismatch")
                record.update(artifact_present=present, artifact_sha256=result.get("artifact_sha256"))
                reason = _stop_reason(result, condition, guard)
                # Retry only a process that provably never launched, without a submission.
                retry_errors = {"model_unverified", "effort_unverified", "no_completed_turn"}
                if (reason == "launch_failure" and not present and number == 1
                        and set(audit.get("errors", [])) <= retry_errors):
                    record["status"] = "retry_pending"
                    runtime.write_json(path, record)
                    continue
                if present or reason is None:
                    _freeze(workspace, _artifact_path(out, row), result.get("artifact_sha256"))
                if reason:
                    record.update(status="blocked", stop_reason=reason)
                    _save_record(path, record)
                    stop(row["provider"], reason, row)
                    return False
                record.update(status="completed", eligible_for_grading=True)
                if audit.get("provider_refusal") is True:
                    record["outcome"] = "provider_refusal"
                _save_record(path, record)
                if guard.get("stop"):
                    stop(row["provider"], guard["reason"], row)
                return True
            except Exception as exc:
                if record["attempts"] and record["attempts"][-1]["number"] == number:
                    record["attempts"][-1].update(status="blocked", error_type=type(exc).__name__,
                                                   error=str(exc))
                else:
                    record["orchestration_error"] = {"error_type": type(exc).__name__, "error": str(exc)}
                record.update(status="blocked", stop_reason="orchestration_failure")
                _save_record(path, record)
                stop(row["provider"], "orchestration_failure", row)
                return False
        raise AssertionError("bounded attempt loop exhausted")

    authoring_open = True
    with cf.ThreadPoolExecutor(max_workers=2) as grading:
        for sample in range(1, manifest["samples"] + 1):
            futures = []

            def worker(provider):
                for row in manifest["schedule"]:
                    if row["sample"] != sample or row["provider"] != provider:
                        continue
                    check_controls(provider)
                    path = _record_path(out, row)
                    if path.exists():
                        record = _read(path)
                        eligible = record["status"] == "completed"
                        if (record["status"] == "deferred" and provider in resume_providers
                                and authoring_open and not stopped(provider)):
                            eligible = author(row)
                    elif not authoring_open or stopped(provider):
                        continue
                    else:
                        eligible = author(row)
                    if eligible and grade_completed:
                        for seed in manifest["seeds"]:
                            with lock:
                                futures.append(grading.submit(grade, row, seed))
                check_controls(provider)

            with cf.ThreadPoolExecutor(max_workers=len(state["providers"])) as authors:
                jobs = [authors.submit(worker, provider) for provider in state["providers"]]
                for job in jobs:
                    job.result()
            for future in futures:
                future.result()
            # Stop new authoring waves, but grade all previously frozen waves.
            if any(stopped(provider) for provider in state["providers"]):
                authoring_open = False
    return status(out)


def status(out: Path) -> dict:
    manifest = _read(out / "evaluation_manifest.json")
    counts = Counter()
    for row in manifest["schedule"]:
        path = _record_path(out, row)
        counts[_read(path).get("status", "unknown") if path.exists() else "deferred"] += 1
    grade_counts = Counter()
    for row in manifest["expected_runs"]:
        run = out / row["label"] / f"{row['task']}_s{row['seed']}"
        grade_counts["graded" if (run / "grade.json").exists() else
                     "failure" if (run / "failure.json").exists() else "missing"] += 1
    state = _read(out / "orchestration.json")
    requests = _stop_requests(out, manifest)
    acknowledged = state.get("control_acknowledgements", {})
    pending = {provider: [request["request_id"] for request in requests
                          if provider in request["providers"]
                          and request["request_id"] not in acknowledged.get(provider, [])]
               for provider in state["providers"]}
    return {"authoring": dict(counts), "grading": dict(grade_counts),
            "planned_authors": len(manifest["schedule"]),
            "planned_runs": len(manifest["expected_runs"]),
            "providers": state["providers"],
            "stop_requests": {"total": len(requests), "pending_by_provider": pending}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "author", "run", "status", "request-stop"),
                        help="author freezes programs without grading; run also grades frozen programs")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cases", type=Path, default=Path(__file__).with_name("cases.json"))
    parser.add_argument("--preflight-root", type=Path)
    parser.add_argument("--timeout", type=float, default=matrix.AUTHOR_TIMEOUT_S)
    parser.add_argument("--resume-provider", action="append", default=[])
    parser.add_argument("--provider", action="append", default=[],
                        help="request-stop target; repeat for multiple providers, default all")
    args = parser.parse_args()
    if args.command == "plan":
        if args.preflight_root is None:
            parser.error("plan requires --preflight-root")
        plan_experiment(args.out, args.cases, args.preflight_root, args.timeout)
        result = status(args.out)
    elif args.command in {"author", "run"}:
        result = run_experiment(args.out, tuple(args.resume_provider), grade_completed=args.command == "run")
    elif args.command == "request-stop":
        result = request_stop(args.out, tuple(args.provider))
    else:
        result = status(args.out)
    print(json.dumps(result, indent=2))
    if args.command in {"author", "run"}:
        return int(any(p["status"] != "ready" for p in result["providers"].values())
                   or (result["grading"].get("missing", 0) > 0 if args.command == "run"
                       else result["authoring"].get("completed", 0) != result["planned_authors"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
