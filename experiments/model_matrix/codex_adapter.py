"""Subscription-authenticated Codex baseline for externally contained runs.

Private rollout files are retained only to verify the CLI's effective model
and effort. They are not proof of a server-side model snapshot. The caller
must provide filesystem and network containment and keep the runtime out of
publications. Native Codex sandboxing is disabled to avoid nested Seatbelt
failures; this adapter must never launch outside the evaluator's OS sandbox.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path


SYSTEM_SKILLS = (
    "imagegen", "openai-docs", "plugin-creator", "review-agent",
    "skill-creator", "skill-installer",
)
DISABLED_FEATURES = (
    "apps", "plugins", "hooks", "memories", "multi_agent", "skill_search",
    "skill_mcp_dependency_install", "remote_plugin", "recommended_plugins",
    "tool_suggest", "browser_use", "browser_use_external", "in_app_browser",
    "computer_use", "image_generation", "workspace_dependencies", "goals",
    "shell_snapshot",
)
EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
TOKEN_FIELDS = (
    "input_tokens", "cached_input_tokens", "output_tokens",
    "reasoning_output_tokens", "total_tokens",
)


def _settings(runtime_dir: Path) -> list[str]:
    codex_home = runtime_dir.resolve() / "codex_home"
    # CLI 0.147 requires the SKILL.md path, not its containing directory.
    disabled = ", ".join(
        "{path=" + json.dumps(str(codex_home / "skills/.system" / name / "SKILL.md"))
        + ",enabled=false}" for name in SYSTEM_SKILLS
    )
    return [
        'forced_login_method="chatgpt"', 'cli_auth_credentials_store="file"',
        'model_provider="openai"', 'web_search="disabled"',
        "project_doc_max_bytes=0", "project_doc_fallback_filenames=[]",
        'personality="none"', "mcp_servers={}", f"skills.config=[{disabled}]",
        'shell_environment_policy.inherit="core"',
        "shell_environment_policy.experimental_use_profile=false",
        "shell_environment_policy.ignore_default_excludes=false",
        *[f"features.{feature}=false" for feature in DISABLED_FEATURES],
    ]


def prepare_runtime(runtime_dir: Path) -> dict:
    """Create a fresh private, auth-only runtime without editing global state.

    Apply unset_env first, then env, when constructing the process environment.
    Credentials are never returned. Each runtime is single-use.
    """
    runtime = Path(runtime_dir).resolve()
    source_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    source = source_home.resolve() / "auth.json"
    if (runtime.is_relative_to(source_home.resolve())
            or source_home.resolve().is_relative_to(runtime)):
        raise ValueError("Runtime and the existing credential store must not overlap")
    auth = json.loads(source.read_text())
    if auth.get("auth_mode") != "chatgpt":
        raise ValueError("A stored ChatGPT subscription login is required")
    tokens = auth.get("tokens")
    if not isinstance(tokens, dict) or not tokens.get("access_token"):
        raise ValueError("The ChatGPT login has no usable cached access token")
    if runtime.exists() and any(runtime.iterdir()):
        raise ValueError("Codex runtime must be empty and single-use")
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    runtime.chmod(0o700)
    for name in ("codex_home", "home", "tmp", "xdg_config", "xdg_cache"):
        (runtime / name).mkdir(mode=0o700)
    auth_path = runtime / "codex_home/auth.json"
    private_auth = {key: auth[key] for key in ("auth_mode", "tokens", "last_refresh")
                    if key in auth}
    fd = os.open(auth_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(private_auth, stream)
    config_path = runtime / "codex_home/config.toml"
    fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write("\n".join(_settings(runtime)) + "\n")
    prefixes = ("OPENAI_", "AZURE_OPENAI_", "CODEX_", "CHATGPT_", "CLAUDE_",
                "ANTHROPIC_", "AWS_", "GEMINI_")
    explicit = {"GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "HTTP_PROXY",
                "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"}
    unset = sorted({key for key in os.environ if key.startswith(prefixes)} | explicit)
    return {
        "env": {
            "CODEX_HOME": str(runtime / "codex_home"), "HOME": str(runtime / "home"),
            "TMPDIR": str(runtime / "tmp"), "XDG_CONFIG_HOME": str(runtime / "xdg_config"),
            "XDG_CACHE_HOME": str(runtime / "xdg_cache"),
        },
        "unset_env": unset,
        "auth_method": "chatgpt",
        "provenance_policy": "private_rollout_cli_effective_context",
    }


def build_command(model: str, effort: str, runtime_dir: Path, workspace: Path) -> list[str]:
    """Build a stdin command for an already filesystem/network-contained process.

    The native sandbox is disabled, not the caller's outer OS boundary. Do not
    execute this command without that boundary. Approval bypass aliases are
    deliberately not used; unattended denials remain configured explicitly.
    """
    if effort not in EFFORTS:
        raise ValueError("Unknown reasoning effort")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", model):
        raise ValueError("Invalid model identifier")
    executable = shutil.which("codex")
    if executable is None:
        raise FileNotFoundError("Codex CLI is not installed")
    command = [executable, "exec", "--ignore-user-config", "--ignore-rules",
               "--strict-config", "--skip-git-repo-check", "--json", "--color", "never",
               "--sandbox", "danger-full-access", "--model", model,
               "--cd", str(Path(workspace).resolve())]
    for setting in [*_settings(Path(runtime_dir)), 'approval_policy="never"',
                    "model_reasoning_effort=" + json.dumps(effort)]:
        command.extend(["-c", setting])
    # Keep private session files: exec JSON alone may omit effective context.
    command.append("-")
    return command


def _json_lines(path: Path):
    if not path.exists():
        return
    with path.open(errors="replace") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                yield None
                continue
            yield value if isinstance(value, dict) else None


def _token_values(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in TOKEN_FIELDS
            if type(value.get(key)) is int and value[key] >= 0}


def inspect_trace(stdout_path: Path, stderr_path: Path, model: str, effort: str,
                  *, runtime_dir: Path | None = None, allow_incomplete: bool = False) -> dict:
    """Validate observed completion and context without returning prompt text.

    Missing effective-model evidence fails closed. Requested flags are never
    substituted for observations. stdout/stderr and private runtime files must
    all belong to this single invocation. allow_incomplete permits a trace
    stopped at an external deadline; it relaxes only turn-completion evidence,
    never model, effort, authentication, or customization checks.
    """
    stdout_path, stderr_path = Path(stdout_path), Path(stderr_path)
    stderr = stderr_path.read_text(errors="replace") if stderr_path.exists() else ""
    errors, contexts, sources = [], [], set()
    completed = failed = limited = custom_context = external_tool = False
    event_count = malformed = 0
    thread_ids, totals = set(), {}
    rate_details = []
    for event in _json_lines(stdout_path):
        if event is None:
            malformed += 1
            continue
        event_count += 1
        kind = event.get("type")
        if kind == "thread.started" and isinstance(event.get("thread_id"), str):
            thread_ids.add(event["thread_id"])
        if kind == "turn.completed":
            completed = True
            for key, value in _token_values(event.get("usage")).items():
                totals[key] = totals.get(key, 0) + value
        if kind in {"turn.failed", "error"}:
            failed = True
            text = json.dumps(event).lower()
            limited |= any(word in text for word in ("rate_limit", "rate limit", "usage limit", "quota"))
        if kind in {"turn_context", "session_context"}:
            payload = event.get("payload", event)
            if payload.get("model") and (payload.get("effort") or payload.get("reasoning_effort")):
                contexts.append((payload["model"], payload.get("effort", payload.get("reasoning_effort"))))
                sources.add("stdout_cli_effective_context")
        item = event.get("item", {})
        if isinstance(item, dict) and item.get("type") in {
                "mcp_tool_call", "web_search", "web_search_call", "collab_agent_tool_call"}:
            external_tool = True

    if runtime_dir is None:
        candidate = stdout_path.parent / "runtime"
        runtime_dir = candidate if (candidate / "codex_home").is_dir() else None
    auth_method = None
    runtime_contexts = 0
    if runtime_dir is not None:
        codex_home = Path(runtime_dir) / "codex_home"
        auth_path = codex_home / "auth.json"
        if auth_path.exists():
            auth_method = json.loads(auth_path.read_text()).get("auth_mode")
        for path in sorted((codex_home / "sessions").rglob("*.jsonl")):
            selected = False
            for event in _json_lines(path):
                if event is None:
                    continue
                kind, payload = event.get("type"), event.get("payload", {})
                if not isinstance(payload, dict):
                    continue
                if kind == "session_meta":
                    selected = payload.get("id") in thread_ids
                if not selected:
                    continue
                if kind == "turn_context" and payload.get("model") and payload.get("effort"):
                    contexts.append((payload["model"], payload["effort"]))
                    sources.add("private_rollout_cli_effective_turn_context")
                    runtime_contexts += 1
                if kind == "response_item" and payload.get("role") in {"developer", "user"}:
                    encoded = json.dumps(payload)
                    custom_context |= any(marker in encoded for marker in (
                        "<skills_instructions>", "<user_instructions>", "# AGENTS.md instructions"))
                if kind == "event_msg" and payload.get("type") == "token_count":
                    limits = payload.get("rate_limits")
                    if isinstance(limits, dict):
                        rate_details.append({name: value for name, value in limits.items()
                                             if name in {"primary", "secondary", "credits", "plan_type"}})
    banner_model = re.search(r"^model:\s*(\S+)\s*$", stderr, re.MULTILINE)
    banner_effort = re.search(r"^reasoning effort:\s*(\S+)\s*$", stderr, re.MULTILINE)
    if "OpenAI Codex v" in stderr and banner_model and banner_effort:
        contexts.append((banner_model.group(1), banner_effort.group(1)))
        sources.add("stderr_cli_startup_banner")
    observed_models = sorted({str(value[0]) for value in contexts})
    observed_efforts = sorted({str(value[1]) for value in contexts})
    if observed_models != [model]:
        errors.append("model_unverified" if not observed_models else "model_mismatch")
    if observed_efforts != [effort]:
        errors.append("effort_unverified" if not observed_efforts else "effort_mismatch")
    if auth_method != "chatgpt":
        errors.append("subscription_auth_unverified")
    if not completed and not allow_incomplete:
        errors.append("no_completed_turn")
    if failed:
        errors.append("provider_error_or_failed_turn")
    if malformed:
        errors.append("malformed_stdout_json")
    if len(thread_ids) != 1:
        errors.append("thread_identity_unverified")
    if custom_context:
        errors.append("unexpected_custom_instructions")
    if external_tool:
        errors.append("unexpected_external_or_delegated_tool")
    limited |= any(word in stderr.lower() for word in ("rate_limit", "rate limit", "usage limit", "quota exceeded"))
    return {
        "valid": not errors, "errors": errors,
        "requested_model": model, "requested_effort": effort,
        "resolved_model": observed_models[0] if len(observed_models) == 1 else None,
        "resolved_effort": observed_efforts[0] if len(observed_efforts) == 1 else None,
        "requested": {"model": model, "effort": effort},
        "resolved": {"model": observed_models[0] if len(observed_models) == 1 else None,
                     "effort": observed_efforts[0] if len(observed_efforts) == 1 else None,
                     "provenance": sorted(sources), "server_snapshot_verified": False},
        "auth_method": auth_method, "usage": totals,
        "rate_limit": {"detected": limited, "observed": rate_details},
        "trace": {"json_events": event_count, "malformed_json_lines": malformed,
                  "completed": completed, "failed": failed,
                  "allow_incomplete": allow_incomplete,
                  "runtime_context_records": runtime_contexts},
    }
