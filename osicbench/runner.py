"""Execute a submission against a task's farm and collect everything.

Mode A (one-shot): the farm starts only AFTER the submission is frozen -
the code was necessarily written blind, from the brief and manuals alone.
The runner then executes it once and grades what physically happened.

Isolation of hidden state: the farm writes its flight recorder (which
carries seeded ground truth) into a PRIVATE temporary directory outside
the run tree. The submission is handed only a copy of ``endpoints.json``
(host/port/resource - nothing else) under the run directory. After the
farm stops, its files are collected into ``<run>/farm/`` for grading.
This is best-effort separation for a trust-based sandbox, not an OS
security boundary - see docs/anti-gaming.md; value-level fabrication
reconciliation is the backstop that makes copied ground truth detectable
regardless.

Run directories are single-use: a non-empty output directory is refused
(or wiped with ``overwrite=True``) so stale results and stale recorder
lines can never leak into a fresh grade.

SIGKILL scenario: when the task declares ``sigkill_at_s``, the runner
SIGKILLs the submission's MAIN process at that moment. Child processes
survive deliberately - a supervisor daemon is a legitimate defense, as is
configuring the instrument's own watchdog. Tasks that use this declare it
in their brief.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from .taskspec import TaskSpec

REPO_ROOT = Path(__file__).resolve().parents[1]
FARM_READY_TIMEOUT_S = 15.0


class RunDirError(RuntimeError):
    """Raised when an output directory would contaminate a fresh run."""


def prepare_run_dir(path: Path, overwrite: bool = False) -> Path:
    """Create a guaranteed-fresh run directory (absolute).

    Reusing a directory that already holds results or a recorder would let
    a previous run's artifacts grade a new submission; refuse unless the
    caller explicitly asks for a wipe.
    """
    p = Path(path).resolve()
    if p.exists() and any(p.iterdir()):
        if not overwrite:
            raise RunDirError(
                f"refusing to reuse non-empty run directory {p} "
                f"(pass overwrite=True / --overwrite to wipe it)"
            )
        shutil.rmtree(p)
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class RunResult:
    run_dir: Path
    exit_code: Optional[int]
    killed_by_limit: bool
    sigkilled: bool
    wall_s: float

    @property
    def meta(self) -> Dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "killed_by_limit": self.killed_by_limit,
            "sigkilled": self.sigkilled,
            "wall_s": round(self.wall_s, 3),
        }


class FarmProcess:
    """The farm as a child process with clean TERM->snapshot shutdown.

    The farm's working files (recorder.jsonl with ground truth, its own
    endpoints.json, farm.out) live in a private mkdtemp directory whose
    path the submission is never told. ``collect()`` moves them into the
    run tree once the farm has stopped.
    """

    def __init__(self, task: TaskSpec, seed: int) -> None:
        self.private_dir = Path(tempfile.mkdtemp(prefix="osic-farm-"))
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "osicsim.farm",
             "--config", str(task.yaml_path), "--seed", str(seed),
             "--out", str(self.private_dir)],
            env=env,
            stdout=open(self.private_dir / "farm.out", "w"),
            stderr=subprocess.STDOUT,
        )

    def wait_ready(self) -> Path:
        endpoints = self.private_dir / "endpoints.json"
        deadline = time.monotonic() + FARM_READY_TIMEOUT_S
        while time.monotonic() < deadline:
            if endpoints.exists() and endpoints.stat().st_size > 0:
                return endpoints
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"farm exited early (rc={self.proc.returncode}); "
                    f"see {self.private_dir / 'farm.out'}"
                )
            time.sleep(0.05)
        raise TimeoutError("farm did not become ready")

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=6.0)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3.0)

    def collect(self, dest: Path) -> Path:
        """Move the farm's files into the run tree (call after stop())."""
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        for name in ("recorder.jsonl", "endpoints.json", "farm.out"):
            src = self.private_dir / name
            if src.exists():
                shutil.move(str(src), str(dest / name))
        shutil.rmtree(self.private_dir, ignore_errors=True)
        return dest


def _stage_submission(submission: Path, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    submission = Path(submission)
    if submission.is_dir():
        for item in submission.iterdir():
            target = dest / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)
        main = dest / "main.py"
        if not main.exists():
            raise FileNotFoundError("directory submissions must contain main.py")
        return main
    target = dest / "main.py"
    shutil.copy2(submission, target)
    return target


def run_submission(
    task: TaskSpec,
    submission: Path,
    seed: int,
    out_dir: Path,
    label: str = "unlabeled",
    overwrite: bool = False,
) -> RunResult:
    """Mode A execution: farm up -> run submission once -> farm down."""
    run_dir = prepare_run_dir(out_dir, overwrite=overwrite)
    results_dir = run_dir / "results"
    results_dir.mkdir(exist_ok=True)
    main_py = _stage_submission(submission, run_dir / "submission")

    farm = FarmProcess(task, seed)
    sigkilled = False
    killed_by_limit = False
    restarted = False
    exit_code: Optional[int] = None
    t0 = time.monotonic()
    try:
        endpoints_src = farm.wait_ready()
        io_dir = run_dir / "io"
        io_dir.mkdir(exist_ok=True)
        endpoints = io_dir / "endpoints.json"
        shutil.copy2(endpoints_src, endpoints)
        env = dict(os.environ)
        env["OSIC_ENDPOINTS"] = str(endpoints)
        env["OSIC_RESULTS_DIR"] = str(results_dir)
        env.pop("PYTHONPATH", None)  # submissions are self-contained

        def spawn(mode: str) -> subprocess.Popen:
            return subprocess.Popen(
                [sys.executable, "-u", str(main_py)],
                cwd=str(run_dir / "submission"),
                env=env,
                stdout=open(run_dir / "agent.out", mode),
                stderr=open(run_dir / "agent.err", mode),
                start_new_session=True,
            )

        agent = spawn("w")
        deadline = t0 + task.wall_clock_limit_s
        kill_at = None if task.sigkill_at_s is None else t0 + task.sigkill_at_s
        restart_at: Optional[float] = None
        while True:
            rc = agent.poll()
            now = time.monotonic()
            if rc is not None:
                if restart_at is None:
                    exit_code = rc
                    break
                if now >= restart_at:
                    # Restart scenario: the SAME main.py runs again in the
                    # same working directory against the same farm - the
                    # submission's own checkpointing is what resumes it.
                    agent = spawn("a")
                    restarted = True
                    restart_at = None
                    continue
            if kill_at is not None and now >= kill_at and not sigkilled:
                os.kill(agent.pid, signal.SIGKILL)  # main pid ONLY
                sigkilled = True
                kill_at = None
                if task.restart_after_kill_s is not None:
                    restart_at = now + task.restart_after_kill_s
                continue
            if now >= deadline:
                killed_by_limit = True
                try:  # terminal cleanup: the whole session group
                    os.killpg(os.getpgid(agent.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                agent.wait(timeout=3.0)
                exit_code = agent.returncode
                break
            time.sleep(0.02)
        # Grace window: instrument-side watchdogs / surviving children act.
        time.sleep(task.post_exit_grace_s)
    finally:
        farm.stop()
        farm.collect(run_dir / "farm")
    wall = time.monotonic() - t0
    result = RunResult(run_dir, exit_code, killed_by_limit, sigkilled, wall)
    meta = {
        "task": task.id,
        "seed": seed,
        "label": label,
        "mode": "a",
        "restarted": restarted,
        **result.meta,
    }
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    return result


class LiveSession:
    """Mode B: the agent works against live farms with a hard reset budget.

    Every reset restarts the farm with the SAME seed (identical hidden
    physics, fresh state) in a new attempt directory. The FINAL attempt is
    what gets graded. Farm internals stay in a private directory per
    attempt; the attempt directory exposes only ``io/endpoints.json``.
    """

    def __init__(self, task: TaskSpec, seed: int, out_dir: Path,
                 overwrite: bool = False) -> None:
        self.task = task
        self.seed = seed
        self.out_dir = prepare_run_dir(out_dir, overwrite=overwrite)
        self.attempt = 0
        self.max_attempts = 1 + task.mode_b_resets
        self.farm: Optional[FarmProcess] = None

    def attempt_dir(self) -> Path:
        return self.out_dir / f"attempt_{self.attempt:02d}"

    def start(self) -> Path:
        if self.attempt + 1 > self.max_attempts:
            # Refuse BEFORE advancing: the last real attempt stays graded.
            raise RuntimeError(f"reset budget exhausted ({self.max_attempts} farms)")
        self.attempt += 1
        d = self.attempt_dir()
        (d / "results").mkdir(parents=True, exist_ok=True)
        (d / "io").mkdir(exist_ok=True)
        self.farm = FarmProcess(self.task, self.seed)
        endpoints_src = self.farm.wait_ready()
        endpoints = d / "io" / "endpoints.json"
        shutil.copy2(endpoints_src, endpoints)
        return endpoints

    def _collect_current(self) -> None:
        if self.farm is not None:
            self.farm.stop()
            self.farm.collect(self.attempt_dir() / "farm")
            self.farm = None

    def reset(self) -> Path:
        self._collect_current()
        return self.start()

    def finish(self) -> Path:
        self._collect_current()
        meta = {
            "task": self.task.id,
            "seed": self.seed,
            "mode": "b",
            "attempts_used": self.attempt,
            "reset_budget": self.max_attempts,
        }
        final = self.attempt_dir()
        final.mkdir(parents=True, exist_ok=True)
        (final / "meta.json").write_text(json.dumps(meta, indent=2))
        (self.out_dir / "meta.json").write_text(json.dumps(meta, indent=2))
        return final
