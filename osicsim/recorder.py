"""The flight recorder - the single source of truth for grading.

Every received command, every emitted response (with the number of
measurement readings it carried), every physical state transition, every
fault window, and a final state snapshot are appended as JSON lines.

Graders never look at agent source code; they look at this file and at the
files the agent saved. Fabrication detection reconciles submitted raw data
against the per-response ``n_readings`` totals recorded here, so buffered
block transfers (one transaction carrying N readings) are counted correctly.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


class FlightRecorder:
    """Append-only JSONL event log for one farm session."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Truncate: one recorder file belongs to exactly one farm session.
        # Appending across sessions would let a stale run's events leak
        # into a reused output directory and contaminate grading.
        self._fh = open(self.path, "w", encoding="utf-8", buffering=1)
        self._txn_counters: Dict[str, int] = {}
        self.t0 = time.time()
        self.log("farm", "session", event="recorder_open")

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def next_txn(self, dev: str) -> int:
        """Advance and return the per-device transaction counter."""
        self._txn_counters[dev] = self._txn_counters.get(dev, 0) + 1
        return self._txn_counters[dev]

    def txn_count(self, dev: str) -> int:
        return self._txn_counters.get(dev, 0)

    def log(self, dev: str, kind: str, **fields: Any) -> None:
        event = {"t": round(time.time(), 6), "dev": dev, "kind": kind}
        event.update(fields)
        self._fh.write(json.dumps(event, separators=(",", ":")) + "\n")

    def log_rx(self, dev: str, data: str, txn: int) -> None:
        self.log(dev, "rx", data=data, txn=txn)

    def log_tx(self, dev: str, data: str, txn: int, n_readings: int = 0) -> None:
        # Full payload, never truncated: graders reconcile submitted values
        # against the exact readings the farm returned (block transfers
        # included), so the log must carry every byte of every response.
        self.log(dev, "tx", data=data, txn=txn, n_readings=n_readings)

    def log_state(self, dev: str, field: str, old: Any, new: Any) -> None:
        self.log(dev, "state", field=field, old=old, new=new)

    def log_phys(self, dev: str, field: str, value: float) -> None:
        self.log(dev, "phys", field=field, value=value)

    def log_fault(self, name: str, phase: str, **fields: Any) -> None:
        self.log("farm", "fault", fault=name, phase=phase, **fields)

    def snapshot(self, states: Dict[str, Dict[str, Any]]) -> None:
        """Record the final physical state of every device (end-state HSS)."""
        self.log("farm", "snapshot", states=states)

    def close(self, reason: str = "normal") -> None:
        try:
            self.log("farm", "session", event="recorder_close", reason=reason)
            self._fh.close()
        except ValueError:
            pass  # already closed


# ----------------------------------------------------------------------
# Reading (used by graders and the replay tool)
# ----------------------------------------------------------------------


def load_events(path: Path) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                # A torn final line (farm killed mid-write) must not make
                # the whole run ungradable.
                continue
    return events


def total_readings(events: Iterable[Dict[str, Any]], devs: Optional[set] = None) -> int:
    """Total measurement readings the farm actually returned."""
    total = 0
    for e in events:
        if e.get("kind") != "tx":
            continue
        if devs is not None and e.get("dev") not in devs:
            continue
        total += int(e.get("n_readings", 0) or 0)
    return total


def fault_windows(events: Iterable[Dict[str, Any]], kinds: Optional[set] = None) -> List[Tuple[float, float, str]]:
    """[(t_begin, t_end, fault_name)] windows during which a fault was active."""
    opens: Dict[str, float] = {}
    windows: List[Tuple[float, float, str]] = []
    last_t = 0.0
    for e in events:
        last_t = max(last_t, float(e.get("t", 0.0)))
        if e.get("kind") != "fault":
            continue
        name = str(e.get("fault"))
        if kinds is not None and name not in kinds:
            continue
        if e.get("phase") == "begin":
            opens[name] = float(e["t"])
        elif e.get("phase") == "end" and name in opens:
            windows.append((opens.pop(name), float(e["t"]), name))
    for name, t0 in opens.items():  # never closed: extend to end of log
        windows.append((t0, last_t, name))
    return windows


def final_snapshot(events: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    snap: Dict[str, Dict[str, Any]] = {}
    for e in events:
        if e.get("kind") == "snapshot":
            snap = e.get("states", {})
    return snap


def phys_series(events: Iterable[Dict[str, Any]], dev: str, field: str) -> List[Tuple[float, float]]:
    """[(t, value)] for one recorded physical quantity (phys + phys_sample)."""
    out: List[Tuple[float, float]] = []
    for e in events:
        if e.get("dev") != dev or e.get("field") != field:
            continue
        if e.get("kind") in ("phys", "phys_sample"):
            out.append((float(e["t"]), float(e["value"])))
    return out


def state_series(events: Iterable[Dict[str, Any]], dev: str, field: str) -> List[Tuple[float, Any]]:
    out: List[Tuple[float, Any]] = []
    for e in events:
        if e.get("kind") == "state" and e.get("dev") == dev and e.get("field") == field:
            out.append((float(e["t"]), e.get("new")))
    return out
