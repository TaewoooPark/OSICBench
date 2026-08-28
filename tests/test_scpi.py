"""Unit tests for the SCPI message engine."""
import math

import pytest

from osicsim import scpi


class TestMnemonics:
    def test_short_form(self):
        assert scpi.short_form("SOURce") == "SOUR"
        assert scpi.short_form("NPLCycles") == "NPLC"
        assert scpi.short_form("VOLTage") == "VOLT"

    @pytest.mark.parametrize("token", ["SOUR", "sour", "SOURCE", "Source"])
    def test_matches_long_and_short(self, token):
        assert scpi.mnemonic_matches("SOURce", token)

    def test_rejects_partial(self):
        assert not scpi.mnemonic_matches("SOURce", "SOU")
        assert not scpi.mnemonic_matches("SOURce", "SOURC")


class TestMessageParsing:
    def test_simple_query(self):
        (cmd,) = scpi.parse_message(":MEAS:VOLT:DC?")
        assert cmd.path == ("MEAS", "VOLT", "DC")
        assert cmd.is_query and cmd.args == []

    def test_args_split(self):
        (cmd,) = scpi.parse_message("SOUR:VOLT 1.5, 2")
        assert cmd.args == ["1.5", "2"]

    def test_quoted_arg_keeps_commas(self):
        (cmd,) = scpi.parse_message("SENS:FUNC 'VOLT,DC'")
        assert cmd.args == ["'VOLT,DC'"]

    def test_semicolon_relative_context(self):
        cmds = scpi.parse_message(":SOUR:VOLT 1; CURR 2")
        assert cmds[0].path == ("SOUR", "VOLT")
        assert cmds[1].path == ("SOUR", "CURR"), "relative header resolves to sibling"

    def test_semicolon_absolute_reset(self):
        cmds = scpi.parse_message(":SOUR:VOLT 1;:OUTP ON")
        assert cmds[1].path == ("OUTP",)

    def test_star_commands_are_absolute(self):
        cmds = scpi.parse_message(":SOUR:VOLT 1;*OPC?")
        assert cmds[1].path == ("*OPC",) and cmds[1].is_query


class TestNumbers:
    @pytest.mark.parametrize(
        "text,value",
        [
            ("1.5", 1.5),
            ("-2E-3", -0.002),
            ("100 mV", 0.1),
            ("2.5MA", 2.5e-3),  # milliamp, not mega: MA + base A -> M x A
            ("305K", 305.0),  # kelvin is a base unit, not a multiplier
            ("50HZ", 50.0),
            ("1.2KOHM", 1200.0),
        ],
    )
    def test_units(self, text, value):
        assert scpi.parse_number(text) == pytest.approx(value)

    def test_min_max_def(self):
        assert scpi.parse_number("MIN", minimum=0.02) == 0.02
        assert scpi.parse_number("max", maximum=100) == 100
        assert scpi.parse_number("DEF", default=1.0) == 1.0
        with pytest.raises(scpi.ScpiParseError):
            scpi.parse_number("MIN")

    def test_garbage_raises(self):
        with pytest.raises(scpi.ScpiParseError):
            scpi.parse_number("bogus")


class TestBlocks:
    def test_round_trip(self):
        values = [1.0, -2.5e-3, 9.9e37]
        blob = scpi.encode_block(values)
        assert blob.startswith(b"#")
        out = scpi.decode_block(blob)
        assert out == pytest.approx(values)

    def test_truncated_block_raises(self):
        blob = scpi.encode_block([1.0, 2.0])[:-3]
        with pytest.raises(scpi.ScpiParseError):
            scpi.decode_block(blob)

    def test_sentinels_survive(self):
        assert math.isfinite(scpi.NOT_A_NUMBER)  # in-band by design
        out = scpi.decode_block(scpi.encode_block([scpi.NOT_A_NUMBER]))
        assert out[0] == pytest.approx(scpi.NOT_A_NUMBER)
