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


def _refusal_events():
    """Neutral metadata-only fixture matching the native server-refusal shape."""
    events = _events()
    events[1] = {"type": "system", "subtype": "model_refusal_no_fallback",
                 "original_model": MODEL, "api_refusal_category": "cyber",
                 "api_refusal_explanation": "Neutral policy-refusal explanation."}
    events.insert(2, {
        "type": "assistant", "error": "invalid_request", "is_api_error_message": True,
        "message": {"model": "<synthetic>", "stop_reason": "refusal",
                    "stop_details": {"type": "refusal", "category": "cyber",
                                     "explanation": "Neutral policy-refusal explanation."},
                    "content": [{"type": "text", "text": "Provider policy refusal."}]}})
    events[-1].update(is_error=True, stop_reason="refusal", terminal_reason="api_error",
                       api_error_status=None)
    return events


def test_native_policy_refusal_is_valid_identity_but_not_a_successful_answer(tmp_path):
    result = _inspect(tmp_path, _refusal_events())
    assert result["valid"] is True and result["errors"] == []
    assert result["provider_refusal"] is True
    assert result["completion_successful"] is False
    assert result["completion_status"] == "completed_provider_refusal"
    assert result["identity_evidence"] == "init_and_refusal_metadata"
    assert result["resolved_model"] == MODEL and result["primary_models"] == [MODEL]
    assert "<synthetic>" not in result["primary_models"]
    assert result["auxiliary_models"] == ["claude-haiku-4-5-20251001"]
    assert result["refusal_category"] == "cyber"
    assert result["provider_error"] == "invalid_request"
    assert result["provider_refusal_details"] == {
        "category": "cyber", "provider_error": "invalid_request", "provider_error_status": None,
        "terminal_reason": "api_error", "system_subtype": "model_refusal_no_fallback",
        "original_model": MODEL, "envelope_count": 1}
    assert result["effort_verification"] == "launch_configuration_only"
    assert result["incomplete_trace_accepted"] is False
    assert "Neutral policy-refusal" not in json.dumps(result)


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 503, 0, False, "429"])
def test_native_refusal_does_not_downgrade_explicit_http_errors(tmp_path, status):
    events = _refusal_events()
    events[-1]["api_error_status"] = status
    result = _inspect(tmp_path, events)
    assert result["valid"] is False
    assert result["provider_refusal"] is False
    assert "native_provider_refusal_unverified" in result["errors"]


def test_native_refusal_allows_missing_http_status_like_observed_null(tmp_path):
    events = _refusal_events()
    del events[-1]["api_error_status"]
    result = _inspect(tmp_path, events)
    assert result["valid"] is True and result["provider_refusal"] is True


@pytest.mark.parametrize("violation", [
    "no_native_event", "wrong_native_event", "wrong_init", "wrong_original_model",
    "no_requested_usage", "bad_usage", "no_details", "wrong_category", "no_api_marker",
    "wrong_provider_error", "wrong_assistant_stop", "wrong_terminal_stop", "wrong_terminal_reason",
    "not_error", "wrong_terminal_subtype", "no_assistant", "second_synthetic",
    "synthetic_result", "reordered_envelope", "duplicate_native_event", "unknown_synthetic_model",
])
def test_refusal_exception_requires_all_native_evidence(tmp_path, violation):
    events = _refusal_events()
    if violation == "no_native_event":
        del events[1]
    elif violation == "wrong_native_event":
        events[1]["subtype"] = "unrecognized_refusal"
    elif violation == "wrong_init":
        events[0]["model"] = "claude-sonnet-5"
    elif violation == "wrong_original_model":
        events[1]["original_model"] = "claude-sonnet-5"
    elif violation == "no_requested_usage":
        del events[-1]["modelUsage"][MODEL]
    elif violation == "bad_usage":
        events[-1]["modelUsage"][MODEL] = None
    elif violation == "no_details":
        del events[2]["message"]["stop_details"]
    elif violation == "wrong_category":
        events[2]["message"]["stop_details"]["category"] = "different_category"
    elif violation == "no_api_marker":
        del events[2]["is_api_error_message"]
    elif violation == "wrong_provider_error":
        events[2]["error"] = "unrecognized_error"
    elif violation == "wrong_assistant_stop":
        events[2]["message"]["stop_reason"] = "end_turn"
    elif violation == "wrong_terminal_stop":
        events[-1]["stop_reason"] = "end_turn"
    elif violation == "wrong_terminal_reason":
        events[-1]["terminal_reason"] = "unrecognized_error"
    elif violation == "not_error":
        events[-1]["is_error"] = False
    elif violation == "wrong_terminal_subtype":
        events[-1]["subtype"] = "error_during_execution"
    elif violation == "no_assistant":
        del events[2]
    elif violation == "second_synthetic":
        events.insert(3, dict(events[2]))
    elif violation == "synthetic_result":
        events[-1]["model"] = "<synthetic>"
    elif violation == "reordered_envelope":
        events[1], events[2] = events[2], events[1]
    elif violation == "duplicate_native_event":
        events.insert(2, dict(events[1]))
    elif violation == "unknown_synthetic_model":
        events[2]["message"]["model"] = "<other-synthetic>"
    result = _inspect(tmp_path, events, allow_incomplete=True)
    assert result["valid"] is False and result["provider_refusal"] is False
    assert result["incomplete_trace_accepted"] is False


@pytest.mark.parametrize("violation,expected_error", [
    ("auth", "subscription_auth_not_confirmed"),
    ("skills", "unexpected_or_missing_skills"),
    ("tools", "unexpected_or_missing_tools"),
    ("tool_use", "unexpected_tool_use"),
    ("effort", "effort_mismatch"),
    ("overage", "paid_overage_observed"),
    ("quota", "rate_limit_not_allowed"),
    ("fallback_event", "model_fallback_observed"),
    ("fallback_field", "model_fallback_observed"),
    ("other_real_model", "primary_model_mismatch"),
    ("provider_error", "provider_error_event"),
])
def test_native_refusal_does_not_relax_other_audit_checks(tmp_path, violation, expected_error):
    events = _refusal_events()
    if violation == "auth":
        events[0]["apiKeySource"] = "environment"
    elif violation == "skills":
        events[0]["skills"] = ["ambient-customization"]
    elif violation == "tools":
        events[0]["tools"].append("WebFetch")
    elif violation == "tool_use":
        events[2]["message"]["content"].append({"type": "tool_use", "name": "WebFetch"})
    elif violation == "effort":
        events[0]["effort"] = "low"
    elif violation == "overage":
        events[3]["rate_limit_info"]["isUsingOverage"] = True
    elif violation == "quota":
        events[3]["rate_limit_info"]["status"] = "rejected"
    elif violation == "fallback_event":
        events.insert(1, {"type": "model_fallback"})
    elif violation == "fallback_field":
        events[1]["fallback_model"] = "claude-sonnet-5"
    elif violation == "other_real_model":
        events.insert(1, {"type": "assistant", "message": {"model": "claude-sonnet-5"}})
    elif violation == "provider_error":
        events.insert(3, {"type": "error"})
    result = _inspect(tmp_path, events)
    assert result["valid"] is False and result["provider_refusal"] is False
    assert expected_error in result["errors"]


def test_native_refusal_does_not_suppress_stderr_fallback(tmp_path):
    result = _inspect(tmp_path, _refusal_events(), "falling back to another model")
    assert result["valid"] is False and result["provider_refusal"] is False
    assert "stderr_fallback_or_effort_warning" in result["errors"]


def test_no_fallback_event_name_is_not_itself_fallback_evidence(tmp_path):
    events = _refusal_events()
    del events[2]["message"]["stop_details"]
    errors = _inspect(tmp_path, events)["errors"]
    assert "native_provider_refusal_unverified" in errors
    assert "model_fallback_observed" not in errors


def test_clean_success_has_no_refusal_classification(tmp_path):
    result = _inspect(tmp_path, _events())
    assert result["valid"] and result["completion_successful"]
    assert result["completion_status"] == "completed_successfully"
    assert result["provider_refusal"] is False
    assert result["provider_refusal_details"] is None


def _repeated_refusal_events():
    """Neutral fixture for the exact evidenced two-request refusal sequence."""
    base = _events()
    explanation = "Neutral repeated policy-refusal explanation."

    def native(request_id, event_id):
        return {"type": "system", "subtype": "model_refusal_no_fallback",
                "request_id": request_id, "uuid": event_id,
                "refused_user_message_uuid": "user-message-1", "original_model": MODEL,
                "api_refusal_category": "cyber", "content": "Native refusal content.",
                "api_refusal_explanation": explanation}

    def synthetic(request_id, event_id, text):
        return {"type": "assistant", "request_id": request_id, "uuid": event_id,
                "error": "invalid_request", "is_api_error_message": True,
                "message": {"model": "<synthetic>", "stop_reason": "refusal",
                            "stop_details": {"type": "refusal", "category": "cyber",
                                             "explanation": explanation},
                            "content": [{"type": "text", "text": text}]}}

    final = base[-1]
    final.update(is_error=True, stop_reason="refusal", terminal_reason="api_error",
                 api_error_status=None)
    return [
        base[0], base[2],
        {"type": "assistant", "message": {"model": MODEL,
                                           "content": [{"type": "text", "text": "Working."}]}},
        {"type": "assistant", "message": {"model": MODEL, "content": [
            {"type": "tool_use", "name": "Bash", "id": "tool-use-1", "input": {"command": "true"}}]}},
        native("request-1", "system-1"),
        synthetic("request-1", "assistant-1", "First provider refusal."),
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tool-use-1", "is_error": False,
             "content": "Command completed."}]},
         "tool_use_result": {"interrupted": False, "isImage": False,
                             "noOutputExpected": False, "stdout": "", "stderr": ""}},
        native("request-2", "system-2"),
        synthetic("request-2", "assistant-2", "Second provider refusal."),
        final,
    ]


def test_exact_repeated_native_refusal_is_a_valid_nonsuccess_outcome(tmp_path):
    result = _inspect(tmp_path, _repeated_refusal_events())
    assert result["valid"] is True and result["errors"] == []
    assert result["provider_refusal"] is True and result["completion_successful"] is False
    assert result["completion_status"] == "completed_provider_refusal"
    assert result["resolved_model"] == MODEL and result["primary_models"] == [MODEL]
    assert result["provider_refusal_details"]["envelope_count"] == 2
    assert result["tools_used"] == ["Bash"]
    encoded = json.dumps(result)
    for private_metadata in ("request-1", "request-2", "user-message-1", "system-1",
                             "assistant-1", "Native refusal content", "Neutral repeated"):
        assert private_metadata not in encoded


@pytest.mark.parametrize("violation", [
    "shared_request", "unmatched_request", "missing_request", "different_user", "empty_user",
    "wrong_model", "wrong_category", "different_content", "different_native_explanation",
    "different_synthetic_explanation", "missing_tool_result", "extra_user_event",
    "tool_result_error", "tool_interrupted", "tool_image", "tool_no_output_expected",
    "malformed_tool_output", "unmatched_tool_id", "missing_prior_tool_use", "out_of_order",
    "extra_prior_tool_use", "non_bash_tool", "pre_refusal_noise", "malformed_prior_content",
    "interposed_noise", "missing_native_content",
    "missing_native_explanation", "reversed_pairs", "third_native", "third_synthetic",
])
def test_repeated_refusal_requires_exact_pairing_and_interposed_tool_result(tmp_path, violation):
    events = _repeated_refusal_events()
    if violation == "shared_request":
        events[7]["request_id"] = events[8]["request_id"] = "request-1"
    elif violation == "unmatched_request":
        events[8]["request_id"] = "unmatched-request"
    elif violation == "missing_request":
        del events[7]["request_id"]
    elif violation == "different_user":
        events[7]["refused_user_message_uuid"] = "user-message-2"
    elif violation == "empty_user":
        events[7]["refused_user_message_uuid"] = ""
    elif violation == "wrong_model":
        events[7]["original_model"] = "claude-sonnet-5"
    elif violation == "wrong_category":
        events[7]["api_refusal_category"] = "different"
    elif violation == "different_content":
        events[7]["content"] = "Different native content."
    elif violation == "different_native_explanation":
        events[7]["api_refusal_explanation"] = "Different explanation."
    elif violation == "different_synthetic_explanation":
        events[8]["message"]["stop_details"]["explanation"] = "Different explanation."
    elif violation == "missing_tool_result":
        del events[6]
    elif violation == "extra_user_event":
        events.insert(7, json.loads(json.dumps(events[6])))
    elif violation == "tool_result_error":
        events[6]["message"]["content"][0]["is_error"] = True
    elif violation == "tool_interrupted":
        events[6]["tool_use_result"]["interrupted"] = True
    elif violation == "tool_image":
        events[6]["tool_use_result"]["isImage"] = True
    elif violation == "tool_no_output_expected":
        events[6]["tool_use_result"]["noOutputExpected"] = True
    elif violation == "malformed_tool_output":
        events[6]["tool_use_result"]["stdout"] = None
    elif violation == "unmatched_tool_id":
        events[6]["message"]["content"][0]["tool_use_id"] = "another-tool"
    elif violation == "missing_prior_tool_use":
        events[3]["message"]["content"][0]["id"] = "another-tool"
    elif violation == "extra_prior_tool_use":
        events[3]["message"]["content"].append(
            {"type": "tool_use", "name": "Read", "id": "unused-tool", "input": {}})
    elif violation == "non_bash_tool":
        events[3]["message"]["content"][0]["name"] = "Read"
    elif violation == "pre_refusal_noise":
        events.insert(4, {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}})
    elif violation == "malformed_prior_content":
        events[3]["message"]["content"] = 1
    elif violation == "interposed_noise":
        events.insert(5, {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}})
    elif violation == "missing_native_content":
        del events[7]["content"]
    elif violation == "missing_native_explanation":
        del events[7]["api_refusal_explanation"]
    elif violation == "out_of_order":
        events[6], events[7] = events[7], events[6]
    elif violation == "reversed_pairs":
        events[4], events[7] = events[7], events[4]
    elif violation == "third_native":
        events.insert(-1, json.loads(json.dumps(events[7])))
    elif violation == "third_synthetic":
        events.insert(-1, json.loads(json.dumps(events[8])))
    result = _inspect(tmp_path, events, allow_incomplete=True)
    assert result["valid"] is False and result["provider_refusal"] is False
    assert result["incomplete_trace_accepted"] is False
    assert "native_provider_refusal_unverified" in result["errors"]


@pytest.mark.parametrize("violation,expected_error", [
    ("auth", "subscription_auth_not_confirmed"),
    ("customization", "unexpected_or_missing_skills"),
    ("quota", "rate_limit_not_allowed"),
    ("overage", "paid_overage_observed"),
    ("fallback", "model_fallback_observed"),
    ("other_model", "primary_model_mismatch"),
    ("http_status", "native_provider_refusal_unverified"),
])
def test_repeated_refusal_retains_every_independent_audit_gate(tmp_path, violation, expected_error):
    events = _repeated_refusal_events()
    if violation == "auth":
        events[0]["apiKeySource"] = "environment"
    elif violation == "customization":
        events[0]["skills"] = ["ambient"]
    elif violation == "quota":
        events[1]["rate_limit_info"]["status"] = "rejected"
    elif violation == "overage":
        events[1]["rate_limit_info"]["isUsingOverage"] = True
    elif violation == "fallback":
        events.insert(2, {"type": "model_fallback"})
    elif violation == "other_model":
        events[2]["message"]["model"] = "claude-sonnet-5"
    elif violation == "http_status":
        events[-1]["api_error_status"] = 429
    result = _inspect(tmp_path, events)
    assert result["valid"] is False and result["provider_refusal"] is False
    assert expected_error in result["errors"]
