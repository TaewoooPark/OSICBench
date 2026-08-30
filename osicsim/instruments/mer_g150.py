"""MER-G150 chamber gauge.

A single-quantity monitor: ``PRESsure?`` returns the chamber level in
kilopascals. Readings are instantaneous (50 ms latency, no integration
settings) with a small multiplicative noise. The gauge is the sensing
half of any interlock built on it - it never acts on anything itself.
"""
from __future__ import annotations

from .. import scpi
from ..device import Response, SCPIDevice

NOISE_FRAC = 0.004
READ_LATENCY_S = 0.05


class MerG150(SCPIDevice):
    IDN = "Meridian Instruments,MER-G150,G150-0771,1.1"

    def build(self) -> None:
        self.register("PRESsure", query=self._q_pressure)

    def power_on(self) -> None:
        pass

    def _q_pressure(self):
        level = self.hub.pull(self.name, "level", default=0.0) if self.hub else 0.0
        value = level * (1.0 + self.gauss(NOISE_FRAC))
        return Response(payload=scpi.format_number(self.maybe_stuck("p", value)),
                        n_readings=1, latency_s=READ_LATENCY_S)
