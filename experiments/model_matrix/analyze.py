#!/usr/bin/env python3
"""Publish manifest-backed model/harness comparisons without private log data."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
from collections import Counter
from pathlib import Path

import yaml

from osicbench.report import build_report, validate_evaluation_manifest
from osicbench.stats import mcnemar_exact_p


IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]*\Z")
FAILURE_REASONS = {"missing_author_artifact", "grading_timeout", "grading_error"}
TOKEN_FIELDS = {
    "input_tokens", "output_tokens", "total_tokens", "cached_input_tokens",
    "cache_creation_input_tokens", "cache_read_input_tokens", "reasoning_output_tokens",
}
CAVEATS = [
    "This is a public-development-set evaluation, not a private held-out leaderboard result.",
    "Cross-provider comparisons concern model-plus-native-harness configurations, not isolated model effects or equal inference compute.",
    "A task/sample passes only when every planned seed passes. Seeds and repeated samples are not independent task trials.",
    "Repeated-sample means, standard deviations and ranges are descriptive; no cross-sample pooled p-value is reported.",
    "Holm adjustment is applied across all predeclared contrasts separately within each matched authoring sample, not across samples.",
    "Missing results remain operational nonpasses. Infrastructure failures and unresolved coverage are not evidence of poor instrument control.",
    "DFS/HSS/RS and transaction summaries use observed grades only. RS coverage counts only rows where the oracle reported RS; missing RS is not zero.",
    "HSS-applicable summaries include only tasks with nonempty task.yaml safety rules; no safety claim is made for undeclared hazards.",
    "Provider-reported tokens or price estimates do not establish subscription billing. Unavailable costs are not inferred from elapsed time.",
]


def _identity(value: object) -> str:
    if not isinstance(value, str) or IDENTITY.fullmatch(value) is None:
        raise ValueError("Public identities must be neutral single-component identifiers")
    return value


def _number(value: object, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{field} must be a finite number or null")
    return float(value)


def _spread(values: list[float]) -> dict:
    return {"n": len(values), "mean": statistics.mean(values) if values else None,
            "stddev": statistics.stdev(values) if len(values) > 1 else None,
            "min": min(values) if values else None, "max": max(values) if values else None}


def holm_adjust(p_values: dict[str, float]) -> dict[str, float]:
    """Holm's step-down adjustment, with monotonic adjusted p-values."""
    adjusted = {}
    previous = 0.0
    for rank, (name, p_value) in enumerate(sorted(p_values.items(), key=lambda item: (item[1], item[0]))):
        if not math.isfinite(p_value) or not 0 <= p_value <= 1:
            raise ValueError("p-values must lie in [0, 1]")
        previous = max(previous, min(1.0, (len(p_values) - rank) * p_value))
        adjusted[name] = previous
    return adjusted


def _contrasts(manifest: dict, conditions: set[str]) -> list[dict]:
    declared = manifest.get("contrasts", [])
    if not isinstance(declared, list):
        raise ValueError("Manifest contrasts must be a list")
    out, seen_ids, seen_pairs = [], set(), set()
    for item in declared:
        if not isinstance(item, dict):
            raise ValueError("Each contrast must be an object")
        name, a, b = (_identity(item.get(key)) for key in ("id", "a", "b"))
        if a == b or a not in conditions or b not in conditions:
            raise ValueError("Contrasts must name two different planned conditions")
        pair = tuple(sorted((a, b)))
        if name in seen_ids or pair in seen_pairs:
            raise ValueError("Duplicate contrast ID or condition pair")
        seen_ids.add(name)
        seen_pairs.add(pair)
        out.append({"id": name, "a": a, "b": b})
    return out


def _task_metadata(tasks_root: Path, task_ids: list[str]) -> dict:
    out = {}
    for task in task_ids:
        data = (tasks_root / task / "task.yaml").read_bytes()
        config = yaml.safe_load(data)
        if not isinstance(config, dict) or config.get("id") != task:
            raise ValueError("Task metadata must match the manifest task identity")
        rules = config.get("hss") or []
        if not isinstance(rules, list) or any(not isinstance(rule, dict) for rule in rules):
            raise ValueError("Task HSS rules must be a list of objects")
        out[task] = {"task_yaml_sha256": hashlib.sha256(data).hexdigest(),
                     "hss_applicable": bool(rules), "hss_rule_count": len(rules),
                     "hss_required_rule_count": sum(bool(rule.get("required")) for rule in rules)}
    return out


def _run_rows(root: Path, expected: list[dict], report: dict) -> list[dict]:
    missing = {(r["label"], r["task"], r["seed"]): r for r in report["missing_results"]}
    out = []
    for planned in sorted(expected, key=lambda r: (r["condition"], r["sample"], r["task"], r["seed"])):
        row = {key: planned[key] for key in ("condition", "sample", "label", "task", "seed")}
        key = row["label"], row["task"], row["seed"]
        absent = missing.get(key)
        grade = None if absent else json.loads((root / row["label"] / f"{row['task']}_s{row['seed']}" / "grade.json").read_text())
        row.update(status=absent["status"] if absent else "graded",
                   passed=False if absent else grade["pass"],
                   failure_reason=None, hss_findings_observed=False,
                   hss_failed_rules=None, hss_required_failed_rules=None)
        if absent:
            reason = absent.get("failure", {}).get("reason")
            row["failure_reason"] = (reason if reason in FAILURE_REASONS else
                                     "other_recorded_failure" if reason is not None else "not_recorded")
        for field in ("dfs", "hss", "rs", "transactions"):
            row[field] = _number(grade.get(field), field) if grade is not None else None
        row["fabricated"] = bool(grade.get("fabricated")) if grade else None
        if grade is not None and isinstance(grade.get("hss_findings"), list):
            findings = grade["hss_findings"]
            if any(not isinstance(item, dict) or type(item.get("ok")) is not bool for item in findings):
                raise ValueError("HSS findings must contain boolean ok fields")
            row.update(hss_findings_observed=True,
                       hss_failed_rules=sum(not item["ok"] for item in findings),
                       hss_required_failed_rules=sum(not item["ok"] and bool(item.get("required")) for item in findings))
        out.append(row)
    return out


def _metric(rows: list[dict], field: str) -> dict:
    values = [row[field] for row in rows if row[field] is not None]
    graded = sum(row["status"] == "graded" for row in rows)
    return {"observed_runs": len(values), "planned_runs": len(rows),
            "ungraded_runs": len(rows) - graded, "graded_without_metric": graded - len(values),
            "mean": statistics.mean(values) if values else None,
            "median": statistics.median(values) if values else None}


def _coverage(rows: list[dict]) -> dict:
    graded = [row for row in rows if row["status"] == "graded"]
    return {"planned_runs": len(rows), "graded_runs": len(graded),
            "observed_passes": sum(row["passed"] for row in graded),
            "observed_failures": sum(not row["passed"] for row in graded),
            "missing_runs_or_grades": len(rows) - len(graded),
            "failure_reasons": dict(sorted(Counter(row["failure_reason"] for row in rows
                                                    if row["failure_reason"] is not None).items()))}


def _safety(rows: list[dict], tasks: dict) -> dict:
    applicable = [row for row in rows if tasks[row["task"]]["hss_applicable"]]
    observed = [row for row in applicable if row["hss_findings_observed"]]
    return {"applicable_tasks": sorted(task for task, meta in tasks.items() if meta["hss_applicable"]),
            "hss": _metric(applicable, "hss"),
            "findings_observed_runs": len(observed),
            "runs_with_any_rule_failure": sum(bool(row["hss_failed_rules"]) for row in observed),
            "runs_with_required_rule_failure": sum(bool(row["hss_required_failed_rules"]) for row in observed),
            "failed_rules": sum(row["hss_failed_rules"] for row in observed),
            "required_failed_rules": sum(row["hss_required_failed_rules"] for row in observed)}


def _authoring(root: Path, expected: list[dict]) -> dict:
    identities = sorted({(r["label"], r["task"]) for r in expected})
    times, attempts, timed_out, records, audits = [], 0, 0, 0, 0
    audit_valid, audit_invalid, rate_limited = 0, 0, 0
    model_fields = {key: set() for key in ("requested_model", "requested_effort",
                                         "resolved_model", "resolved_effort", "observed_primary_models")}
    token_values = {key: [] for key in TOKEN_FIELDS}
    for label, task in identities:
        path = root / "authoring" / label / f"{task}.json"
        if not path.exists():
            continue
        record = json.loads(path.read_text())
        if not isinstance(record, dict) or record.get("label", label) != label or record.get("task", task) != task:
            raise ValueError("Authoring record identity mismatch")
        records += 1
        wall_s = _number(record.get("wall_s"), "authoring wall_s")
        if wall_s is not None:
            if wall_s < 0:
                raise ValueError("Authoring time cannot be negative")
            times.append(wall_s)
        events = record.get("attempts", [])
        if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
            raise ValueError("Authoring attempts must be a list of objects")
        attempts += len(events)
        timed_out += sum(event.get("timed_out") is True for event in events)
        audit = record.get("provider_audit")
        if isinstance(audit, dict):
            audits += 1
            audit_valid += audit.get("valid") is True
            audit_invalid += audit.get("valid") is False
            rate_limited += audit.get("rate_limited") is True
            for key in model_fields:
                values = audit.get(key, []) if key == "observed_primary_models" else [audit.get(key)]
                if isinstance(values, list):
                    model_fields[key].update(value for value in values
                                             if isinstance(value, str) and IDENTITY.fullmatch(value))
            usage = audit.get("usage")
            if isinstance(usage, dict):
                for key in TOKEN_FIELDS:
                    number = _number(usage.get(key), key)
                    if number is not None:
                        if number < 0 or not number.is_integer():
                            raise ValueError("Token counts must be nonnegative integers")
                        token_values[key].append(int(number))
    return {"planned_artifacts": len(identities), "records_observed": records,
            "wall_s_observed_artifacts": len(times), "wall_s_total": sum(times) if times else None,
            "wall_s": _spread(times), "attempts_observed": attempts,
            "timed_out_attempts_observed": timed_out, "provider_audit_records_observed": audits,
            "provider_audit_valid_records": audit_valid, "provider_audit_invalid_records": audit_invalid,
            "provider_rate_limited_records": rate_limited,
            "provider_model_observations": {key: sorted(values) for key, values in model_fields.items()},
            "token_usage": {key: {"total": sum(values), "observed_artifacts": len(values)}
                            for key, values in sorted(token_values.items()) if values} or None,
            "billing": None,
            "cost_note": "Only allowlisted provider token counters are summed, with field-level coverage; counters may overlap and are not added together. Unknown usage fields and billing are not inferred."}


def _paired(contrast: dict, rows: list[dict], sample: int) -> dict:
    task_passes = {}
    for condition in (contrast["a"], contrast["b"]):
        selected = [r for r in rows if r["condition"] == condition and r["sample"] == sample]
        task_passes[condition] = {task: all(r["passed"] for r in selected if r["task"] == task)
                                  for task in sorted({r["task"] for r in selected})}
    a, b = task_passes[contrast["a"]], task_passes[contrast["b"]]
    if not a or set(a) != set(b):
        raise ValueError("Paired comparison requires identical nonempty task coverage")
    a_only = sum(a[task] and not b[task] for task in a)
    b_only = sum(b[task] and not a[task] for task in a)
    return {"sample": sample, "paired_tasks": len(a),
            "a_pass_b_fail": a_only, "b_pass_a_fail": b_only,
            "both_pass": sum(a[task] and b[task] for task in a),
            "both_fail": sum(not a[task] and not b[task] for task in a),
            "task_pass_rate_delta_b_minus_a": (b_only - a_only) / len(a),
            "mcnemar_p_tasks": mcnemar_exact_p(a_only, b_only)}


def build_analysis(runs_root: Path, tasks_root: Path) -> dict:
    """Validate private inputs, then construct only allowlisted public fields."""
    root = Path(runs_root)
    manifest_bytes = (root / "evaluation_manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    expected = validate_evaluation_manifest(manifest)
    for row in expected:
        for key in ("label", "condition", "task"):
            _identity(row[key])
    report = build_report(root)
    condition_ids = sorted({r["condition"] for r in expected})
    sample_ids = sorted({r["sample"] for r in expected})
    task_ids = sorted({r["task"] for r in expected})
    contrasts = _contrasts(manifest, set(condition_ids))
    tasks = _task_metadata(Path(tasks_root), task_ids)
    rows = _run_rows(root, expected, report)
    public_plan = [{key: row[key] for key in ("label", "condition", "sample", "task", "seed")}
                   for row in sorted(expected, key=lambda r: (r["label"], r["task"], r["seed"]))]
    provenance = {"input_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                  "public_plan_sha256": hashlib.sha256(json.dumps(public_plan, sort_keys=True).encode()).hexdigest()}
    for field, length in (("benchmark_sha256", 64), ("prompt_sha256", 64), ("benchmark_commit", 40)):
        value = manifest.get(field)
        provenance[field] = value if isinstance(value, str) and re.fullmatch(f"[0-9a-f]{{{length}}}", value) else None
    out = {"schema_version": 1, "track": "public_development_native_harness_matrix",
           "provenance": provenance,
           "design": {"conditions": len(condition_ids), "authoring_samples": len(sample_ids),
                      "tasks": len(task_ids),
                      "planned_artifacts": len({(r["condition"], r["sample"], r["task"]) for r in expected}),
                      "planned_runs": len(expected), "sample_ids": sample_ids,
                      "evaluation_seeds": sorted({r["seed"] for r in expected}),
                      "seeds_per_task": {task: sorted({r["seed"] for r in expected if r["task"] == task})
                                         for task in task_ids},
                      "preliminary_single_sample": len(sample_ids) == 1},
           "coverage": _coverage(rows), "tasks": tasks, "conditions": {},
           "contrasts": [], "caveats": list(CAVEATS), "runs": rows}
    if not contrasts:
        out["caveats"].append("No contrasts were predeclared; no pairwise tests are computed.")
    if len(sample_ids) == 1:
        out["caveats"].append("Only one authoring sample was planned; authoring variability and repeat spread are unevaluated.")
    if len(sample_ids) < 3 or any(len(seeds) < 5 for seeds in out["design"]["seeds_per_task"].values()):
        out["caveats"].append("The matrix does not meet the protocol minimum of three authoring samples and five evaluation seeds per task.")
    if out["coverage"]["missing_runs_or_grades"]:
        out["caveats"].append("The matrix is incomplete: comparisons summarize operational accounting, not a fully observed model-quality ranking.")
    for condition in condition_ids:
        subset = [row for row in rows if row["condition"] == condition]
        native = report["conditions"][condition]
        samples = {}
        for sample in sample_ids:
            sample_rows = [row for row in subset if row["sample"] == sample]
            samples[str(sample)] = {
                "task_pass": native["samples"][str(sample)]["task_pass"],
                "run_pass": native["samples"][str(sample)]["pass"],
                "coverage": _coverage(sample_rows),
                "metrics": {field: _metric(sample_rows, field) for field in ("dfs", "hss", "rs", "transactions")},
            }
        out["conditions"][condition] = {
            "task_pass": native["task_pass"], "run_pass": native["pass"],
            "sample_task_pass_rates": native["sample_task_pass_rates"], "samples": samples,
            "coverage": _coverage(subset),
            "metrics": {field: _metric(subset, field) for field in ("dfs", "hss", "rs", "transactions")},
            "hss_applicable": _safety(subset, tasks),
            "authoring": _authoring(root, [r for r in expected if r["condition"] == condition]),
        }
    if any(item["authoring"]["provider_audit_invalid_records"] for item in out["conditions"].values()):
        out["caveats"].append("Some provider audits are invalid; their observed artifacts remain in operational accounting, but resolved model/harness attribution is not fully verified.")
    for contrast in contrasts:
        samples = {str(sample): _paired(contrast, rows, sample) for sample in sample_ids}
        out["contrasts"].append({**contrast, "samples": samples,
                                 "sample_task_pass_rate_deltas": _spread([
                                     item["task_pass_rate_delta_b_minus_a"] for item in samples.values()]),
                                 "pooled_p_value": None})
    for sample in sample_ids:
        adjusted = holm_adjust({contrast["id"]: contrast["samples"][str(sample)]["mcnemar_p_tasks"]
                                for contrast in out["contrasts"]})
        for contrast in out["contrasts"]:
            contrast["samples"][str(sample)]["holm_p_tasks"] = adjusted[contrast["id"]]
            contrast["samples"][str(sample)]["holm_family_size"] = len(contrasts)
    return out


def _formatted(value: float | None, *, percent: bool = False) -> str:
    if value is None:
        return "N/E"
    return f"{value:.2%}" if percent else f"{value:.4g}"


def render_markdown(analysis: dict) -> str:
    design, coverage = analysis["design"], analysis["coverage"]
    lines = ["# OSICBench model and native-harness matrix", "",
             f"{design['conditions']} conditions; {design['tasks']} tasks; "
             f"{design['authoring_samples']} authoring samples; "
             f"{len(design['evaluation_seeds'])} evaluation seeds.", "",
             f"Observed grades: {coverage['graded_runs']}/{coverage['planned_runs']}. "
             f"Missing runs or grades: {coverage['missing_runs_or_grades']}.", "",
             "## Condition summaries", "",
             "| Condition | Mean task pass | Sample SD | Sample range | Graded / planned | DFS | Applicable HSS | Reported RS |",
             "|---|---|---|---|---|---|---|---|"]
    for condition, item in analysis["conditions"].items():
        spread, cov = item["sample_task_pass_rates"], item["coverage"]
        hss, rs = item["hss_applicable"]["hss"], item["metrics"]["rs"]
        lines.append(f"| {condition} | {_formatted(spread['mean'], percent=True)} "
                     f"| {_formatted(spread['stddev'], percent=True)} "
                     f"| {_formatted(spread['min'], percent=True)}–{_formatted(spread['max'], percent=True)} "
                     f"| {cov['graded_runs']}/{cov['planned_runs']} "
                     f"| {_formatted(item['metrics']['dfs']['mean'])} "
                     f"| {_formatted(hss['mean'])} ({hss['observed_runs']}/{hss['planned_runs']}) "
                     f"| {_formatted(rs['mean'])} ({rs['observed_runs']}/{rs['planned_runs']}) |")
    lines.extend(["", "## Authoring-sample task pass", "",
                  "| Condition | Sample | Tasks passed / planned | Task pass | Within-sample Wilson interval |",
                  "|---|---|---|---|---|"])
    for condition, item in analysis["conditions"].items():
        for sample, result in item["samples"].items():
            task_pass = result["task_pass"]
            lines.append(f"| {condition} | {sample} | {task_pass['passed']}/{task_pass['total']} "
                         f"| {_formatted(task_pass['rate'], percent=True)} "
                         f"| [{_formatted(task_pass['ci_lo'], percent=True)}, {_formatted(task_pass['ci_hi'], percent=True)}] |")
    lines.extend(["", "## Matched sample comparisons", "",
                  "All deltas are condition B minus condition A. There is no cross-sample pooled test.", "",
                  "| Contrast | A | B | Sample | Task delta | Discordant A/B | Raw p | Holm p | Family size |",
                  "|---|---|---|---|---|---|---|---|---|"])
    for contrast in analysis["contrasts"]:
        for sample, item in contrast["samples"].items():
            lines.append(f"| {contrast['id']} | {contrast['a']} | {contrast['b']} | {sample} "
                         f"| {100 * item['task_pass_rate_delta_b_minus_a']:.2f} pp "
                         f"| {item['a_pass_b_fail']}/{item['b_pass_a_fail']} "
                         f"| {_formatted(item['mcnemar_p_tasks'])} | {_formatted(item['holm_p_tasks'])} "
                         f"| {item['holm_family_size']} |")
    lines.extend(["", "### Descriptive contrast spread across samples", "",
                  "| Contrast | Mean delta | Sample SD | Sample range |",
                  "|---|---|---|---|"])
    for contrast in analysis["contrasts"]:
        spread = contrast["sample_task_pass_rate_deltas"]
        stddev = "N/E" if spread["stddev"] is None else f"{100 * spread['stddev']:.2f} pp"
        lines.append(f"| {contrast['id']} | {100 * spread['mean']:.2f} pp | {stddev} "
                     f"| [{100 * spread['min']:.2f}, {100 * spread['max']:.2f}] pp |")
    lines.extend(["", "## Authoring coverage", "",
                  "| Condition | Records / planned artifacts | Timed-out attempts | Observed wall seconds | Timing coverage |",
                  "|---|---|---|---|---|"])
    for condition, item in analysis["conditions"].items():
        author = item["authoring"]
        lines.append(f"| {condition} | {author['records_observed']}/{author['planned_artifacts']} "
                     f"| {author['timed_out_attempts_observed']} | {_formatted(author['wall_s_total'])} "
                     f"| {author['wall_s_observed_artifacts']}/{author['planned_artifacts']} |")
    lines.extend(["", "## Interpretation and limitations", ""])
    lines.extend(f"- {caveat}" for caveat in analysis["caveats"])
    lines.append("")
    return "\n".join(lines)


def write_analysis(runs_root: Path, tasks_root: Path, out_dir: Path) -> dict:
    """Create a new publication snapshot without overwriting existing outputs."""
    output = Path(out_dir)
    json_path, markdown_path = output / "analysis.json", output / "analysis.md"
    if json_path.exists() or markdown_path.exists():
        raise FileExistsError("Analysis output already exists; choose a fresh publication directory")
    analysis = build_analysis(runs_root, tasks_root)
    output.mkdir(parents=True, exist_ok=True)
    with json_path.open("x") as stream:
        json.dump(analysis, stream, indent=2, allow_nan=False)
        stream.write("\n")
    with markdown_path.open("x") as stream:
        stream.write(render_markdown(analysis))
    return analysis


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", type=Path, required=True)
    parser.add_argument("--tasks-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    analysis = write_analysis(args.runs_dir, args.tasks_dir, args.out_dir)
    print(f"Published {analysis['coverage']['graded_runs']}/{analysis['coverage']['planned_runs']} graded runs.")


if __name__ == "__main__":
    main()
