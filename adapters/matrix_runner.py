#!/usr/bin/env python3
"""Drive a matrix of CLI agents through the frozen protocol in SPEC.md.

    python3 adapters/matrix_runner.py plan    --agents agents.json --samples 3
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
runs land in <runs-dir>/<label>[@sN]/. Use a fresh runs directory per
experiment; plan freezes every expected condition/sample/task/seed first.

Implementation notes (see SPEC.md "Isolation requirements"): agent
stdout/stderr are redirected to files, never pipes - agents spawn
background mock servers whose inherited pipes outlive them; timeouts
clean up tracked process groups, not workspace-name regex matches. This
adapter does not provide an OS security boundary or configure CLI isolation.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import yaml

from osicbench.report import validate_evaluation_manifest

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


def _write_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def _config_hash(agents: dict) -> str:
    return hashlib.sha256(json.dumps(agents, sort_keys=True).encode()).hexdigest()


def _benchmark_hash() -> str:
    digest = hashlib.sha256()
    for name in ("osicbench", "osicsim", "tasks", "manuals", "adapters"):
        for path in sorted((REPO / name).rglob("*")):
            if path.is_file() and path.suffix in {".py", ".yaml", ".md", ".json", ".csv"}:
                digest.update(str(path.relative_to(REPO)).encode() + b"\0")
                digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _safe_component(value: str) -> bool:
    return (isinstance(value, str) and bool(value) and value not in {".", ".."}
            and all(c.isalnum() or c in "_-.@" for c in value))


def _seeds(value: str) -> list[int]:
    seeds = [int(s) for s in value.split(",")]
    if not seeds or len(set(seeds)) != len(seeds):
        raise ValueError("seeds must be nonempty and unique")
    return seeds


def plan(args, agents):
    """Freeze the intended denominator before any authoring calls."""
    if args.samples < 1 or not agents:
        raise ValueError("plan requires agents and at least one sample")
    if Path(args.workdir).resolve().is_relative_to(REPO.resolve()):
        raise ValueError("authoring workspaces must be outside the benchmark repository")
    rows = []
    labels = set()
    for agent, cfg in agents.items():
        condition = cfg.get("label", agent)
        if not _safe_component(agent) or not _safe_component(condition):
            raise ValueError("agent names and labels must be safe path components")
        for sample in range(1, args.samples + 1):
            label = run_label(cfg, agent, sample)
            if label in {"evaluation_manifest.json", "authoring", "grading_logs", "_report"}:
                raise ValueError(f"reserved bookkeeping label: {label}")
            if label in labels:
                raise ValueError(f"duplicate run label: {label}")
            labels.add(label)
            rows.extend(dict(label=label, condition=condition, sample=sample,
                             task=t.name, seed=s)
                        for t in tasks(REPO / "tasks") for s in _seeds(args.seeds))
    if not rows:
        raise ValueError("cannot plan an empty task matrix")
    manifest = dict(schema_version=1, expected_runs=rows,
                    workdir=str(Path(args.workdir).resolve()),
                    agent_config_sha256=_config_hash(agents),
                    prompt_sha256=hashlib.sha256(PROMPT.encode()).hexdigest(),
                    author_timeout_s=AUTHOR_TIMEOUT_S, benchmark_sha256=_benchmark_hash())
    validate_evaluation_manifest(manifest)
    runs = Path(args.runs_dir)
    path = runs / "evaluation_manifest.json"
    if path.exists():
        if json.loads(path.read_text()) != manifest:
            raise ValueError("plan is frozen; use a fresh --runs-dir for a different matrix")
    else:
        if runs.exists() and any(runs.iterdir()):
            raise ValueError("plan requires an empty --runs-dir")
        if any((Path(args.workdir) / f"ws-{agent}").exists() for agent in agents):
            raise ValueError("plan requires fresh agent workspaces; choose a new --workdir")
        _write_json(path, manifest)
    print(f"planned {len(rows)} runs in {path}")


def _planned_rows(args, agents) -> list[dict]:
    path = Path(args.runs_dir) / "evaluation_manifest.json"
    if not path.exists():
        raise ValueError("run plan before prep/author/grade")
    manifest = json.loads(path.read_text())
    validated = validate_evaluation_manifest(manifest)
    if (manifest.get("schema_version") != 1
            or manifest.get("agent_config_sha256") != _config_hash(agents)
            or manifest.get("workdir") != str(Path(args.workdir).resolve())
            or manifest.get("prompt_sha256") != hashlib.sha256(PROMPT.encode()).hexdigest()
            or manifest.get("author_timeout_s") != AUTHOR_TIMEOUT_S
            or manifest.get("benchmark_sha256") != _benchmark_hash()):
        raise ValueError("configuration differs from the frozen plan")
    label = run_label(agents[args.agent], args.agent, args.sample)
    rows = [r for r in validated if r["label"] == label]
    if not rows or any(r["sample"] != args.sample or
                       r["condition"] != agents[args.agent].get("label", args.agent)
                       for r in rows):
        raise ValueError("agent/sample is not in the frozen plan")
    if set(r["seed"] for r in rows) != set(_seeds(args.seeds)):
        raise ValueError("seeds differ from the frozen plan")
    if set(r["task"] for r in rows) != {t.name for t in tasks(REPO / "tasks")}:
        raise ValueError("task set differs from the frozen plan")
    if {(r["task"], r["seed"]) for r in rows} != {
            (t.name, s) for t in tasks(REPO / "tasks") for s in _seeds(args.seeds)}:
        raise ValueError("task/seed coverage differs from the frozen plan")
    return rows


def _artifact_hash(ws: Path) -> str | None:
    paths = sorted(ws.rglob("*"))
    if ws.is_symlink() or any(path.is_symlink() for path in paths):
        raise ValueError(f"self-contained submission must not contain symlinks: {ws}")
    if not (ws / "main.py").is_file():
        return None
    digest = hashlib.sha256()
    for path in (p for p in paths if p.is_file()):
        digest.update(str(path.relative_to(ws)).encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _terminate_tree(proc, grace_s: float = 0.2):
    """Clean up the owned group and still-parented separate-session children.

    Detached/reparented daemons require evaluator-managed OS containment;
    process-name matching is neither safe cleanup nor an isolation boundary.
    """
    groups = {proc.pid}
    listing = subprocess.run(["ps", "-e", "-o", "pid=", "-o", "ppid="],
                             capture_output=True, text=True, check=True)
    parents = {int(pid): int(ppid) for pid, ppid in
               (line.split() for line in listing.stdout.splitlines())}
    descendants = {proc.pid}
    while True:
        expanded = descendants | {pid for pid, parent in parents.items()
                                  if parent in descendants}
        if expanded == descendants:
            break
        descendants = expanded
    for pid in descendants:
        try:
            pgid = os.getpgid(pid)
            if pgid in descendants:
                groups.add(pgid)
        except ProcessLookupError:
            pass
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pgid in groups:
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                pass
            except PermissionError:
                # macOS may report EPERM for an already-dead group. Verify
                # that it has no live members; never suppress a live denial.
                listing = subprocess.run(["ps", "-e", "-o", "pgid=", "-o", "stat="],
                                         capture_output=True, text=True, check=True)
                if any(int(group) == pgid and not state.startswith("Z")
                       for group, state in (line.split() for line in listing.stdout.splitlines())):
                    raise
        if sig == signal.SIGTERM:
            time.sleep(grace_s)
    proc.wait(timeout=5)


def _run_logged(cmd, *, cwd, env, stdout, stderr, timeout):
    t0 = time.monotonic()
    record = dict(exit_code=None, timed_out=False)
    proc = None
    stdout.parent.mkdir(parents=True, exist_ok=True)
    with stdout.open("w") as lf, stderr.open("w") as ef:
        try:
            proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=lf, stderr=ef,
                                    stdin=subprocess.DEVNULL, start_new_session=True)
            record["exit_code"] = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            record["timed_out"] = True
        except OSError as exc:
            record["error"] = str(exc)
        finally:
            if proc is not None:
                try:
                    _terminate_tree(proc)
                except (OSError, subprocess.SubprocessError) as exc:
                    record["cleanup_error"] = str(exc)
    record["wall_s"] = round(time.monotonic() - t0, 3)
    return record


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
    _planned_rows(args, agents)
    workdir = Path(args.workdir)
    for tdir in tasks(REPO / "tasks"):
        ws = ws_dir(workdir, args.agent, tdir.name, args.sample)
        record = (Path(args.runs_dir) / "authoring" /
                  run_label(agents[args.agent], args.agent, args.sample) / f"{tdir.name}.json")
        if record.exists():
            if args.force:
                raise ValueError("cannot replace a frozen authoring attempt; use a new experiment")
            print(f"skip {ws}: authoring already attempted")
            continue
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


def _author_one(agent: str, cfg: dict, task_id: str, workdir: Path, sample: int,
                runs_dir: Path):
    ws = ws_dir(workdir, agent, task_id, sample)
    logs = runs_dir / "authoring" / run_label(cfg, agent, sample)
    logs.mkdir(parents=True, exist_ok=True)
    stem = f"{task_id}.{agent}.s{sample}"
    cmd = [_fill(c, ws, workdir) for c in cfg["command"]]
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
    env.update({k: _fill(v, ws, workdir) for k, v in cfg.get("env", {}).items()})
    t0 = time.monotonic()
    record = dict(task=task_id, label=run_label(cfg, agent, sample), sample=sample,
                  attempts=[])
    for attempt in (1, 2):
        record.update(status="running", artifact_present=False, artifact_sha256=None)
        record["attempts"].append(dict(attempt=attempt, status="running"))
        _write_json(logs / f"{task_id}.json", record)
        result = _run_logged(cmd, cwd=ws, env=env,
                             stdout=logs / f"{stem}.attempt{attempt}.log",
                             stderr=logs / f"{stem}.attempt{attempt}.stderr",
                             timeout=AUTHOR_TIMEOUT_S)
        result["attempt"] = attempt
        record["attempts"][-1] = result
        record["artifact_sha256"] = _artifact_hash(ws)
        record["artifact_present"] = record["artifact_sha256"] is not None
        record["wall_s"] = round(time.monotonic() - t0, 3)
        record["status"] = ("completed" if record["artifact_present"] or attempt == 2
                            else "retry_pending")
        _write_json(logs / f"{task_id}.json", record)
        if result.get("cleanup_error"):
            raise RuntimeError(f"author cleanup failed: {result['cleanup_error']}")
        if record["artifact_present"]:
            break
        if attempt == 1:
            time.sleep(20)  # infrastructure retry only (SPEC.md)
    return task_id, record["artifact_present"], record["wall_s"]


def author(args, agents):
    _planned_rows(args, agents)
    cfg = agents[args.agent]
    workdir = Path(args.workdir)
    todo = [t.name for t in tasks(REPO / "tasks")]
    records = Path(args.runs_dir) / "authoring" / run_label(cfg, args.agent, args.sample)
    for task_id in todo:
        path = records / f"{task_id}.json"
        if path.exists() and json.loads(path.read_text()).get("status") in {"running", "retry_pending"}:
            raise ValueError(f"unresolved authoring attempt; inspect before resuming: {task_id}")
        if ((ws_dir(workdir, args.agent, task_id, args.sample) / "main.py").exists()
                and not (records / f"{task_id}.json").exists()):
            raise ValueError(f"unrecorded artifact in workspace: {task_id}")
    attempted = {t for t in todo if (records / f"{t}.json").exists()}
    if attempted and not args.skip_done:
        raise ValueError("authoring already attempted; use --skip-done to resume remaining tasks")
    todo = [t for t in todo if t not in attempted]
    print(f"[{args.agent} s{args.sample}] authoring {len(todo)} tasks "
          f"(parallel {args.jobs})", flush=True)
    with cf.ThreadPoolExecutor(args.jobs) as ex:
        futs = [ex.submit(_author_one, args.agent, cfg, t, workdir, args.sample,
                          Path(args.runs_dir))
                for t in todo]
        for fut in cf.as_completed(futs):
            task_id, ok, dt = fut.result()
            print(f"  {task_id}: {'main.py OK' if ok else 'NO main.py'} "
                  f"({dt:.0f}s)", flush=True)


def _grade_one(label: str, ws: Path, task_id: str, seed: int, runs_dir: Path):
    out = runs_dir / label / f"{task_id}_s{seed}"
    fingerprint = _artifact_hash(ws)
    identity = dict(label=label, task=task_id, seed=seed)
    author_path = runs_dir / "authoring" / label / f"{task_id}.json"
    if author_path.exists():
        author_record = json.loads(author_path.read_text())
        if author_record.get("status") in {"running", "retry_pending"}:
            raise ValueError(f"unresolved authoring attempt: {author_path}")
        if author_record.get("artifact_sha256") != fingerprint:
            raise ValueError(f"artifact changed after authoring: {ws}")
        if any(r.get("cleanup_error") for r in author_record.get("attempts", [])):
            raise ValueError(f"unresolved author cleanup failure: {author_path}")
    elif fingerprint is not None:
        raise ValueError(f"artifact has no authoring provenance: {ws}")
    if (out / "grade.json").exists():
        adapter_path = out / "adapter.json"
        meta_path = out / "meta.json"
        if ((out / "failure.json").exists() or not adapter_path.exists() or not meta_path.exists()
                or json.loads(adapter_path.read_text()).get("artifact_sha256") != fingerprint
                or any(json.loads(meta_path.read_text()).get(k) != v
                       for k, v in identity.items())):
            raise ValueError(f"refusing unverified/stale cached grade: {out}")
        cached = json.loads(adapter_path.read_text())
        if cached.get("cleanup_error") or cached.get("timed_out") or cached.get("error"):
            raise ValueError(f"unresolved grading infrastructure failure: {out}")
        return task_id, seed, json.loads((out / "grade.json").read_text())
    if out.exists() and any(out.iterdir()):
        # Keep failed attempts single-use: retry policy must be explicit.
        if (out / "failure.json").exists():
            return task_id, seed, None
        raise ValueError(f"incomplete run retained; inspect before retrying: {out}")
    if fingerprint is None:
        _write_json(out / "failure.json", dict(**identity, stage="authoring",
                                               reason="missing_author_artifact"))
        return task_id, seed, None
    cmd = [sys.executable, "-m", "osicbench.cli", "run",
           "--task", str(REPO / "tasks" / task_id), "--submission", str(ws),
           "--seed", str(seed), "--out", str(out), "--label", label]
    logs = runs_dir / "grading_logs" / label
    record = _run_logged(cmd, cwd=REPO, env=None,
                         stdout=logs / f"{task_id}_s{seed}.log",
                         stderr=logs / f"{task_id}_s{seed}.stderr",
                         timeout=GRADE_TIMEOUT_S)
    record.update(identity, artifact_sha256=fingerprint)
    _write_json(out / "adapter.json", record)
    gpath = out / "grade.json"
    if (not gpath.exists() or record.get("cleanup_error")
            or record.get("timed_out") or record.get("error")):
        reason = "grading_timeout" if record["timed_out"] else "grading_error"
        _write_json(out / "failure.json", dict(**identity, stage="grading", reason=reason))
        if gpath.exists() or record.get("cleanup_error"):
            raise RuntimeError(f"grading infrastructure failure; artifacts retained: {out}")
        return task_id, seed, None
    return task_id, seed, json.loads(gpath.read_text())


def grade(args, agents):
    rows = _planned_rows(args, agents)
    cfg = agents[args.agent]
    workdir = Path(args.workdir)
    label = run_label(cfg, args.agent, args.sample)
    jobs = [(r["task"], r["seed"]) for r in rows]
    print(f"[{label}] grading {len(jobs)} runs (parallel {args.jobs})", flush=True)
    with cf.ThreadPoolExecutor(args.jobs) as ex:
        futs = [ex.submit(_grade_one, label,
                          ws_dir(workdir, args.agent, t, args.sample), t, s,
                          Path(args.runs_dir))
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
                    "--runs", str(Path(args.runs_dir)),
                    "--out", str(Path(args.runs_dir) / "_report")], cwd=REPO, check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["plan", "prep", "author", "grade", "summary"])
    ap.add_argument("--agents", required=True)
    ap.add_argument("--agent", default=None)
    ap.add_argument("--workdir", default="/tmp/osicbench-matrix")
    ap.add_argument("--runs-dir", default=str(REPO / "runs"))
    ap.add_argument("--samples", type=int, default=3,
                    help="number of authoring samples to freeze with plan")
    ap.add_argument("--sample", type=int, default=1,
                    help="authoring sample index (SPEC.md: k >= 3 for leaderboards)")
    ap.add_argument("--seeds", default="101,102,103,104,105")
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--skip-done", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    args.workdir = str(Path(args.workdir).resolve())
    args.runs_dir = str(Path(args.runs_dir).resolve())
    agents = load_agents(Path(args.agents))
    if args.cmd in {"prep", "author", "grade"} and args.agent not in agents:
        ap.error("--agent is required for prep/author/grade")
    if args.sample < 1 or args.jobs < 1:
        ap.error("--sample and --jobs must be positive")
    {"plan": plan, "prep": prep, "author": author, "grade": grade,
     "summary": summary}[args.cmd](args, agents)


if __name__ == "__main__":
    main()
