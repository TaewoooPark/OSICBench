"""Physics-model math and full farm integration round-trips."""
import asyncio
import json
import math
import time

import pytest

from osicsim import physics, seeding
from osicsim.circuit import Wire, WiringHub
from osicsim.farm import Farm


class TestResolveParams:
    def test_fixed_and_seeded(self):
        spec = {"r": 100.0, "r_lead": {"uniform": [5, 15]}}
        a = physics.resolve_params(7, "dut1", spec)
        b = physics.resolve_params(7, "dut1", spec)
        c = physics.resolve_params(8, "dut1", spec)
        assert a == b
        assert a["r"] == 100.0
        assert 5 <= a["r_lead"] <= 15
        assert a["r_lead"] != c["r_lead"]


class TestModels:
    def test_settling_closed_form(self):
        sv = physics.SettlingValue(0.0, tau_s=0.2)
        sv.set_target(1.0)
        t0 = time.monotonic()
        assert sv.value(t0 + 0.2) == pytest.approx(1 - math.exp(-1), rel=1e-3)
        assert sv.value(t0 + 2.0) == pytest.approx(1.0, abs=1e-4)

    def test_diode_shockley(self):
        dut = physics.DiodeDUT("d", {"i_s": 1e-9, "n": 1.5, "temp_k": 300.0},
                               seeding.derive_rng(1, "x"))
        dut.bind_input("bias_v", lambda: 0.4)
        i = dut.output("i")
        vt = 8.617333262e-5 * 300
        assert i == pytest.approx(1e-9 * (math.exp(0.4 / (1.5 * vt)) - 1), rel=1e-9)

    def test_resistor_two_vs_four_wire(self):
        dut = physics.ResistorDUT("r", {"r": 100.0, "r_lead": 10.0},
                                  seeding.derive_rng(1, "x"))
        dut.bind_input("force_i", lambda: 0.01)
        assert dut.output("v_4w") == pytest.approx(1.0)
        assert dut.output("v_2w") == pytest.approx(1.2)

    def test_hysteresis_branches_differ(self):
        dut = physics.HysteresisDUT(
            "h", {"ms": 1.0, "hc": 0.3, "w": 0.1, "k_sens": 1.0},
            seeding.derive_rng(1, "x"))
        h_values = [1.0, 0.5, 0.2, 0.0, -0.2, -0.5, -1.0]
        down = []
        for h in h_values:
            dut.bind_input("h", lambda h=h: h)
            down.append(dut.output("v_m"))
        up = []
        for h in reversed(h_values):
            dut.bind_input("h", lambda h=h: h)
            up.append(dut.output("v_m"))
        # at h=0: descending branch is still positive, ascending still negative
        assert down[3] > 0.5
        assert up[3] < -0.5

    def test_thermal_plant_steady_state(self):
        dut = physics.ThermalPlantDUT(
            "p", {"t_amb": 293.0, "gain_k_per_w": 10.0, "tau_s": 0.05},
            seeding.derive_rng(1, "x"))
        dut.bind_input("power_w", lambda: 2.0)
        dut.output("temp_k")  # latch power
        time.sleep(0.4)  # 8 tau
        assert dut.output("temp_k") == pytest.approx(293 + 20, abs=0.2)

    def test_resonance_lorentzian_and_kick(self):
        dut = physics.ResonanceDUT(
            "res", {"f0": 1000.0, "gamma": 10.0, "amp": 1e-3,
                    "drift_rate_hz_per_rt_s": 0.0},
            seeding.derive_rng(1, "x"))
        dut.bind_input("drive_f", lambda: 1000.0)
        assert dut.output("r") == pytest.approx(1e-3)
        dut.bind_input("drive_f", lambda: 1010.0)
        assert dut.output("r") == pytest.approx(5e-4, rel=1e-6)
        dut.kick("f0", 10.0)
        assert dut.output("r") == pytest.approx(1e-3)


class TestWiringHub:
    def test_pull_chain_device_to_dut_to_meter(self):
        hub = WiringHub()
        dut = physics.ResistorDUT("dut1", {"r": 100.0, "r_lead": 0.0},
                                  seeding.derive_rng(1, "x"))
        hub.add_dut(dut)
        source_level = {"v": 0.02}
        hub.add_device("src", lambda field: source_level[field])
        hub.add_wire(Wire("src", "v", "dut1", "force_i", gain=1.0))
        hub.add_wire(Wire("dut1", "v_4w", "meter", "input_v", gain=1.0))
        hub.finalize()
        assert hub.pull("meter", "input_v") == pytest.approx(2.0)

    def test_duplicate_wire_rejected(self):
        hub = WiringHub()
        hub.add_wire(Wire("a", "x", "b", "y"))
        with pytest.raises(ValueError):
            hub.add_wire(Wire("c", "z", "b", "y"))


FARM_CONFIG = {
    "farm": {
        "devices": {
            "dmm1": {"type": "mer_d610", "quirks": {"banner": "MER-D610 SCPI READY",
                                                     "read_term": "\r\n",
                                                     "write_term": "\r\n"}},
        },
        "duts": {
            "dut1": {"model": "const_voltage", "params": {"v_true": {"uniform": [0.9, 1.1]}}},
        },
        "wiring": [
            {"src": "dut1.v", "dst": "dmm1.input_v"},
        ],
        "phys_sample": [{"node": "dut1", "field": "v"}],
    }
}


@pytest.mark.integration
class TestFarmIntegration:
    def test_full_round_trip(self, tmp_path):
        async def scenario():
            farm = Farm(FARM_CONFIG, seed=11, out_dir=tmp_path)
            endpoints = await farm.start()
            port = endpoints["dmm1"]["port"]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            banner = await asyncio.wait_for(reader.readuntil(b"\r\n"), 2)
            writer.write(b"SENS:VOLT:DC:NPLC 10\r\n*OPC?\r\n")
            await writer.drain()
            await asyncio.wait_for(reader.readuntil(b"\r\n"), 2)
            values = []
            for _ in range(5):
                writer.write(b"READ?\r\n")
                await writer.drain()
                raw = await asyncio.wait_for(reader.readuntil(b"\r\n"), 5)
                values.append(float(raw.strip()))
            writer.close()
            await asyncio.sleep(0.25)  # let the sampler log ground truth
            await farm.stop()
            return banner, values

        banner, values = asyncio.run(scenario())
        assert banner.startswith(b"MER-D610")

        v_true = physics.resolve_params(11, "dut1", FARM_CONFIG["farm"]["duts"]["dut1"]["params"])["v_true"]
        mean = sum(values) / len(values)
        assert mean == pytest.approx(v_true, abs=5e-4), "readings must track hidden truth"

        endpoints = json.loads((tmp_path / "endpoints.json").read_text())
        assert endpoints["dmm1"]["resource"].startswith("TCPIP0::127.0.0.1::")

        from osicsim.recorder import final_snapshot, load_events, phys_series, total_readings
        events = load_events(tmp_path / "recorder.jsonl")
        assert total_readings(events, devs={"dmm1"}) == 5
        truth = phys_series(events, "dut1", "v")
        assert truth and all(v == pytest.approx(v_true) for _, v in truth)
        snap = final_snapshot(events)
        assert snap["dmm1"]["nplc"] == 10.0

    def test_grader_rederives_same_truth(self, tmp_path):
        p1 = physics.resolve_params(11, "dut1", FARM_CONFIG["farm"]["duts"]["dut1"]["params"])
        p2 = physics.resolve_params(11, "dut1", FARM_CONFIG["farm"]["duts"]["dut1"]["params"])
        assert p1 == p2


def test_dead_leg_pair_exactly_one_leg_silent():
    from osicsim.physics import build_dut

    spec = {"v_a": {"uniform": [0.9, 1.1]}, "v_b": {"uniform": [0.9, 1.1]},
            "dead_leg": {"choice": [1, 2]}}
    seen = set()
    for seed in range(1, 30):
        d = build_dut("pair1", "dead_leg_pair", seed, spec)
        va, vb = d.output("va"), d.output("vb")
        dead = int(round(d.params["dead_leg"]))
        seen.add(dead)
        if dead == 1:
            assert va == 0.0 and 0.9 <= vb <= 1.1
        else:
            assert vb == 0.0 and 0.9 <= va <= 1.1
    assert seen == {1, 2}  # both outcomes occur across seeds
