"""Scheduled fault injection.

Faults are declared per task and triggered either by a device's transaction
index (preferred: deterministic regardless of agent speed) or by wall time
since farm start (only where physical time is the point, e.g. drift kicks
during closed-loop control).

Supported kinds (v0.1):
    link_drop     - close the device's connections; refuse reconnects for
                    ``duration_s`` seconds
    timeout_burst - delay every response by ``delay_s`` for ``duration_s``
    garbage_bytes - prefix the next ``count`` responses with junk bytes
    power_glitch  - device reboots: state returns to power-on defaults and
                    the connection drops momentarily
    stuck_value   - measurement values freeze for ``duration_s``
    drift_step    - kick a DUT parameter (``dut``, ``field``, ``delta``)
    error_flood   - push ``count`` spurious errors into the device queue
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class FaultSpec:
    kind: str
    dev: Optional[str] = None          # device the trigger counts / applies to
    after_txn: Optional[int] = None    # fire when dev txn count reaches this
    at_t: Optional[float] = None       # or: seconds since farm start
    duration_s: float = 0.0
    params: Dict[str, Any] = field(default_factory=dict)
    fired: bool = False

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "FaultSpec":
        known = {"kind", "dev", "after_txn", "at_t", "duration_s"}
        params = {k: v for k, v in d.items() if k not in known}
        return cls(
            kind=str(d["kind"]),
            dev=d.get("dev"),
            after_txn=d.get("after_txn"),
            at_t=d.get("at_t"),
            duration_s=float(d.get("duration_s", 0.0)),
            params=params,
        )


class FaultInjector:
    """Evaluates the schedule and exposes the currently-active effects."""

    def __init__(self, specs: List[FaultSpec], recorder, t0: Optional[float] = None) -> None:
        self.specs = specs
        self.recorder = recorder
        self.t0 = time.monotonic() if t0 is None else t0
        self._active: List[Dict[str, Any]] = []
        self._garbage_budget: Dict[str, int] = {}
        self.on_power_glitch: Optional[Callable[[str], None]] = None
        self.on_drift_step: Optional[Callable[[str, str, float], None]] = None
        self.on_stuck: Optional[Callable[[str, bool], None]] = None
        self.on_error_flood: Optional[Callable[[str, int], None]] = None

    # ------------------------------------------------------------------

    def poll(self, dev: str, txn_count: int) -> None:
        """Called by the transport after each transaction on ``dev``."""
        now_rel = time.monotonic() - self.t0
        for spec in self.specs:
            if spec.fired:
                continue
            hit = False
            if spec.after_txn is not None and (spec.dev in (None, dev)) and txn_count >= spec.after_txn:
                hit = True
            if spec.at_t is not None and now_rel >= spec.at_t:
                hit = True
            if hit:
                self._fire(spec)
        self._expire()

    def poll_time(self) -> None:
        """Called periodically by the farm for purely time-triggered faults."""
        now_rel = time.monotonic() - self.t0
        for spec in self.specs:
            if not spec.fired and spec.at_t is not None and now_rel >= spec.at_t:
                self._fire(spec)
        self._expire()

    def _fire(self, spec: FaultSpec) -> None:
        spec.fired = True
        target = spec.dev or spec.params.get("dut") or "farm"
        self.recorder.log_fault(spec.kind, "begin", target=target, duration_s=spec.duration_s)
        entry = {
            "kind": spec.kind,
            "dev": spec.dev,
            "until": time.monotonic() + spec.duration_s,
            "params": spec.params,
        }
        if spec.kind == "garbage_bytes":
            self._garbage_budget[spec.dev or ""] = int(spec.params.get("count", 1))
        if spec.kind == "power_glitch" and self.on_power_glitch and spec.dev:
            self.on_power_glitch(spec.dev)
        if spec.kind == "drift_step" and self.on_drift_step:
            self.on_drift_step(
                str(spec.params.get("dut")),
                str(spec.params.get("field", "f0")),
                float(spec.params.get("delta", 0.0)),
            )
        if spec.kind == "stuck_value" and self.on_stuck and spec.dev:
            self.on_stuck(spec.dev, True)
        if spec.kind == "error_flood" and self.on_error_flood and spec.dev:
            self.on_error_flood(spec.dev, int(spec.params.get("count", 5)))
        if spec.duration_s > 0:
            self._active.append(entry)
        else:
            self.recorder.log_fault(spec.kind, "end", target=target)

    def _expire(self) -> None:
        now = time.monotonic()
        still: List[Dict[str, Any]] = []
        for entry in self._active:
            if now >= entry["until"]:
                self.recorder.log_fault(entry["kind"], "end", target=entry["dev"] or "farm")
                if entry["kind"] == "stuck_value" and self.on_stuck and entry["dev"]:
                    self.on_stuck(entry["dev"], False)
            else:
                still.append(entry)
        self._active = still

    # ------------------------------------------------------------------
    # Effect queries (transport-side)
    # ------------------------------------------------------------------

    def link_down(self, dev: str) -> bool:
        self._expire()
        return any(
            e["kind"] in ("link_drop", "power_glitch") and e["dev"] in (None, dev)
            for e in self._active
        )

    def response_delay(self, dev: str) -> float:
        self._expire()
        for e in self._active:
            if e["kind"] == "timeout_burst" and e["dev"] in (None, dev):
                return float(e["params"].get("delay_s", 5.0))
        return 0.0

    def take_garbage(self, dev: str) -> bytes:
        budget = self._garbage_budget.get(dev, 0)
        if budget > 0:
            self._garbage_budget[dev] = budget - 1
            return b"\xff\xfe??"
        return b""
