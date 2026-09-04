"""Aggregate run directories without dropping planned failures from reports."""
from __future__ import annotations

import json
import os
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List

from .stats import mcnemar_exact_p, summarize_pass


MANIFEST_NAME = "evaluation_manifest.json"


def _identity(row: dict) -> tuple:
    label, task, seed = row.get("label"), row.get("task"), row.get("seed")
    if not isinstance(label, str) or not label or not isinstance(task, str) or not task:
        raise ValueError("Run identities require nonempty label and task strings")
    if type(seed) is not int:
        raise ValueError("Run identities require an integer seed")
    return label, task, seed


def _result_directories(root: Path) -> List[Path]:
    """Stop at run roots; submissions and outputs may contain unrelated JSON."""
    planned = (root / MANIFEST_NAME).exists()
    found = []
    for current, directories, filenames in os.walk(root):
        path = Path(current)
        if {"meta.json", "grade.json", "failure.json"}.intersection(filenames):
            found.append(path)
            directories.clear()
        elif planned and len(path.relative_to(root).parts) >= 2:
            # Manifest identities have the shape label/task_seed. An incomplete
            # run without metadata must not expose nested submission fixtures.
            directories.clear()
    return sorted(found)


def _read_failure(path: Path) -> dict:
    failure = json.loads(path.read_text())
    if not isinstance(failure, dict) or not isinstance(failure.get("reason"), str):
        raise ValueError(f"Failure record requires a reason string: {path}")
    return failure


def collect_runs(runs_root: Path) -> List[Dict[str, Any]]:
    """Collect metadata even without a grade; reject ambiguous run identities."""
    out: List[Dict[str, Any]] = []
    seen = set()
    root = Path(runs_root)
    for run_dir in _result_directories(root):
        meta_path = run_dir / "meta.json"
        grade_path = run_dir / "grade.json"
        failure_path = run_dir / "failure.json"
        if not meta_path.exists():
            if grade_path.exists():
                raise ValueError(f"Grade has no metadata: {grade_path}")
            if not (root / MANIFEST_NAME).exists():
                raise ValueError(f"Failure without metadata requires an evaluation manifest: {failure_path}")
            continue
        meta = json.loads(meta_path.read_text())
        if not isinstance(meta, dict):
            raise ValueError(f"Run metadata must be an object: {meta_path}")
        meta.setdefault("label", "unlabeled")
        key = _identity(meta)
        if key in seen:
            raise ValueError(f"Duplicate actual run identity: {key}")
        seen.add(key)
        grade = json.loads(grade_path.read_text()) if grade_path.exists() else None
        if grade_path.exists() and (not isinstance(grade, dict)
                                    or type(grade.get("pass")) is not bool):
            raise ValueError(f"Grade must contain a boolean pass field: {grade_path}")
        row = {**meta, "dir": str(run_dir), "grade": grade,
               "status": ("missing_grade" if grade is None else
                          "graded_passed" if grade["pass"] else "graded_failed")}
        if failure_path.exists():
            if grade is not None:
                raise ValueError(f"Run has both grade and failure records: {run_dir}")
            row["failure"] = _read_failure(failure_path)
        out.append(row)
    return out


def validate_evaluation_manifest(manifest: dict) -> List[Dict[str, Any]]:
    """Validate the planned denominator and matched authoring-sample coverage."""
    if not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int \
            or manifest["schema_version"] != 1:
        raise ValueError("Evaluation manifest requires schema_version 1")
    rows = manifest.get("expected_runs")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Evaluation manifest requires a nonempty expected_runs list")
    seen, labels, groups = set(), {}, {}
    coverage = defaultdict(set)
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Expected runs must be objects")
        key = _identity(row)
        for segment in key[:2]:
            if segment in (".", "..") or "/" in segment or "\\" in segment:
                raise ValueError("Manifest labels/tasks must be single path components")
        condition, sample = row.get("condition"), row.get("sample")
        if not isinstance(condition, str) or not condition:
            raise ValueError("Expected runs require a nonempty condition")
        if type(sample) is not int or sample < 1:
            raise ValueError("Expected runs require a positive integer sample")
        if key in seen:
            raise ValueError(f"Duplicate expected run identity: {key}")
        seen.add(key)
        group = condition, sample
        if key[0] in labels and labels[key[0]] != group:
            raise ValueError(f"Label maps to multiple condition/sample pairs: {key[0]}")
        if group in groups and groups[group] != key[0]:
            raise ValueError(f"Condition/sample maps to multiple labels: {group}")
        labels[key[0]], groups[group] = group, key[0]
        coverage[group].add(key[1:])
    if any(keys != next(iter(coverage.values())) for keys in coverage.values()):
        raise ValueError("Condition/sample groups have unequal planned task/seed coverage")
    samples = defaultdict(set)
    for condition, sample in groups:
        samples[condition].add(sample)
    if any(ids != next(iter(samples.values())) for ids in samples.values()):
        raise ValueError("Conditions have unequal planned authoring sample IDs")
    return rows


def load_evaluation_manifest(runs_root: Path) -> List[Dict[str, Any]]:
    """Read and validate runs_root/evaluation_manifest.json."""
    return validate_evaluation_manifest(json.loads((Path(runs_root) / MANIFEST_NAME).read_text()))


def _planned_runs(root: Path, actual: List[dict], expected: List[dict]) -> List[dict]:
    plan = {_identity(row): row for row in expected}
    index = {_identity(row): row for row in actual}
    unexpected = set(index) - set(plan)
    if unexpected:
        raise ValueError(f"Unexpected actual run identities: {sorted(unexpected)}")
    for key, row in index.items():
        intended = root / key[0] / f"{key[1]}_s{key[2]}"
        if Path(row["dir"]).resolve() != intended.resolve():
            raise ValueError(f"Run directory/metadata identity mismatch: {row['dir']}")
        for field in ("condition", "sample"):
            if field in row and row[field] != plan[key][field]:
                raise ValueError(f"Run metadata/manifest {field} mismatch: {key}")
    failure_paths = {root / label / f"{task}_s{seed}" / "failure.json"
                     for label, task, seed in plan}
    for directory in _result_directories(root):
        path = directory / "failure.json"
        if path.exists() and path not in failure_paths:
            raise ValueError(f"Unexpected failure record: {path}")
    rows = []
    for key, planned in sorted(plan.items()):
        row = ({**index[key], "condition": planned["condition"], "sample": planned["sample"]}
               if key in index else
               {**planned, "dir": None, "grade": None, "status": "missing_run"})
        failure_path = root / key[0] / f"{key[1]}_s{key[2]}" / "failure.json"
        if failure_path.exists():
            if row["grade"] is not None:
                raise ValueError(f"Run has both grade and failure records: {failure_path.parent}")
            row["failure"] = _read_failure(failure_path)
        rows.append(row)
    return rows


def _passed(row: dict) -> bool:
    return row["grade"] is not None and row["grade"]["pass"]


def _coverage(items: List[dict]) -> dict:
    counts = {status: sum(r["status"] == status for r in items)
              for status in ("graded_passed", "graded_failed", "missing_grade", "missing_run")}
    return {"planned_or_known_runs": len(items),
            "graded_runs": counts["graded_passed"] + counts["graded_failed"], **counts,
            "failure_reasons": dict(sorted(Counter(r["failure"]["reason"] for r in items
                                                    if "failure" in r).items()))}


def _summary(items: List[dict]) -> dict:
    graded = [r for r in items if r["grade"] is not None]
    dfs = [float(r["grade"].get("dfs", 0.0)) for r in graded]
    hss = [float(r["grade"].get("hss", 0.0)) for r in graded]
    txn = [int(r["grade"].get("transactions", 0)) for r in graded]
    per_task = defaultdict(lambda: {"pass": 0, "total": 0, "graded": 0})
    for row in items:
        slot = per_task[row["task"]]
        slot["total"] += 1
        slot["pass"] += int(_passed(row))
        slot["graded"] += int(row["grade"] is not None)
    return {
        "pass": summarize_pass([_passed(r) for r in items]),
        "task_pass": summarize_pass([s["pass"] == s["total"] for s in per_task.values()]),
        "coverage": _coverage(items),
        "fabricated_runs": sum(bool(r["grade"].get("fabricated")) for r in graded),
        "dfs_mean": round(statistics.mean(dfs), 2) if dfs else None,
        "hss_mean": round(statistics.mean(hss), 2) if hss else None,
        "transactions_median": sorted(txn)[len(txn) // 2] if txn else None,
        "metric_coverage": "graded_runs_only",
        "per_task": dict(sorted(per_task.items())),
    }


def _spread(values: List[float]) -> dict:
    return {"n": len(values), "mean": statistics.mean(values),
            "stddev": statistics.stdev(values) if len(values) > 1 else None,
            "min": min(values), "max": max(values)}


def _paired(a: str, b: str, a_rows: List[dict], b_rows: List[dict]) -> dict:
    ia = {(r["task"], r["seed"]): _passed(r) for r in a_rows}
    ib = {(r["task"], r["seed"]): _passed(r) for r in b_rows}
    if set(ia) != set(ib):
        return {"a": a, "b": b, "status": "unavailable_coverage_mismatch"}
    a_only = sum(ia[k] and not ib[k] for k in ia)
    b_only = sum(ib[k] and not ia[k] for k in ia)
    ta = {t: all(value for (task, _), value in ia.items() if task == t)
          for t, _ in ia}
    tb = {t: all(value for (task, _), value in ib.items() if task == t)
          for t, _ in ib}
    t_a_only = sum(ta[t] and not tb[t] for t in ta)
    t_b_only = sum(tb[t] and not ta[t] for t in ta)
    return {
        "a": a, "b": b, "status": "paired", "paired_runs": len(ia),
        "paired_tasks": len(ta),
        "a_pass_b_fail": a_only, "b_pass_a_fail": b_only,
        "mcnemar_p_runs": round(mcnemar_exact_p(a_only, b_only), 6),
        "task_a_pass_b_fail": t_a_only, "task_b_pass_a_fail": t_b_only,
        "mcnemar_p_tasks": round(mcnemar_exact_p(t_a_only, t_b_only), 6),
        "task_pass_rate_delta_b_minus_a": (sum(tb.values()) - sum(ta.values())) / len(ta),
    }


def build_report(runs_root: Path) -> Dict[str, Any]:
    root = Path(runs_root)
    runs = collect_runs(root)
    verified = (root / MANIFEST_NAME).exists()
    if verified:
        runs = _planned_runs(root, runs, load_evaluation_manifest(root))
    else:
        runs = [{**r, "condition": r["label"], "sample": 1} for r in runs]
    by_condition = defaultdict(list)
    for row in runs:
        by_condition[row["condition"]].append(row)
    report: Dict[str, Any] = {
        "runs": len(runs), "conditions": {},
        "coverage": {"verified": verified,
                     "status": "manifest_verified" if verified else "unverified_legacy",
                     **_coverage(runs)},
        "warnings": ([] if verified else [
            "No evaluation manifest: completely absent tasks/seeds cannot be detected; coverage is unverified."
        ]),
    }
    if any(r["grade"] is None for r in runs):
        report["warnings"].append(
            "Missing results count as operational failures, not observed graded failures; DFS/HSS/transactions use graded rows only.")
    for condition, items in sorted(by_condition.items()):
        by_sample = defaultdict(list)
        for row in items:
            by_sample[row["sample"]].append(row)
        samples = {str(sample): {"label": rows[0]["label"], **_summary(rows)}
                   for sample, rows in sorted(by_sample.items())}
        summary = _summary(items)
        if len(samples) > 1:
            # Repeated submissions share tasks. Do not pool task/sample trials
            # into a binomial interval or pretend they are independent tasks.
            task_pass = [c["task_pass"] for c in samples.values()]
            passed = sum(p["passed"] for p in task_pass)
            total = sum(p["total"] for p in task_pass)
            summary["task_pass"] = {"passed": passed, "total": total,
                                    "rate": passed / total, "ci_lo": None, "ci_hi": None,
                                    "unit": "task_sample_descriptive_only"}
            summary["pass"].update(ci_lo=None, ci_hi=None,
                                   unit="task_seed_sample_descriptive_only")
        summary["samples"] = samples
        summary["sample_task_pass_rates"] = _spread([c["task_pass"]["rate"]
                                                       for c in samples.values()])
        report["conditions"][condition] = summary
    conditions = sorted(by_condition)
    if len(conditions) == 2:
        a, b = conditions
        sample_ids = sorted({r["sample"] for r in by_condition[a]})
        pairs = {str(sample): _paired(
            a, b, [r for r in by_condition[a] if r["sample"] == sample],
            [r for r in by_condition[b] if r["sample"] == sample])
            for sample in sample_ids}
        if len(pairs) == 1:
            report["paired_comparison"] = {**next(iter(pairs.values())), "per_sample": pairs}
        else:
            report["paired_comparison"] = {
                "a": a, "b": b, "status": "paired_by_sample", "per_sample": pairs,
                "task_pass_rate_delta_b_minus_a": _spread([
                    pc["task_pass_rate_delta_b_minus_a"] for pc in pairs.values()]),
                "mcnemar_p_runs": None, "mcnemar_p_tasks": None,
                "inference_note": "No pooled p-value: authoring samples reuse the same tasks.",
            }
    report["missing_results"] = [
        {k: r[k] for k in ("label", "condition", "sample", "task", "seed", "status", "failure")
         if k in r} for r in runs if r["grade"] is None]
    return report


def _interval(value: dict) -> str:
    if value["ci_lo"] is None:
        return "not pooled"
    return f"[{value['ci_lo']:.0%}, {value['ci_hi']:.0%}]"


def render_markdown(report: Dict[str, Any]) -> str:
    lines: List[str] = ["# OSIC-Bench report", "", f"Total runs: {report['runs']}", ""]
    cov = report["coverage"]
    lines.append(f"Coverage: **{cov['status']}**; graded {cov['graded_runs']}/{report['runs']}; "
                 f"missing run {cov['missing_run']}, missing grade {cov['missing_grade']}, "
                 f"observed graded failures {cov['graded_failed']}.")
    lines.append("")
    for warning in report["warnings"]:
        lines.extend([f"Warning: {warning}", ""])
    lines.append("| condition | run-level pass | 95% CI | task-level pass | 95% CI | DFS mean | HSS mean | fabricated | txn median |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for label, c in report["conditions"].items():
        p, tp = c["pass"], c["task_pass"]
        dfs, hss, txn = ["N/E" if c[k] is None else c[k]
                         for k in ("dfs_mean", "hss_mean", "transactions_median")]
        lines.append(
            f"| {label} | {p['passed']}/{p['total']} = {p['rate']:.0%} "
            f"| {_interval(p)} | {tp['passed']}/{tp['total']} = {tp['rate']:.0%} "
            f"| {_interval(tp)} | {dfs} | {hss} "
            f"| {c['fabricated_runs']} | {txn} |")
    lines.extend(["", "A task/sample passes only if every planned seed passed. Run-level intervals are "
                  "clustered and descriptive only. Repeated authoring samples are summarized by "
                  "sample task-pass rates, without a pooled task-level interval or p-value. "
                  "DFS/HSS/transactions describe graded runs only; missing results are not assigned measured safety scores.", ""])
    for label, c in report["conditions"].items():
        lines.extend([f"## {label} - per task", "",
                      "| task | run pass | graded / planned or known |", "|---|---|---|"])
        for task, slot in c["per_task"].items():
            lines.append(f"| {task} | {slot['pass']}/{slot['total']} | {slot['graded']}/{slot['total']} |")
        lines.append("")
        spread = c["sample_task_pass_rates"]
        stddev = ("N/A (one sample)" if spread["stddev"] is None
                  else f"{spread['stddev']:.2%}")
        lines.extend([f"Authoring samples: {spread['n']}; task-pass mean {spread['mean']:.2%}; "
                      f"range [{spread['min']:.2%}, {spread['max']:.2%}]; "
                      f"sample standard deviation {stddev}.", "",
                      "| sample | label | task pass | graded / planned or known |", "|---|---|---|---|"])
        for sample, summary in c["samples"].items():
            tp, coverage = summary["task_pass"], summary["coverage"]
            lines.append(f"| {sample} | {summary['label']} | {tp['passed']}/{tp['total']} "
                         f"| {coverage['graded_runs']}/{coverage['planned_or_known_runs']} |")
        lines.append("")
    if "paired_comparison" in report:
        pc = report["paired_comparison"]
        lines.extend(["## Paired comparison", ""])
        for sample, pair in pc["per_sample"].items():
            if pair["status"] != "paired":
                lines.extend(["Paired comparison unavailable: unequal task/seed coverage.", ""])
                continue
            lines.extend([
                f"Sample {sample}: {pair['a']} vs {pair['b']} - run level (clustered, indicative only): "
                f"discordant {pair['a_pass_b_fail']}/{pair['b_pass_a_fail']}, "
                f"exact McNemar p = {pair['mcnemar_p_runs']}", "",
                f"Sample {sample}: {pair['a']} vs {pair['b']} - task level (primary): "
                f"discordant {pair['task_a_pass_b_fail']}/{pair['task_b_pass_a_fail']}, "
                f"exact McNemar p = {pair['mcnemar_p_tasks']}", ""])
        if pc["status"] == "paired_by_sample":
            delta = pc["task_pass_rate_delta_b_minus_a"]
            lines.extend([f"Mean paired task-pass difference (b minus a): {100 * delta['mean']:.2f} pp; "
                          f"sample range [{100 * delta['min']:.2f}, {100 * delta['max']:.2f}] pp. "
                          + pc["inference_note"], ""])
    if report["missing_results"]:
        lines.extend(["## Missing results (operational failures)", "",
                      "| label | task | seed | status | reason |", "|---|---|---|---|---|"])
        for row in report["missing_results"]:
            reason = row.get("failure", {}).get("reason", "not recorded")
            lines.append(f"| {row['label']} | {row['task']} | {row['seed']} | {row['status']} | {reason} |")
        lines.append("")
    return "\n".join(lines)


def write_report(runs_root: Path, out_dir: Path) -> Path:
    report = build_report(runs_root)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report, indent=2))
    md = out_dir / "report.md"
    md.write_text(render_markdown(report))
    return md
