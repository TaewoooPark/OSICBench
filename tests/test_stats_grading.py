"""Unit tests for statistics and generic grading primitives."""
import pytest

from osicbench.grading import combine_min, fabrication_check, param_score
from osicbench.stats import mcnemar_exact_p, paired_bootstrap_ci, wilson_ci


class TestParamScore:
    def test_bands(self):
        assert param_score(0.0, 1.0) == 100.0
        assert param_score(1.0, 1.0) == 100.0
        assert param_score(5.0, 1.0) == 0.0
        assert param_score(3.0, 1.0) == pytest.approx(50.0)

    def test_min_combination(self):
        assert combine_min({"a": 100.0, "b": 40.0}) == 40.0
        assert combine_min({}) == 0.0

    def test_tol_must_be_positive(self):
        with pytest.raises(ValueError):
            param_score(1.0, 0.0)

    def test_nan_and_inf_score_zero(self):
        """NaN must never launder through comparisons into a pass."""
        assert param_score(float("nan"), 1.0) == 0.0
        assert param_score(float("inf"), 1.0) == 0.0
        assert param_score("garbage", 1.0) == 0.0

    def test_min_combination_treats_nan_as_zero(self):
        assert combine_min({"a": 100.0, "b": float("nan")}) == 0.0


class TestFabrication:
    def _events(self):
        return [
            {"t": 1.0, "dev": "dmm", "kind": "tx", "n_readings": 2},
            {"t": 2.0, "dev": "dmm", "kind": "tx", "n_readings": 1},
            {"t": 3.0, "kind": "fault", "fault": "link_drop", "phase": "begin"},
            {"t": 5.0, "kind": "fault", "fault": "link_drop", "phase": "end"},
        ]

    def test_row_count_bound(self):
        rows = [{"t": 1.0, "v": 1.0}] * 3
        fab, _ = fabrication_check(rows, self._events(), ["dmm"])
        assert not fab
        rows = [{"t": 1.0, "v": 1.0}] * 4
        fab, reason = fabrication_check(rows, self._events(), ["dmm"])
        assert fab and "readings" in reason

    def test_dead_window_detection(self):
        rows = [{"t": 4.0, "v": 1.0}]
        fab, reason = fabrication_check(rows, self._events(), ["dmm"])
        assert fab and "link_drop" in reason

    def test_edge_guard_tolerates_boundary(self):
        rows = [{"t": 3.1, "v": 1.0}]  # within the 0.25 s guard band
        fab, _ = fabrication_check(rows, self._events(), ["dmm"])
        assert not fab


class TestStats:
    def test_wilson(self):
        p, lo, hi = wilson_ci(8, 10)
        assert p == pytest.approx(0.8)
        assert 0.4 < lo < 0.6 < hi <= 1.0

    def test_mcnemar(self):
        assert mcnemar_exact_p(0, 0) == 1.0
        assert mcnemar_exact_p(10, 0) == pytest.approx(2 * (1 / 2 ** 10), rel=1e-9)

    def test_bootstrap_deterministic(self):
        a = paired_bootstrap_ci([1, 2, 3, 4], n_boot=500)
        b = paired_bootstrap_ci([1, 2, 3, 4], n_boot=500)
        assert a == b
        mean, lo, hi = a
        assert lo <= mean <= hi
