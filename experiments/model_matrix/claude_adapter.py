"""Subscription-authenticated Claude Code authoring with audited isolation.

The caller must enforce one outer OS filesystem and network sandbox, including
provider-only egress, and owns the process timeout and raw trace retention. The
CLI's nested Bash sandbox is disabled because nested sandbox application fails
on macOS. This adapter alone does not provide containment. Runtime settings
affect this invocation only; account credentials and global configuration are
never copied or changed.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path


TOOLS = ("Bash", "Edit", "Read", "Write")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
MODEL_EFFORTS = {
    "claude-opus-5": EFFORTS,
    "claude-opus-4-8": EFFORTS,
    "claude-fable-5": EFFORTS,
    "claude-fable-5-1": EFFORTS,
    "claude-sonnet-5": EFFORTS,
    "claude-sonnet-4-6": ("low", "medium", "high", "max"),
}
ENV_PREFIXES = ("ANTHROPIC_", "CLAUDE_", "AWS_", "AZURE_", "GOOGLE_", "GEMINI_")
ENV_NAMES = {
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID",
    "CLOUD_ML_REGION", "BASH_ENV", "ENV", "NODE_OPTIONS", "PYTHONPATH",
    "PYTHONSTARTUP", "PYTHONUSERBASE", "ZDOTDIR",
}
SETTINGS_NAME = "claude-settings.json"


def prepare_runtime(runtime_dir: Path) -> dict:
    """Prepare local settings and environment edits, without reading secrets.

    Apply ``unset_env`` to a copy of the parent environment before applying
    ``env``. Preserve HOME and Keychain access for the existing subscription
    login. The CLI's API-key-only ``--bare`` mode is deliberately not used.
    Invoke only under the caller's independently verified outer filesystem and
    network sandbox; disabling the redundant nested layer must not relax it.
    """
    runtime = Path(runtime_dir).resolve()
    if runtime in (Path.home().resolve(), Path(runtime.anchor)):
        raise ValueError("runtime_dir must be a dedicated experiment directory")
    runtime.mkdir(parents=True, exist_ok=True)
    settings = {"sandbox": {"enabled": False}}
    payload = json.dumps(settings, indent=2) + "\n"
    destination = runtime / SETTINGS_NAME
    if destination.exists() and destination.read_text(encoding="utf-8") != payload:
        raise ValueError("runtime settings differ from the frozen adapter policy")
    if not destination.exists():
        destination.write_text(payload, encoding="utf-8")
    unset = sorted(name for name in os.environ
                   if name in ENV_NAMES or name.startswith(ENV_PREFIXES))
    return {
        "env": {
            "CLAUDE_CODE_SAFE_MODE": "1",
            "CLAUDE_CODE_NO_MODEL_FALLBACK": "1",
        },
        "unset_env": unset,
        "settings_path": str(destination),
        "auth_policy": "existing_claude_ai_subscription",
        "outer_containment_required": True,
    }


def build_command(model: str, effort: str, runtime_dir: Path, workspace: Path) -> list[str]:
    """Build shell-free argv; pass the frozen authoring prompt on stdin."""
    if model not in MODEL_EFFORTS:
        raise ValueError("use a supported exact first-party model ID, not an alias")
    if effort not in MODEL_EFFORTS[model]:
        raise ValueError("effort is not supported by this model in the pinned CLI catalog")
    if not Path(workspace).is_dir():
        raise ValueError("workspace must be an existing dedicated directory")
    settings = Path(runtime_dir).resolve() / SETTINGS_NAME
    if not settings.is_file():
        raise ValueError("call prepare_runtime before build_command")
    tools = ",".join(TOOLS)
    return [
        "claude", "-p", "--safe-mode", "--restricted", "--disable-slash-commands",
        "--strict-mcp-config", "--no-chrome", "--no-session-persistence",
        "--prompt-suggestions", "false", "--setting-sources", "",
        "--settings", str(settings), "--permission-mode", "dontAsk",
        "--tools", tools, "--allowedTools", tools,
        "--model", model, "--effort", effort,
        "--output-format", "stream-json", "--verbose",
    ]


def _numeric_usage(value):
    """Keep accounting fields without copying arbitrary text from a trace."""
    if isinstance(value, dict):
        return {key: _numeric_usage(item) for key, item in value.items()
                if isinstance(key, str) and isinstance(item, (dict, int, float))
                and not isinstance(item, bool)}
    return value


def inspect_trace(stdout_path: Path, stderr_path: Path, model: str, effort: str,
                  *, allow_incomplete: bool = False) -> dict:
    """Validate isolation, exact primary identity, completion, and usage.

    Effective effort is not normally present in stream-json. An absent effort
    field is reported as launch-only evidence, never as verified server effort.
    Partial traces preserve earlier identity and usage evidence. The caller may
    set ``allow_incomplete`` for its declared authoring deadline artifact rule:
    this tolerates a missing final event only, not an explicit failure or any
    identity, authentication, isolation, fallback, or trace integrity error.
    The caller must independently establish the deadline and freeze the artifact.
    """
    errors, warnings, events = [], [], []
    source = Path(stdout_path)
    if not source.is_file():
        errors.append("stdout_missing")
    else:
        with source.open(encoding="utf-8", errors="replace") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    errors.append(f"malformed_json_line_{line_number}")
                    continue
                if not isinstance(event, dict):
                    errors.append(f"non_object_event_line_{line_number}")
                    continue
                events.append(event)
    inits = [e for e in events if e.get("type") == "system" and e.get("subtype") == "init"]
    finals = [e for e in events if e.get("type") == "result"]
    if len(inits) != 1:
        errors.append("init_event_count_not_one")
    if len(finals) != 1 and not (allow_incomplete and not finals):
        errors.append("result_event_count_not_one")
    if allow_incomplete and not finals:
        warnings.append("authoring_incomplete_final_event_missing")
    init, final = (inits[0] if inits else {}), (finals[-1] if finals else {})
    for field in ("plugins", "skills", "mcp_servers", "slash_commands"):
        if init.get(field) != []:
            errors.append(f"unexpected_or_missing_{field}")
    if (not isinstance(init.get("tools"), list)
            or not all(isinstance(name, str) for name in init["tools"])
            or sorted(init["tools"]) != sorted(TOOLS)):
        errors.append("unexpected_or_missing_tools")
    if init.get("apiKeySource") != "none":
        errors.append("subscription_auth_not_confirmed")
    primary, observed_efforts, rates, tool_names = set(), set(), [], set()
    assistant_count = 0
    for event in events:
        kind = event.get("type")
        records = [event]
        if kind == "assistant":
            assistant_count += 1
            message = event.get("message")
            if isinstance(message, dict):
                records.append(message)
                content = message.get("content")
                for item in content if isinstance(content, list) else []:
                    if isinstance(item, dict) and item.get("type") == "tool_use":
                        tool_names.add(str(item.get("name")))
        for record in records:
            if (kind in ("assistant", "result") or event in inits) and record.get("model"):
                primary.add(str(record["model"]))
            for key in ("effort", "effortLevel", "effort_level"):
                value = record.get(key)
                if isinstance(value, str) and value in EFFORTS:
                    observed_efforts.add(value)
            if any("fallback" in key.lower() and bool(value)
                   for key, value in record.items() if isinstance(key, str)):
                errors.append("model_fallback_observed")
        if any("fallback" in str(event.get(key, "")).lower() for key in ("type", "subtype")):
            errors.append("model_fallback_observed")
        if kind == "rate_limit_event":
            info = event.get("rate_limit_info")
            if isinstance(info, dict):
                rates.append({key: info[key] for key in (
                    "status", "rateLimitType", "resetsAt", "utilization", "isUsingOverage")
                              if key in info})
        if kind == "error":
            errors.append("provider_error_event")
    if not primary or primary != {model}:
        errors.append("primary_model_mismatch")
    if assistant_count == 0:
        errors.append("assistant_event_missing")
    if tool_names - set(TOOLS):
        errors.append("unexpected_tool_use")
    if observed_efforts and observed_efforts != {effort}:
        errors.append("effort_mismatch")
    if not observed_efforts:
        warnings.append("effective_effort_not_in_trace")
    completion_successful = (len(finals) == 1 and final.get("subtype") == "success"
                             and final.get("is_error") is False)
    if not completion_successful and not (allow_incomplete and not finals):
        errors.append("completion_not_successful")
    if any(rate.get("status") not in (None, "allowed") for rate in rates):
        errors.append("rate_limit_not_allowed")
    if any(rate.get("isUsingOverage") is True for rate in rates):
        errors.append("paid_overage_observed")
    overage_flags = [rate["isUsingOverage"] for rate in rates
                    if isinstance(rate.get("isUsingOverage"), bool)]
    if not overage_flags:
        warnings.append("overage_status_not_in_trace")
    stderr = Path(stderr_path)
    stderr_text = stderr.read_text(encoding="utf-8", errors="replace") if stderr.is_file() else ""
    if re.search(r"fallback|falling back|effort.{0,60}(?:downgrad|unsupported|not supported)",
                 stderr_text, re.IGNORECASE):
        errors.append("stderr_fallback_or_effort_warning")
    if stderr_text.strip():
        warnings.append("stderr_present_review_raw_private_trace")
    model_usage = final.get("modelUsage") if isinstance(final.get("modelUsage"), dict) else {}
    errors = list(dict.fromkeys(errors))
    return {
        "valid": not errors, "errors": errors, "warnings": warnings,
        "completion_successful": completion_successful,
        "incomplete_trace_accepted": bool(allow_incomplete and not finals and not errors),
        "requested_model": model,
        "resolved_model": next(iter(primary)) if len(primary) == 1 else None,
        "primary_models": sorted(primary),
        "auxiliary_models": sorted(set(model_usage) - primary),
        "requested_effort": effort, "observed_efforts": sorted(observed_efforts),
        "effort_verification": "trace" if observed_efforts else "launch_configuration_only",
        "usage": _numeric_usage(final.get("usage", {})),
        "model_usage": {name: _numeric_usage(value) for name, value in model_usage.items()},
        "reported_cost_usd": final.get("total_cost_usd"),
        "rate_limit": {"events": rates,
                       "using_overage": any(overage_flags) if overage_flags else None},
        "init_cwd": init.get("cwd"), "tools_used": sorted(tool_names),
        "result_subtype": final.get("subtype"), "event_count": len(events),
        "assistant_event_count": assistant_count,
    }
