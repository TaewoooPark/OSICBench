"""MER-D610 6.5-digit digital multimeter.

Interface facts that matter (see the manual):

- The interface requires CR+LF command termination and sends a greeting
  banner on connect.
- Reading noise scales as sigma = 200 uV / sqrt(NPLC). The power-on
  default is NPLC 0.06 ("FAST"): fine for monitoring, far too noisy for
  precision work.
- With autozero OFF, a residual input offset (up to +/-1 mV, unit
  dependent) adds to every reading. ``SYSTem:AZERo ONCE`` nulls it until
  the next NPLC change; ``ON`` (default) nulls continuously at the cost
  of doubled reading time.
- Buffered acquisition: ``SAMPle:COUNt N`` then ``INITiate`` starts an
  autonomous acquisition; ``TRACe:DATA?`` returns all N readings as one
  definite-length block, but errors (-230, no response) if the
  acquisition has not finished. ``*OPC?`` blocks until the buffer is done.
"""
from __future__ import annotations

import math
import time
from typing import List, Optional

from .. import scpi
from ..device import ParamOutOfRange, Response, ScpiCommandError, SCPIDevice

NPLC_RANGE = (0.02, 100.0)
NPLC_DEFAULT = 0.06
LINE_FREQ = 50.0
BASE_SIGMA_V = 200e-6  # at NPLC 1
SAMP_RANGE = (1, 10000)
READ_OVERHEAD_S = 0.004


class MerD610(SCPIDevice):
    IDN = "Meridian Instruments,MER-D610,D610-0093,1.7"

    def build(self) -> None:
        self.register("SENSe:VOLTage:DC:NPLCycles", write=self._w_nplc, query=lambda: self.nplc)
        self.register("SYSTem:AZERo", write=self._w_azer, query=self._q_azer)
        self.register("SAMPle:COUNt", write=self._w_samp, query=lambda: float(self.samp_count))
        self.register("INITiate", write=self._w_init)
        self.register("TRACe:DATA", query=self._q_trace)
        self.register("READ", query=self._q_read)
        self.register("FETCh", query=self._q_fetch)

    def power_on(self) -> None:
        self.nplc = NPLC_DEFAULT
        self.azer_mode = "ON"
        self.zeroed = True  # continuous autozero holds the offset nulled
        self.samp_count = 1
        self.buffer_start: Optional[float] = None
        self.buffer_n = 0
        self.buffer_nplc = NPLC_DEFAULT
        self.last_reading: Optional[float] = None

    # ------------------------------------------------------------------

    def _offset_v(self) -> float:
        """Residual offset, hidden physics: drawn once per device stream."""
        if not hasattr(self, "_offset_cache"):
            rng = self.rng or __import__("random").Random(0)
            magnitude = rng.uniform(4e-4, 9e-4)
            sign = 1.0 if rng.random() < 0.5 else -1.0
            self._offset_cache = sign * magnitude
        return self._offset_cache

    def _reading_time(self, nplc: float) -> float:
        t = nplc / LINE_FREQ + READ_OVERHEAD_S
        if self.azer_mode == "ON":
            t *= 2.0
        return t

    def _one_value(self, nplc: float) -> float:
        v = self.hub.pull(self.name, "input_v", default=0.0) if self.hub else 0.0
        if self.azer_mode == "OFF" and not self.zeroed:
            v += self._offset_v()
        v += self.gauss(BASE_SIGMA_V / math.sqrt(nplc))
        return self.maybe_stuck("v", v)

    # ------------------------------------------------------------------

    def _w_nplc(self, args: List[str]) -> None:
        v = scpi.parse_number(args[0], minimum=NPLC_RANGE[0], maximum=NPLC_RANGE[1],
                              default=NPLC_DEFAULT)
        if not (NPLC_RANGE[0] <= v <= NPLC_RANGE[1]):
            raise ParamOutOfRange(f"NPLC {v}")
        self.record_state("nplc", self.nplc, v)
        self.nplc = v
        if self.azer_mode == "OFF":
            self.zeroed = False  # a zero performed earlier no longer applies

    def _w_azer(self, args: List[str]) -> None:
        token = args[0].strip().upper()
        if token in ("ON", "1"):
            self.azer_mode, self.zeroed = "ON", True
        elif token in ("OFF", "0"):
            self.azer_mode = "OFF"
            self.zeroed = False
        elif token == "ONCE":
            self.azer_mode = "OFF"
            self.zeroed = True
        else:
            raise ParamOutOfRange(f"AZER {token}")
        self.record_state("azer", None, f"{self.azer_mode}/zeroed={self.zeroed}")

    def _q_azer(self):
        return self.azer_mode

    def _w_samp(self, args: List[str]) -> None:
        n = int(scpi.parse_number(args[0], minimum=SAMP_RANGE[0], maximum=SAMP_RANGE[1]))
        if not (SAMP_RANGE[0] <= n <= SAMP_RANGE[1]):
            raise ParamOutOfRange(f"SAMP:COUN {n}")
        self.samp_count = n

    def _w_init(self, args: List[str]) -> None:
        self.buffer_start = time.monotonic()
        self.buffer_n = self.samp_count
        self.buffer_nplc = self.nplc

    def _buffer_remaining(self) -> float:
        if self.buffer_start is None:
            return 0.0
        need = self.buffer_n * self._reading_time(self.buffer_nplc)
        return max(0.0, need - (time.monotonic() - self.buffer_start))

    def _q_trace(self):
        if self.buffer_start is None:
            raise ScpiCommandError(-230, "Data corrupt or stale;no acquisition initiated")
        if self._buffer_remaining() > 0:
            raise ScpiCommandError(-230, "Data corrupt or stale;acquisition in progress")
        values = [self._one_value(self.buffer_nplc) for _ in range(self.buffer_n)]
        n = self.buffer_n
        self.buffer_start = None
        return Response(payload=scpi.encode_block(values), n_readings=n, latency_s=0.01)

    def _q_read(self):
        value = self._one_value(self.nplc)
        self.last_reading = value
        return Response(payload=scpi.format_number(value), n_readings=1,
                        latency_s=self._reading_time(self.nplc))

    def _q_fetch(self):
        if self.last_reading is None:
            raise ScpiCommandError(-230, "Data corrupt or stale;no reading available")
        return Response(payload=scpi.format_number(self.last_reading), n_readings=1,
                        latency_s=0.002)

    # ------------------------------------------------------------------

    def opc_delay(self) -> float:
        return self._buffer_remaining()

    def state_summary(self):
        return {"nplc": self.nplc, "azer": self.azer_mode}
