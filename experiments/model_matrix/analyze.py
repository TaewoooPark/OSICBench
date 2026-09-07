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
HOST_SUSPENSION_FAILURE = "host_suspend_or_clock_discontinuity"
FAILURE_REASONS = {"missing_author_artifact", "grading_timeout", "grading_error",
                   HOST_SUSPENSION_FAILURE}
TOKEN_FIELDS = {
    "input_tokens", "output_tokens", "total_tokens", "cached_input_tokens",
    "cache_creation_input_tokens", "cache_read_input_tokens", "reasoning_output_tokens",
}
COMPLETION_STATES = {"completed_provider_refusal", "completed_successfully", "incomplete_or_error"}
IDENTITY_EVIDENCE = {"init_and_refusal_metadata", "trace_primary_models"}
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
    "Provider refusals without submissions are operational nonpasses, not graded evidence of instrument-control competence. Their DFS/HSS/RS and transaction counts remain unmeasured, not zero.",
    "Host-suspension or clock-discontinuity failures are infrastructure operational nonpasses. They are not provider refusals or graded evidence, and their DFS/HSS/RS and transaction counts remain unmeasured.",
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


def public_completion(audit: dict) -> dict:
    """Allowlist completion provenance, with strict native-refusal consistency."""
    result = {}
    for field in ("provider_refusal", "completion_successful"):
        if field in audit:
            if type(audit[field]) is not bool:
                raise ValueError("Completion flags must be boolean")
            result[field] = audit[field]
    for field, choices in (("completion_status", COMPLETION_STATES), ("identity_evidence", IDENTITY_EVIDENCE)):
        if field in audit:
            if not isinstance(audit[field], str) or audit[field] not in choices:
                raise ValueError("Unsupported completion provenance")
            result[field] = audit[field]
    if audit.get("provider_refusal") is True:
        if (audit.get("valid") is not True or audit.get("completion_successful") is not False
                or audit.get("completion_status") != "completed_provider_refusal"
                or audit.get("identity_evidence") != "init_and_refusal_metadata"
                or audit.get("provider_error") != "invalid_request" or audit.get("errors")):
            raise ValueError("Inconsistent provider refusal completion")
        category = _identity(audit.get("refusal_category"))
        model = _identity(audit.get("requested_model"))
        if audit.get("resolved_model") != model:
            raise ValueError("Refusal model identity mismatch")
        details = audit.get("provider_refusal_details")
        if (not isinstance(details, dict) or details.get("category") != category
                or details.get("provider_error") != "invalid_request"
                or details.get("system_subtype") != "model_refusal_no_fallback"
                or details.get("terminal_reason") != "api_error" or details.get("original_model") != model):
            raise ValueError("Inconsistent provider refusal metadata")
        status = audit.get("provider_error_status")
        if status is not None:
            raise ValueError("A nonnull HTTP error cannot be treated as the supported native refusal")
        if details.get("provider_error_status") != status:
            raise ValueError("Provider error status mismatch")
        result.update(refusal_category=category, provider_error="invalid_request", provider_error_status=status,
                      provider_refusal_details={"category": category, "provider_error": "invalid_request",
                                               "provider_error_status": status, "terminal_reason": "api_error",
                                               "system_subtype": "model_refusal_no_fallback", "original_model": model})
    elif audit.get("completion_status") == "completed_provider_refusal":
        raise ValueError("Refusal completion requires an explicit refusal flag")
    return result


def public_migration(value: dict) -> dict:
    """Retain imported-record hash links without private paths or raw receipts."""
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("Invalid migration provenance")
    out = {"schema_version": 1}
    for field in ("source_plan_sha256", "source_record_sha256", "source_record_file_sha256"):
        digest = value.get(field)
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("Invalid imported-record digest")
        out[field] = digest
    if value.get("original_status") not in {"completed", "blocked", "running", "retry_pending", "deferred"} or type(value.get("outcome_reclassified")) is not bool:
        raise ValueError("Invalid imported-record status")
    out.update(original_status=value["original_status"], outcome_reclassified=value["outcome_reclassified"])
    calls = value.get("call_ids")
    if not isinstance(calls, list):
        raise ValueError("Migration calls must be a list")
    if value["original_status"] == "deferred" and (calls or value["outcome_reclassified"]):
        raise ValueError("Deferred migration must have no calls or outcome reclassification")
    out["call_ids"] = []
    for call in calls:
        if not isinstance(call, str) or len(call.split("/")) != 3:
            raise ValueError("Invalid neutral call identifier")
        label, task, attempt = call.split("/")
        _identity(label)
        _identity(task)
        if re.fullmatch(r"attempt-[1-9][0-9]*", attempt) is None:
            raise ValueError("Invalid neutral call identifier")
        out["call_ids"].append(call)
    return out


def _host_suspension_outcome(record: dict):
    attempts = record.get("attempts")
    audit = record.get("provider_audit")
    outcome = record.get("outcome")
    if ((outcome is not None and outcome != HOST_SUSPENSION_FAILURE)
            or record.get("status") != "blocked"
            or record.get("stop_reason") != "invalid_provider_audit"
            or record.get("eligible_for_grading") is not False
            or record.get("artifact_present") is not False
            or record.get("artifact_sha256") is not None
            or not isinstance(attempts, list) or len(attempts) != 1
            or any(not isinstance(attempt, dict) for attempt in attempts)
            or record.get("orchestration_error") is not None):
        raise ValueError("Invalid host-suspension operational failure")
    if any(attempt.get("launch_error") or attempt.get("cleanup_error") for attempt in attempts):
        raise ValueError("Host-suspension evidence cannot include launch or cleanup failures")
    last = attempts[-1]
    if (last.get("status") != "completed" or last.get("timed_out") is not True
            or last.get("clock_discontinuity") is not True
            or last.get("exit_code") is not None
            or last.get("artifact_present") is not False
            or last.get("artifact_sha256") is not None):
        raise ValueError("Inconsistent host-suspension terminal attempt")
    if (not isinstance(audit, dict) or audit.get("valid") is not False
            or audit.get("errors") != [HOST_SUSPENSION_FAILURE]
            or audit.get("provider_refusal") is not False
            or audit.get("completion_successful") is not False
            or audit.get("completion_status") != "incomplete_or_error"):
        raise ValueError("Inconsistent host-suspension provider audit")
    if "provider_audit" in last and last["provider_audit"] != audit:
        raise ValueError("Host-suspension attempt and record audits differ")


def author_outcome(record: dict) -> str | None:
    """Derive narrow operational outcomes while retaining historical compatibility."""
    outcome = record.get("outcome")
    if record.get("status") == "deferred" and (record.get("attempts") or record.get("artifact_present")
                                                or record.get("artifact_sha256") is not None or outcome is not None):
        raise ValueError("Deferred authoring records cannot contain an attempt, artifact, or outcome")
    audit = record.get("provider_audit", {})
    attempts = record.get("attempts")
    last = attempts[-1] if isinstance(attempts, list) and attempts else {}
    errors = audit.get("errors") if isinstance(audit, dict) else None
    host_suspension_evidence = (outcome == HOST_SUSPENSION_FAILURE
                                or (isinstance(errors, list)
                                    and HOST_SUSPENSION_FAILURE in errors)
                                or (isinstance(last, dict)
                                    and last.get("clock_discontinuity") is True))
    if host_suspension_evidence:
        _host_suspension_outcome(record)
        return HOST_SUSPENSION_FAILURE
    if outcome is not None and outcome != "provider_refusal":
        raise ValueError("Unsupported declared authoring outcome")
    completion = public_completion(audit) if isinstance(audit, dict) else {}
    refused = completion.get("provider_refusal") is True
    if outcome == "provider_refusal" and not refused:
        raise ValueError("Provider refusal outcome lacks matching audit evidence")
    if refused:
        if (record.get("status") != "completed" or record.get("eligible_for_grading") is not True
                or record.get("artifact_present") is not False or record.get("artifact_sha256") is not None):
            raise ValueError("Provider refusal requires a completed no-artifact authoring outcome")
        return "provider_refusal"
    return None


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
    out, outcomes = [], {}
    for planned in sorted(expected, key=lambda r: (r["condition"], r["sample"], r["task"], r["seed"])):
        row = {key: planned[key] for key in ("condition", "sample", "label", "task", "seed")}
        key = row["label"], row["task"], row["seed"]
        absent = missing.get(key)
        grade = None if absent else json.loads((root / row["label"] / f"{row['task']}_s{row['seed']}" / "grade.json").read_text())
        author_key = row["label"], row["task"]
        if author_key not in outcomes:
            author = root / "authoring" / row["label"] / f"{row['task']}.json"
            outcomes[author_key] = author_outcome(json.loads(author.read_text())) if author.exists() else None
        if outcomes[author_key] in {"provider_refusal", HOST_SUSPENSION_FAILURE} and grade is not None:
            raise ValueError("A no-artifact authoring outcome cannot have instrument grades")
        row.update(status=absent["status"] if absent else "graded",
                   passed=False if absent else grade["pass"],
                   failure_reason=None, hss_findings_observed=False,
                   hss_failed_rules=None, hss_required_failed_rules=None)
        row.update(authoring_outcome=outcomes[author_key],
                   provider_refusal=outcomes[author_key] == "provider_refusal",
                   operational_failure=outcomes[author_key] == HOST_SUSPENSION_FAILURE)
        if absent:
            failure = absent.get("failure", {})
            reason = failure.get("reason")
            row["failure_reason"] = (reason if reason in FAILURE_REASONS else
                                     "other_recorded_failure" if reason is not None else "not_recorded")
            if row["provider_refusal"] and reason is not None and reason != "missing_author_artifact":
                raise ValueError("Provider refusal failure accounting must preserve missing_author_artifact")
            if (row["operational_failure"]
                    and (reason != HOST_SUSPENSION_FAILURE or failure.get("stage") != "authoring")):
                raise ValueError("Host-suspension failure accounting must preserve its operational reason")
            if reason == HOST_SUSPENSION_FAILURE and not row["operational_failure"]:
                raise ValueError("Host-suspension failure accounting requires matching author evidence")
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
            "provider_refusal_runs": sum(row["provider_refusal"] for row in rows),
            "provider_refusal_missing_artifact_failures": sum(row["provider_refusal"] and row["failure_reason"] == "missing_author_artifact" for row in rows),
            "operational_failure_runs": sum(row["operational_failure"] for row in rows),
            "host_suspend_or_clock_discontinuity_failures": sum(
                row["operational_failure"] and row["failure_reason"] == HOST_SUSPENSION_FAILURE
                for row in rows),
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
    refusals, operational_failures, migrated, refusal_categories = [], [], [], Counter()
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
        outcome = author_outcome(record)
        if outcome == "provider_refusal":
            completion = public_completion(record["provider_audit"])
            refusal_categories[completion["refusal_category"]] += 1
            refusals.append({"label": label, "task": task, **completion})
        elif outcome == HOST_SUSPENSION_FAILURE:
            operational_failures.append({"label": label, "task": task,
                                         "reason": HOST_SUSPENSION_FAILURE})
        if "migration" in record:
            migrated.append({"label": label, "task": task, **public_migration(record["migration"])})
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
                if key == "observed_primary_models" and isinstance(values, list) and isinstance(audit.get("primary_models"), list):
                    values = values + audit["primary_models"]
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
            "provider_refusal_records": len(refusals),
            "provider_refusal_categories": dict(sorted(refusal_categories.items())),
            "provider_refusals": refusals,
            "operational_failure_records": len(operational_failures),
            "operational_failures": operational_failures, "imported_records": migrated,
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
           "quality_ranking_claim": False,
           "pass_accounting": "planned_denominator_operational",
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
        out["caveats"].append("Some provider audits are invalid; their records and any artifacts remain in operational accounting, but resolved model/harness attribution is not fully verified.")
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
             "Pass denominators below retain recorded operational failures as nonpasses; "
             "model-quality metrics use observed grades only.", "",
             "## Condition summaries", "",
             "| Condition | Mean operational task pass | Sample SD | Sample range | Graded / planned | DFS | Applicable HSS | Reported RS |",
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
    lines.extend(["", "## Authoring-sample operational task pass", "",
                  "| Condition | Sample | Tasks passed / planned | Task pass | Within-sample Wilson interval |",
                  "|---|---|---|---|---|"])
    for condition, item in analysis["conditions"].items():
        for sample, result in item["samples"].items():
            task_pass = result["task_pass"]
            lines.append(f"| {condition} | {sample} | {task_pass['passed']}/{task_pass['total']} "
                         f"| {_formatted(task_pass['rate'], percent=True)} "
                         f"| [{_formatted(task_pass['ci_lo'], percent=True)}, {_formatted(task_pass['ci_hi'], percent=True)}] |")
    lines.extend(["", "## Matched sample operational comparisons", "",
                  "All deltas are condition B minus condition A. Recorded infrastructure failures remain operational nonpasses, not model-quality observations. There is no cross-sample pooled test.", "",
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
                  "| Condition | Records / planned artifacts | Provider refusals | Operational failures | Timed-out attempts | Observed wall seconds | Timing coverage |",
                  "|---|---|---|---|---|---|---|"])
    for condition, item in analysis["conditions"].items():
        author = item["authoring"]
        lines.append(f"| {condition} | {author['records_observed']}/{author['planned_artifacts']} "
                     f"| {author['provider_refusal_records']} | {author['operational_failure_records']} "
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
