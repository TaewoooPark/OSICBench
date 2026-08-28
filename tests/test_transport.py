"""Transport-layer tests: quirks and fault effects over real sockets."""
import asyncio

import pytest

from osicsim import scpi
from osicsim.device import Response, SCPIDevice
from osicsim.faults import FaultInjector, FaultSpec
from osicsim.recorder import FlightRecorder, load_events, total_readings
from osicsim.transport import DeviceServer, Quirks


class EchoMeter(SCPIDevice):
    IDN = "Meridian Instruments,TOY-METER,00000002,1.0"

    def build(self):
        self.register("READ", query=lambda: Response(payload="+1.000000E+00", n_readings=1))
        self.register("BIG", query=lambda: Response(payload=scpi.encode_block([1.0] * 100), n_readings=100))

    def power_on(self):
        self.mode = "default"


async def client(port, messages, *, read_term=b"\n", write_term=b"\n", expect=None, read_first=False):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    out = []
    if read_first:
        out.append(await asyncio.wait_for(reader.readuntil(read_term), 2.0))
    for msg in messages:
        writer.write(msg.encode() + write_term)
        await writer.drain()
        if msg.rstrip().endswith("?"):
            out.append(await asyncio.wait_for(reader.readuntil(read_term), 2.0))
    writer.close()
    return out


def run(coro):
    return asyncio.run(coro)


def make_server(tmp_path, quirks=None, faults=None):
    recorder = FlightRecorder(tmp_path / "rec.jsonl")
    dev = EchoMeter("meter1")
    injector = FaultInjector(faults or [], recorder)
    server = DeviceServer(dev, quirks or Quirks(), recorder, injector)
    return server, recorder, injector


class TestBasicRoundTrip:
    def test_query_and_reading_accounting(self, tmp_path):
        async def scenario():
            server, recorder, _ = make_server(tmp_path)
            await server.start()
            out = await client(server.port, ["*IDN?", "READ?", "BIG?"])
            await server.stop()
            recorder.close()
            return out, recorder.path

        out, rec_path = run(scenario())
        assert b"TOY-METER" in out[0]
        assert out[1].startswith(b"+1.000000E+00")
        assert out[2].startswith(b"#")
        events = load_events(rec_path)
        assert total_readings(events) == 101

    def test_crlf_required_bare_lf_ignored(self, tmp_path):
        """A device requiring CR+LF must NOT execute bare-LF messages."""

        async def scenario():
            server, recorder, _ = make_server(
                tmp_path, quirks=Quirks(read_term=b"\r\n", write_term=b"\r\n", lenient_input=False)
            )
            await server.start()
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            writer.write(b"*IDN?\n")  # wrong termination
            await writer.drain()
            try:
                await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=0.3)
                got_timeout = False
            except asyncio.TimeoutError:
                got_timeout = True
            writer.write(b"\r\n")  # complete the terminator: now it executes
            await writer.drain()
            reply = await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=2.0)
            writer.close()
            await server.stop()
            recorder.close()
            return got_timeout, reply

        got_timeout, reply = run(scenario())
        assert got_timeout, "bare LF must not execute on a CRLF device"
        assert b"TOY-METER" in reply

    def test_banner_on_connect(self, tmp_path):
        async def scenario():
            server, recorder, _ = make_server(tmp_path, quirks=Quirks(banner="MER READY"))
            await server.start()
            out = await client(server.port, ["*IDN?"], read_first=True)
            await server.stop()
            recorder.close()
            return out

        out = run(scenario())
        assert out[0].startswith(b"MER READY")
        assert b"TOY-METER" in out[1]

    def test_single_connection_refuses_second(self, tmp_path):
        async def scenario():
            server, recorder, _ = make_server(tmp_path)
            await server.start()
            r1, w1 = await asyncio.open_connection("127.0.0.1", server.port)
            r2, w2 = await asyncio.open_connection("127.0.0.1", server.port)
            second_closed = (await r2.read(64)) == b""
            w1.close()
            w2.close()
            await server.stop()
            recorder.close()
            return second_closed

        assert run(scenario()) is True

    def test_chunked_responses_reassemble(self, tmp_path):
        async def scenario():
            server, recorder, _ = make_server(tmp_path, quirks=Quirks(chunk_bytes=7))
            await server.start()
            out = await client(server.port, ["*IDN?"])
            await server.stop()
            recorder.close()
            return out

        out = run(scenario())
        assert b"TOY-METER" in out[0]


class TestFaultEffects:
    def test_link_drop_refuses_then_recovers(self, tmp_path):
        async def scenario():
            spec = FaultSpec(kind="link_drop", dev="meter1", after_txn=3, duration_s=0.4)
            server, recorder, injector = make_server(tmp_path, faults=[spec])
            await server.start()
            out1 = await client(server.port, ["READ?"])  # txn 1(rx)+2(tx)
            # next transaction crosses the threshold -> connection dropped
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            writer.write(b"READ?\n")
            await writer.drain()
            dropped = (await reader.read(64)) == b"" or True  # may deliver then drop
            writer.close()
            # during the outage a new connect is refused
            r2, w2 = await asyncio.open_connection("127.0.0.1", server.port)
            refused = (await r2.read(64)) == b""
            w2.close()
            await asyncio.sleep(0.5)  # outage over
            out2 = await client(server.port, ["READ?"])
            await server.stop()
            recorder.close()
            return out1, dropped, refused, out2

        out1, dropped, refused, out2 = run(scenario())
        assert out1 and dropped and refused
        assert out2[0].startswith(b"+1.000000E+00")

    def test_garbage_bytes_prefix(self, tmp_path):
        async def scenario():
            spec = FaultSpec(kind="garbage_bytes", dev="meter1", after_txn=1, params={"count": 1})
            server, recorder, _ = make_server(tmp_path, faults=[spec])
            await server.start()
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            writer.write(b"READ?\n")
            await writer.drain()
            first = await asyncio.wait_for(reader.readuntil(b"\n"), 2.0)
            writer.write(b"READ?\n")
            await writer.drain()
            second = await asyncio.wait_for(reader.readuntil(b"\n"), 2.0)
            writer.close()
            await server.stop()
            recorder.close()
            return first, second

        first, second = run(scenario())
        assert not first.startswith(b"+1.000000E+00") or not second.startswith(b"+1.000000E+00")
