"""Export a privacy-checked, byte-preserving public replay snapshot.

The exporter never imports or executes submissions. Invalid programs are valid
evidence. The source tree is an evaluator-owned frozen snapshot, not an active
author workspace. Unknown binary files require review; repository-provided rig
files may be copied only when their relative name and bytes match exactly.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path, PurePosixPath

from osicbench.report import validate_evaluation_manifest
from . import analyze


TEXT_SUFFIXES = {".py", ".json", ".jsonl", ".csv", ".tsv", ".txt", ".md",
                 ".yaml", ".yml", ".toml", ".ini", ".cfg", ".dat"}
SKIP_DIRS = {"__pycache__", ".git", ".claude", ".codex", ".agents"}
SKIP_NAMES = {"endpoints.json", "agent.out", "agent.err", "auth.json",
              ".credentials.json", "credentials.json"}
META_FIELDS = {"mode", "restarted", "exit_code", "killed_by_limit", "sigkilled", "wall_s"}
AUDIT_IDENTITIES = {"requested_model", "resolved_model", "requested_effort", "resolved_effort",
                    "effort_verification"}
AUDIT_LISTS = {"observed_primary_models", "primary_models", "auxiliary_models", "observed_efforts"}
HASH_FIELDS = {"benchmark_sha256", "prompt_sha256", "source_sha256", "cases_sha256",
               "plan_sha256", "transport_policy_sha256"}
SECRET = re.compile(
    r"(?:sk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}|"
    r"(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{16,}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|"
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"(?:authorization[\"']?\s*[:=]\s*[\"']?(?:bearer|basic)\s+\S+)|"
    r"[\"']?(?:access_token|refresh_token|api_key|client_secret)[\"']?\s*[:=]\s*[\"'][^\"']+[\"'])",
    re.IGNORECASE,
)
PRIVATE_PATH = re.compile(r"(?:/Users/|/home/|/root/|/private/|/var/folders/|[A-Za-z]:[\\/]Users[\\/]|~/)")


class ExportError(ValueError):
    """A rejection containing only a neutral reason and opaque path ID."""


def _reject(reason: str, identifier: str = "snapshot"):
    digest = hashlib.sha256(identifier.encode()).hexdigest()[:12]
    raise ExportError(f"{reason} [path-id:{digest}]") from None


def _identity(value: object) -> str:
    if not isinstance(value, str) or analyze.IDENTITY.fullmatch(value) is None:
        _reject("unsafe_identity")
    return value


def _relative(value: str) -> str:
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or "\\" in value or
            any(part in {"", ".", ".."} for part in value.split("/"))):
        _reject("unsafe_relative_path", value)
    _scan(value.encode(), value)
    return value


def _no_symlinks(path: Path):
    for item in (path, *path.parents):
        if item.is_symlink():
            _reject("symlink_rejected", item.name)


def _read(path: Path, identifier: str) -> bytes:
    _no_symlinks(path)
    try:
        with path.open("rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                _reject("nonregular_file", identifier)
            return stream.read()
    except (OSError, ValueError):
        _reject("unreadable_file", identifier)


def _json(data: bytes, identifier: str) -> dict:
    try:
        result = json.loads(data)
    except (ValueError, UnicodeError):
        _reject("invalid_json", identifier)
    if not isinstance(result, dict):
        _reject("invalid_json_object", identifier)
    return result


def _encoded(value: object) -> bytes:
    try:
        return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    except (ValueError, TypeError):
        _reject("invalid_public_value")


def _scan(data: bytes, identifier: str):
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        _reject("unknown_binary_requires_review", identifier)
    normalized = text.replace("\\/", "/")
    if SECRET.search(normalized) or PRIVATE_PATH.search(normalized) or "\x00" in text:
        _reject("private_content_rejected", identifier)
    # JSON escape sequences must not bypass checks of decoded string values.
    stack = []
    for candidate in [text, *text.splitlines()]:
        try:
            stack.append(json.loads(candidate))
        except ValueError:
            continue
    while stack:
        value = stack.pop()
        if isinstance(value, str) and (SECRET.search(value) or PRIVATE_PATH.search(value)):
            _reject("private_content_rejected", identifier)
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(key, str) and key.lower() in {
                        "access_token", "refresh_token", "api_key", "client_secret", "authorization"} and item:
                    _reject("private_content_rejected", identifier)
            stack.extend(value.keys())
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)


def _tree(root: Path) -> list[Path]:
    _no_symlinks(root)
    if not root.is_dir():
        _reject("missing_directory", root.name)
    paths = []
    for current, directories, filenames in os.walk(root, followlinks=False):
        for name in directories + filenames:
            path = Path(current) / name
            _no_symlinks(path)
            _relative(path.relative_to(root).as_posix())
        paths.extend(Path(current) / name for name in filenames)
    return sorted(paths)


def _artifact(root: Path) -> tuple[str, dict[str, bytes]]:
    files, digest = {}, hashlib.sha256()
    for path in _tree(root):
        relative = path.relative_to(root).as_posix()
        data = _read(path, relative)
        digest.update(relative.encode() + b"\0")
        digest.update(hashlib.sha256(data).digest())
        files[relative] = data
    if "main.py" not in files:
        _reject("missing_original_submission")
    return digest.hexdigest(), files


def _audit(record: dict) -> dict:
    source = record.get("provider_audit", {})
    if not isinstance(source, dict):
        return {}
    outcome = analyze.author_outcome(record)
    result = {key: source[key] for key in ("valid", "rate_limited") if type(source.get(key)) is bool}
    result.update(analyze.public_completion(source))
    if (outcome == analyze.HOST_SUSPENSION_FAILURE
            and source.get("errors") == [analyze.HOST_SUSPENSION_FAILURE]):
        result["errors"] = [analyze.HOST_SUSPENSION_FAILURE]
    for key in AUDIT_IDENTITIES:
        if source.get(key) is not None:
            result[key] = _identity(source[key])
    for key in AUDIT_LISTS:
        if key in source:
            if not isinstance(source[key], list):
                _reject("invalid_audit_list")
            result[key] = [_identity(value) for value in source[key]]
    result["observed_primary_models"] = sorted(set(result.get("observed_primary_models", []) +
                                                    result.get("primary_models", [])))
    usage = source.get("usage", {})
    if isinstance(usage, dict):
        result["usage"] = {key: value for key, value in usage.items() if key in analyze.TOKEN_FIELDS
                           and type(value) is int and value >= 0}
    return result


def _author_public_fields(record: dict, raw: bytes) -> dict:
    """Publish outcome and imported-record hash links, never private records."""
    outcome = analyze.author_outcome(record)
    result = {"private_record_file_sha256": hashlib.sha256(raw).hexdigest()}
    if record.get("status") == "completed" and not isinstance(record.get("record_sha256"), str):
        _reject("completed_author_missing_sealed_digest")
    if outcome == analyze.HOST_SUSPENSION_FAILURE and not isinstance(record.get("record_sha256"), str):
        _reject("operational_failure_missing_sealed_digest")
    if record.get("record_sha256") is not None:
        unsigned = {key: value for key, value in record.items() if key != "record_sha256"}
        actual = hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":"),
                                           allow_nan=False).encode()).hexdigest()
        if record["record_sha256"] != actual:
            _reject("author_record_hash_mismatch")
    if outcome is not None:
        result["outcome"] = outcome
    if record.get("status") in {"completed", "blocked", "running", "retry_pending", "deferred"}:
        result["status"] = record["status"]
    if outcome == analyze.HOST_SUSPENSION_FAILURE:
        result["stop_reason"] = "invalid_provider_audit"
    for field in ("eligible_for_grading", "artifact_present"):
        if field in record:
            if type(record[field]) is not bool:
                _reject("invalid_author_outcome_flag")
            result[field] = record[field]
    for field in ("artifact_sha256", "record_sha256"):
        if field in record:
            digest = record[field]
            if digest is not None and (not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None):
                _reject("invalid_author_provenance_digest")
            result["private_record_sha256" if field == "record_sha256" else field] = digest
    if "migration" in record:
        if not isinstance(record.get("record_sha256"), str):
            _reject("imported_record_missing_sealed_digest")
        result["migration"] = analyze.public_migration(record["migration"])
        if result["migration"]["original_status"] == "deferred" and (
                record.get("status") != "deferred" or record.get("attempts") != []):
            _reject("deferred_migration_has_execution_evidence")
        prefix = f"{record.get('label')}/{record.get('task')}/"
        if any(not call.startswith(prefix) for call in result["migration"]["call_ids"]):
            _reject("imported_record_call_identity_mismatch")
    return result


def _public_attempts(record: dict) -> list[dict]:
    attempts = record.get("attempts", [])
    if not isinstance(attempts, list):
        _reject("invalid_author_attempts")
    outcome = analyze.author_outcome(record)
    result = []
    for attempt in attempts:
        if not isinstance(attempt, dict):
            if outcome == analyze.HOST_SUSPENSION_FAILURE:
                _reject("invalid_operational_failure_attempt")
            continue
        public = {"timed_out": attempt.get("timed_out") is True}
        if outcome == analyze.HOST_SUSPENSION_FAILURE:
            public.update(status=attempt.get("status"),
                          clock_discontinuity=attempt.get("clock_discontinuity") is True,
                          exit_code=attempt.get("exit_code"),
                          artifact_present=attempt.get("artifact_present") is True,
                          artifact_sha256=attempt.get("artifact_sha256"),
                          launch_error=bool(attempt.get("launch_error")),
                          cleanup_error=bool(attempt.get("cleanup_error")))
        result.append(public)
    return result


def _public_manifest(source: dict, expected: list[dict]) -> dict:
    commit = source.get("benchmark_commit")
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        _reject("missing_benchmark_commit")
    plan = [{key: row[key] for key in ("condition", "sample", "label", "task", "seed")}
            for row in expected]
    for row in plan:
        for key in ("condition", "label", "task"):
            _identity(row[key])
    conditions = []
    for item in source.get("conditions", []):
        if not isinstance(item, dict):
            _reject("invalid_condition")
        conditions.append({key: _identity(item.get(key)) for key in ("id", "provider", "model", "effort")})
    if {item["id"] for item in conditions} != {row["condition"] for row in plan} or len(conditions) != len({item["id"] for item in conditions}):
        _reject("condition_plan_mismatch")
    result = {"schema_version": 1, "benchmark_commit": commit, "expected_runs": plan,
              "conditions": conditions, "samples": len({row["sample"] for row in plan}),
              "seeds": sorted({row["seed"] for row in plan}),
              "contrasts": analyze._contrasts(source, {row["condition"] for row in plan})}
    if "migration" in source:
        migration = source["migration"]
        if not isinstance(migration, dict) or migration.get("schema_version") != 1:
            _reject("invalid_manifest_migration")
        result["migration"] = {"schema_version": 1, "amendment_id": _identity(migration.get("amendment_id"))}
        for field in ("source_plan_sha256", "source_manifest_file_sha256"):
            digest = migration.get(field)
            if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                _reject("invalid_manifest_migration_digest")
            result["migration"][field] = digest
    for key in HASH_FIELDS:
        value = source.get(key)
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
            result[key] = value
    environment = source.get("environment", {})
    result["environment"] = {}
    if isinstance(environment, dict):
        for key in ("os", "python"):
            if isinstance(environment.get(key), str):
                result["environment"][key] = environment[key]
        for key in ("author_workers_per_provider", "grading_workers", "grading_timeout_s"):
            if type(environment.get(key)) in (int, float) and math.isfinite(environment[key]):
                result["environment"][key] = environment[key]
        versions = environment.get("cli_versions", {})
        if isinstance(versions, dict):
            result["environment"]["cli_versions"] = {key: versions[key] for key in ("openai", "anthropic")
                                                      if isinstance(versions.get(key), str)}
    if type(source.get("schedule_seed")) is int:
        result["schedule_seed"] = source["schedule_seed"]
    if type(source.get("author_timeout_s")) in (int, float):
        result["author_timeout_s"] = source["author_timeout_s"]
    result["effort_provenance"] = {}
    preflights = source.get("preflight", {})
    if isinstance(preflights, dict):
        for condition in conditions:
            item = preflights.get(condition["id"], {})
            record = item.get("record", {}) if isinstance(item, dict) else {}
            if not isinstance(record, dict):
                continue
            evidence = {"provider_audit": _audit(record)}
            for key in ("passed", "effective_effort_checked", "source_exact"):
                if type(record.get(key)) is bool:
                    evidence[key] = record[key]
            probe = record.get("probe", {})
            if isinstance(probe, dict) and probe.get("effort") is not None:
                evidence["tool_environment_effort"] = _identity(probe["effort"])
            result["effort_provenance"][condition["id"]] = evidence
    return result


def _build_bundle(runs_root: Path, tasks_root: Path, out_dir: Path, stage: Path) -> dict:
    for path in (runs_root, tasks_root, out_dir):
        _no_symlinks(Path(path).absolute())
    root, tasks, output = (Path(path).resolve() for path in (runs_root, tasks_root, out_dir))
    if output.exists():
        _reject("output_already_exists")
    if any(output.is_relative_to(source) or source.is_relative_to(output) for source in (root, tasks)):
        _reject("overlapping_output")
    manifest_bytes = _read(root / "evaluation_manifest.json", "evaluation_manifest.json")
    source = _json(manifest_bytes, "evaluation_manifest.json")
    try:
        expected = validate_evaluation_manifest(source)
        public = _public_manifest(source, expected)
    except (ValueError, TypeError, KeyError):
        _reject("invalid_public_plan")
    files, author_cache, artifacts = {}, {}, {}

    def add(relative: str, data: bytes, *, original: bytes | None = None, binary: bool = False):
        _relative(relative)
        if not binary:
            _scan(data, relative)
        path = stage / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        files[relative] = {"path": relative, "sha256": hashlib.sha256(data).hexdigest(),
                           "bytes": len(data), "original_sha256": hashlib.sha256(
                               original if original is not None else data).hexdigest()}

    add("evaluation_manifest.json", _encoded(public), original=manifest_bytes)
    graded, failed, missing = 0, 0, 0
    for row in expected:
        base = f"{row['label']}/{row['task']}_s{row['seed']}"
        run = root / base
        _no_symlinks(run)
        grade_path, failure_path = run / "grade.json", run / "failure.json"
        _no_symlinks(grade_path)
        _no_symlinks(failure_path)
        if grade_path.exists() and failure_path.exists():
            _reject("ambiguous_run_status", base)
        if not grade_path.exists():
            if failure_path.exists():
                raw = _read(failure_path, base + "/failure.json")
                failure = _json(raw, base)
                if not isinstance(failure.get("reason"), str):
                    _reject("invalid_failure_accounting", base)
                reason = failure["reason"] if failure["reason"] in analyze.FAILURE_REASONS else "other_recorded_failure"
                if (reason == analyze.HOST_SUSPENSION_FAILURE
                        and failure.get("stage") != "authoring"):
                    _reject("invalid_operational_failure_stage", base)
                if reason == analyze.HOST_SUSPENSION_FAILURE:
                    author_relative = f"authoring/{row['label']}/{row['task']}.json"
                    author_path = root / author_relative
                    if not author_path.is_file():
                        _reject("operational_failure_missing_author_record", base)
                    author = _json(_read(author_path, author_relative), author_relative)
                    if any(author.get(field) != row[field]
                           for field in ("label", "condition", "sample", "task")):
                        _reject("operational_failure_author_identity_mismatch", base)
                    try:
                        outcome = analyze.author_outcome(author)
                    except (ValueError, TypeError, KeyError):
                        _reject("invalid_operational_failure_author", base)
                    if outcome != analyze.HOST_SUSPENSION_FAILURE:
                        _reject("operational_failure_missing_author_evidence", base)
                public_failure = {**row, "reason": reason}
                if failure.get("stage") in ("authoring", "grading"):
                    public_failure["stage"] = failure["stage"]
                add(base + "/failure.json", _encoded(public_failure), original=raw)
                failed += 1
            else:
                missing += 1
            continue
        graded += 1
        add(base + "/grade.json", _read(grade_path, base + "/grade.json"))
        raw_meta = _read(run / "meta.json", base + "/meta.json")
        meta = _json(raw_meta, base)
        if any(meta.get(key) != row[key] for key in ("label", "task", "seed")):
            _reject("run_identity_mismatch", base)
        normalized = {**row, **{key: meta[key] for key in META_FIELDS if key in meta}}
        if ("mode" in normalized and normalized["mode"] not in ("a", "b")) or any(
                key in normalized and type(normalized[key]) is not bool
                for key in ("restarted", "killed_by_limit", "sigkilled")):
            _reject("invalid_run_metadata", base)
        if any(key in normalized and normalized[key] is not None and
               (type(normalized[key]) not in (int, float) or not math.isfinite(normalized[key]))
               for key in ("exit_code", "wall_s")):
            _reject("invalid_run_metadata", base)
        add(base + "/meta.json", _encoded(normalized), original=raw_meta)
        add(base + "/farm/recorder.jsonl", _read(run / "farm/recorder.jsonl", base + "/farm/recorder.jsonl"))
        if (run / "results").exists():
            for path in _tree(run / "results"):
                relative = path.relative_to(run).as_posix()
                if path.name in SKIP_NAMES or path.suffix in {".log", ".out", ".err"}:
                    _reject("forbidden_result_file", base + "/" + relative)
                add(base + "/" + relative, _read(path, base + "/" + relative))
        key = row["label"], row["task"]
        if key not in author_cache:
            author_relative = f"authoring/{row['label']}/{row['task']}.json"
            author_bytes = _read(root / author_relative, author_relative)
            author = _json(author_bytes, author_relative)
            if any(author.get(field) != row[field] for field in ("label", "condition", "sample", "task")):
                _reject("author_identity_mismatch", author_relative)
            if author.get("status") != "completed" or author.get("eligible_for_grading") is not True:
                _reject("ineligible_author_has_grade", author_relative)
            unsigned = {key: value for key, value in author.items() if key != "record_sha256"}
            record_sha256 = hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":"),
                                                      allow_nan=False).encode()).hexdigest()
            if author.get("record_sha256") != record_sha256:
                _reject("author_record_hash_mismatch", author_relative)
            attempts = author.get("attempts", [])
            if (not isinstance(attempts, list) or any(not isinstance(item, dict) or item.get("cleanup_error")
                                                       for item in attempts)):
                _reject("unresolved_author_cleanup", author_relative)
            if not isinstance(author.get("provider_audit"), dict) or author["provider_audit"].get("valid") is not True:
                _reject("invalid_author_audit_has_grade", author_relative)
            fingerprint, original_files = _artifact(root / "private/artifacts" / row["label"] / row["task"])
            if author.get("artifact_sha256") != fingerprint:
                _reject("original_artifact_hash_mismatch", author_relative)
            selected = {}
            for relative, data in original_files.items():
                parts, path = PurePosixPath(relative).parts, PurePosixPath(relative)
                if any(part in SKIP_DIRS for part in parts) or path.name in SKIP_NAMES or path.suffix in {".log", ".out", ".err", ".pyc"}:
                    continue
                binary = False
                try:
                    data.decode("utf-8")
                    text = b"\0" not in data
                except UnicodeError:
                    text = False
                if path.suffix not in TEXT_SUFFIXES or not text:
                    known = tasks / row["task"] / relative
                    if parts[0] != "rig" or not known.is_file() or _read(known, relative) != data:
                        _reject("unknown_file_requires_review", relative)
                    binary = True
                selected[relative] = binary, hashlib.sha256(data).hexdigest()
            author_cache[key] = selected
            sanitized = {field: row[field] for field in ("label", "condition", "sample", "task")}
            sanitized.update(artifact_sha256=fingerprint, provider_audit=_audit(author))
            sanitized.update(_author_public_fields(author, author_bytes))
            if type(author.get("wall_s")) in (int, float):
                sanitized["wall_s"] = author["wall_s"]
            sanitized["attempts"] = _public_attempts(author)
            add(author_relative, _encoded(sanitized), original=author_bytes)
            artifacts[f"{row['label']}/{row['task']}"] = {"original_artifact_sha256": fingerprint,
                                                          "exported_files": sorted(selected)}
        for relative, (binary, expected_sha256) in author_cache[key].items():
            data = _read(root / "private/artifacts" / row["label"] / row["task"] / relative, relative)
            if hashlib.sha256(data).hexdigest() != expected_sha256:
                _reject("original_artifact_changed_during_export", relative)
            add(base + "/submission/" + relative, data, binary=binary)
        adapter = _json(_read(run / "adapter.json", base + "/adapter.json"), base)
        if adapter.get("cleanup_error") or adapter.get("timed_out") or adapter.get("error"):
            _reject("unresolved_grading_failure", base)
        if adapter.get("artifact_sha256") != artifacts[f"{row['label']}/{row['task']}"]["original_artifact_sha256"]:
            _reject("grade_artifact_hash_mismatch", base)
    for label, task in sorted({(row["label"], row["task"]) for row in expected} - set(author_cache)):
        relative = f"authoring/{label}/{task}.json"
        path = root / relative
        _no_symlinks(path)
        if not path.exists():
            continue
        raw = _read(path, relative)
        record = _json(raw, relative)
        row = next(row for row in expected if row["label"] == label and row["task"] == task)
        if any(record.get(field) != row[field] for field in ("label", "condition", "sample", "task")):
            _reject("author_identity_mismatch", relative)
        sanitized = {field: row[field] for field in ("label", "condition", "sample", "task")}
        if (analyze.author_outcome(record) == analyze.HOST_SUSPENSION_FAILURE
                and (root / "private/artifacts" / label / task).exists()):
            _reject("operational_failure_has_artifact", relative)
        sanitized["provider_audit"] = _audit(record)
        sanitized.update(_author_public_fields(record, raw))
        if type(record.get("wall_s")) in (int, float):
            sanitized["wall_s"] = record["wall_s"]
        sanitized["attempts"] = _public_attempts(record)
        add(relative, _encoded(sanitized), original=raw)
    summary = {"schema_version": 1, "status": "complete" if missing == 0 else "incomplete",
               "all_planned_runs_accounted": missing == 0, "fully_graded": graded == len(expected),
               "quality_ranking_claim": False, "planned_runs": len(expected), "graded_runs": graded,
               "recorded_failures": failed, "unresolved_runs": missing,
               "benchmark_commit": public["benchmark_commit"],
               "postprocessing_sha256": {"publish.py": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                                           "analyze.py": hashlib.sha256(Path(analyze.__file__).read_bytes()).hexdigest()},
               "original_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
               "artifacts": artifacts,
               "policy": {"meta": "Only planned identity fields and mode/restarted/exit_code/killed_by_limit/sigkilled/wall_s are retained; all other metadata fields are removed.",
                          "raw_bytes": "Grades, recorder events, results and included submission files are unchanged.",
                          "submission": "Frozen authoring files only; raw logs, endpoints, credentials and customization/cache directories are excluded. Unknown binaries require review. Programs are never executed or syntax-filtered.",
                          "accounting": "Complete means each planned run has a grade or recorded failure; it does not imply complete quality observation or a model-quality ranking."}}
    receipt = root / "migration_receipt.json"
    _no_symlinks(receipt)
    if receipt.exists():
        summary["private_migration_receipt_sha256"] = hashlib.sha256(_read(receipt, "migration_receipt.json")).hexdigest()
    add("bundle.json", _encoded(summary))
    try:
        analyze.write_analysis(stage, tasks, stage / "analysis")
    except (ValueError, OSError, TypeError, KeyError):
        _reject("analysis_validation_failed")
    for path in sorted((stage / "analysis").iterdir()):
        relative, data = path.relative_to(stage).as_posix(), path.read_bytes()
        _scan(data, relative)
        files[relative] = {"path": relative, "sha256": hashlib.sha256(data).hexdigest(),
                           "bytes": len(data), "original_sha256": None}
    manifest = {"schema_version": 1, "benchmark_commit": public["benchmark_commit"],
                "files": [files[relative] for relative in sorted(files)],
                "note": "This manifest excludes itself. Original hashes identify source bytes before allowlist normalization; generated analysis has no original hash."}
    (stage / "files.sha256.json").write_bytes(_encoded(manifest))
    if output.exists():
        _reject("output_already_exists")
    stage.rename(output)
    return summary


def export_bundle(runs_root: Path, tasks_root: Path, out_dir: Path) -> dict:
    """Write one new atomic snapshot; reject rather than redact replay bytes."""
    output = Path(out_dir).absolute()
    for path in (output, Path(runs_root).absolute(), Path(tasks_root).absolute()):
        _no_symlinks(path)
    if output.exists():
        _reject("output_already_exists")
    if any(output.resolve().is_relative_to(Path(source).resolve()) or
           Path(source).resolve().is_relative_to(output.resolve()) for source in (runs_root, tasks_root)):
        _reject("overlapping_output")
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".replay-export-", dir=output.parent))
    try:
        return _build_bundle(runs_root, tasks_root, out_dir, stage)
    except ExportError:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        _reject("export_validation_failed")
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", type=Path, required=True)
    parser.add_argument("--tasks-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        summary = export_bundle(args.runs_dir, args.tasks_dir, args.out_dir)
    except ExportError as exc:
        parser.exit(1, f"Export rejected: {exc}\n")
    print(f"Exported {summary['status']} snapshot: {summary['graded_runs']}/{summary['planned_runs']} grades.")


if __name__ == "__main__":
    main()
