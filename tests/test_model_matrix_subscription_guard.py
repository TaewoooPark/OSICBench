"""Quota guard normalization and fail-closed policy without network calls."""
import json
from urllib.error import HTTPError

import pytest

from experiments.model_matrix import subscription_guard as guard


def _claude():
    return {"five_hour": {"utilization": 10}, "seven_day": {"utilization": 20},
            "seven_day_opus": None, "extra_usage": {"is_enabled": False}}


def _codex():
    return {"rate_limit": {"allowed": True, "limit_reached": False,
                           "primary_window": {"used_percent": 20},
                           "secondary_window": {"used_percent": 30}},
            "credits": {"has_credits": False, "unlimited": False, "balance": "0"}}


@pytest.mark.parametrize("provider,payload", [("anthropic", _claude()), ("openai", _codex())])
def test_available_subscription_without_paid_usage_passes(provider, payload):
    result = guard.evaluate_usage(provider, payload)
    assert result["allowed"] is True
    assert result["paid_usage_enabled"] is False
    assert result["remaining_percent"] >= 5


@pytest.mark.parametrize("used,allowed", [(95, True), (95.01, False), (100, False)])
def test_headroom_boundary(used, allowed):
    payload = _claude()
    payload["five_hour"]["utilization"] = used
    assert guard.evaluate_usage("anthropic", payload)["allowed"] is allowed


def test_claude_extra_usage_is_blocked_even_with_subscription_headroom():
    payload = _claude()
    payload["extra_usage"]["is_enabled"] = True
    result = guard.evaluate_usage("anthropic", payload)
    assert result["allowed"] is False
    assert result["reason"] == "paid_usage_enabled"


@pytest.mark.parametrize("key,value", [("has_credits", True), ("unlimited", True), ("balance", "10.0")])
def test_codex_paid_credit_availability_blocks_launch(key, value):
    payload = _codex()
    payload["credits"][key] = value
    assert guard.evaluate_usage("openai", payload)["reason"] == "paid_usage_enabled"


@pytest.mark.parametrize("provider,payload", [("anthropic", {}), ("openai", {}),
                                              ("anthropic", {"five_hour": {"utilization": False}})])
def test_unknown_quota_or_paid_state_fails_closed(provider, payload):
    assert guard.evaluate_usage(provider, payload)["allowed"] is False


def test_model_specific_window_is_checked_for_requested_family():
    payload = _claude()
    payload["seven_day_opus"] = {"utilization": 100}
    assert not guard.evaluate_usage("anthropic", payload, model="claude-opus-5")["allowed"]
    assert guard.evaluate_usage("anthropic", payload, model="claude-sonnet-5")["allowed"]


def test_explicit_exhaustion_overrides_healthy_numeric_window():
    payload = _codex()
    payload["rate_limit"]["limit_reached"] = True
    assert guard.evaluate_usage("openai", payload)["reason"] == "quota_exhausted"


def test_codex_model_specific_quota_applies_only_to_requested_model():
    payload = _codex()
    payload["additional_rate_limits"] = [{
        "normal_model_slug": "gpt-5.3-codex-spark",
        "rate_limit": {"allowed": False, "limit_reached": True,
                       "primary_window": {"used_percent": 100}},
    }]
    assert guard.evaluate_usage("openai", payload, model="gpt-5.6-terra")["allowed"]
    assert not guard.evaluate_usage("openai", payload, model="gpt-5.3-codex-spark")["allowed"]
    assert not guard.evaluate_usage("openai", payload)["allowed"]


def test_unknown_model_specific_quota_shape_is_not_ignored():
    payload = _codex()
    payload["additional_rate_limits"] = [{"normal_model_slug": "gpt-5.6-terra"}]
    assert not guard.evaluate_usage("openai", payload, model="gpt-5.6-terra")["allowed"]


def test_check_never_returns_raw_api_fields(monkeypatch):
    payload = _claude()
    payload["private_field"] = "private-test-value"
    monkeypatch.setattr(guard, "_fetch_usage", lambda _provider: payload)
    result = guard.check_subscription("anthropic")
    assert result["allowed"] is True
    assert "private-test-value" not in json.dumps(result)
    assert result["checked_at"]


def test_http_errors_are_redacted(monkeypatch):
    def fail(_provider):
        raise HTTPError("https://private-test-value", 401, "private-test-value", {}, None)
    monkeypatch.setattr(guard, "_fetch_usage", fail)
    result = guard.check_subscription("openai")
    assert result["allowed"] is False
    assert result["reason"] == "quota_api_http_401"
    assert "private-test-value" not in json.dumps(result)


def test_redirects_are_refused_before_authorization_can_cross_hosts():
    with pytest.raises(guard.GuardError, match="redirect_refused"):
        guard._NoRedirect().redirect_request(None, None, None, None, None, None)


@pytest.mark.parametrize("rate,reason", [
    ({"events": [{"isUsingOverage": True}]}, "paid_usage_detected_or_available"),
    ({"observed": [{"credits": {"has_credits": True}}]}, "paid_usage_detected_or_available"),
    ({"events": [{"utilization": 0.97}]}, "quota_headroom_low"),
    ({"observed": [{"primary": {"used_percent": 96}}]}, "quota_headroom_low"),
    ({"observed": [{"primary": {"used_percent": 100}}]}, "quota_exhausted"),
    ({"events": [{"utilization": 1}]}, "quota_exhausted"),
    ({"detected": True}, "quota_exhausted"),
    ({"events": [{"status": "allowed", "isUsingOverage": False, "utilization": 0.1}]}, None),
])
def test_runtime_metadata_stop_reasons(rate, reason):
    assert guard.runtime_stop_reason({"provider_audit": {"rate_limit": rate, "errors": []}}) == reason


def test_runtime_classifier_ignores_generated_text():
    result = {"provider_audit": {"rate_limit": {}, "errors": []},
              "assistant_text": "quota exhausted; paid_overage_observed"}
    assert guard.runtime_stop_reason(result) is None


def test_runtime_classifier_handles_malformed_error_metadata():
    assert guard.runtime_stop_reason({"provider_audit": {"errors": None}}) == "provider_audit_unavailable"
