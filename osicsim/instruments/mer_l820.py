"""MER-L820 lock-in amplifier with internal oscillator.

Behavior that matters (see the manual):

- The displayed value is a filtered estimate: after ANY change (drive
  frequency, sensitivity, or the signal itself) the display converges to
  the true value with the selected time constant. Wait ~5 time constants
  before trusting a reading.
- Sensitivity is a discrete full-scale table. A signal above full scale
  reads as the overrange sentinel 9.9e37 and sets the questionable-status
  overload bit. Reading noise is a fixed fraction of FULL SCALE, so an
  unnecessarily coarse sensitivity buries small signals.
"""
from __future__ import annotations

import math
import time
from typing import List

from .. import scpi
from ..device import ParamOutOfRange, Response, SCPIDevice

FREQ_RANGE = (1.0, 100_000.0)

SENS_FS_V = [1e-6, 2e-6, 5e-6, 1e-5, 2e-5, 5e-5, 1e-4, 2e-4, 5e-4,
             1e-3, 2e-3, 5e-3, 1e-2, 2e-2, 5e-2, 1e-1, 2e-1, 5e-1, 1.0]
OFLT_TAU_S = [0.01, 0.03, 0.1, 0.3, 1.0, 3.0]
NOISE_FRAC_OF_FS = 2e-4
READ_LATENCY_S = 0.02


class MerL820(SCPIDevice):
    IDN = "Meridian Instruments,MER-L820,L820-0538,4.0"

    def build(self) -> None:
        self.register("SOURce:FREQuency", write=self._w_freq, query=lambda: self.freq)
        self.register("SENSitivity", write=self._w_sens, query=lambda: str(self.sens_idx))
        self.register("OFLT", write=self._w_oflt, query=lambda: str(self.oflt_idx))
        self.register("OUTP", query=self._q_outp)
        self.register("STATus:QUEStionable:CONDition", query=self._q_ques)

    def power_on(self) -> None:
        self.freq = 1000.0
        self.sens_idx = 12          # 10 mV full scale
        self.oflt_idx = 2           # 100 ms
        self._disp = 0.0
        self._disp_t = time.monotonic()
        self._overloaded = False

    # ------------------------------------------------------------------

    def _w_freq(self, args: List[str]) -> None:
        f = scpi.parse_number(args[0], minimum=FREQ_RANGE[0], maximum=FREQ_RANGE[1])
        if not (FREQ_RANGE[0] <= f <= FREQ_RANGE[1]):
            raise ParamOutOfRange(f"FREQ {f}")
        self.record_state("freq", self.freq, f)
        self.freq = f

    def _w_sens(self, args: List[str]) -> None:
        i = int(scpi.parse_number(args[0]))
        if not (0 <= i < len(SENS_FS_V)):
            raise ParamOutOfRange(f"SENS {i}")
        self.sens_idx = i

    def _w_oflt(self, args: List[str]) -> None:
        i = int(scpi.parse_number(args[0]))
        if not (0 <= i < len(OFLT_TAU_S)):
            raise ParamOutOfRange(f"OFLT {i}")
        self.oflt_idx = i

    # ------------------------------------------------------------------

    def _true_signal(self) -> float:
        return self.hub.pull(self.name, "signal_r", default=0.0) if self.hub else 0.0

    def _display(self) -> float:
        """Advance the output filter toward the live true signal."""
        now = time.monotonic()
        tau = OFLT_TAU_S[self.oflt_idx]
        true = self._true_signal()
        dt = max(0.0, now - self._disp_t)
        self._disp = true + (self._disp - true) * math.exp(-dt / tau)
        self._disp_t = now
        return self._disp

    def _q_outp(self, args: List[str]):
        which = int(scpi.parse_number(args[0])) if args else 3
        if which not in (1, 2, 3, 4):
            raise ParamOutOfRange(f"OUTP? {which}")
        disp = self._display()
        fs = SENS_FS_V[self.sens_idx]
        if disp > fs:
            self._overloaded = True
            return Response(payload=scpi.format_number(scpi.POSITIVE_INFINITY),
                            n_readings=1, latency_s=READ_LATENCY_S)
        self._overloaded = False
        noise = self.gauss(fs * NOISE_FRAC_OF_FS)
        if which in (1, 3):        # X and R (in-phase rig: X ~ R)
            value = disp + noise
        elif which == 2:           # Y
            value = noise
        else:                      # theta (degrees)
            value = self.gauss(0.05)
        return Response(payload=scpi.format_number(self.maybe_stuck(f"o{which}", value)),
                        n_readings=1, latency_s=READ_LATENCY_S)

    def _q_ques(self):
        disp = self._display()
        return "1" if disp > SENS_FS_V[self.sens_idx] else "0"

    # ------------------------------------------------------------------

    def get_export(self, field: str) -> float:
        if field == "freq":
            return self.freq
        raise KeyError(field)

    def state_summary(self):
        return {"freq": self.freq, "sens_idx": self.sens_idx, "oflt_idx": self.oflt_idx}
