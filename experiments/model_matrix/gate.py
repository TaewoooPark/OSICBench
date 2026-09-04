"""Verify an explicitly composed release gate without rerunning programs."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import yaml


DEFAULT_BASE = "1d4788504d5374a85fee0a0a8c91c729be57312e"
CORE = ("osicbench", "osicsim", "tasks", "manuals", "adapters")
CORRECTION = "tasks/t03_diode_iv/mutants/m3_compliance_blind.py"
REPEATED_TASK = "t06_thermal_hold"
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


class GateNotReady(Exception):
    """The original validation process has not published its final record."""

    def __init__(self, message, code="original_full_gate_not_finalized"):
        super().__init__(message)
        self.code = code


class GateInvalid(ValueError):
    """The evidence does not establish the requested full composed gate."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if result.returncode:
        raise GateInvalid("Cannot read the required local Git evidence")
    return result.stdout


def _source_evidence(repo: Path, base: str) -> tuple[dict, dict, dict]:
    if re.fullmatch(r"[0-9a-f]{40}", base) is None:
        raise GateInvalid("Base commit must be a full lowercase commit hash")
    resolved = _git(repo, "rev-parse", "--verify", f"{base}^{{commit}}").decode().strip()
    if resolved != base:
        raise GateInvalid("Base commit resolution mismatch")
    baseline, modes = {}, {}
    for entry in _git(repo, "ls-tree", "-r", "-z", base, "--", *CORE).split(b"\0"):
        if not entry:
            continue
        header, name = entry.split(b"\t", 1)
        mode, kind, _oid = header.decode().split()
        relative = name.decode()
        if kind != "blob" or mode not in ("100644", "100755"):
            raise GateInvalid("Core source must contain only regular files")
        baseline[relative] = _git(repo, "show", f"{base}:{relative}")
        modes[relative] = mode == "100755"
    if not baseline or CORRECTION not in baseline:
        raise GateInvalid("Base source is missing the declared negative control")
    current = {}
    for prefix in CORE:
        if (repo / prefix).is_symlink():
            raise GateInvalid("Core source contains a symlink")
        for path in sorted((repo / prefix).rglob("*")):
            relative = path.relative_to(repo)
            if "__pycache__" in relative.parts or path.suffix in (".pyc", ".pyo") or path.name == ".DS_Store":
                continue
            if path.is_symlink():
                raise GateInvalid("Core source contains a symlink")
            if path.is_file():
                name = relative.as_posix()
                current[name] = path.read_bytes()
                if name in modes and bool(path.stat().st_mode & 0o111) != modes[name]:
                    raise GateInvalid("Core source executable mode differs from the base")
    if set(current) != set(baseline):
        raise GateInvalid("Core source file coverage differs from the base")
    changed = sorted(name for name in current if current[name] != baseline[name])
    if changed != [CORRECTION]:
        raise GateInvalid("Core source differs outside the one declared negative-control correction")
    hashes = {name: {"base_sha256": _sha(baseline[name]), "current_sha256": _sha(current[name])}
              for name in sorted(current)}
    def fingerprint(which):
        digest = hashlib.sha256()
        for name, values in hashes.items():
            digest.update(name.encode() + b"\0" + bytes.fromhex(values[which]))
        return digest.hexdigest()
    evidence = {
        "base_commit": base, "current_commit": _git(repo, "rev-parse", "HEAD").decode().strip(),
        "core_directories": list(CORE), "changed_source_files": changed,
        "fingerprint_algorithm": "sha256 over sorted relative path, NUL, and binary file sha256",
        "base_core_sha256": fingerprint("base_sha256"),
        "current_core_sha256": fingerprint("current_sha256"), "files": hashes,
        "ignored_runtime_artifacts": ["__pycache__ directories", "*.pyc", "*.pyo", ".DS_Store"],
    }
    return baseline, current, evidence


def _programs(files: dict[str, bytes]) -> dict[tuple[str, str], dict]:
    programs = {}
    for relative in sorted(files):
        parts = Path(relative).parts
        if len(parts) != 4 or parts[0] != "tasks" or parts[2] not in ("reference", "mutants") or not parts[3].endswith(".py"):
            continue
        task, program = parts[1], Path(parts[3]).stem
        if NAME.fullmatch(task) is None or NAME.fullmatch(program) is None:
            raise GateInvalid("Task and program names must be neutral relative identifiers")
        task_yaml = f"tasks/{task}/task.yaml"
        config = yaml.safe_load(files[task_yaml]) if task_yaml in files else None
        if not isinstance(config, dict) or config.get("id") != task:
            raise GateInvalid("Program task identity does not match its task definition")
        key = task, program
        if key in programs:
            raise GateInvalid("Duplicate task/program identity")
        programs[key] = {"source": relative, "expected_pass": parts[2] == "reference"}
    task_ids = {Path(name).parts[1] for name in files
                if re.fullmatch(r"tasks/[^/]+/task\.yaml", name)}
    for task in task_ids:
        kinds = {value["expected_pass"] for key, value in programs.items() if key[0] == task}
        if kinds != {False, True}:
            raise GateInvalid("Each task requires both reference and mutant coverage")
    return programs


def _seeds(record: dict) -> list[int]:
    seeds = record.get("seeds")
    if not isinstance(seeds, list) or len(seeds) < 10 or any(type(seed) is not int for seed in seeds) or len(set(seeds)) != len(seeds):
        raise GateInvalid("A complete gate requires at least ten unique integer seeds")
    return sorted(seeds)


def _record_index(record: dict, expected: set[tuple]) -> dict:
    rows = record.get("results")
    if not isinstance(rows, list):
        raise GateInvalid("Gate record requires a result list")
    index = {}
    for row in rows:
        if not isinstance(row, dict):
            raise GateInvalid("Gate result rows must be objects")
        key = row.get("task"), row.get("program"), row.get("seed")
        if type(key[2]) is not int or not all(isinstance(item, str) for item in key[:2]):
            raise GateInvalid("Invalid gate result identity")
        if key in index:
            raise GateInvalid("Duplicate gate result identity")
        index[key] = row
    if set(index) != expected:
        raise GateInvalid("Gate result coverage is missing or contains unexpected identities")
    return index


def _check_run_directories(root: Path, expected: set[tuple]) -> None:
    allowed = {f"{task}/{program}_s{seed}" for task, program, seed in expected}
    for pattern in ("*/*/meta.json", "*/*/grade.json"):
        for path in root.glob(pattern):
            if path.parent.relative_to(root).as_posix() not in allowed:
                raise GateInvalid("Unexpected actual validation run directory")


def _bytes(path: Path, root: Path) -> bytes:
    for item in (path, *path.parents):
        if item.is_symlink():
            raise GateInvalid("Validation evidence cannot use symlinked artifacts")
        if item == root:
            break
    if not path.is_file():
        raise GateInvalid("A required validation artifact is missing")
    return path.read_bytes()


def _finite(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise GateInvalid(f"Invalid numeric {name} in grade evidence")
    return float(value)


def _recorder_span(data: bytes) -> list[float]:
    times = []
    for line in data.splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if not isinstance(event, dict):
            raise GateInvalid("Recorder events must be objects")
        if "t" in event:
            times.append(_finite(event["t"], "recorder timestamp"))
    if not times:
        raise GateInvalid("Incident verification requires timestamped recorder events")
    return [min(times), max(times)]


def _verify_run(root: Path, key: tuple, claimed: dict, source: bytes,
                expected_pass: bool, origin: str, *, inspect_timing: bool = False) -> dict:
    task, program, seed = key
    relative = f"{task}/{program}_s{seed}"
    directory = root / relative
    names = {"submission": "submission/main.py", "grade": "grade.json",
             "meta": "meta.json", "recorder": "farm/recorder.jsonl"}
    contents = {name: _bytes(directory / path, root) for name, path in names.items()}
    if contents["submission"] != source:
        raise GateInvalid("Saved submission does not match its verified source revision")
    grade, meta = json.loads(contents["grade"]), json.loads(contents["meta"])
    if not isinstance(grade, dict) or not isinstance(meta, dict):
        raise GateInvalid("Grade and metadata must be objects")
    label = "reference" if expected_pass else "mutant"
    if any(meta.get(field) != value for field, value in
           (("task", task), ("seed", seed), ("label", label), ("mode", "a"))):
        raise GateInvalid("Saved metadata does not match the expected validation identity")
    if type(meta.get("seed")) is not int:
        raise GateInvalid("Metadata seed must be an integer")
    if type(grade.get("pass")) is not bool or type(grade.get("budget_ok")) is not bool:
        raise GateInvalid("Grade must include boolean pass and budget results")
    dfs, hss = _finite(grade.get("dfs"), "DFS"), _finite(grade.get("hss"), "HSS")
    rs = grade.get("rs")
    derived = dfs >= 70 and hss >= 80 and grade["budget_ok"] and not grade.get("fabricated")
    if rs is not None:
        derived = derived and _finite(rs, "RS") >= 60
    if grade["pass"] != bool(derived):
        raise GateInvalid("Saved pass result contradicts the pinned grade thresholds")
    if (type(claimed.get("pass")) is not bool or claimed["pass"] != grade["pass"]
            or type(claimed.get("ok")) is not bool or claimed["ok"] != (grade["pass"] == expected_pass)
            or _finite(claimed.get("dfs"), "claimed DFS") != dfs):
        raise GateInvalid("Gate summary row contradicts saved grade evidence")
    hashes = {f"{name}_sha256": _sha(data) for name, data in contents.items()}
    for name, value in hashes.items():
        if name in claimed and claimed[name] != value:
            raise GateInvalid("Recorded artifact hash does not match the retained evidence")
    return {"task": task, "program": program, "seed": seed,
            "pass": grade["pass"], "ok": grade["pass"] == expected_pass,
            "dfs": dfs, "hss": hss, "expected_pass": expected_pass,
            "origin": origin, "relative_run": relative,
            "recorded_span_utc_epoch": _recorder_span(contents["recorder"]) if inspect_timing else None,
            **hashes}


def _repeat_task(repo_files: dict, programs: dict, seeds: list[int], original_rows: list[dict],
                 repeat_root: Path, repeat_gate: Path, incident_path: Path) -> tuple[list[dict], dict]:
    try:
        gate_bytes = Path(repeat_gate).read_bytes()
        gate = json.loads(gate_bytes)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise GateNotReady("Repeated full-task gate is not ready", "repeat_task_gate_not_finalized") from exc
    incident_bytes = _bytes(Path(incident_path), Path(incident_path).parent)
    incident = json.loads(incident_bytes)
    if not isinstance(incident, dict) or not isinstance(incident.get("incident"), dict):
        raise GateInvalid("Incident receipt requires a declared suspension interval")
    try:
        start = datetime.fromisoformat(incident["incident"]["start_utc"])
        end = datetime.fromisoformat(incident["incident"]["end_utc"])
    except (KeyError, ValueError, TypeError) as exc:
        raise GateInvalid("Invalid incident timestamps") from exc
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise GateInvalid("Incident timestamps require ordered timezone-aware values")
    def overlaps(row):
        first, last = row["recorded_span_utc_epoch"]
        return first <= end.timestamp() and last >= start.timestamp()
    affected = [row for row in original_rows if overlaps(row)]
    affected_ids = sorted(row["relative_run"] for row in affected)
    receipt = incident.get("overlap")
    if (not isinstance(receipt, dict) or not isinstance(receipt.get("run_ids"), list)
            or sorted(receipt["run_ids"]) != affected_ids or not affected):
        raise GateInvalid("Incident receipt does not match all original overlapping runs")
    if any(row["task"] != REPEATED_TASK for row in affected):
        raise GateInvalid("Suspension affects tasks outside the declared full-task replacement")
    if receipt.get("task_counts") != {REPEATED_TASK: len(affected)}:
        raise GateInvalid("Incident task counts do not match verified overlaps")
    if receipt.get("count", len(affected)) != len(affected):
        raise GateInvalid("Incident overlap count does not match verified overlaps")
    if not isinstance(gate, dict) or _seeds(gate) != seeds:
        raise GateInvalid("Repeated task must use every original seed")
    expected = {(task, program, seed) for task, program in programs if task == REPEATED_TASK for seed in seeds}
    if not expected:
        raise GateInvalid("Declared repeated task is absent from the source revision")
    index = _record_index(gate, expected)
    _check_run_directories(Path(repeat_root), expected)
    repeated = [_verify_run(Path(repeat_root), key, index[key], repo_files[programs[key[:2]]["source"]],
                            programs[key[:2]]["expected_pass"], "task_repeat", inspect_timing=True)
                for key in sorted(expected)]
    if any(overlaps(row) for row in repeated):
        raise GateInvalid("Repeated-task evidence still overlaps the suspension interval")
    summary = _summary(repeated, seeds)
    for field in ("runs", "programs", "behaved", "validation_passed", "escapes"):
        if gate.get(field) != summary[field]:
            raise GateInvalid("Repeated-task gate accounting contradicts its saved runs")
    return repeated, {
        "task": REPEATED_TASK, "reason": "host_suspend", "scope": "all_programs_all_original_seeds",
        "incident_receipt_sha256": _sha(incident_bytes), "repeat_gate_sha256": _sha(gate_bytes),
        "incident": {"start_utc": start.astimezone(timezone.utc).isoformat(),
                     "end_utc": end.astimezone(timezone.utc).isoformat()},
        "intersection_rule": "recorder_start <= incident_end and recorder_end >= incident_start",
        "original_rows_checked_for_overlap": len(original_rows), "contaminated_original_run_ids": affected_ids,
        "contaminated_original_runs": len(affected), "full_task_runs_replaced": len(repeated),
        "nonoverlapping_task_runs_also_replaced": len(repeated) - len(affected),
        "original_task_results": [row for row in original_rows if row["task"] == REPEATED_TASK],
        "replacement_summary": summary,
    }


def _summary(rows: list[dict], seeds: list[int]) -> dict:
    attempts, bad, reference = {}, {}, {}
    for row in rows:
        key = row["task"], row["program"]
        attempts[key] = attempts.get(key, 0) + 1
        if not row["ok"]:
            bad.setdefault(key, []).append(row["seed"])
        if row["expected_pass"]:
            reference.setdefault(row["task"], []).append(row["dfs"])
    stability = {"evaluated": True, "seed_count": len(seeds), "cv_limit": 0.05, "per_task": {}}
    for task, values in sorted(reference.items()):
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / len(values)
        cv = math.sqrt(variance) / mean if mean > 0 else None
        stability["per_task"][task] = {"runs": len(values), "mean": mean, "cv": cv,
                                       "pass": cv is not None and cv <= 0.05}
    escapes = [{"task": task, "program": program, "seeds": sorted(failed), "attempts": attempts[(task, program)]}
               for (task, program), failed in sorted(bad.items())]
    return {"seeds": seeds, "programs": len(attempts), "runs": len(rows),
            "behaved": sum(row["ok"] for row in rows), "escapes": escapes,
            "reference_stability": stability,
            "validation_passed": not escapes and bool(reference)
            and all(value["pass"] for value in stability["per_task"].values())}


def compose_gate(repo_root: Path, original_root: Path, original_gate: Path,
                 replacement_root: Path, *, base_commit: str = DEFAULT_BASE,
                 repeat_task_root: Path | None = None, repeat_task_gate: Path | None = None,
                 incident_audit: Path | None = None) -> dict:
    """Verify complete immutable evidence; return only relative public identities."""
    repo, original_root, replacement_root = map(Path, (repo_root, original_root, replacement_root))
    try:
        original_bytes = Path(original_gate).read_bytes()
        original = json.loads(original_bytes)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise GateNotReady("Original full-gate record is not ready") from exc
    if not isinstance(original, dict):
        raise GateInvalid("Original full-gate record must be an object")
    repeat_requested = any(value is not None for value in (repeat_task_root, repeat_task_gate, incident_audit))
    if repeat_requested and not all(value is not None for value in (repeat_task_root, repeat_task_gate, incident_audit)):
        raise GateInvalid("Full-task replacement requires its root, gate and incident audit together")
    baseline, current, sources = _source_evidence(repo, base_commit)
    programs, seeds = _programs(baseline), _seeds(original)
    expected = {(task, program, seed) for task, program in programs for seed in seeds}
    original_index = _record_index(original, expected)
    _check_run_directories(original_root, expected)
    original_rows = [_verify_run(original_root, key, original_index[key],
                                 baseline[programs[key[:2]]["source"]],
                                 programs[key[:2]]["expected_pass"], "original", inspect_timing=repeat_requested)
                     for key in sorted(expected)]
    old_summary = _summary(original_rows, seeds)
    for field in ("runs", "programs", "behaved", "validation_passed", "escapes"):
        if original.get(field) != old_summary[field]:
            raise GateInvalid("Original full-gate accounting contradicts its verified runs")
    old_stability = original.get("reference_stability")
    if not isinstance(old_stability, dict) or old_stability.get("evaluated") is not True:
        raise GateInvalid("Original gate lacks evaluated reference stability")
    replacement_bytes = _bytes(replacement_root / "gate.json", replacement_root)
    replacement = json.loads(replacement_bytes)
    if not isinstance(replacement, dict) or _seeds(replacement) != seeds:
        raise GateInvalid("Replacement seeds must equal the complete original seed set")
    corrected = ("t03_diode_iv", "m3_compliance_blind")
    replacement_expected = {(*corrected, seed) for seed in seeds}
    replacement_index = _record_index(replacement, replacement_expected)
    _check_run_directories(replacement_root, replacement_expected)
    if replacement.get("program_sha256") != _sha(current[CORRECTION]):
        raise GateInvalid("Replacement source receipt does not match the current negative control")
    if replacement.get("original_program_sha256", _sha(baseline[CORRECTION])) != _sha(baseline[CORRECTION]):
        raise GateInvalid("Replacement receipt does not match the original negative control")
    replaced_rows = [_verify_run(replacement_root, key, replacement_index[key], current[CORRECTION], False,
                                 "replacement") for key in sorted(replacement_expected)]
    final_rows = [row for row in original_rows if (row["task"], row["program"]) != corrected] + replaced_rows
    task_receipt = None
    if repeat_requested:
        repeated, task_receipt = _repeat_task(current, programs, seeds, original_rows,
                                             repeat_task_root, repeat_task_gate, incident_audit)
        final_rows = [row for row in final_rows if row["task"] != REPEATED_TASK] + repeated
    final_rows.sort(key=lambda row: (row["task"], row["program"], row["seed"]))
    summary = _summary(final_rows, seeds)
    return {"schema_version": 1, "composition": True, "complete_release_gate": True,
            "method": "Replace the declared revised negative control on every original seed; optionally replace the entire unchanged task affected by the verified host suspension. Retain all other rows.",
            "source_verification": sources,
            "original_gate_sha256": _sha(original_bytes), "replacement_gate_sha256": _sha(replacement_bytes),
            "original_gate_history": {**old_summary,
                                      "replaced_program_results": [row for row in original_rows
                                                                   if (row["task"], row["program"]) == corrected]},
            "correction_receipt": {"source": CORRECTION,
                                   "base_sha256": _sha(baseline[CORRECTION]), "current_sha256": _sha(current[CORRECTION]),
                                   "replaced_runs": len(replaced_rows),
                                   "retained_runs": sum(row["origin"] == "original" for row in final_rows),
                                   "all_original_seeds_rechecked": True},
            "task_repeat_receipt": task_receipt,
            **summary, "results": final_rows,
            "limitations": [
                "Composition is explicit; this is not represented as a fresh full sweep of every program.",
                "Existing grades are verified against their saved submissions, metadata and pinned pass thresholds, not recomputed by executing task oracles.",
                "Artifact hashes bind the inspected evidence; unchanged inputs do not eliminate stochastic or host-timing variation in future runs.",
                "The original failed gate and all unaffected failures remain part of the record."]}


def write_composed_gate(repo_root: Path, original_root: Path, original_gate: Path,
                        replacement_root: Path, out: Path, *, base_commit: str = DEFAULT_BASE,
                        repeat_task_root: Path | None = None, repeat_task_gate: Path | None = None,
                        incident_audit: Path | None = None) -> dict:
    if Path(out).exists():
        raise FileExistsError("Composition output already exists; choose a new destination")
    result = compose_gate(repo_root, original_root, original_gate, replacement_root, base_commit=base_commit,
                          repeat_task_root=repeat_task_root, repeat_task_gate=repeat_task_gate, incident_audit=incident_audit)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    with Path(out).open("x") as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
        stream.write("\n")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original-root", type=Path, required=True)
    parser.add_argument("--original-gate", type=Path, required=True)
    parser.add_argument("--replacement-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--base-commit", default=DEFAULT_BASE)
    parser.add_argument("--repeat-task-root", type=Path)
    parser.add_argument("--repeat-task-gate", type=Path)
    parser.add_argument("--incident-audit", type=Path)
    args = parser.parse_args()
    try:
        result = write_composed_gate(Path(__file__).resolve().parents[2], args.original_root,
                                     args.original_gate, args.replacement_root, args.out, base_commit=args.base_commit,
                                     repeat_task_root=args.repeat_task_root, repeat_task_gate=args.repeat_task_gate,
                                     incident_audit=args.incident_audit)
    except GateNotReady as exc:
        print(json.dumps({"status": "not_ready", "reason": exc.code}))
        return 2
    except (GateInvalid, FileExistsError, OSError, json.JSONDecodeError, yaml.YAMLError) as exc:
        # Exception text may contain private filenames from JSON/YAML loaders.
        print(json.dumps({"status": "invalid", "reason": type(exc).__name__}))
        return 1
    print(json.dumps({"status": "verified", "composition": True,
                      "validation_passed": result["validation_passed"], "runs": result["runs"],
                      "behaved": result["behaved"], "escapes": result["escapes"]}))
    return 0 if result["validation_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
