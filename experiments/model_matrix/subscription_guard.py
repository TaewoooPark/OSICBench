"""Read-only subscription quota guards; never generate, refresh, or buy usage.

Credentials remain in memory and are sent only to fixed first-party HTTPS
usage endpoints. Neither tokens nor raw API responses belong in public logs.
Check immediately before every invocation; the check cannot reserve quota or
prevent another client from concurrently spending the same account's budget.
"""
from __future__ import annotations

import getpass
import json
import math
import os
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, request


ENDPOINTS = {
    "anthropic": "https://api.anthropic.com/api/oauth/usage",
    "openai": "https://chatgpt.com/backend-api/wham/usage",
}


class GuardError(RuntimeError):
    """A fixed public reason code, never a raw credential or HTTP error."""


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise GuardError("quota_api_redirect_refused")


def _read_auth(provider: str) -> dict:
    if provider == "openai":
        source = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
        auth = json.loads(source.read_text(encoding="utf-8"))
        if auth.get("auth_mode") != "chatgpt":
            raise GuardError("subscription_auth_unverified")
        tokens = auth.get("tokens", {})
        token, account = tokens.get("access_token"), tokens.get("account_id")
        if not isinstance(token, str) or not token or not isinstance(account, str) or not account:
            raise GuardError("subscription_auth_unverified")
        return {"Authorization": "Bearer " + token, "ChatGPT-Account-Id": account}
    if provider != "anthropic":
        raise GuardError("unsupported_provider")
    auth = None
    if platform.system() == "Darwin":
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-a", getpass.getuser(),
             "-w", "-s", "Claude Code-credentials"],
            capture_output=True, text=True, timeout=10, check=False)
        if result.returncode == 0:
            auth = json.loads(result.stdout)
        elif result.returncode != 44:
            raise GuardError("subscription_auth_unavailable")
    if auth is None:
        auth = json.loads((Path.home() / ".claude/.credentials.json").read_text(encoding="utf-8"))
    oauth = auth.get("claudeAiOauth", {})
    token = oauth.get("accessToken")
    if not isinstance(token, str) or not token or not oauth.get("subscriptionType"):
        raise GuardError("subscription_auth_unverified")
    return {"Authorization": "Bearer " + token, "anthropic-beta": "oauth-2025-04-20"}


def _fetch_usage(provider: str) -> dict:
    headers = _read_auth(provider)
    headers.update({"Accept": "application/json", "User-Agent": "OSICBench-subscription-quota-guard"})
    query = request.Request(ENDPOINTS[provider], headers=headers, method="GET")
    opener = request.build_opener(request.ProxyHandler({}), _NoRedirect())
    with opener.open(query, timeout=15) as response:
        payload = json.loads(response.read(1024 * 1024))
    if not isinstance(payload, dict):
        raise GuardError("quota_response_invalid")
    return payload


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _window(name: str, value: object, percent_key: str) -> dict | None:
    if not isinstance(value, dict):
        return None
    used = _number(value.get(percent_key))
    if used is None or not 0.0 <= used <= 100.0:
        return None
    return {"name": name, "used_percent": used, "remaining_percent": 100.0 - used}


def evaluate_usage(provider: str, payload: dict, *, model: str | None = None,
                   minimum_remaining_percent: float = 5.0) -> dict:
    """Normalize a usage response conservatively, without exposing raw data."""
    threshold = _number(minimum_remaining_percent)
    if threshold is None or not 0 <= threshold <= 100:
        raise ValueError("minimum_remaining_percent must be between 0 and 100")
    windows, reason, paid = [], None, None
    if provider == "anthropic":
        for key in ("five_hour", "seven_day"):
            window = _window(key, payload.get(key), "utilization")
            if window is None:
                reason = "quota_state_unverified"
            else:
                windows.append(window)
        for key, value in payload.items():
            if key in ("five_hour", "seven_day", "extra_usage") or value is None:
                continue
            if key.startswith(("five_hour_", "seven_day_")):
                if key.endswith("_opus") and model and "opus" not in model:
                    continue
                if key.endswith("_sonnet") and model and "sonnet" not in model:
                    continue
                window = _window(key, value, "utilization")
                if window is None:
                    reason = "quota_state_unverified"
                else:
                    windows.append(window)
        extra = payload.get("extra_usage")
        if isinstance(extra, dict) and isinstance(extra.get("is_enabled"), bool):
            paid = extra["is_enabled"]
        else:
            reason = "paid_usage_state_unverified"
    elif provider == "openai":
        rate = payload.get("rate_limit")
        if not isinstance(rate, dict):
            reason = "quota_state_unverified"
            rate = {}
        if rate.get("allowed") is False or rate.get("limit_reached") is True:
            reason = "quota_exhausted"
        elif rate.get("allowed") is not True or rate.get("limit_reached") is not False:
            reason = "quota_state_unverified"
        primary = _window("primary", rate.get("primary_window"), "used_percent")
        if primary is None:
            reason = "quota_state_unverified"
        else:
            windows.append(primary)
        if rate.get("secondary_window") is not None:
            secondary = _window("secondary", rate["secondary_window"], "used_percent")
            if secondary is None:
                reason = "quota_state_unverified"
            else:
                windows.append(secondary)
        additional = payload.get("additional_rate_limits")
        if additional is not None and not isinstance(additional, list):
            reason = "quota_state_unverified"
        for entry in additional if isinstance(additional, list) else []:
            if not isinstance(entry, dict):
                reason = "quota_state_unverified"
                continue
            specific_model = entry.get("normal_model_slug")
            if model and isinstance(specific_model, str) and specific_model != model:
                continue
            specific = entry.get("rate_limit")
            if not isinstance(specific, dict):
                reason = "quota_state_unverified"
                continue
            if specific.get("allowed") is False or specific.get("limit_reached") is True:
                reason = "quota_exhausted"
            elif specific.get("allowed") is not True or specific.get("limit_reached") is not False:
                reason = "quota_state_unverified"
            for key in ("primary_window", "secondary_window"):
                if key == "secondary_window" and specific.get(key) is None:
                    continue
                window = _window(f"model_specific_{key}", specific.get(key), "used_percent")
                if window is None:
                    reason = "quota_state_unverified"
                else:
                    windows.append(window)
        credits = payload.get("credits")
        if (isinstance(credits, dict) and isinstance(credits.get("has_credits"), bool)
                and isinstance(credits.get("unlimited"), bool)):
            balance = _number(credits.get("balance"))
            paid = credits["has_credits"] or credits["unlimited"] or (balance is not None and balance > 0)
        else:
            reason = "paid_usage_state_unverified"
        if payload.get("rate_limit_reached_type"):
            reason = "quota_exhausted"
    else:
        raise ValueError("unsupported provider")
    remaining = min((window["remaining_percent"] for window in windows), default=None)
    if remaining is not None and remaining < threshold:
        reason = "quota_headroom_low"
    if paid is True:
        reason = "paid_usage_enabled"
    return {"allowed": reason is None and bool(windows) and paid is False,
            "reason": reason, "provider": provider, "remaining_percent": remaining,
            "minimum_remaining_percent": threshold, "windows": windows,
            "paid_usage_enabled": paid}


def check_subscription(provider: str, model: str | None = None,
                       minimum_remaining_percent: float = 5.0) -> dict:
    """Read current first-party usage; any unverified state blocks a launch."""
    checked_at = datetime.now(timezone.utc).isoformat()
    try:
        if provider not in ENDPOINTS:
            raise GuardError("unsupported_provider")
        result = evaluate_usage(provider, _fetch_usage(provider), model=model,
                                minimum_remaining_percent=minimum_remaining_percent)
    except error.HTTPError as exc:
        result = {"allowed": False, "reason": f"quota_api_http_{exc.code}"}
    except GuardError as exc:
        result = {"allowed": False, "reason": str(exc)}
    except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
        result = {"allowed": False, "reason": "quota_check_unavailable"}
    result.update(provider=provider, checked_at=checked_at)
    return result


def runtime_stop_reason(result: dict, minimum_remaining_percent: float = 5.0) -> str | None:
    """Classify only provider audit metadata, never arbitrary generated text."""
    audit = result.get("provider_audit", result)
    if not isinstance(audit, dict):
        return "provider_audit_unavailable"
    errors = audit.get("errors", [])
    if not isinstance(errors, list):
        return "provider_audit_unavailable"
    if "paid_overage_observed" in errors:
        return "paid_usage_detected"
    rate = audit.get("rate_limit", {})
    if not isinstance(rate, dict):
        return "quota_state_unverified"
    paid, exhausted, low = False, False, False

    def visit(value):
        nonlocal paid, exhausted, low
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            paid |= value.get("isUsingOverage") is True or value.get("using_overage") is True
            exhausted |= (value.get("detected") is True or value.get("limit_reached") is True
                          or value.get("allowed") is False
                          or value.get("status") in ("rejected", "blocked"))
            if isinstance(value.get("credits"), dict):
                credits = value["credits"]
                balance = _number(credits.get("balance"))
                paid |= (credits.get("has_credits") is True or credits.get("unlimited") is True
                         or (balance is not None and balance > 0))
            for key in ("used_percent", "usedPercent"):
                used = _number(value.get(key))
                exhausted |= used is not None and used >= 100
                low |= used is not None and 100.0 - used < minimum_remaining_percent
            if "utilization" in value:
                utilization = _number(value["utilization"])
                if utilization is not None and 0 <= utilization <= 1:
                    exhausted |= utilization >= 1
                    low |= 100.0 * (1.0 - utilization) < minimum_remaining_percent
            for item in value.values():
                if isinstance(item, (list, dict)):
                    visit(item)

    visit(rate)
    if paid:
        return "paid_usage_detected_or_available"
    if exhausted or "rate_limit_not_allowed" in errors:
        return "quota_exhausted"
    if low:
        return "quota_headroom_low"
    return None
