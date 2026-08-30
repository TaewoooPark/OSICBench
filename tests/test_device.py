"""Unit tests for the SCPIDevice base: dispatch, errors, common commands."""
import pytest

from osicsim import scpi
from osicsim.device import (
    ParamOutOfRange,
    Response,
    SCPIDevice,
    SettingsConflict,
)


class ToyPSU(SCPIDevice):
    IDN = "Meridian Instruments,TOY-1,00000001,1.0"

    def build(self):
        self.register("OUTPut", write=self._w_outp, query=lambda: self.output)
        self.register("SOURce:VOLTage", write=self._w_volt, query=lambda: self.volt)
        self.register("SOURce:RANGe", write=self._w_range, query=lambda: float(self.range))
        self.register("READ", query=self._q_read)

    def power_on(self):
        self.output = False
        self.volt = 0.0
        self.range = 10

    def _w_outp(self, args):
        self.output = scpi.parse_bool(args[0])

    def _w_volt(self, args):
        v = scpi.parse_number(args[0], minimum=0.0, maximum=10.0, default=1.0)
        if not (0.0 <= v <= 10.0):
            raise ParamOutOfRange(f"VOLT {v}")
        self.volt = v

    def _w_range(self, args):
        if self.output:
            raise SettingsConflict("range change with output on")
        self.range = int(scpi.parse_number(args[0]))

    def _q_read(self):
        return Response(payload=scpi.format_number(self.volt), n_readings=1, latency_s=0.0)


@pytest.fixture()
def dev():
    return ToyPSU("toy1")


def q(dev, message):
    """Send a message; return list of payload strings."""
    return [r.payload for r in dev.process_message(message)]


class TestDispatch:
    def test_idn(self, dev):
        assert q(dev, "*IDN?") == [ToyPSU.IDN]

    def test_short_and_long_forms(self, dev):
        dev.process_message(":sour:volt 2.5")
        assert q(dev, ":SOURCE:VOLTAGE?") == ["+2.500000E+00"]

    def test_chaining_relative(self, dev):
        dev.process_message(":SOUR:VOLT 3; RANG 5")
        assert dev.volt == 3.0 and dev.range == 5

    def test_read_counts_reading(self, dev):
        (resp,) = dev.process_message("READ?")
        assert resp.n_readings == 1


class TestErrorSemantics:
    def test_unknown_header_queued_silently(self, dev):
        assert dev.process_message(":BOGUS:CMD 1") == []
        code, msg = dev.pop_error()
        assert code == -113 and "Undefined header" in msg

    def test_failed_query_yields_no_response(self, dev):
        """The client sees a timeout, never an error string (real behavior)."""
        assert dev.process_message(":BOGUS?") == []

    def test_settings_conflict_ignored(self, dev):
        dev.process_message("OUTP ON")
        dev.process_message(":SOUR:RANG 2")
        assert dev.range == 10, "conflicting command must be IGNORED"
        assert q(dev, "SYST:ERR?")[0].startswith("-221,")

    def test_out_of_range_rejected(self, dev):
        dev.process_message(":SOUR:VOLT 99")
        assert dev.volt == 0.0
        assert q(dev, "SYST:ERR?")[0].startswith("-222,")

    def test_error_queue_fifo_and_empty(self, dev):
        dev.process_message(":BOGUS:A")
        dev.process_message(":SOUR:VOLT 99")
        assert q(dev, "SYST:ERR?")[0].startswith("-113,")
        assert q(dev, "SYST:ERR?")[0].startswith("-222,")
        assert q(dev, "SYST:ERR?")[0].startswith('0,"No error"')

    def test_queue_overflow_marks_350(self, dev):
        for _ in range(30):
            dev.process_message(":BOGUS:X")
        codes = []
        while True:
            first = q(dev, "SYST:ERR?")[0]
            code = int(first.split(",")[0])
            if code == 0:
                break
            codes.append(code)
        assert -350 in codes and len(codes) <= 20

    def test_cls_clears(self, dev):
        dev.process_message(":BOGUS:X")
        dev.process_message("*CLS")
        assert q(dev, "SYST:ERR?")[0].startswith('0,')


class TestCommon:
    def test_rst_restores_power_on_defaults(self, dev):
        dev.process_message(":SOUR:VOLT 5;:OUTP ON")
        dev.process_message("*RST")
        assert dev.volt == 0.0 and dev.output is False

    def test_opc_returns_1(self, dev):
        assert q(dev, "*OPC?") == ["1"]

    def test_esr_latches_and_clears_on_read(self, dev):
        dev.process_message(":SOUR:VOLT 99")
        first = int(q(dev, "*ESR?")[0])
        second = int(q(dev, "*ESR?")[0])
        assert first != 0 and second == 0

    def test_stb_reflects_error_queue(self, dev):
        assert int(q(dev, "*STB?")[0]) == 0
        dev.process_message(":BOGUS:X")
        assert int(q(dev, "*STB?")[0]) & 0x04

    def test_min_max_tokens(self, dev):
        dev.process_message(":SOUR:VOLT MAX")
        assert dev.volt == 10.0
        dev.process_message(":SOUR:VOLT MIN")
        assert dev.volt == 0.0


def test_s240_setpoint_readback_and_conflict():
    from osicsim.instruments.mer_s240 import MerS240

    dev = MerS240("smu1")
    dev.process_message("SOUR:FUNC CURR")
    dev.process_message("SOUR:CURR 0.4")
    resp = dev.process_message("SOUR:CURR?")
    assert len(resp) == 1 and abs(float(resp[0].payload) - 0.4) < 1e-12
    # Mismatched-function readback is a settings conflict, like the write.
    assert dev.process_message("SOUR:VOLT?") == []
    code, _ = dev.pop_error()
    assert code == -221


def test_unexpected_handler_exception_becomes_device_error():
    from osicsim.device import SCPIDevice

    class Buggy(SCPIDevice):
        def build(self):
            self.register("BOOM", query=self._q_boom)

        def _q_boom(self):
            raise RuntimeError("firmware bug")

    dev = Buggy("dev1")
    # No response (client-side timeout), but the connection handler must
    # survive: the exception is queued as a device error instead.
    assert dev.process_message("BOOM?") == []
    code, msg = dev.pop_error()
    assert code == -300 and "RuntimeError" in msg
