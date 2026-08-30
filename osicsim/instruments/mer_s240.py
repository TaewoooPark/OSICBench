"""MER-S240 source-measure unit.

Source a voltage or a current; measure the complementary quantity, with
compliance clamping. Documented behaviors that matter (see the manual):

- ``READ?`` returns the most recent COMPLETED reading and never triggers
  one; ``MEASure:FRESh?`` triggers a new conversion and waits for it.
- Source level changes settle exponentially (tau = 250 ms); ``*OPC?``
  blocks until the source is settled to 0.1 %.
- Changing ``SOURce:FUNCtion`` while the output is on is a settings
  conflict (-221) and is ignored.
- ``OUTPut:WDOG <s>`` arms a bus watchdog that disables the output if no
  message arrives for <s> seconds (protection for unattended runs).
- ``STATus:QUEStionable:CONDition?`` bit 3 (value 8) reports compliance.
"""
from __future__ import annotations

import math
import time
from typing import List, Optional

from .. import scpi
from ..device import Response, SCPIDevice, SettingsConflict
from ..physics import SettlingValue

APERTURE_S = 0.020
SETTLE_TAU_S = 0.25
V_RANGE = (-20.0, 20.0)
I_RANGE = (-1.0, 1.0)
ILIM_RANGE = (1e-6, 1.05)
VLIM_RANGE = (0.2, 21.0)
WDOG_RANGE = (0.5, 60.0)


class MerS240(SCPIDevice):
    IDN = "Meridian Instruments,MER-S240,S240-1207,2.4"

    def build(self) -> None:
        self.register("SOURce:FUNCtion", write=self._w_func, query=lambda: self.func)
        self.register("SOURce:VOLTage", write=self._w_volt, query=lambda: self._target_of("VOLT"))
        self.register("SOURce:CURRent", write=self._w_curr, query=lambda: self._target_of("CURR"))
        self.register("SENSe:CURRent:PROTection", write=self._w_ilim, query=lambda: self.ilim)
        self.register("SENSe:VOLTage:PROTection", write=self._w_vlim, query=lambda: self.vlim)
        self.register("SYSTem:RSENse", write=self._w_rsen, query=lambda: self.rsen)
        self.register("OUTPut", write=self._w_outp, query=lambda: self.output)
        self.register("OUTPut:WDOG", write=self._w_wdog, query=self._q_wdog)
        self.register("INITiate", write=self._w_init)
        self.register("READ", query=self._q_read)
        self.register("MEASure:FRESh", query=self._q_fresh)
        self.register("MEASure:CURRent", query=self._q_meas_curr)
        self.register("MEASure:VOLTage", query=self._q_meas_volt)
        self.register("STATus:QUEStionable:CONDition", query=self._q_ques)

    def power_on(self) -> None:
        self.func = "VOLT"
        self.level = SettlingValue(0.0, SETTLE_TAU_S)
        self.output = False
        self.rsen = False
        self.ilim = 1.05e-4
        self.vlim = 21.0
        self.wdog_s: Optional[float] = None
        self.last_reading: Optional[float] = None
        self.in_compliance = False

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def _w_func(self, args: List[str]) -> None:
        token = args[0].strip().upper().strip("'\"")
        if token not in ("VOLT", "VOLTAGE", "CURR", "CURRENT"):
            raise SettingsConflict(f"unknown source function {token}")
        new = "VOLT" if token.startswith("VOLT") else "CURR"
        if self.output:
            raise SettingsConflict("source function change with output on")
        if new != self.func:
            self.record_state("source_func", self.func, new)
            self.func = new
            self.level = SettlingValue(0.0, SETTLE_TAU_S)

    def _w_volt(self, args: List[str]) -> None:
        v = scpi.parse_number(args[0], minimum=V_RANGE[0], maximum=V_RANGE[1], default=0.0)
        self._guard_range(v, V_RANGE, "SOUR:VOLT")
        if self.func != "VOLT":
            raise SettingsConflict("SOUR:VOLT while sourcing current")
        self._set_level(v)

    def _w_curr(self, args: List[str]) -> None:
        i = scpi.parse_number(args[0], minimum=I_RANGE[0], maximum=I_RANGE[1], default=0.0)
        self._guard_range(i, I_RANGE, "SOUR:CURR")
        if self.func != "CURR":
            raise SettingsConflict("SOUR:CURR while sourcing voltage")
        self._set_level(i)

    def _set_level(self, value: float) -> None:
        old = self.level.target
        self.level.set_target(value)
        self.record_state("source_target", old, value)

    def _target_of(self, func: str) -> float:
        """Setpoint readback; mismatched function is a conflict, like writes."""
        if self.func != func:
            raise SettingsConflict(
                f"SOUR:{func}? while sourcing "
                f"{'current' if self.func == 'CURR' else 'voltage'}")
        return self.level.target

    def _w_ilim(self, args: List[str]) -> None:
        v = scpi.parse_number(args[0], minimum=ILIM_RANGE[0], maximum=ILIM_RANGE[1])
        self._guard_range(v, ILIM_RANGE, "SENS:CURR:PROT")
        self.record_state("ilim", self.ilim, v)
        self.ilim = v

    def _w_vlim(self, args: List[str]) -> None:
        v = scpi.parse_number(args[0], minimum=VLIM_RANGE[0], maximum=VLIM_RANGE[1])
        self._guard_range(v, VLIM_RANGE, "SENS:VOLT:PROT")
        self.record_state("vlim", self.vlim, v)
        self.vlim = v

    def _w_rsen(self, args: List[str]) -> None:
        new = scpi.parse_bool(args[0])
        self.record_state("rsen", self.rsen, new)
        self.rsen = new

    def _w_outp(self, args: List[str]) -> None:
        new = scpi.parse_bool(args[0])
        self.record_state("output", self.output, new)
        self.output = new

    def _w_wdog(self, args: List[str]) -> None:
        token = args[0].strip().upper()
        if token in ("OFF", "0"):
            self.record_state("wdog_s", self.wdog_s, None)
            self.wdog_s = None
            return
        v = scpi.parse_number(token, minimum=WDOG_RANGE[0], maximum=WDOG_RANGE[1])
        self._guard_range(v, WDOG_RANGE, "OUTP:WDOG")
        self.record_state("wdog_s", self.wdog_s, v)
        self.wdog_s = v

    def _q_wdog(self):
        return "OFF" if self.wdog_s is None else scpi.format_number(self.wdog_s)

    @staticmethod
    def _guard_range(value: float, bounds, label: str) -> None:
        lo, hi = bounds
        if not (lo <= value <= hi):
            from ..device import ParamOutOfRange

            raise ParamOutOfRange(f"{label} {value}")

    # ------------------------------------------------------------------
    # Conversions
    # ------------------------------------------------------------------

    def _convert(self, quantity: str) -> float:
        """One conversion of 'CURR' or 'VOLT' at the present instant."""
        now = time.monotonic()
        applied = self.level.value(now) if self.output else 0.0
        self.in_compliance = False
        if self.func == "VOLT":
            if quantity == "VOLT":
                value = applied + self.gauss(abs(applied) * 1e-4 + 1e-5)
            else:
                raw = self._pull("sense_i") if self.output else 0.0
                if abs(raw) > self.ilim:
                    raw = math.copysign(self.ilim, raw)
                    self.in_compliance = True
                value = raw + self.gauss(abs(raw) * 5e-4 + 2e-11)
        else:  # sourcing current
            if quantity == "CURR":
                value = applied + self.gauss(abs(applied) * 1e-4 + 1e-8)
            else:
                field = "sense_v_4w" if self.rsen else "sense_v_2w"
                if self.hub is not None and not self.hub.has_input(self.name, field):
                    field = "sense_v"
                raw = self._pull(field) if self.output else 0.0
                if abs(raw) > self.vlim:
                    raw = math.copysign(self.vlim, raw)
                    self.in_compliance = True
                value = raw + self.gauss(abs(raw) * 2e-4 + 1e-6)
        return self.maybe_stuck(quantity, value)

    def _pull(self, field: str) -> float:
        if self.hub is None:
            return 0.0
        return self.hub.pull(self.name, field, default=0.0)

    def _complementary(self) -> str:
        return "CURR" if self.func == "VOLT" else "VOLT"

    def _w_init(self, args: List[str]) -> None:
        self.last_reading = self._convert(self._complementary())

    def _q_read(self):
        if self.last_reading is None:
            from ..device import ScpiCommandError

            raise ScpiCommandError(-230, "Data corrupt or stale;no reading available")
        return Response(payload=scpi.format_number(self.last_reading), n_readings=1,
                        latency_s=0.002)

    def _q_fresh(self):
        value = self._convert(self._complementary())
        self.last_reading = value
        return Response(payload=scpi.format_number(value), n_readings=1, latency_s=APERTURE_S)

    def _q_meas_curr(self):
        value = self._convert("CURR")
        self.last_reading = value
        return Response(payload=scpi.format_number(value), n_readings=1, latency_s=APERTURE_S)

    def _q_meas_volt(self):
        value = self._convert("VOLT")
        self.last_reading = value
        return Response(payload=scpi.format_number(value), n_readings=1, latency_s=APERTURE_S)

    def _q_ques(self):
        applied = self.level.value() if self.output else 0.0
        live = self.in_compliance
        if self.output and self.func == "VOLT":
            raw = self._pull("sense_i")
            live = abs(raw) > self.ilim
        elif self.output and self.func == "CURR":
            field = "sense_v_4w" if self.rsen else "sense_v_2w"
            if self.hub is not None and not self.hub.has_input(self.name, field):
                field = "sense_v"
            live = abs(self._pull(field)) > self.vlim
        _ = applied
        return "8" if live else "0"

    # ------------------------------------------------------------------
    # Farm surface
    # ------------------------------------------------------------------

    def get_export(self, field: str) -> float:
        now = time.monotonic()
        if field == "source_v":
            return self.level.value(now) if (self.output and self.func == "VOLT") else 0.0
        if field == "source_i":
            return self.level.value(now) if (self.output and self.func == "CURR") else 0.0
        raise KeyError(field)

    def opc_delay(self) -> float:
        now = time.monotonic()
        target = self.level.target
        err = abs(self.level.value(now) - target)
        threshold = max(1e-6, 1e-3 * max(abs(target), 1e-3))
        if err <= threshold:
            return 0.0
        return SETTLE_TAU_S * math.log(err / threshold)

    def tick(self, now: float) -> None:
        if self.wdog_s is not None and self.output:
            if now - self._last_msg_t > self.wdog_s:
                self.record_state("output", self.output, False)
                self.output = False
                self.push_error(603, "Watchdog timeout;output disabled")

    def state_summary(self):
        return {
            "output": self.output,
            "source_func": self.func,
            "source_target": self.level.target,
            "in_compliance": self.in_compliance,
        }
