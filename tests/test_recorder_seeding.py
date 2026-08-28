"""Unit tests for the flight recorder and seed derivation."""
import pytest

from osicsim import recorder as rec
from osicsim import seeding


class TestSeeding:
    def test_deterministic(self):
        a = seeding.derive_rng(42, "dut1", "params").random()
        b = seeding.derive_rng(42, "dut1", "params").random()
        assert a == b

    def test_scopes_independent(self):
        a = seeding.derive_rng(42, "dut1").random()
        b = seeding.derive_rng(42, "dut2").random()
        assert a != b

    def test_seeds_differ(self):
        a = seeding.derive_uniform(1, 0, 1, "x")
        b = seeding.derive_uniform(2, 0, 1, "x")
        assert a != b

    def test_loguniform_bounds(self):
        for seed in range(20):
            v = seeding.derive_loguniform(seed, 1e-9, 1e-7, "is")
            assert 1e-9 <= v <= 1e-7

    def test_loguniform_rejects_bad_range(self):
        with pytest.raises(ValueError):
            seeding.derive_loguniform(1, 0, 1, "x")


class TestRecorder:
    def test_txn_counters_per_device(self, tmp_path):
        r = rec.FlightRecorder(tmp_path / "r.jsonl")
        assert r.next_txn("a") == 1
        assert r.next_txn("b") == 1
        assert r.next_txn("a") == 2
        r.close()

    def test_round_trip_and_reading_totals(self, tmp_path):
        path = tmp_path / "r.jsonl"
        r = rec.FlightRecorder(path)
        r.log_tx("dmm", "+1.0E+00", txn=1, n_readings=1)
        r.log_tx("dmm", "<block>", txn=2, n_readings=500)
        r.log_tx("smu", "+0.0E+00", txn=1, n_readings=1)
        r.log_tx("dmm", "ok", txn=3, n_readings=0)
        r.close()
        events = rec.load_events(path)
        assert rec.total_readings(events) == 502
        assert rec.total_readings(events, devs={"dmm"}) == 501

    def test_fault_windows(self, tmp_path):
        path = tmp_path / "r.jsonl"
        r = rec.FlightRecorder(path)
        r.log_fault("link_drop", "begin")
        r.log("x", "tx", data="v", txn=1, n_readings=1)
        r.log_fault("link_drop", "end")
        r.log_fault("power_glitch", "begin")  # never closed
        r.close()
        events = rec.load_events(path)
        windows = rec.fault_windows(events)
        names = sorted(w[2] for w in windows)
        assert names == ["link_drop", "power_glitch"]
        for t0, t1, _ in windows:
            assert t1 >= t0

    def test_snapshot_and_series(self, tmp_path):
        path = tmp_path / "r.jsonl"
        r = rec.FlightRecorder(path)
        r.log_phys("plant", "temp_k", 300.0)
        r.log(
            "plant", "phys_sample", field="temp_k", value=301.0
        )
        r.log_state("psu", "output", old=False, new=True)
        r.snapshot({"psu": {"output": True, "volt": 1.0}})
        r.close()
        events = rec.load_events(path)
        assert rec.final_snapshot(events)["psu"]["output"] is True
        series = rec.phys_series(events, "plant", "temp_k")
        assert [v for _, v in series] == [300.0, 301.0]
        states = rec.state_series(events, "psu", "output")
        assert states[-1][1] is True

    def test_torn_final_line_is_tolerated(self, tmp_path):
        path = tmp_path / "r.jsonl"
        r = rec.FlightRecorder(path)
        r.log_tx("dmm", "x", txn=1, n_readings=1)
        r.close()
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{"t": 1.0, "dev": "dmm", "kind": "tx", "n_read')  # torn
        events = rec.load_events(path)
        assert rec.total_readings(events) == 1
