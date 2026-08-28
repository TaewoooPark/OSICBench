"""MER-P330 dual-channel DC power supply.

Channel state machine: commands address the SELECTED channel
(``INSTrument:SELect OUT1|OUT2``) - a classic statefulness trap documented
in the manual. Over-voltage protection latches the channel off; the latch
must be cleared with ``VOLTage:PROTection:CLEar`` before the output can be
re-enabled.
"""
from __future__ import annotations

import time
from typing import Dict, List

from .. import scpi
from ..device import ParamOutOfRange, Response, SCPIDevice, SettingsConflict
from ..physics import SettlingValue

V_RANGE = (0.0, 30.0)
I_RANGE = (0.0, 3.0)
OVP_RANGE = (1.0, 32.0)
SETTLE_TAU_S = 0.05


class _Channel:
    def __init__(self) -> None:
        self.volt = SettlingValue(0.0, SETTLE_TAU_S)
        self.ilim = 1.0
        self.output = False
        self.ovp = 32.0
        self.tripped = False


class MerP330(SCPIDevice):
    IDN = "Meridian Instruments,MER-P330,P330-0412,3.1"

    def build(self) -> None:
        self.register("INSTrument:SELect", write=self._w_sel, query=lambda: f"OUT{self.sel}")
        self.register("INSTrument:NSELect", write=self._w_nsel, query=lambda: str(self.sel))
        self.register("SOURce:VOLTage", write=self._w_volt, query=lambda: self._ch().volt.target)
        self.register("SOURce:CURRent", write=self._w_curr, query=lambda: self._ch().ilim)
        self.register("OUTPut", write=self._w_outp, query=lambda: self._ch().output)
        self.register("SOURce:VOLTage:PROTection", write=self._w_ovp, query=lambda: self._ch().ovp)
        self.register("SOURce:VOLTage:PROTection:TRIPped", query=lambda: self._ch().tripped)
        self.register("SOURce:VOLTage:PROTection:CLEar", write=self._w_clear)
        self.register("MEASure:VOLTage", query=self._q_meas_v)
        self.register("MEASure:CURRent", query=self._q_meas_i)

    def power_on(self) -> None:
        self.channels: Dict[int, _Channel] = {1: _Channel(), 2: _Channel()}
        self.sel = 1

    def _ch(self) -> _Channel:
        return self.channels[self.sel]

    # ------------------------------------------------------------------

    def _w_sel(self, args: List[str]) -> None:
        token = args[0].strip().upper()
        if token not in ("OUT1", "OUT2"):
            raise ParamOutOfRange(f"INST:SEL {token}")
        self.sel = int(token[-1])

    def _w_nsel(self, args: List[str]) -> None:
        n = int(scpi.parse_number(args[0]))
        if n not in (1, 2):
            raise ParamOutOfRange(f"INST:NSEL {n}")
        self.sel = n

    def _w_volt(self, args: List[str]) -> None:
        v = scpi.parse_number(args[0], minimum=V_RANGE[0], maximum=V_RANGE[1], default=0.0)
        if not (V_RANGE[0] <= v <= V_RANGE[1]):
            raise ParamOutOfRange(f"VOLT {v}")
        ch = self._ch()
        old = ch.volt.target
        ch.volt.set_target(v)
        self.record_state(f"volt_target_ch{self.sel}", old, v)

    def _w_curr(self, args: List[str]) -> None:
        i = scpi.parse_number(args[0], minimum=I_RANGE[0], maximum=I_RANGE[1], default=1.0)
        if not (I_RANGE[0] <= i <= I_RANGE[1]):
            raise ParamOutOfRange(f"CURR {i}")
        self._ch().ilim = i

    def _w_outp(self, args: List[str]) -> None:
        on = scpi.parse_bool(args[0])
        ch = self._ch()
        if on and ch.tripped:
            raise SettingsConflict("OVP tripped;clear protection first")
        self.record_state(f"output_ch{self.sel}", ch.output, on)
        ch.output = on
        self._check_trip(self.sel)

    def _w_ovp(self, args: List[str]) -> None:
        v = scpi.parse_number(args[0], minimum=OVP_RANGE[0], maximum=OVP_RANGE[1])
        if not (OVP_RANGE[0] <= v <= OVP_RANGE[1]):
            raise ParamOutOfRange(f"VOLT:PROT {v}")
        self._ch().ovp = v

    def _w_clear(self, args: List[str]) -> None:
        ch = self._ch()
        if ch.tripped:
            self.record_state(f"ovp_tripped_ch{self.sel}", True, False)
        ch.tripped = False

    # ------------------------------------------------------------------

    def _check_trip(self, n: int) -> None:
        ch = self.channels[n]
        if ch.output and ch.volt.value() > ch.ovp:
            ch.output = False
            ch.tripped = True
            self.record_state(f"ovp_tripped_ch{n}", False, True)
            self.record_state(f"output_ch{n}", True, False)
            self.push_error(601, f"Over-voltage protection tripped;OUT{n}")

    def _applied_v(self, n: int) -> float:
        ch = self.channels[n]
        return ch.volt.value() if ch.output else 0.0

    def _load_r(self, n: int) -> float:
        return float(self.options.get(f"r_load_ch{n}", 1e9))

    def _q_meas_v(self):
        v = self._applied_v(self.sel) + self.gauss(1e-3)
        return Response(payload=scpi.format_number(self.maybe_stuck(f"v{self.sel}", v)),
                        n_readings=1, latency_s=0.02)

    def _q_meas_i(self):
        v = self._applied_v(self.sel)
        i = min(v / self._load_r(self.sel), self._ch().ilim) + self.gauss(2e-4)
        return Response(payload=scpi.format_number(self.maybe_stuck(f"i{self.sel}", i)),
                        n_readings=1, latency_s=0.02)

    # ------------------------------------------------------------------

    def get_export(self, field: str) -> float:
        now = time.monotonic()
        for n in (1, 2):
            ch = self.channels[n]
            if field == f"v_ch{n}":
                return ch.volt.value(now) if ch.output else 0.0
            if field == f"p_ch{n}":
                v = ch.volt.value(now) if ch.output else 0.0
                i = min(v / self._load_r(n), ch.ilim)
                return v * i
        raise KeyError(field)

    def opc_delay(self) -> float:
        ch = self._ch()
        err = abs(ch.volt.value() - ch.volt.target)
        if err <= max(1e-3, 1e-3 * abs(ch.volt.target)):
            return 0.0
        return 4 * SETTLE_TAU_S

    def tick(self, now: float) -> None:
        for n in (1, 2):
            self._check_trip(n)

    def state_summary(self):
        return {
            f"ch{n}": {
                "output": self.channels[n].output,
                "volt_target": self.channels[n].volt.target,
                "tripped": self.channels[n].tripped,
            }
            for n in (1, 2)
        }
