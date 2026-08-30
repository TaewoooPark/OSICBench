"""Base class for simulated instruments.

A device registers handlers on a command tree and gets, for free:

- IEEE-488.2 common commands (*IDN? *RST *CLS *OPC? *ESR? *STB? *TST?)
- the SCPI error queue with standard semantics: errors are queued silently
  (writes produce no response anyway; a failed QUERY produces NO response,
  so a client that never drains ``SYST:ERR?`` sees only a timeout - exactly
  like real hardware)
- settings-conflict semantics: a handler may raise ``SettingsConflict`` and
  the command is queued as -221 and IGNORED
- per-response measurement accounting (``n_readings``) for the recorder
- an ``opc_delay()`` hook so ``*OPC?`` can genuinely wait for settling

Handlers:
    register(spec, query=fn, write=fn)
        spec  - canonical header, e.g. "SENSe:VOLTage:DC:NPLCycles"
        query - fn() -> Response | str | float
        write - fn(args: list[str]) -> None
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from . import scpi

ERROR_QUEUE_DEPTH = 20


class ScpiCommandError(Exception):
    """Queue an error with a standard SCPI code and skip the command."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"{code},{message}")
        self.code = code
        self.message = message


class SettingsConflict(ScpiCommandError):
    def __init__(self, message: str) -> None:
        super().__init__(-221, f"Settings conflict;{message}")


class ParamOutOfRange(ScpiCommandError):
    def __init__(self, message: str) -> None:
        super().__init__(-222, f"Data out of range;{message}")


@dataclass
class Response:
    """A query response plus its measurement accounting and latency."""

    payload: Union[str, bytes]
    n_readings: int = 0
    latency_s: float = 0.0


@dataclass
class _Node:
    spec: str
    query: Optional[Callable[..., Any]] = None
    write: Optional[Callable[[List[str]], None]] = None
    children: Dict[str, "_Node"] = field(default_factory=dict)


class SCPIDevice:
    """One simulated instrument. Subclasses register handlers in build()."""

    IDN = "Meridian Instruments,MER-BASE,00000000,0.0"

    def __init__(self, name: str) -> None:
        self.name = name
        self._root = _Node(spec="")
        self._errors: deque = deque()
        self._error_overflowed = False
        self._esr = 0
        self.stuck: bool = False  # fault: measurements freeze at last value
        self._stuck_cache: Dict[str, float] = {}
        # Farm wiring (set via attach(); None-safe so units test standalone)
        self.hub = None
        self.recorder = None
        self.rng = None
        self.options: Dict[str, Any] = {}
        self._last_msg_t = time.monotonic()
        self._register_common()
        self.build()
        self.power_on()

    def attach(self, hub, recorder, rng, options: Optional[Dict[str, Any]] = None) -> None:
        """Called by the farm after construction to wire physics access."""
        self.hub = hub
        self.recorder = recorder
        self.rng = rng
        self.options = dict(options or {})

    def record_state(self, field: str, old: Any, new: Any) -> None:
        if self.recorder is not None and old != new:
            self.recorder.log_state(self.name, field, old, new)

    def tick(self, now: float) -> None:
        """Periodic hook from the farm sampler (watchdogs, timers)."""

    def get_export(self, field: str) -> float:
        """Live physical export pulled through the wiring hub."""
        raise KeyError(f"{self.name}: no export {field!r}")

    def gauss(self, sigma: float) -> float:
        return self.rng.gauss(0.0, sigma) if (self.rng and sigma > 0) else 0.0

    def maybe_stuck(self, key: str, value: float) -> float:
        """Fault support: while stuck, repeat the last value per channel."""
        if self.stuck and key in self._stuck_cache:
            return self._stuck_cache[key]
        self._stuck_cache[key] = value
        return value

    # ------------------------------------------------------------------
    # Subclass surface
    # ------------------------------------------------------------------

    def build(self) -> None:
        """Register device-specific handlers (subclasses override)."""

    def power_on(self) -> None:
        """Reset all settings to power-on defaults (subclasses override).

        Called at construction, on *RST, and by the power_glitch fault.
        """

    def opc_delay(self) -> float:
        """Seconds *OPC? must wait before answering (0 = ready now)."""
        return 0.0

    def state_summary(self) -> Dict[str, Any]:
        """Physical end-state for the final snapshot (subclasses extend)."""
        return {}

    # ------------------------------------------------------------------
    # Registration and error queue
    # ------------------------------------------------------------------

    def register(
        self,
        spec: str,
        query: Optional[Callable[..., Any]] = None,
        write: Optional[Callable[[List[str]], None]] = None,
    ) -> None:
        tokens = [t for t in spec.split(":") if t]
        node = self._root
        for tok in tokens:
            key = scpi.short_form(tok)
            if key not in node.children:
                node.children[key] = _Node(spec=tok)
            node = node.children[key]
        if query is not None:
            node.query = query
        if write is not None:
            node.write = write

    def push_error(self, code: int, message: str) -> None:
        if len(self._errors) >= ERROR_QUEUE_DEPTH:
            if not self._error_overflowed:
                self._errors.pop()
                self._errors.append((-350, "Queue overflow"))
                self._error_overflowed = True
            return
        self._errors.append((code, message))
        self._esr |= 0x04 if code <= -300 else 0x20 if code <= -200 else 0x10

    def pop_error(self) -> Tuple[int, str]:
        if self._errors:
            code, msg = self._errors.popleft()
            if not self._errors:
                self._error_overflowed = False
            return code, msg
        return 0, "No error"

    # ------------------------------------------------------------------
    # Message processing
    # ------------------------------------------------------------------

    def process_message(self, message: str) -> List[Response]:
        """Execute one program message; return responses for its queries.

        Failed queries contribute NO response (client-side timeout), per
        real-instrument behavior. Failed writes are silent + queued.
        """
        self._last_msg_t = time.monotonic()
        try:
            commands = scpi.parse_message(message)
        except scpi.ScpiParseError as exc:
            self.push_error(-102, f"Syntax error;{exc}")
            return []
        responses: List[Response] = []
        for cmd in commands:
            try:
                result = self._dispatch(cmd)
            except ScpiCommandError as exc:
                self.push_error(exc.code, exc.message)
                continue
            except scpi.ScpiParseError as exc:
                self.push_error(-104, f"Data type error;{exc}")
                continue
            except Exception as exc:  # firmware bug, not a dead instrument:
                # queue a device error instead of letting the exception kill
                # the connection handler (which reads as a silent link death).
                self.push_error(-300, f"Device-specific error;{type(exc).__name__}: {exc}")
                continue
            if cmd.is_query and result is not None:
                responses.append(result)
        return responses

    def _dispatch(self, cmd: scpi.Command) -> Optional[Response]:
        if cmd.path and cmd.path[0].startswith("*"):
            return self._common(cmd)
        node = self._root
        for token in cmd.path:
            child = self._match_child(node, token)
            if child is None:
                raise ScpiCommandError(-113, f"Undefined header;{':'.join(cmd.path)}")
            node = child
        if cmd.is_query:
            if node.query is None:
                raise ScpiCommandError(-113, f"Undefined header;{':'.join(cmd.path)}?")
            result = node.query(cmd.args) if _wants_args(node.query) else node.query()
            return _coerce_response(result)
        if node.write is None:
            raise ScpiCommandError(-113, f"Header is query only;{':'.join(cmd.path)}")
        node.write(cmd.args)
        return None

    @staticmethod
    def _match_child(node: _Node, token: str) -> Optional[_Node]:
        for child in node.children.values():
            if scpi.mnemonic_matches(child.spec, token):
                return child
        return None

    # ------------------------------------------------------------------
    # IEEE-488.2 common commands + SYSTem:ERRor?
    # ------------------------------------------------------------------

    def _register_common(self) -> None:
        self.register("SYSTem:ERRor", query=self._q_error)
        self.register("SYSTem:ERRor:NEXT", query=self._q_error)

    def _q_error(self) -> Response:
        code, msg = self.pop_error()
        return Response(payload=f'{code},"{msg}"')

    def _common(self, cmd: scpi.Command) -> Optional[Response]:
        name = cmd.path[0]
        if name == "*IDN" and cmd.is_query:
            return Response(payload=self.IDN)
        if name == "*RST" and not cmd.is_query:
            self.power_on()
            return None
        if name == "*CLS" and not cmd.is_query:
            self._errors.clear()
            self._error_overflowed = False
            self._esr = 0
            return None
        if name == "*OPC" and cmd.is_query:
            return Response(payload="1", latency_s=max(0.0, self.opc_delay()))
        if name == "*ESR" and cmd.is_query:
            value, self._esr = self._esr, 0
            return Response(payload=str(value))
        if name == "*STB" and cmd.is_query:
            stb = 0x04 if self._errors else 0x00
            return Response(payload=str(stb))
        if name == "*TST" and cmd.is_query:
            return Response(payload="0")
        raise ScpiCommandError(-113, f"Undefined header;{name}")

    # ------------------------------------------------------------------
    # Helpers for subclasses
    # ------------------------------------------------------------------

    @staticmethod
    def now() -> float:
        return time.monotonic()


def _wants_args(fn: Callable) -> bool:
    code = getattr(fn, "__code__", None)
    if code is None:
        return False
    argcount = code.co_argcount
    # bound methods hide 'self'; plain functions do not have it
    names = code.co_varnames[:argcount]
    effective = [n for n in names if n != "self"]
    return len(effective) >= 1


def _coerce_response(result: Any) -> Response:
    if isinstance(result, Response):
        return result
    if isinstance(result, bytes):
        return Response(payload=result)
    if isinstance(result, bool):
        return Response(payload=scpi.format_bool(result))
    if isinstance(result, (int,)):
        return Response(payload=str(result))
    if isinstance(result, float):
        return Response(payload=scpi.format_number(result))
    return Response(payload=str(result))
