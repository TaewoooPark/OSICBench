"""Farm assembly and runtime.

Reads a task's ``farm`` configuration, builds devices / DUTs / wiring /
faults from one seed, serves every device on its own 127.0.0.1 port, runs
the 10 Hz sampler (ground-truth physics for grading, device tick hooks,
time-triggered faults), and writes a final state snapshot on shutdown.

Run standalone:
    python -m osicsim.farm --config task.yaml --seed 3 --out rundir/
The farm writes ``endpoints.json`` into the run dir when every device is
listening, and shuts down cleanly on SIGTERM/SIGINT.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import seeding
from .circuit import Wire, WiringHub
from .faults import FaultInjector, FaultSpec
from .instruments import REGISTRY
from .physics import build_dut
from .recorder import FlightRecorder
from .transport import DeviceServer, Quirks

SAMPLER_PERIOD_S = 0.1


class Farm:
    def __init__(self, config: Dict[str, Any], seed: int, out_dir: Path) -> None:
        self.config = config
        self.seed = int(seed)
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.recorder = FlightRecorder(self.out_dir / "recorder.jsonl")
        self.hub = WiringHub()
        self.devices: Dict[str, Any] = {}
        self.servers: Dict[str, DeviceServer] = {}
        self._stopping = False
        self._build()

    # ------------------------------------------------------------------

    def _build(self) -> None:
        farm_cfg = self.config.get("farm", self.config)

        for name, spec in (farm_cfg.get("duts") or {}).items():
            dut = build_dut(name, spec["model"], self.seed, spec.get("params") or {})
            self.hub.add_dut(dut)

        injector_specs = [FaultSpec.from_dict(d) for d in (farm_cfg.get("faults") or [])]
        self.injector = FaultInjector(injector_specs, self.recorder)

        for name, spec in (farm_cfg.get("devices") or {}).items():
            cls = REGISTRY[spec["type"]]
            dev = cls(name)
            dev.attach(
                self.hub,
                self.recorder,
                seeding.derive_rng(self.seed, "dev", name, "meter-noise"),
                spec.get("options") or {},
            )
            self.devices[name] = dev
            self.hub.add_device(name, dev.get_export)
            quirks = Quirks.from_dict(spec.get("quirks") or {})
            self.servers[name] = DeviceServer(dev, quirks, self.recorder, self.injector)

        for wire_cfg in farm_cfg.get("wiring") or []:
            self.hub.add_wire(Wire.from_dict(wire_cfg))
        self.hub.finalize()

        self.sample_spec: List[Dict[str, str]] = list(farm_cfg.get("phys_sample") or [])

        self.injector.on_power_glitch = self._fault_power_glitch
        self.injector.on_drift_step = self._fault_drift
        self.injector.on_stuck = self._fault_stuck
        self.injector.on_error_flood = self._fault_error_flood

    # -- fault callbacks -------------------------------------------------

    def _fault_power_glitch(self, dev: str) -> None:
        device = self.devices.get(dev)
        if device is None:
            return
        device.power_on()
        self.recorder.log_state(dev, "power", "on", "rebooted-defaults")
        server = self.servers.get(dev)
        if server is not None:
            server.drop_connections()

    def _fault_drift(self, dut: str, fld: str, delta: float) -> None:
        try:
            self.hub.dut(dut).kick(fld, delta)
            self.recorder.log(dut, "phys", field=f"kick:{fld}", value=delta)
        except KeyError:
            pass

    def _fault_stuck(self, dev: str, stuck: bool) -> None:
        device = self.devices.get(dev)
        if device is not None:
            device.stuck = stuck
            if not stuck:
                device._stuck_cache.clear()

    def _fault_error_flood(self, dev: str, count: int) -> None:
        device = self.devices.get(dev)
        if device is not None:
            for i in range(count):
                device.push_error(-360, f"Communication error;spurious {i + 1}")

    # ------------------------------------------------------------------

    async def start(self) -> Dict[str, Dict[str, Any]]:
        endpoints: Dict[str, Dict[str, Any]] = {}
        for name, server in self.servers.items():
            port = await server.start()
            endpoints[name] = {
                "host": "127.0.0.1",
                "port": port,
                "resource": f"TCPIP0::127.0.0.1::{port}::SOCKET",
                "type": self.config.get("farm", self.config)["devices"][name]["type"],
            }
        (self.out_dir / "endpoints.json").write_text(json.dumps(endpoints, indent=2))
        self._sampler_task = asyncio.create_task(self._sampler())
        return endpoints

    async def _sampler(self) -> None:
        while not self._stopping:
            now = time.monotonic()
            for entry in self.sample_spec:
                node, fld = entry["node"], entry["field"]
                try:
                    if node in self.hub.duts():
                        value = self.hub.dut(node).output(fld, now)
                    else:
                        value = self.devices[node].get_export(fld)
                except KeyError:
                    continue
                self.recorder.log(node, "phys_sample", field=fld, value=value)
            for dev in self.devices.values():
                dev.tick(now)
            self.injector.poll_time()
            await asyncio.sleep(SAMPLER_PERIOD_S)

    async def stop(self, reason: str = "normal") -> None:
        if self._stopping:
            return
        self._stopping = True
        try:
            self._sampler_task.cancel()
        except Exception:
            pass
        states: Dict[str, Dict[str, Any]] = {}
        for name, dev in self.devices.items():
            try:
                states[name] = dev.state_summary()
            except Exception as exc:
                states[name] = {"error": str(exc)}
        for name, dut in self.hub.duts().items():
            try:
                states[f"dut:{name}"] = dut.sample_fields()
            except Exception as exc:
                states[f"dut:{name}"] = {"error": str(exc)}
        self.recorder.snapshot(states)
        for server in self.servers.values():
            await server.stop()
        self.recorder.close(reason)


async def run_farm(config: Dict[str, Any], seed: int, out_dir: Path) -> None:
    farm = Farm(config, seed, out_dir)
    await farm.start()
    stop_event = asyncio.Event()

    def _request_stop(*_args) -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:  # non-POSIX fallback
            signal.signal(sig, lambda *_: _request_stop())
    await stop_event.wait()
    await farm.stop("signal")


def load_config(path: Path) -> Dict[str, Any]:
    import yaml

    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run an osicsim instrument farm.")
    parser.add_argument("--config", required=True, help="task.yaml (or any yaml with a farm section)")
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--out", required=True, help="run directory (endpoints.json, recorder.jsonl)")
    args = parser.parse_args(argv)
    config = load_config(Path(args.config))
    asyncio.run(run_farm(config, args.seed, Path(args.out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
