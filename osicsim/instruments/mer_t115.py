"""MER-T115 temperature monitor.

Reads the fixture's sensor. ``KRDG?`` answers in kelvin, ``CRDG?`` in
degrees Celsius - the manual is explicit, and confusing the two is a
classic unit accident the readings themselves will not flag.
"""
from __future__ import annotations

from ..device import Response, SCPIDevice
from .. import scpi

SENSOR_SIGMA_K = 0.02
READ_LATENCY_S = 0.08


class MerT115(SCPIDevice):
    IDN = "Meridian Instruments,MER-T115,T115-2210,1.2"

    def build(self) -> None:
        self.register("KRDG", query=self._q_krdg)
        self.register("CRDG", query=self._q_crdg)

    def power_on(self) -> None:
        pass

    def _temp_k(self) -> float:
        t = self.hub.pull(self.name, "temp_k", default=293.15) if self.hub else 293.15
        return self.maybe_stuck("t", t + self.gauss(SENSOR_SIGMA_K))

    def _q_krdg(self):
        return Response(payload=scpi.format_number(self._temp_k()), n_readings=1,
                        latency_s=READ_LATENCY_S)

    def _q_crdg(self):
        return Response(payload=scpi.format_number(self._temp_k() - 273.15), n_readings=1,
                        latency_s=READ_LATENCY_S)
