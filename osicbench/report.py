"""Aggregate run directories into a report (markdown + json)."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List

from .stats import mcnemar_exact_p, summarize_pass


def collect_runs(runs_root: Path) -> List[Dict[str, Any]]:
    """Every directory containing meta.json + grade.json is one run."""
    out: List[Dict[str, Any]] = []
    for meta_path in sorted(Path(runs_root).rglob("meta.json")):
        run_dir = meta_path.parent
        grade_path = run_dir / "grade.json"
        if not grade_path.exists():
            continue
        meta = json.loads(meta_path.read_text())
        grade = json.loads(grade_path.read_text())
        out.append({"dir": str(run_dir), **meta, "grade": grade})
    return out


def build_report(runs_root: Path) -> Dict[str, Any]:
    runs = collect_runs(runs_root)
    by_label: Dict[str, List[dict]] = defaultdict(list)
    for r in runs:
        by_label[r.get("label", "unlabeled")].append(r)

    report: Dict[str, Any] = {"runs": len(runs), "conditions": {}}
    for label, items in sorted(by_label.items()):
        passes = [bool(r["grade"].get("pass")) for r in items]
        fabricated = sum(1 for r in items if r["grade"].get("fabricated"))
        dfs = [float(r["grade"].get("dfs", 0.0)) for r in items]
        hss = [float(r["grade"].get("hss", 0.0)) for r in items]
        txn = [int(r["grade"].get("transactions", 0)) for r in items]
        per_task: Dict[str, Any] = defaultdict(lambda: {"pass": 0, "total": 0})
        for r in items:
            slot = per_task[r["task"]]
            slot["total"] += 1
            slot["pass"] += 1 if r["grade"].get("pass") else 0
        report["conditions"][label] = {
            "pass": summarize_pass(passes),
            "fabricated_runs": fabricated,
            "dfs_mean": round(sum(dfs) / len(dfs), 2) if dfs else 0.0,
            "hss_mean": round(sum(hss) / len(hss), 2) if hss else 0.0,
            "transactions_median": sorted(txn)[len(txn) // 2] if txn else 0,
            "per_task": dict(sorted(per_task.items())),
        }

    labels = sorted(by_label)
    if len(labels) == 2:
        a, b = labels
        index = {}
        for r in by_label[a]:
            index[(r["task"], r["seed"])] = bool(r["grade"].get("pass"))
        b_only = a_only = 0
        for r in by_label[b]:
            key = (r["task"], r["seed"])
            if key not in index:
                continue
            pa, pb = index[key], bool(r["grade"].get("pass"))
            if pa and not pb:
                a_only += 1
            elif pb and not pa:
                b_only += 1
        report["paired_comparison"] = {
            "a": a, "b": b,
            "a_pass_b_fail": a_only, "b_pass_a_fail": b_only,
            "mcnemar_p": round(mcnemar_exact_p(a_only, b_only), 6),
        }
    return report


def render_markdown(report: Dict[str, Any]) -> str:
    lines: List[str] = ["# OSIC-Bench report", ""]
    lines.append(f"Total runs: {report['runs']}")
    lines.append("")
    lines.append("| condition | pass rate | 95% CI | DFS mean | HSS mean | fabricated | txn median |")
    lines.append("|---|---|---|---|---|---|---|")
    for label, c in report["conditions"].items():
        p = c["pass"]
        lines.append(
            f"| {label} | {p['passed']}/{p['total']} = {p['rate']:.0%} "
            f"| [{p['ci_lo']:.0%}, {p['ci_hi']:.0%}] "
            f"| {c['dfs_mean']} | {c['hss_mean']} | {c['fabricated_runs']} "
            f"| {c['transactions_median']} |"
        )
    lines.append("")
    for label, c in report["conditions"].items():
        lines.append(f"## {label} - per task")
        lines.append("")
        lines.append("| task | pass |")
        lines.append("|---|---|")
        for task, slot in c["per_task"].items():
            lines.append(f"| {task} | {slot['pass']}/{slot['total']} |")
        lines.append("")
    if "paired_comparison" in report:
        pc = report["paired_comparison"]
        lines.append("## Paired comparison")
        lines.append("")
        lines.append(
            f"{pc['a']} vs {pc['b']}: discordant {pc['a_pass_b_fail']}/{pc['b_pass_a_fail']}, "
            f"exact McNemar p = {pc['mcnemar_p']}"
        )
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
