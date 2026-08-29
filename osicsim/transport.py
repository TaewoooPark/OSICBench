"""Per-device TCP servers with realistic interface quirks.

Each simulated instrument listens on its own 127.0.0.1 port (the address a
task hands to the agent, VISA-style: ``TCPIP0::127.0.0.1::<port>::SOCKET``).
Interface behavior is deliberately imperfect in the ways real bench gear is:

- configurable read/write terminations (a device may REQUIRE CR+LF)
- an optional greeting banner on connect
- response latency (base + per-command physics latency, e.g. integration)
- optional chunked writes (responses arrive in fragments)
- single-connection semantics: a second concurrent client is refused
- optional idle disconnect

All of these are documented in each instrument's manual; none vary by seed.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

from .device import Response, SCPIDevice
from .faults import FaultInjector
from .recorder import FlightRecorder


@dataclass
class Quirks:
    read_term: bytes = b"\n"        # terminator the DEVICE requires on input
    write_term: bytes = b"\n"       # terminator the device appends to output
    lenient_input: bool = True      # also accept bare LF when read_term is CRLF
    banner: Optional[str] = None    # greeting sent on connect
    base_latency_s: float = 0.005
    chunk_bytes: int = 0            # >0: fragment responses into chunks
    idle_disconnect_s: float = 0.0  # >0: close the socket after idle
    single_connection: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "Quirks":
        return cls(
            read_term=d.get("read_term", "\n").encode(),
            write_term=d.get("write_term", "\n").encode(),
            lenient_input=bool(d.get("lenient_input", True)),
            banner=d.get("banner"),
            base_latency_s=float(d.get("base_latency_s", 0.005)),
            chunk_bytes=int(d.get("chunk_bytes", 0)),
            idle_disconnect_s=float(d.get("idle_disconnect_s", 0.0)),
            single_connection=bool(d.get("single_connection", True)),
        )


class DeviceServer:
    """One TCP endpoint wrapping one simulated device."""

    def __init__(
        self,
        device: SCPIDevice,
        quirks: Quirks,
        recorder: FlightRecorder,
        faults: Optional[FaultInjector] = None,
    ) -> None:
        self.device = device
        self.quirks = quirks
        self.recorder = recorder
        self.faults = faults
        self.port: Optional[int] = None
        self._server: Optional[asyncio.base_events.Server] = None
        self._busy = False
        self._writers: list = []

    # ------------------------------------------------------------------

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, host="127.0.0.1", port=0)
        self.port = self._server.sockets[0].getsockname()[1]
        self.recorder.log(self.device.name, "session", event="listening", port=self.port)
        return self.port

    async def stop(self) -> None:
        for w in list(self._writers):
            self._close_writer(w)
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def drop_connections(self) -> None:
        """Used by link_drop / power_glitch faults."""
        for w in list(self._writers):
            self._close_writer(w)

    def _close_writer(self, writer: asyncio.StreamWriter) -> None:
        try:
            writer.close()
        except Exception:
            pass
        if writer in self._writers:
            self._writers.remove(writer)
        self._busy = False

    # ------------------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        dev = self.device.name
        if self.faults and self.faults.link_down(dev):
            self.recorder.log(dev, "session", event="connect_refused", reason="link_down")
            writer.close()
            return
        if self.quirks.single_connection and self._busy:
            self.recorder.log(dev, "session", event="connect_refused", reason="busy")
            writer.close()
            return
        self._busy = True
        self._writers.append(writer)
        self.recorder.log(dev, "session", event="connect")
        try:
            if self.quirks.banner:
                writer.write(self.quirks.banner.encode() + self.quirks.write_term)
                await writer.drain()
            await self._session(reader, writer)
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            pass
        finally:
            self.recorder.log(dev, "session", event="disconnect")
            self._close_writer(writer)

    async def _session(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        dev = self.device.name
        buffer = b""
        while True:
            try:
                if self.quirks.idle_disconnect_s > 0:
                    chunk = await asyncio.wait_for(reader.read(4096), timeout=self.quirks.idle_disconnect_s)
                else:
                    chunk = await reader.read(4096)
            except asyncio.TimeoutError:
                self.recorder.log(dev, "session", event="idle_disconnect")
                return
            if not chunk:
                return
            buffer += chunk
            while True:
                line, buffer, complete = self._extract_line(buffer)
                if not complete:
                    break
                if line.strip():
                    keep_open = await self._transact(line.strip(), writer)
                    if not keep_open:
                        return

    def _extract_line(self, buffer: bytes):
        term = self.quirks.read_term
        idx = buffer.find(term)
        if idx >= 0:
            return buffer[:idx].decode(errors="replace"), buffer[idx + len(term):], True
        if self.quirks.lenient_input and term != b"\n":
            jdx = buffer.find(b"\n")
            if jdx >= 0:
                return buffer[:jdx].rstrip(b"\r").decode(errors="replace"), buffer[jdx + 1:], True
        return "", buffer, False

    async def _transact(self, message: str, writer: asyncio.StreamWriter) -> bool:
        dev = self.device.name
        txn = self.recorder.next_txn(dev)
        self.recorder.log_rx(dev, message, txn)

        responses = self.device.process_message(message)

        if self.faults:
            extra = self.faults.response_delay(dev)
            if extra > 0:
                await asyncio.sleep(extra)
            self.faults.poll(dev, self.recorder.txn_count(dev))
            if self.faults.link_down(dev):
                self.recorder.log(dev, "session", event="link_dropped_mid_txn")
                return False

        for resp in responses:
            await asyncio.sleep(self.quirks.base_latency_s + max(0.0, resp.latency_s))
            out_txn = self.recorder.next_txn(dev)
            payload = resp.payload if isinstance(resp.payload, bytes) else resp.payload.encode()
            # Log the FULL payload: graders reconcile submitted values
            # against the exact readings served, block transfers included.
            self.recorder.log_tx(
                dev,
                payload.decode(errors="replace"),
                out_txn,
                n_readings=resp.n_readings,
            )
            data = payload + self.quirks.write_term
            if self.faults:
                data = self.faults.take_garbage(dev) + data
            if self.quirks.chunk_bytes > 0:
                for i in range(0, len(data), self.quirks.chunk_bytes):
                    writer.write(data[i : i + self.quirks.chunk_bytes])
                    await writer.drain()
                    await asyncio.sleep(0.001)
            else:
                writer.write(data)
                await writer.drain()
        return True
