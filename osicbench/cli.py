"""osicbench command-line interface.

    osicbench run      --task tasks/t01_first_light --submission sol.py --seed 1 --out runs/x
    osicbench grade    --task tasks/t01_first_light --run runs/x --seed 1
    osicbench validate --tasks tasks [--task t01_first_light] [--seeds 2] [--jobs 4]
    osicbench report   --runs runs --out reports/
    osicbench live     --task tasks/t01_first_light --seed 1 --out runs/live1
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from .grading import grade_run
from .report import write_report
from .runner import LiveSession, run_submission
from .taskspec import discover_tasks, load_task


def _cmd_run(args) -> int:
    task = load_task(Path(args.task))
    run_submission(task, Path(args.submission), args.seed, Path(args.out),
                   label=args.label, overwrite=args.overwrite)
    grade = grade_run(task, args.seed, Path(args.out))
    print(json.dumps({k: grade[k] for k in ("pass", "dfs", "hss", "rs", "transactions")
                      if k in grade}, indent=2))
    return 0 if grade.get("pass") else 1


def _cmd_grade(args) -> int:
    task = load_task(Path(args.task))
    grade = grade_run(task, args.seed, Path(args.run))
    print(json.dumps(grade, indent=2))
    return 0 if grade.get("pass") else 1


def _one_validation(task_dir: str, sub: str, seed: int, expect_pass: bool, out_root: str) -> Tuple[str, str, int, bool, bool, float]:
    task = load_task(Path(task_dir))
    name = Path(sub).stem
    out = Path(out_root) / task.id / f"{name}_s{seed}"
    run_submission(task, Path(sub), seed, out,
                   label=("reference" if expect_pass else "mutant"), overwrite=True)
    grade = grade_run(task, seed, out)
    ok = bool(grade.get("pass")) == expect_pass
    return task.id, name, seed, bool(grade.get("pass")), ok, float(grade.get("dfs", 0.0))


def _cmd_validate(args) -> int:
    tasks = discover_tasks(Path(args.tasks))
    if args.task:
        tasks = [t for t in tasks if t.id == args.task]
        if not tasks:
            print(f"no such task: {args.task}", file=sys.stderr)
            return 2
    jobs: List[Tuple[str, str, int, bool]] = []
    for task in tasks:
        refs = task.references() if not args.mutants_only else []
        muts = task.mutants() if not args.refs_only else []
        if not refs and not args.mutants_only:
            print(f"WARNING {task.id}: no reference solutions", file=sys.stderr)
        for seed in range(1, args.seeds + 1):
            for ref in refs:
                jobs.append((str(task.task_dir), str(ref), seed, True))
            for mut in muts:
                jobs.append((str(task.task_dir), str(mut), seed, False))

    failures: List[str] = []
    results = []
    with cf.ProcessPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(_one_validation, td, sub, seed, exp, args.out)
                   for td, sub, seed, exp in jobs]
        for fut in cf.as_completed(futures):
            task_id, name, seed, passed, ok, dfs = fut.result()
            results.append((task_id, name, seed, passed, ok, dfs))
            status = "OK " if ok else "BAD"
            print(f"[{status}] {task_id:24s} {name:24s} seed={seed} pass={passed}")
            if not ok:
                failures.append(f"{task_id}/{name} seed={seed}: pass={passed}")

    n_ref = sum(1 for r in results if r[4])
    print(f"\nvalidation: {n_ref}/{len(results)} behaved as expected")

    # Seed-stability table: per-task DFS mean and CV across reference runs.
    by_task: dict = {}
    ref_names = {Path(r).stem for task in tasks for r in task.references()}
    for task_id, name, seed, passed, ok, dfs in results:
        if name in ref_names:
            by_task.setdefault(task_id, []).append(dfs)
    if by_task and args.seeds >= 2:
        print("\nreference DFS stability across seeds (mean / CV):")
        for task_id in sorted(by_task):
            xs = by_task[task_id]
            mean = sum(xs) / len(xs)
            var = sum((x - mean) ** 2 for x in xs) / len(xs)
            cv = (var ** 0.5) / mean if mean else float("inf")
            flag = "" if cv <= 0.05 else "  <-- CV above 5% gate"
            print(f"  {task_id:26s} {mean:7.2f} / {cv:6.2%}{flag}")
    if failures:
        print("FAILURES:")
        for f in failures:
            print(" -", f)
        return 1
    return 0


def _cmd_report(args) -> int:
    md = write_report(Path(args.runs), Path(args.out))
    print(md.read_text())
    print(f"\nwritten: {md}")
    return 0


def _cmd_live(args) -> int:
    task = load_task(Path(args.task))
    session = LiveSession(task, args.seed, Path(args.out), overwrite=args.overwrite)
    endpoints = session.start()
    print(f"farm up (attempt 1/{session.max_attempts}); endpoints: {endpoints}")
    print("commands: reset | done")
    for line in sys.stdin:
        cmd = line.strip().lower()
        if cmd == "reset":
            try:
                endpoints = session.reset()
                print(f"farm reset (attempt {session.attempt}/{session.max_attempts}); "
                      f"endpoints: {endpoints}")
            except RuntimeError as exc:
                print(f"refused: {exc}")
        elif cmd == "done":
            final = session.finish()
            print(f"final attempt dir: {final}")
            grade = grade_run(task, args.seed, final)
            print(json.dumps(grade, indent=2))
            return 0 if grade.get("pass") else 1
    session.finish()
    return 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="osicbench")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="execute one submission (mode A) and grade it")
    p.add_argument("--task", required=True)
    p.add_argument("--submission", required=True)
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--label", default="unlabeled")
    p.add_argument("--overwrite", action="store_true",
                   help="wipe a non-empty output directory instead of refusing")
    p.set_defaults(fn=_cmd_run)

    p = sub.add_parser("grade", help="re-grade an existing run directory")
    p.add_argument("--task", required=True)
    p.add_argument("--run", required=True)
    p.add_argument("--seed", type=int, required=True)
    p.set_defaults(fn=_cmd_grade)

    p = sub.add_parser("validate", help="references must pass, mutants must fail")
    p.add_argument("--tasks", default="tasks")
    p.add_argument("--task", default=None)
    p.add_argument("--seeds", type=int, default=1)
    p.add_argument("--jobs", type=int, default=4)
    p.add_argument("--out", default="runs/validate")
    p.add_argument("--refs-only", action="store_true", dest="refs_only")
    p.add_argument("--mutants-only", action="store_true", dest="mutants_only")
    p.set_defaults(fn=_cmd_validate)

    p = sub.add_parser("report", help="aggregate runs into a report")
    p.add_argument("--runs", required=True)
    p.add_argument("--out", default="reports")
    p.set_defaults(fn=_cmd_report)

    p = sub.add_parser("live", help="mode B session with a reset budget")
    p.add_argument("--task", required=True)
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--overwrite", action="store_true",
                   help="wipe a non-empty output directory instead of refusing")
    p.set_defaults(fn=_cmd_live)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
