"""SCPI-1999 / IEEE-488.2 message engine (pragmatic subset).

Implements the parts of the standards that real bench code actually hits:

- command-tree matching with long and short mnemonic forms
  (``SOURce:VOLTage`` accepts ``SOUR:VOLT``, ``source:voltage``, ...)
- message chaining with tree context: ``:SOUR:VOLT 1; CURR 2`` resolves the
  second header relative to the first header's parent, per the standard;
  ``;:`` resets to the root
- numeric parameters with unit suffixes and metric multipliers
  (``100 mV``, ``2.5E-3``, ``10K``) plus ``MIN`` / ``MAX`` / ``DEF`` tokens
- IEEE-488.2 definite-length arbitrary block encoding (``#42000<bytes>``)
- SCPI sentinel values for overrange / not-a-number (9.9e37 / 9.91e37)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

# SCPI-1999 sentinel values (vol. 1, syntax & style)
POSITIVE_INFINITY = 9.9e37
NOT_A_NUMBER = 9.91e37

_UNIT_MULTIPLIERS = {
    "G": 1e9,
    "MA": 1e6,   # mega, only when written as MA + base unit (rare); see below
    "K": 1e3,
    "M": 1e-3,
    "U": 1e-6,
    "N": 1e-9,
    "P": 1e-12,
}

# Base units we accept and strip. Longest-first so 'MV' wins over 'V'.
_BASE_UNITS = ["OHM", "HZ", "V", "A", "S", "K", "PCT"]


class ScpiParseError(ValueError):
    """Raised for malformed program messages (maps to error -1xx)."""


# ----------------------------------------------------------------------
# Mnemonics
# ----------------------------------------------------------------------


def short_form(mnemonic: str) -> str:
    """Short form of a spec mnemonic: its leading uppercase letters.

    ``SOURce`` -> ``SOUR``; ``NPLCycles`` -> ``NPLC``.
    """
    out = []
    for ch in mnemonic:
        if ch.isupper() or ch.isdigit():
            out.append(ch)
        else:
            break
    return "".join(out)


def mnemonic_matches(spec: str, token: str) -> bool:
    token_u = token.upper()
    return token_u == spec.upper() or token_u == short_form(spec)


# ----------------------------------------------------------------------
# Program-message splitting
# ----------------------------------------------------------------------


@dataclass
class Command:
    """One parsed program-message unit."""

    path: Tuple[str, ...]  # header tokens as sent (upper-cased)
    is_query: bool
    args: List[str]
    raw: str


def split_message(message: str) -> List[Tuple[str, bool]]:
    """Split a program message on ``;`` into (unit, absolute) pairs.

    ``absolute`` is True when the unit began with ``:`` or is a ``*`` common
    command; relative units resolve against the previous header's parent.
    """
    units: List[Tuple[str, bool]] = []
    for part in message.split(";"):
        part = part.strip()
        if not part:
            continue
        if part.startswith(":"):
            units.append((part[1:].strip(), True))
        elif part.startswith("*"):
            units.append((part, True))
        else:
            units.append((part, False))
    return units


def parse_message(message: str) -> List[Command]:
    """Parse a full program message into absolute Commands.

    Applies SCPI tree-context rules for relative headers after ``;``.
    """
    commands: List[Command] = []
    context: Tuple[str, ...] = ()  # parent path of the previous header
    for unit, absolute in split_message(message):
        if not unit:
            continue
        header, args_text = _split_header_args(unit)
        is_query = header.endswith("?")
        if is_query:
            header = header[:-1]
        if header.startswith("*"):
            path: Tuple[str, ...] = (header.upper(),)
            context = ()
        else:
            tokens = tuple(t for t in header.split(":") if t)
            if not tokens:
                raise ScpiParseError(f"empty header in {unit!r}")
            path = tokens if absolute else context + tokens
            context = path[:-1]
        args = _split_args(args_text)
        commands.append(Command(path=tuple(t.upper() for t in path), is_query=is_query, args=args, raw=unit))
    return commands


def _split_header_args(unit: str) -> Tuple[str, str]:
    m = re.match(r"^(\S+)\s*(.*)$", unit.strip())
    if not m:
        raise ScpiParseError(f"cannot parse {unit!r}")
    return m.group(1), m.group(2)


def _split_args(text: str) -> List[str]:
    text = text.strip()
    if not text:
        return []
    args: List[str] = []
    depth = 0
    current = []
    in_quote: Optional[str] = None
    for ch in text:
        if in_quote:
            current.append(ch)
            if ch == in_quote:
                in_quote = None
            continue
        if ch in "'\"":
            in_quote = ch
            current.append(ch)
        elif ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            args.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        args.append("".join(current).strip())
    return args


# ----------------------------------------------------------------------
# Parameter decoding
# ----------------------------------------------------------------------


def parse_number(
    text: str,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
    default: Optional[float] = None,
) -> float:
    """Decode a decimal numeric program data element.

    Handles plain/scientific notation, MIN/MAX/DEF tokens, and unit
    suffixes with metric multipliers (``100 mV`` -> 0.1).
    """
    token = text.strip().upper().replace(" ", "")
    if token in ("MIN", "MINIMUM"):
        if minimum is None:
            raise ScpiParseError("MIN not defined for this parameter")
        return float(minimum)
    if token in ("MAX", "MAXIMUM"):
        if maximum is None:
            raise ScpiParseError("MAX not defined for this parameter")
        return float(maximum)
    if token in ("DEF", "DEFAULT"):
        if default is None:
            raise ScpiParseError("DEF not defined for this parameter")
        return float(default)

    m = re.match(r"^([+-]?(?:\d+\.?\d*|\.\d+)(?:E[+-]?\d+)?)([A-Z]*)$", token)
    if not m:
        raise ScpiParseError(f"not a number: {text!r}")
    value = float(m.group(1))
    suffix = m.group(2)
    if suffix:
        value *= _decode_suffix(suffix)
    return value


def _decode_suffix(suffix: str) -> float:
    for base in _BASE_UNITS:
        if suffix == base:
            return 1.0
        if suffix.endswith(base):
            prefix = suffix[: -len(base)]
            if prefix in _UNIT_MULTIPLIERS:
                return _UNIT_MULTIPLIERS[prefix]
    # bare multiplier without base unit (e.g. '10K')
    if suffix in _UNIT_MULTIPLIERS:
        return _UNIT_MULTIPLIERS[suffix]
    raise ScpiParseError(f"unknown unit suffix: {suffix!r}")


def parse_bool(text: str) -> bool:
    token = text.strip().upper()
    if token in ("ON", "1"):
        return True
    if token in ("OFF", "0"):
        return False
    raise ScpiParseError(f"not a boolean: {text!r}")


# ----------------------------------------------------------------------
# Response encoding
# ----------------------------------------------------------------------


def format_number(value: float) -> str:
    """NR3 response format: ``+1.234567E-03``."""
    return f"{value:+.6E}"


def format_bool(value: bool) -> str:
    return "1" if value else "0"


def encode_block(values: Sequence[float]) -> bytes:
    """IEEE-488.2 definite-length block of ASCII floats, comma separated."""
    payload = ",".join(format_number(v) for v in values).encode("ascii")
    length = str(len(payload))
    return b"#" + str(len(length)).encode() + length.encode() + payload


def decode_block(data: bytes) -> List[float]:
    """Inverse of :func:`encode_block` (provided for tests and tooling)."""
    if not data.startswith(b"#"):
        raise ScpiParseError("not a definite-length block")
    ndigits = int(data[1:2])
    length = int(data[2 : 2 + ndigits])
    payload = data[2 + ndigits : 2 + ndigits + length]
    if len(payload) != length:
        raise ScpiParseError("block payload truncated")
    text = payload.decode("ascii")
    return [float(x) for x in text.split(",")] if text else []
