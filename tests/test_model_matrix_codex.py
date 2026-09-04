"""Codex baseline isolation and trace parsing without model requests."""
import json
import os
from pathlib import Path

import pytest

from experiments.model_matrix import codex_adapter as adapter


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def write_events(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(value) for value in values) + "\n")


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    original = tmp_path / "original"
    write_json(original / "auth.json", {
        "auth_mode": "chatgpt", "tokens": {"access_token": "private-test-token"},
        "OPENAI_API_KEY": "must-not-copy", "last_refresh": "2026-09-04T00:00:00Z",
    })
    (original / "config.toml").write_text('developer_instructions="Do not inherit"\n')
    monkeypatch.setenv("CODEX_HOME", str(original))
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-inherit")
    target = tmp_path / "runtime"
    result = adapter.prepare_runtime(target)
    return target, original, result


def test_runtime_copies_only_private_subscription_auth(runtime):
    target, original, result = runtime
    copied = json.loads((target / "codex_home/auth.json").read_text())
    assert copied["auth_mode"] == "chatgpt"
    assert "OPENAI_API_KEY" not in copied
    assert (target / "codex_home/auth.json").stat().st_mode & 0o777 == 0o600
    assert target.stat().st_mode & 0o777 == 0o700
    assert "private-test-token" not in json.dumps(result)
    assert "OPENAI_API_KEY" in result["unset_env"]
    assert "Do not inherit" not in (target / "codex_home/config.toml").read_text()
    assert "Do not inherit" in (original / "config.toml").read_text()
    with pytest.raises(ValueError, match="single-use"):
        adapter.prepare_runtime(target)


def test_api_auth_is_rejected(tmp_path, monkeypatch):
    source = tmp_path / "source"
    write_json(source / "auth.json", {"auth_mode": "apikey", "OPENAI_API_KEY": "secret"})
    monkeypatch.setenv("CODEX_HOME", str(source))
    with pytest.raises(ValueError, match="ChatGPT"):
        adapter.prepare_runtime(tmp_path / "runtime")


def test_runtime_cannot_write_inside_original_credential_store(runtime):
    _, original, _ = runtime
    with pytest.raises(ValueError, match="overlap"):
        adapter.prepare_runtime(original / "nested-runtime")
    assert not (original / "nested-runtime").exists()


def test_command_disables_customization_and_preserves_private_provenance(runtime, monkeypatch):
    target, _, _ = runtime
    monkeypatch.setattr(adapter.shutil, "which", lambda name: "/usr/bin/codex")
    cmd = adapter.build_command("gpt-5.6-sol", "max", target, target / "workspace")
    assert cmd[-1] == "-"
    assert "--ignore-user-config" in cmd and "--ignore-rules" in cmd
    assert "--ephemeral" not in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "danger-full-access"
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
    assert "--yolo" not in cmd
    assert 'forced_login_method="chatgpt"' in cmd
    assert 'model_reasoning_effort="max"' in cmd
    assert "project_doc_max_bytes=0" in cmd
    settings = "\n".join(cmd)
    for skill in adapter.SYSTEM_SKILLS:
        assert f"/{skill}/SKILL.md" in settings
    assert "features.plugins=false" in cmd
    assert "features.hooks=false" in cmd
    assert "features.memories=false" in cmd


def trace_fixture(target, model="gpt-5.6-sol", effort="max", completed=True):
    stdout, stderr = target / "stdout.jsonl", target / "stderr.txt"
    events = [{"type": "thread.started", "thread_id": "thread-1"}]
    if completed:
        events.append({"type": "turn.completed", "usage": {
            "input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 30}})
    write_events(stdout, events)
    stderr.write_text("")
    write_events(target / "codex_home/sessions/2026/09/04/session.jsonl", [
        {"type": "session_meta", "payload": {"id": "thread-1"}},
        {"type": "turn_context", "payload": {"model": model, "effort": effort}},
    ])
    return stdout, stderr


def test_private_context_proves_effective_configuration_not_server_snapshot(runtime):
    target, _, _ = runtime
    stdout, stderr = trace_fixture(target)
    result = adapter.inspect_trace(stdout, stderr, "gpt-5.6-sol", "max", runtime_dir=target)
    assert result["valid"]
    assert result["resolved"]["model"] == "gpt-5.6-sol"
    assert result["resolved_model"] == result["requested_model"] == "gpt-5.6-sol"
    assert result["resolved_effort"] == result["requested_effort"] == "max"
    assert result["resolved"]["server_snapshot_verified"] is False
    assert result["usage"]["output_tokens"] == 30
    assert "private-test-token" not in json.dumps(result)


@pytest.mark.parametrize("model,effort,error", [
    ("gpt-5.4-mini", "max", "model_mismatch"),
    ("gpt-5.6-sol", "high", "effort_mismatch"),
])
def test_mismatches_fail_closed(runtime, model, effort, error):
    target, _, _ = runtime
    stdout, stderr = trace_fixture(target, model, effort)
    result = adapter.inspect_trace(stdout, stderr, "gpt-5.6-sol", "max", runtime_dir=target)
    assert not result["valid"] and error in result["errors"]


def test_requested_flags_do_not_substitute_for_missing_provenance(runtime):
    target, _, _ = runtime
    stdout, stderr = trace_fixture(target)
    stdout.write_text('{"type":"thread.started","thread_id":"different"}\n'
                      '{"type":"turn.completed","usage":{}}\n')
    result = adapter.inspect_trace(stdout, stderr, "gpt-5.6-sol", "max", runtime_dir=target)
    assert not result["valid"]
    assert "model_unverified" in result["errors"]
    assert result["resolved"]["model"] is None


def test_failed_rate_limited_turn_is_not_valid(runtime):
    target, _, _ = runtime
    stdout, stderr = trace_fixture(target, completed=False)
    with stdout.open("a") as stream:
        stream.write(json.dumps({"type": "turn.failed", "error": {"message": "Usage limit reached"}}) + "\n")
    result = adapter.inspect_trace(stdout, stderr, "gpt-5.6-sol", "max", runtime_dir=target)
    assert not result["valid"] and result["rate_limit"]["detected"]
    assert "no_completed_turn" in result["errors"]


def test_incomplete_mode_relaxes_only_completion(runtime):
    target, _, _ = runtime
    stdout, stderr = trace_fixture(target, completed=False)
    strict = adapter.inspect_trace(stdout, stderr, "gpt-5.6-sol", "max", runtime_dir=target)
    permitted = adapter.inspect_trace(stdout, stderr, "gpt-5.6-sol", "max",
                                      runtime_dir=target, allow_incomplete=True)
    assert strict["errors"] == ["no_completed_turn"]
    assert permitted["valid"] and not permitted["trace"]["completed"]
    assert permitted["trace"]["allow_incomplete"] is True
    mismatch = adapter.inspect_trace(stdout, stderr, "gpt-5.6-sol", "high",
                                     runtime_dir=target, allow_incomplete=True)
    assert not mismatch["valid"] and mismatch["errors"] == ["effort_mismatch"]


def test_incomplete_mode_does_not_hide_provider_failures(runtime):
    target, _, _ = runtime
    stdout, stderr = trace_fixture(target, completed=False)
    with stdout.open("a") as stream:
        stream.write(json.dumps({"type": "turn.failed", "error": {"message": "rate limit"}}) + "\n")
    result = adapter.inspect_trace(stdout, stderr, "gpt-5.6-sol", "max",
                                   runtime_dir=target, allow_incomplete=True)
    assert not result["valid"]
    assert "provider_error_or_failed_turn" in result["errors"]
    assert "no_completed_turn" not in result["errors"]


def test_loaded_skill_catalog_invalidates_bare_run(runtime):
    target, _, _ = runtime
    stdout, stderr = trace_fixture(target)
    path = target / "codex_home/sessions/2026/09/04/session.jsonl"
    with path.open("a") as stream:
        stream.write(json.dumps({"type": "response_item", "payload": {
            "role": "developer", "content": [{"text": "<skills_instructions>catalog</skills_instructions>"}]}}) + "\n")
    result = adapter.inspect_trace(stdout, stderr, "gpt-5.6-sol", "max", runtime_dir=target)
    assert not result["valid"] and "unexpected_custom_instructions" in result["errors"]


def test_cli_banner_is_labeled_as_cli_provenance(runtime):
    target, _, _ = runtime
    stdout, stderr = trace_fixture(target)
    stdout.write_text('{"type":"thread.started","thread_id":"without-rollout"}\n'
                      '{"type":"turn.completed","usage":{}}\n')
    stderr.write_text("OpenAI Codex v0.147.0\nmodel: gpt-5.6-sol\nreasoning effort: max\n")
    result = adapter.inspect_trace(stdout, stderr, "gpt-5.6-sol", "max", runtime_dir=target)
    assert result["valid"]
    assert result["resolved"]["provenance"] == ["stderr_cli_startup_banner"]
