"""Human-readable replay of a flight-recorder log.

    osicsim-replay rundir/recorder.jsonl [--dev smu1] [--kind rx,tx,state]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from .recorder import load_events


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Replay a recorder.jsonl timeline.")
    parser.add_argument("path")
    parser.add_argument("--dev", default=None, help="filter by device name")
    parser.add_argument("--kind", default=None, help="comma-separated kinds to show")
    args = parser.parse_args(argv)

    events = load_events(Path(args.path))
    if not events:
        print("no events")
        return 1
    t0 = events[0]["t"]
    kinds = set(args.kind.split(",")) if args.kind else None
    for e in events:
        if args.dev and e.get("dev") != args.dev:
            continue
        if kinds and e.get("kind") not in kinds:
            continue
        rel = e["t"] - t0
        kind = e.get("kind")
        dev = e.get("dev")
        if kind in ("rx", "tx"):
            arrow = "->" if kind == "rx" else "<-"
            extra = f" [n={e['n_readings']}]" if e.get("n_readings") else ""
            print(f"{rel:9.3f}  {dev:10s} {arrow} {e.get('data','')!r}{extra}")
        elif kind == "state":
            print(f"{rel:9.3f}  {dev:10s} == {e.get('field')}: {e.get('old')} -> {e.get('new')}")
        elif kind in ("phys", "phys_sample"):
            print(f"{rel:9.3f}  {dev:10s} ~~ {e.get('field')} = {e.get('value'):.6g}")
        elif kind == "fault":
            print(f"{rel:9.3f}  FAULT {e.get('fault')} {e.get('phase')} ({e.get('target','')})")
        elif kind == "snapshot":
            print(f"{rel:9.3f}  SNAPSHOT {e.get('states')}")
        else:
            print(f"{rel:9.3f}  {dev:10s} .. {kind} {e.get('event','')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
