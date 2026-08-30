#!/usr/bin/env python3
"""Drive a matrix of CLI agents through the frozen protocol in SPEC.md.

    python3 adapters/matrix_runner.py prep    --agents agents.json --agent NAME
    python3 adapters/matrix_runner.py author  --agents agents.json --agent NAME
    python3 adapters/matrix_runner.py grade   --agents agents.json --agent NAME
    python3 adapters/matrix_runner.py summary --agents agents.json

The agents file maps a condition name to how its CLI is invoked:

    {
      "myagent": {
        "command": ["some-cli", "--flag", "{prompt}"],
        "env": {"SOME_HOME": "{workdir}/home"},
        "label": "myagent-bare"
      }
    }

Placeholders: {prompt} - the frozen authoring prompt; {ws} - the task
workspace; {workdir} - the matrix work root. Workspaces are created
under <workdir>/ws-<agent>/<task>[__sN] outside the repository; graded
runs land in <repo>/runs/<label>[@sN]/.

Implementation notes (see SPEC.md "Isolation requirements"): agent
stdout/stderr are redirected to files, never pipes - agents spawn
background mock servers whose inherited pipes outlive them; timeouts
kill the whole process group and tolerate EPERM from already-reaped
groups; leftover workspace processes are reaped after every attempt.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
PROMPT = """\
Read brief.md and everything under manuals/ in the current directory, then \
write main.py in the current directory: one self-contained Python 3 program \
(standard library only) that performs the task described in the brief. The \
workspace may contain additional task files referenced by the brief.

Contract:
- main.py will be executed later, non-interactively, on a bench where the \
environment variables described in the brief (OSIC_ENDPOINTS, \
OSIC_RESULTS_DIR) are set. The graded run is the first and only execution of \
your code against the bench.
- No instrument is reachable while you author: writing local tests or mocks \
is allowed, but the bench itself cannot be probed.
- The program must exit on its own within the brief's time ceiling.

When done, leave your final program at ./main.py.
"""
AUTHOR_TIMEOUT_S = 2400  # SPEC.md: 40-minute authoring cap
GRADE_TIMEOUT_S = 420


def load_agents(path: Path) -> dict:
    return json.loads(Path(path).read_text())


def tasks(tasks_dir: Path):
    return sorted(p for p in tasks_dir.iterdir() if (p / "task.yaml").exists())


def ws_dir(workdir: Path, agent: str, task_id: str, sample: int) -> Path:
    suffix = "" if sample <= 1 else f"__s{sample}"
    return workdir / f"ws-{agent}" / f"{task_id}{suffix}"


def run_label(cfg: dict, agent: str, sample: int) -> str:
    base = cfg.get("label", agent)
    return base if sample <= 1 else f"{base}@s{sample}"


def _fill(value: str, ws: Path, workdir: Path) -> str:
    return (value.replace("{prompt}", PROMPT)
                 .replace("{ws}", str(ws))
                 .replace("{workdir}", str(workdir)))


def prep(args, agents):
    workdir = Path(args.workdir)
    for tdir in tasks(REPO / "tasks"):
        ws = ws_dir(workdir, args.agent, tdir.name, args.sample)
        if (ws / "main.py").exists() and not args.force:
            print(f"skip {ws}: main.py already present")
            continue
        if ws.exists():
            shutil.rmtree(ws)
        (ws / "manuals").mkdir(parents=True)
        shutil.copy(tdir / "brief.md", ws / "brief.md")
        cfg = yaml.safe_load((tdir / "task.yaml").read_text())
        for m in cfg.get("manuals", []):
            shutil.copy(REPO / "manuals" / m, ws / "manuals" / m)
        if (tdir / "rig").is_dir():
            shutil.copytree(tdir / "rig", ws / "rig")
        print(f"prepped {ws}")


def _author_one(agent: str, cfg: dict, task_id: str, workdir: Path, sample: int):
    ws = ws_dir(workdir, agent, task_id, sample)
    logs = workdir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    stem = f"{task_id}.{agent}.s{sample}"
    cmd = [_fill(c, ws, workdir) for c in cfg["command"]]
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
    env.update({k: _fill(v, ws, workdir) for k, v in cfg.get("env", {}).items()})
    t0 = time.time()
    for attempt in (1, 2):
        with open(logs / f"{stem}.log", "w") as lf, \
             open(logs / f"{stem}.stderr", "w") as ef:
            proc = subprocess.Popen(cmd, cwd=ws, env=env, stdout=lf,
                                    stderr=ef, stdin=subprocess.DEVNULL,
                                    start_new_session=True)
            try:
                proc.wait(timeout=AUTHOR_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                ef.write(f"\nTIMEOUT after {AUTHOR_TIMEOUT_S}s\n")
                try:
                    os.killpg(proc.pid, 15)
                    time.sleep(5)
                    os.killpg(proc.pid, 9)
                except (ProcessLookupError, PermissionError):
                    pass
        subprocess.run(["pkill", "-f", str(ws)], capture_output=True)
        if (ws / "main.py").exists():
            break
        if attempt == 1:
            time.sleep(20)  # infrastructure retry only (SPEC.md)
    return task_id, (ws / "main.py").exists(), time.time() - t0


def author(args, agents):
    cfg = agents[args.agent]
    workdir = Path(args.workdir)
    todo = [t.name for t in tasks(REPO / "tasks")]
    if args.skip_done:
        todo = [t for t in todo
                if not (ws_dir(workdir, args.agent, t, args.sample) / "main.py").exists()]
    print(f"[{args.agent} s{args.sample}] authoring {len(todo)} tasks "
          f"(parallel {args.jobs})", flush=True)
    with cf.ThreadPoolExecutor(args.jobs) as ex:
        futs = [ex.submit(_author_one, args.agent, cfg, t, workdir, args.sample)
                for t in todo]
        for fut in cf.as_completed(futs):
            task_id, ok, dt = fut.result()
            print(f"  {task_id}: {'main.py OK' if ok else 'NO main.py'} "
                  f"({dt:.0f}s)", flush=True)


def _grade_one(label: str, ws: Path, task_id: str, seed: int):
    out = REPO / "runs" / label / f"{task_id}_s{seed}"
    if (out / "grade.json").exists():
        return task_id, seed, json.loads((out / "grade.json").read_text())
    if out.exists():
        shutil.rmtree(out)
    cmd = [sys.executable, "-m", "osicbench.cli", "run",
           "--task", str(REPO / "tasks" / task_id), "--submission", str(ws),
           "--seed", str(seed), "--out", str(out), "--label", label]
    try:
        subprocess.run(cmd, cwd=REPO, capture_output=True, timeout=GRADE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return task_id, seed, None
    gpath = out / "grade.json"
    return task_id, seed, json.loads(gpath.read_text()) if gpath.exists() else None


def grade(args, agents):
    cfg = agents[args.agent]
    workdir = Path(args.workdir)
    label = run_label(cfg, args.agent, args.sample)
    seeds = [int(s) for s in args.seeds.split(",")]
    jobs = [(t.name, s) for t in tasks(REPO / "tasks") for s in seeds
            if (ws_dir(workdir, args.agent, t.name, args.sample) / "main.py").exists()]
    print(f"[{label}] grading {len(jobs)} runs (parallel {args.jobs})", flush=True)
    with cf.ThreadPoolExecutor(args.jobs) as ex:
        futs = [ex.submit(_grade_one, label,
                          ws_dir(workdir, args.agent, t, args.sample), t, s)
                for t, s in jobs]
        for fut in cf.as_completed(futs):
            task_id, seed, g = fut.result()
            if g is None:
                print(f"  {task_id} s{seed}: ERROR", flush=True)
            else:
                print(f"  {task_id} s{seed}: "
                      f"{'pass' if g.get('pass') else 'FAIL'} "
                      f"dfs={g.get('dfs', 0):.0f} hss={g.get('hss', 0):.0f}",
                      flush=True)


def summary(args, agents):
    subprocess.run([sys.executable, "-m", "osicbench.cli", "report",
                    "--runs", str(REPO / "runs"),
                    "--out", str(REPO / "runs" / "_report")], cwd=REPO)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["prep", "author", "grade", "summary"])
    ap.add_argument("--agents", required=True)
    ap.add_argument("--agent", default=None)
    ap.add_argument("--workdir", default="/tmp/osicbench-matrix")
    ap.add_argument("--sample", type=int, default=1,
                    help="authoring sample index (SPEC.md: k >= 3 for leaderboards)")
    ap.add_argument("--seeds", default="101,102,103,104,105")
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--skip-done", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    agents = load_agents(Path(args.agents))
    if args.cmd != "summary" and not args.agent:
        ap.error("--agent is required for prep/author/grade")
    {"prep": prep, "author": author, "grade": grade, "summary": summary}[args.cmd](args, agents)


if __name__ == "__main__":
    main()
