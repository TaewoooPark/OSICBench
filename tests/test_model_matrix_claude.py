"""Claude subscription adapter policy and synthetic trace regressions."""
import json

import pytest

from experiments.model_matrix import claude_adapter as adapter


MODEL = "claude-opus-5"


def _events():
    return [
        {"type": "system", "subtype": "init", "model": MODEL,
         "plugins": [], "skills": [], "mcp_servers": [], "slash_commands": [],
         "tools": list(adapter.TOOLS), "apiKeySource": "none"},
        {"type": "assistant", "message": {"model": MODEL, "content": [
            {"type": "tool_use", "name": "Write", "input": {"file_path": "main.py"}}]}},
        {"type": "rate_limit_event", "rate_limit_info": {
            "status": "allowed", "isUsingOverage": False, "utilization": 0.2}},
        {"type": "result", "subtype": "success", "is_error": False,
         "usage": {"input_tokens": 2, "output_tokens": 100},
         "modelUsage": {MODEL: {"inputTokens": 2},
                        "claude-haiku-4-5-20251001": {"inputTokens": 10}},
         "total_cost_usd": 0.01},
    ]


def _inspect(tmp_path, events, stderr="", *, allow_incomplete=False):
    stdout_path, stderr_path = tmp_path / "stdout.ndjson", tmp_path / "stderr.txt"
    stdout_path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    stderr_path.write_text(stderr)
    return adapter.inspect_trace(stdout_path, stderr_path, MODEL, "max",
                                 allow_incomplete=allow_incomplete)


def test_runtime_preserves_account_location_but_removes_overrides(tmp_path, monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_EFFORT_LEVEL",
                 "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
                 "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", "BASH_ENV"):
        monkeypatch.setenv(name, "private-test-value")
    runtime = adapter.prepare_runtime(tmp_path)
    assert "HOME" not in runtime["env"] and "HOME" not in runtime["unset_env"]
    assert "CLAUDE_CODE_MAX_OUTPUT_TOKENS" in runtime["unset_env"]
    assert "ANTHROPIC_API_KEY" in runtime["unset_env"]
    assert "private-test-value" not in json.dumps(runtime)
    assert runtime["env"]["CLAUDE_CODE_NO_MODEL_FALLBACK"] == "1"
    settings = json.loads((tmp_path / adapter.SETTINGS_NAME).read_text())
    assert settings == {"sandbox": {"enabled": False}}
    assert runtime["outer_containment_required"] is True


def test_runtime_rejects_changed_frozen_settings(tmp_path):
    adapter.prepare_runtime(tmp_path)
    (tmp_path / adapter.SETTINGS_NAME).write_text("{}")
    with pytest.raises(ValueError, match="frozen"):
        adapter.prepare_runtime(tmp_path)


def test_command_uses_exact_model_subscription_safe_flags_and_no_fallback(tmp_path):
    adapter.prepare_runtime(tmp_path)
    command = adapter.build_command(MODEL, "max", tmp_path, tmp_path)
    assert command[:2] == ["claude", "-p"]
    assert "--bare" not in command and "--fallback-model" not in command
    assert command[command.index("--model") + 1] == MODEL
    assert command[command.index("--effort") + 1] == "max"
    assert command[command.index("--setting-sources") + 1] == ""
    for flag in ("--safe-mode", "--restricted", "--disable-slash-commands",
                 "--strict-mcp-config", "--no-session-persistence"):
        assert flag in command


def test_aliases_and_unsupported_effort_are_rejected(tmp_path):
    adapter.prepare_runtime(tmp_path)
    with pytest.raises(ValueError):
        adapter.build_command("opus", "max", tmp_path, tmp_path)
    with pytest.raises(ValueError):
        adapter.build_command("claude-sonnet-4-6", "xhigh", tmp_path, tmp_path)


def test_clean_trace_separates_primary_auxiliary_and_effort_evidence(tmp_path):
    result = _inspect(tmp_path, _events())
    assert result["valid"] is True
    assert result["resolved_model"] == MODEL
    assert result["auxiliary_models"] == ["claude-haiku-4-5-20251001"]
    assert result["effort_verification"] == "launch_configuration_only"
    assert result["rate_limit"]["using_overage"] is False
    assert result["usage"]["output_tokens"] == 100


@pytest.mark.parametrize("field", ["skills", "plugins", "mcp_servers", "slash_commands"])
def test_customizations_fail_closed(tmp_path, field):
    events = _events()
    events[0][field] = ["ambient-customization"]
    assert _inspect(tmp_path, events)["valid"] is False


def test_missing_isolation_metadata_is_not_assumed_empty(tmp_path):
    events = _events()
    del events[0]["plugins"]
    assert "unexpected_or_missing_plugins" in _inspect(tmp_path, events)["errors"]


def test_same_family_wrong_version_is_not_accepted(tmp_path):
    events = _events()
    events[1]["message"]["model"] = "claude-opus-4-8"
    assert "primary_model_mismatch" in _inspect(tmp_path, events)["errors"]


def test_partial_timeout_keeps_identity_but_is_invalid(tmp_path):
    result = _inspect(tmp_path, _events()[:2])
    assert result["resolved_model"] == MODEL
    assert result["valid"] is False
    assert "result_event_count_not_one" in result["errors"]


def test_declared_incomplete_authoring_tolerates_only_missing_final(tmp_path):
    result = _inspect(tmp_path, _events()[:-1], allow_incomplete=True)
    assert result["valid"] is True
    assert result["incomplete_trace_accepted"] is True
    assert result["completion_successful"] is False
    assert result["resolved_model"] == MODEL
    assert result["effort_verification"] == "launch_configuration_only"


@pytest.mark.parametrize("violation", ["model", "auth", "skills", "tools", "effort", "rate"])
def test_incomplete_authoring_retains_all_policy_checks(tmp_path, violation):
    events = _events()[:-1]
    if violation == "model":
        events[1]["message"]["model"] = "claude-sonnet-5"
    elif violation == "auth":
        events[0]["apiKeySource"] = "environment"
    elif violation == "skills":
        events[0]["skills"] = ["unexpected"]
    elif violation == "tools":
        events[0]["tools"].append("WebFetch")
    elif violation == "effort":
        events[0]["effortLevel"] = "low"
    elif violation == "rate":
        events[2]["rate_limit_info"]["isUsingOverage"] = True
    result = _inspect(tmp_path, events, allow_incomplete=True)
    assert result["valid"] is False
    assert result["incomplete_trace_accepted"] is False


def test_incomplete_authoring_does_not_accept_explicit_terminal_failure(tmp_path):
    events = _events()
    events[-1].update(subtype="error_during_execution", is_error=True)
    result = _inspect(tmp_path, events, allow_incomplete=True)
    assert "completion_not_successful" in result["errors"]
    assert result["incomplete_trace_accepted"] is False


def test_incomplete_authoring_still_requires_unambiguous_init_and_final_counts(tmp_path):
    events = _events()
    events.insert(1, dict(events[0]))
    events.append(dict(events[-1]))
    result = _inspect(tmp_path, events, allow_incomplete=True)
    assert "init_event_count_not_one" in result["errors"]
    assert "result_event_count_not_one" in result["errors"]


def test_incomplete_authoring_cannot_hide_stderr_fallback(tmp_path):
    result = _inspect(tmp_path, _events()[:-1], "falling back to another model",
                      allow_incomplete=True)
    assert "stderr_fallback_or_effort_warning" in result["errors"]


def test_paid_overage_and_api_key_fail_closed(tmp_path):
    events = _events()
    events[0]["apiKeySource"] = "environment"
    events[2]["rate_limit_info"]["isUsingOverage"] = True
    errors = _inspect(tmp_path, events)["errors"]
    assert "paid_overage_observed" in errors
    assert "subscription_auth_not_confirmed" in errors


def test_reported_effort_downgrade_is_invalid(tmp_path):
    events = _events()
    events[0]["effortLevel"] = "high"
    assert "effort_mismatch" in _inspect(tmp_path, events)["errors"]


def test_stderr_fallback_is_invalid_without_echoing_private_text(tmp_path):
    result = _inspect(tmp_path, _events(), "private-test-value: falling back to another model")
    assert result["valid"] is False
    assert "private-test-value" not in json.dumps(result)


def test_malformed_trace_fails_closed(tmp_path):
    stdout_path, stderr_path = tmp_path / "stdout", tmp_path / "stderr"
    stdout_path.write_text("not json\n")
    stderr_path.write_text("")
    result = adapter.inspect_trace(stdout_path, stderr_path, MODEL, "max")
    assert result["valid"] is False
    assert "malformed_json_line_1" in result["errors"]


def test_missing_overage_status_is_unknown_not_false(tmp_path):
    events = _events()
    del events[2]["rate_limit_info"]["isUsingOverage"]
    result = _inspect(tmp_path, events)
    assert result["rate_limit"]["using_overage"] is None
    assert "overage_status_not_in_trace" in result["warnings"]


def test_malformed_tool_metadata_is_invalid_not_an_exception(tmp_path):
    events = _events()
    events[0]["tools"] = [None, "Read"]
    assert "unexpected_or_missing_tools" in _inspect(tmp_path, events)["errors"]


def test_fallback_event_type_is_rejected(tmp_path):
    events = _events()
    events.insert(2, {"type": "model_fallback"})
    assert "model_fallback_observed" in _inspect(tmp_path, events)["errors"]
