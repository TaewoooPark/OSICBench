"""Execute a submission against a task's farm and collect everything.

Mode A (one-shot): the farm starts only AFTER the submission is frozen -
the code was necessarily written blind, from the brief and manuals alone.
The runner then executes it once and grades what physically happened.

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
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from .taskspec import TaskSpec

REPO_ROOT = Path(__file__).resolve().parents[1]
FARM_READY_TIMEOUT_S = 15.0


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
    """The farm as a child process with clean TERM->snapshot shutdown."""

    def __init__(self, task: TaskSpec, seed: int, farm_dir: Path) -> None:
        self.farm_dir = Path(farm_dir)
        self.farm_dir.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "osicsim.farm",
             "--config", str(task.yaml_path), "--seed", str(seed),
             "--out", str(self.farm_dir)],
            env=env,
            stdout=open(self.farm_dir / "farm.out", "w"),
            stderr=subprocess.STDOUT,
        )

    def wait_ready(self) -> Path:
        endpoints = self.farm_dir / "endpoints.json"
        deadline = time.monotonic() + FARM_READY_TIMEOUT_S
        while time.monotonic() < deadline:
            if endpoints.exists() and endpoints.stat().st_size > 0:
                return endpoints
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"farm exited early (rc={self.proc.returncode}); "
                    f"see {self.farm_dir / 'farm.out'}"
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

    @property
    def recorder_path(self) -> Path:
        return self.farm_dir / "recorder.jsonl"


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
) -> RunResult:
    """Mode A execution: farm up -> run submission once -> farm down."""
    run_dir = Path(out_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    results_dir = run_dir / "results"
    results_dir.mkdir(exist_ok=True)
    main_py = _stage_submission(submission, run_dir / "submission")

    farm = FarmProcess(task, seed, run_dir / "farm")
    sigkilled = False
    killed_by_limit = False
    exit_code: Optional[int] = None
    t0 = time.monotonic()
    try:
        endpoints = farm.wait_ready()
        env = dict(os.environ)
        env["OSIC_ENDPOINTS"] = str(endpoints)
        env["OSIC_RESULTS_DIR"] = str(results_dir)
        env.pop("PYTHONPATH", None)  # submissions are self-contained
        agent = subprocess.Popen(
            [sys.executable, "-u", str(main_py)],
            cwd=str(run_dir / "submission"),
            env=env,
            stdout=open(run_dir / "agent.out", "w"),
            stderr=open(run_dir / "agent.err", "w"),
            start_new_session=True,
        )
        deadline = t0 + task.wall_clock_limit_s
        kill_at = None if task.sigkill_at_s is None else t0 + task.sigkill_at_s
        while True:
            rc = agent.poll()
            now = time.monotonic()
            if rc is not None:
                exit_code = rc
                break
            if kill_at is not None and now >= kill_at and not sigkilled:
                os.kill(agent.pid, signal.SIGKILL)  # main pid ONLY
                sigkilled = True
                kill_at = None
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
        try:  # sweep any orphaned children of the submission
            os.killpg(os.getpgid(0), 0)  # no-op guard; never kill our own group
        except Exception:
            pass
    wall = time.monotonic() - t0
    result = RunResult(run_dir, exit_code, killed_by_limit, sigkilled, wall)
    meta = {
        "task": task.id,
        "seed": seed,
        "label": label,
        "mode": "a",
        **result.meta,
    }
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    return result


class LiveSession:
    """Mode B: the agent works against live farms with a hard reset budget.

    Every reset restarts the farm with the SAME seed (identical hidden
    physics, fresh state) in a new attempt directory. The FINAL attempt is
    what gets graded.
    """

    def __init__(self, task: TaskSpec, seed: int, out_dir: Path) -> None:
        self.task = task
        self.seed = seed
        self.out_dir = Path(out_dir)
        self.attempt = 0
        self.max_attempts = 1 + task.mode_b_resets
        self.farm: Optional[FarmProcess] = None

    def attempt_dir(self) -> Path:
        return self.out_dir / f"attempt_{self.attempt:02d}"

    def start(self) -> Path:
        self.attempt += 1
        if self.attempt > self.max_attempts:
            raise RuntimeError(f"reset budget exhausted ({self.max_attempts} farms)")
        d = self.attempt_dir()
        (d / "results").mkdir(parents=True, exist_ok=True)
        self.farm = FarmProcess(self.task, self.seed, d / "farm")
        return self.farm.wait_ready()

    def reset(self) -> Path:
        if self.farm is not None:
            self.farm.stop()
        return self.start()

    def finish(self) -> Path:
        if self.farm is not None:
            self.farm.stop()
        meta = {
            "task": self.task.id,
            "seed": self.seed,
            "mode": "b",
            "attempts_used": self.attempt,
            "reset_budget": self.max_attempts,
        }
        (self.out_dir / "meta.json").write_text(json.dumps(meta, indent=2))
        return self.attempt_dir()
