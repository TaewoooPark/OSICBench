"""Local subscription evaluation runtime with isolated task workspaces."""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path

from adapters import matrix_runner
from .transport import TransportProxy


REPO = Path(__file__).resolve().parents[2]
PROVIDERS = {"openai": "codex_adapter", "anthropic": "claude_adapter"}
EXECUTION_SOURCES = ("runtime.py", "runner.py", "preflight.py", "transport.py",
                     "subscription_guard.py", "codex_adapter.py", "claude_adapter.py")


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def provider_module(provider: str):
    return importlib.import_module(f"experiments.model_matrix.{PROVIDERS[provider]}")


def source_hash() -> str:
    digest = hashlib.sha256(matrix_runner._benchmark_hash().encode())
    # Publication formatting is separately fingerprinted by its exporter and
    # cannot change prompts, submissions, task physics, or per-run grading.
    for name in sorted(EXECUTION_SOURCES):
        path = Path(__file__).parent / name
        digest.update(path.name.encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def clock_discontinuity(wall_elapsed: float, active_elapsed: float) -> bool:
    """Reject suspend or clock jumps that invalidate an authoring duration."""
    return abs(wall_elapsed - active_elapsed) > 2.0


def build_profile(workspace: Path, runtime_dir: Path, evaluation_root: Path, proxy_port: int) -> str:
    """Deny unrelated task/configuration trees and allow only this call's files.

    The entire client and every descendant can reach only a local CONNECT
    gateway whose destinations are limited to account and inference services.
    """
    if platform.system() != "Darwin" or shutil.which("sandbox-exec") is None:
        raise RuntimeError("this evaluated runtime requires macOS sandbox-exec")
    denied = [REPO, REPO.parent.parent, evaluation_root,
              Path.home() / "Documents", Path.home() / ".codex", Path.home() / ".agents"]
    denied.extend(Path.home() / ".claude" / name
                  for name in ("skills", "plugins", "agents", "projects", "commands"))
    if not 1 <= proxy_port <= 65535:
        raise ValueError("invalid proxy port")
    lines = ["(version 1)", "(allow default)", "(deny network*)",
             f'(allow network-outbound (remote tcp "localhost:{proxy_port}"))']
    for path in dict.fromkeys(p.resolve() for p in denied if p.is_dir()):
        lines.append(f"(deny file-read* file-write* (subpath {json.dumps(str(path))}))")
    for name in ("AGENTS.md", "CLAUDE.md", ".agents/AGENTS.md", ".codex/AGENTS.md",
                 ".claude/CLAUDE.md", ".claude/settings.json", ".claude/settings.local.json",
                 ".zshrc", ".zprofile", ".zshenv", ".bashrc", ".bash_profile", ".profile"):
        path = Path.home() / name
        if path.exists():
            lines.append(f"(deny file-read* file-write* (literal {json.dumps(str(path.resolve()))}))")
    for path in (workspace, runtime_dir):
        lines.append(f"(allow file-read* file-write* (subpath {json.dumps(str(path.resolve()))}))")
        for parent in path.resolve().parents:
            lines.append(f"(allow file-read-metadata (literal {json.dumps(str(parent))}))")
    return "\n".join(lines) + "\n"


def process_call(command: list[str], *, workspace: Path, env: dict,
                 prompt: str, log_dir: Path, timeout_s: float) -> dict:
    """Persist logs without pipe inheritance deadlocks; never retry content."""
    log_dir.mkdir(parents=True, exist_ok=False)
    prompt_path = log_dir / "prompt.txt"
    prompt_path.write_text(prompt)
    record = {"status": "running", "exit_code": None, "timed_out": False,
              "command": command, "cwd": str(workspace)}
    record["started_epoch_s"] = time.time()
    write_json(log_dir / "process.json", record)
    started = time.monotonic()
    proc = None
    with prompt_path.open() as stdin, (log_dir / "stdout.jsonl").open("w") as stdout, \
            (log_dir / "stderr.log").open("w") as stderr:
        try:
            proc = subprocess.Popen(command, cwd=workspace, env=env, stdin=stdin,
                                    stdout=stdout, stderr=stderr, start_new_session=True)
            record["pid"] = proc.pid
            write_json(log_dir / "process.json", record)
            record["exit_code"] = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            record["timed_out"] = True
        except OSError as exc:
            record["launch_error"] = type(exc).__name__
        finally:
            if proc is not None:
                try:
                    matrix_runner._terminate_tree(proc)
                except (OSError, subprocess.SubprocessError) as exc:
                    record["cleanup_error"] = type(exc).__name__
    active_elapsed = time.monotonic() - started
    record.update(status="completed", wall_s=round(active_elapsed, 3),
                  finished_epoch_s=time.time())
    wall_elapsed = record["finished_epoch_s"] - record["started_epoch_s"]
    record["epoch_elapsed_s"] = round(wall_elapsed, 3)
    record["clock_discontinuity"] = clock_discontinuity(wall_elapsed, active_elapsed)
    write_json(log_dir / "process.json", record)
    return record


def execute(condition: dict, *, workspace: Path, runtime_dir: Path,
            log_dir: Path, evaluation_root: Path, prompt: str,
            timeout_s: float = matrix_runner.AUTHOR_TIMEOUT_S) -> dict:
    module = provider_module(condition["provider"])
    runtime_dir.mkdir(parents=True, exist_ok=False)
    runtime_dir.chmod(0o700)
    prepared = module.prepare_runtime(runtime_dir)
    env = dict(os.environ)
    for key in prepared.get("unset_env", []):
        env.pop(key, None)
    for key in list(env):
        if key.startswith(("OSIC_", "PYTHONPATH", "PYTHONHOME")):
            env.pop(key, None)
    env.update(prepared.get("env", {}))
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    command = module.build_command(condition["model"], condition["effort"], runtime_dir, workspace)
    command[0] = shutil.which(command[0]) or command[0]
    with TransportProxy(condition["provider"]) as proxy:
        env.update(proxy.environment())
        profile_path = runtime_dir / "containment.sb"
        profile_path.write_text(build_profile(workspace, runtime_dir, evaluation_root, proxy.port))
        wrapped = ["/usr/bin/sandbox-exec", "-f", str(profile_path), *command]
        result = process_call(wrapped, workspace=workspace, env=env, prompt=prompt,
                              log_dir=log_dir, timeout_s=timeout_s)
        result["transport_audit"] = proxy.audit()
    kwargs = {"runtime_dir": runtime_dir} if condition["provider"] == "openai" else {}
    audit = module.inspect_trace(log_dir / "stdout.jsonl", log_dir / "stderr.log",
                                 condition["model"], condition["effort"],
                                 allow_incomplete=result["timed_out"], **kwargs)
    audit.setdefault("requested_model", condition["model"])
    audit.setdefault("requested_effort", condition["effort"])
    if isinstance(audit.get("resolved"), dict):
        audit.setdefault("resolved_model", audit["resolved"].get("model"))
        audit.setdefault("resolved_effort", audit["resolved"].get("effort"))
    if audit.get("init_cwd") and Path(audit["init_cwd"]).resolve() != workspace.resolve():
        audit["errors"].append("workspace_mismatch")
        audit["valid"] = False
    if result["clock_discontinuity"]:
        audit["errors"].append("host_suspend_or_clock_discontinuity")
        audit["valid"] = False
    result["provider_audit"] = audit
    result["artifact_present"] = (workspace / "main.py").is_file()
    result["artifact_sha256"] = matrix_runner._artifact_hash(workspace)
    write_json(log_dir / "audit.json", result)
    return result
