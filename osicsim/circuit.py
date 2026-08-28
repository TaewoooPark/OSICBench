"""Wiring between instruments and DUTs.

Pull-based resolution: when a meter converts, it pulls its input through the
hub; the hub follows the wire to a DUT output; the DUT in turn pulls ITS
inputs (bound at farm build time) from source-device exports. No global
tick, no ordering hazards - every read reflects the physical state at the
moment of conversion, including sources that are still settling.

Wire declaration (task farm config):
    wiring:
      - {src: "smu1.source_v",  dst: "dut1.bias_v",  gain: 1.0}
      - {src: "dut1.i",         dst: "smu1.sense_i"}
      - {src: "coil1.source_i", dst: "dut2.h",       gain: 50.0}   # field/amp
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

from .physics import DUT


@dataclass(frozen=True)
class Wire:
    src_node: str
    src_field: str
    dst_node: str
    dst_field: str
    gain: float = 1.0
    offset: float = 0.0

    @classmethod
    def from_dict(cls, d: dict) -> "Wire":
        src_node, src_field = str(d["src"]).split(".", 1)
        dst_node, dst_field = str(d["dst"]).split(".", 1)
        return cls(src_node, src_field, dst_node, dst_field,
                   gain=float(d.get("gain", 1.0)), offset=float(d.get("offset", 0.0)))


class WiringHub:
    """Resolves wires between device exports and DUT inputs/outputs."""

    def __init__(self) -> None:
        self._duts: Dict[str, DUT] = {}
        self._device_exports: Dict[str, Callable[[str], float]] = {}
        self._wires_by_dst: Dict[Tuple[str, str], Wire] = {}

    def add_dut(self, dut: DUT) -> None:
        self._duts[dut.name] = dut

    def add_device(self, name: str, export_fn: Callable[[str], float]) -> None:
        """export_fn(field) returns a device's live physical export
        (e.g. a source's settled output level)."""
        self._device_exports[name] = export_fn

    def add_wire(self, wire: Wire) -> None:
        key = (wire.dst_node, wire.dst_field)
        if key in self._wires_by_dst:
            raise ValueError(f"duplicate wire into {wire.dst_node}.{wire.dst_field}")
        self._wires_by_dst[key] = wire

    def finalize(self) -> None:
        """Bind every DUT input that has a wire into it."""
        for (dst_node, dst_field), wire in self._wires_by_dst.items():
            if dst_node in self._duts:
                dut = self._duts[dst_node]
                dut.bind_input(dst_field, self._make_source_fn(wire))

    def _make_source_fn(self, wire: Wire) -> Callable[[], float]:
        def fn() -> float:
            return wire.gain * self._read_node(wire.src_node, wire.src_field) + wire.offset

        return fn

    def _read_node(self, node: str, field: str) -> float:
        if node in self._duts:
            return self._duts[node].output(field)
        if node in self._device_exports:
            return self._device_exports[node](field)
        raise KeyError(f"unknown node {node!r}")

    # ------------------------------------------------------------------
    # Meter-side pulls
    # ------------------------------------------------------------------

    def pull(self, dst_node: str, dst_field: str, default: Optional[float] = None) -> float:
        """A device pulls one of ITS wired inputs (e.g. dmm 'input_v')."""
        wire = self._wires_by_dst.get((dst_node, dst_field))
        if wire is None:
            if default is not None:
                return default
            raise KeyError(f"no wire into {dst_node}.{dst_field}")
        return wire.gain * self._read_node(wire.src_node, wire.src_field) + wire.offset

    def has_input(self, dst_node: str, dst_field: str) -> bool:
        return (dst_node, dst_field) in self._wires_by_dst

    def dut(self, name: str) -> DUT:
        return self._duts[name]

    def duts(self) -> Dict[str, DUT]:
        return dict(self._duts)
