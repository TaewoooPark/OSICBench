"""Device-under-test physics models with hidden ground truth.

Each model owns the true physical state of one DUT. Instruments read DUT
outputs through the wiring hub; sources feed DUT inputs the same way. All
models evolve lazily and analytically on wall-clock time (first-order
closed forms), so no ticking loop is required and behavior is independent
of host speed for time constants >= the 100 ms design floor.

Hidden parameters are resolved from the task seed by ``resolve_params`` -
the same function graders call to re-derive ground truth.
"""
from __future__ import annotations

import math
import time
from typing import Any, Callable, Dict, Optional

from . import seeding


def resolve_params(seed: int, dut_name: str, spec: Dict[str, Any]) -> Dict[str, float]:
    """Resolve a DUT parameter spec into concrete values.

    Spec entries are either fixed numbers or seeded draws:
        r: 100.0
        r: {uniform: [90, 110]}
        is: {loguniform: [1e-9, 1e-7]}
    Draw scope is (seed, "dut", name, param) so every parameter is an
    independent deterministic stream.
    """
    out: Dict[str, float] = {}
    for key, value in spec.items():
        if isinstance(value, dict):
            if "uniform" in value:
                lo, hi = value["uniform"]
                out[key] = seeding.derive_uniform(seed, float(lo), float(hi), "dut", dut_name, key)
            elif "loguniform" in value:
                lo, hi = value["loguniform"]
                out[key] = seeding.derive_loguniform(seed, float(lo), float(hi), "dut", dut_name, key)
            elif "choice" in value:
                out[key] = float(seeding.derive_choice(seed, value["choice"], "dut", dut_name, key))
            else:
                raise ValueError(f"unknown draw spec for {dut_name}.{key}: {value}")
        else:
            out[key] = float(value)
    return out


class SettlingValue:
    """First-order exponential approach to a target (closed form)."""

    def __init__(self, initial: float, tau_s: float) -> None:
        self._value = float(initial)
        self._target = float(initial)
        self.tau_s = float(tau_s)
        self._t = time.monotonic()

    def set_target(self, target: float) -> None:
        now = time.monotonic()
        self._value = self.value(now)
        self._target = float(target)
        self._t = now

    def value(self, now: Optional[float] = None) -> float:
        now = time.monotonic() if now is None else now
        if self.tau_s <= 0:
            return self._target
        dt = max(0.0, now - self._t)
        return self._target + (self._value - self._target) * math.exp(-dt / self.tau_s)

    @property
    def target(self) -> float:
        return self._target

    def settled(self, fraction: float = 1e-3, now: Optional[float] = None) -> bool:
        span = abs(self._target - self._value)
        if span == 0:
            return True
        return abs(self.value(now) - self._target) <= fraction * max(abs(self._target), 1e-12)


class DUT:
    """Base DUT: named inputs resolved through callables, named outputs."""

    def __init__(self, name: str, params: Dict[str, float], rng_scope: seeding.random.Random) -> None:
        self.name = name
        self.params = params
        self.rng = rng_scope
        self._inputs: Dict[str, Callable[[], float]] = {}

    def bind_input(self, field: str, fn: Callable[[], float]) -> None:
        self._inputs[field] = fn

    def input(self, field: str, default: float = 0.0) -> float:
        fn = self._inputs.get(field)
        return fn() if fn is not None else default

    def output(self, field: str, now: Optional[float] = None) -> float:
        raise KeyError(f"{self.name}: unknown output {field!r}")

    def kick(self, field: str, delta: float) -> None:
        """Fault hook: step a parameter (drift_step)."""
        self.params[field] = self.params.get(field, 0.0) + delta

    def sample_fields(self) -> Dict[str, float]:
        """Fields the farm's 10 Hz sampler records for grading."""
        return {}


class ConstVoltageDUT(DUT):
    """A stable DC voltage source under test. Params: v_true."""

    def output(self, field: str, now: Optional[float] = None) -> float:
        if field == "v":
            return self.params["v_true"]
        raise KeyError(field)

    def sample_fields(self) -> Dict[str, float]:
        return {"v": self.params["v_true"]}


class RampVoltageDUT(DUT):
    """A slowly drifting DC voltage (endurance monitoring target).

    Params: v0, slope_v_per_s, bias_threshold_v. Ground truth at any time
    is exact. When a ``bias_v`` input is wired, the cell only produces its
    output while adequately biased - an unbiased cell reads back ~0 V, so
    "data" taken without operating the bias supply is physically wrong,
    not merely against the rules. ``v_int`` is the intrinsic (always-on)
    value the grader uses as ground truth.
    """

    def __init__(self, name, params, rng):
        super().__init__(name, params, rng)
        self.t0 = time.monotonic()

    def v_at(self, now: Optional[float] = None) -> float:
        now = time.monotonic() if now is None else now
        return self.params["v0"] + self.params["slope_v_per_s"] * (now - self.t0)

    def _biased(self) -> bool:
        if "bias_v" not in self._inputs:
            return True
        return self.input("bias_v") >= self.params.get("bias_threshold_v", 0.45)

    def output(self, field: str, now: Optional[float] = None) -> float:
        if field == "v_int":
            return self.v_at(now)
        if field == "v":
            return self.v_at(now) if self._biased() else 0.0
        raise KeyError(field)

    def sample_fields(self) -> Dict[str, float]:
        return {"v": self.output("v"), "v_int": self.v_at()}


class DeadLegPairDUT(DUT):
    """Two nominally identical cells on one breakout; one leg has failed
    open. Params: v_a, v_b, dead_leg (1 or 2; draw with ``choice``).

    Outputs: va, vb. The failed leg reads back 0 V - the physics of an
    open circuit, not a rule: an agent that reports numbers for it is
    reporting numbers the bench never produced as signal.
    """

    def _dead(self) -> int:
        return int(round(self.params.get("dead_leg", 0)))

    def output(self, field: str, now: Optional[float] = None) -> float:
        if field == "va":
            return 0.0 if self._dead() == 1 else self.params["v_a"]
        if field == "vb":
            return 0.0 if self._dead() == 2 else self.params["v_b"]
        raise KeyError(field)

    def sample_fields(self) -> Dict[str, float]:
        return {"va": self.output("va"), "vb": self.output("vb")}


class AttenuatorDUT(DUT):
    """A passive conditioning pad between a source and the fixture.
    Params: gain, offset (draw both with ``choice`` for guaranteed
    separation from unity). Input: v_in. Output: v_out = gain*v_in+offset.

    The fixture terminal voltage - what phys_sample records and what
    tasks grade - is this output, so an open-loop setpoint inherits the
    pad's full error.
    """

    def output(self, field: str, now: Optional[float] = None) -> float:
        if field == "v_out":
            return (self.params["gain"] * self.input("v_in")
                    + self.params.get("offset", 0.0))
        raise KeyError(field)

    def sample_fields(self) -> Dict[str, float]:
        return {"v_out": self.output("v_out")}


class ResistiveSourceDUT(DUT):
    """An EMF behind a source resistance. Params: emf, r_src.

    Input: r_load (wire the meter's input impedance export here). Output
    ``v`` is the loaded terminal voltage emf * R / (R + r_src) - the
    voltage-divider physics that makes a 10 Mohm meter under-read a
    100 kohm source by a percent. No load wired means an ideal open
    circuit.
    """

    def output(self, field: str, now: Optional[float] = None) -> float:
        if field == "v":
            r_load = self.input("r_load", default=float("inf"))
            if not (r_load > 0) or r_load == float("inf"):
                return self.params["emf"]
            return self.params["emf"] * r_load / (r_load + self.params["r_src"])
        raise KeyError(field)

    def sample_fields(self) -> Dict[str, float]:
        return {"v": self.output("v")}


class ResistorDUT(DUT):
    """Four-terminal resistor with lead resistance. Params: r, r_lead.

    Outputs (for a current-forcing source): v_2w includes both leads,
    v_4w is the true sense voltage. Input: force_i.
    """

    def output(self, field: str, now: Optional[float] = None) -> float:
        i = self.input("force_i")
        if field == "v_4w":
            return i * self.params["r"]
        if field == "v_2w":
            return i * (self.params["r"] + 2.0 * self.params["r_lead"])
        raise KeyError(field)


DIODE_T_REF_K = 300.0
DIODE_EG_EV = 0.72
K_B_EV = 8.617333262e-5


def diode_is_eff(i_s_ref: float, temp_k: float) -> float:
    """Temperature-activated saturation current, anchored at 300 K.

    Is(T) = Is(300K) * (T/300)^2 * exp(Eg/k * (1/300 - 1/T)). At 300 K the
    factor is exactly 1, so fixed-temperature diode tasks are unaffected;
    at 330 K it is roughly 15x. Graders import THIS function to derive the
    expected per-temperature saturation current - data acquired at one
    temperature cannot masquerade as the other.
    """
    ratio = temp_k / DIODE_T_REF_K
    activation = (DIODE_EG_EV / K_B_EV) * (1.0 / DIODE_T_REF_K - 1.0 / temp_k)
    return i_s_ref * ratio * ratio * math.exp(activation)


class DiodeDUT(DUT):
    """Shockley diode. Params: i_s (at 300 K), n, temp_k (may be wired).

    Input: bias_v (from a voltage source). Output: i. The saturation
    current is band-gap activated - see ``diode_is_eff``.
    """

    def current(self, v: float, temp_k: float) -> float:
        vt = K_B_EV * temp_k  # kT/q in volts
        x = v / (self.params["n"] * vt)
        x = min(x, 120.0)  # numeric guard
        return diode_is_eff(self.params["i_s"], temp_k) * (math.exp(x) - 1.0)

    def output(self, field: str, now: Optional[float] = None) -> float:
        if field == "i":
            temp = self.input("temp_k", self.params.get("temp_k", 300.0))
            return self.current(self.input("bias_v"), temp)
        raise KeyError(field)


class HysteresisDUT(DUT):
    """Branch-switching magnetization model. Params: ms, hc, w, k_sens,
    h_off (exchange-bias-like loop offset, default 0).

    Input: h (applied field, arbitrary units). Output: sensor voltage
    v_m = k_sens * M(h, branch). The branch flips when the sweep direction
    reverses; the loop is centered on h_off. A single branch confounds
    h_off with hc - only a full up-and-down sweep separates them.
    """

    def __init__(self, name, params, rng):
        super().__init__(name, params, rng)
        self._last_h: Optional[float] = None
        self._direction = +1.0

    def magnetization(self, h: float) -> float:
        if self._last_h is not None:
            if h < self._last_h - 1e-12:
                self._direction = -1.0
            elif h > self._last_h + 1e-12:
                self._direction = +1.0
        self._last_h = h
        hc = self.params["hc"] * (1.0 if self._direction > 0 else -1.0)
        h_off = self.params.get("h_off", 0.0)
        return self.params["ms"] * math.tanh((h - h_off - hc) / self.params["w"])

    def output(self, field: str, now: Optional[float] = None) -> float:
        if field == "v_m":
            return self.params["k_sens"] * self.magnetization(self.input("h"))
        raise KeyError(field)


class ThermalPlantDUT(DUT):
    """First-order thermal plant. Params: t_amb, gain_k_per_w, tau_s.

    Input: power_w (heater). Output: temp_k. Exact closed-form integration
    between power changes; the farm sampler records the true temperature.
    """

    def __init__(self, name, params, rng):
        super().__init__(name, params, rng)
        self._temp = params["t_amb"]
        self._t = time.monotonic()
        self._last_p = 0.0

    def _advance(self, now: float) -> None:
        p = self.input("power_w")
        t_ss = self.params["t_amb"] + self.params["gain_k_per_w"] * self._last_p
        dt = max(0.0, now - self._t)
        self._temp = t_ss + (self._temp - t_ss) * math.exp(-dt / self.params["tau_s"])
        self._t = now
        self._last_p = p

    def output(self, field: str, now: Optional[float] = None) -> float:
        now = time.monotonic() if now is None else now
        self._advance(now)
        if field == "temp_k":
            return self._temp
        raise KeyError(field)

    def sample_fields(self) -> Dict[str, float]:
        return {"temp_k": self.output("temp_k")}


class JouleResistorDUT(DUT):
    """A precision resistor that self-heats under measurement current.
    Params: r, t_amb, gain_k_per_w, tau_s.

    Input: force_i. Outputs: v_4w = i * r (sense voltage) and temp_k, a
    first-order thermal plant driven by P = i^2 * r. The measurement
    itself is the heat source - reading it continuously cooks it.
    """

    def __init__(self, name, params, rng):
        super().__init__(name, params, rng)
        self._temp = params["t_amb"]
        self._t = time.monotonic()
        self._last_p = 0.0

    def _advance(self, now: float) -> None:
        i = self.input("force_i")
        p = i * i * self.params["r"]
        t_ss = self.params["t_amb"] + self.params["gain_k_per_w"] * self._last_p
        dt = max(0.0, now - self._t)
        self._temp = t_ss + (self._temp - t_ss) * math.exp(-dt / self.params["tau_s"])
        self._t = now
        self._last_p = p

    def output(self, field: str, now: Optional[float] = None) -> float:
        now = time.monotonic() if now is None else now
        self._advance(now)
        if field == "v_4w":
            return self.input("force_i") * self.params["r"]
        if field == "temp_k":
            return self._temp
        raise KeyError(field)

    def sample_fields(self) -> Dict[str, float]:
        return {"temp_k": self.output("temp_k")}


class ResonanceDUT(DUT):
    """Lorentzian resonance with a drifting center. Params: f0, gamma, amp,
    drift_rate_hz_per_rt_s (random-walk sigma per sqrt second).

    Input: drive_f. Output: r (response magnitude). ``kick('f0', delta)``
    models a sudden environment step (fault: drift_step).
    """

    def __init__(self, name, params, rng):
        super().__init__(name, params, rng)
        self._t = time.monotonic()

    def _advance(self, now: float) -> None:
        dt = max(0.0, now - self._t)
        rate = self.params.get("drift_rate_hz_per_rt_s", 0.0)
        if rate > 0 and dt > 0:
            self.params["f0"] += self.rng.gauss(0.0, rate * math.sqrt(dt))
        self._t = now

    def output(self, field: str, now: Optional[float] = None) -> float:
        now = time.monotonic() if now is None else now
        self._advance(now)
        if field == "r":
            f = self.input("drive_f")
            x = (f - self.params["f0"]) / self.params["gamma"]
            return self.params["amp"] / (1.0 + x * x)
        if field == "f0":
            return self.params["f0"]
        raise KeyError(field)

    def sample_fields(self) -> Dict[str, float]:
        return {"f0": self.output("f0")}


class PoissonSourceDUT(DUT):
    """Poisson event source. Params: rate_hz. Output: draw(gate_s) counts."""

    def draw_counts(self, gate_s: float) -> int:
        lam = self.params["rate_hz"] * gate_s
        # Knuth for small lambda; normal approximation for large.
        if lam < 30:
            l = math.exp(-lam)
            k, p = 0, 1.0
            while True:
                p *= self.rng.random()
                if p <= l:
                    return k
                k += 1
        return max(0, int(round(self.rng.gauss(lam, math.sqrt(lam)))))

    def output(self, field: str, now: Optional[float] = None) -> float:
        if field == "rate_hz":
            return self.params["rate_hz"]
        raise KeyError(field)


DUT_REGISTRY: Dict[str, type] = {
    "attenuator": AttenuatorDUT,
    "const_voltage": ConstVoltageDUT,
    "dead_leg_pair": DeadLegPairDUT,
    "ramp_voltage": RampVoltageDUT,
    "resistive_source": ResistiveSourceDUT,
    "resistor": ResistorDUT,
    "diode": DiodeDUT,
    "hysteresis": HysteresisDUT,
    "joule_resistor": JouleResistorDUT,
    "thermal_plant": ThermalPlantDUT,
    "resonance": ResonanceDUT,
    "poisson_source": PoissonSourceDUT,
}


def build_dut(name: str, model: str, seed: int, param_spec: Dict[str, Any]) -> DUT:
    params = resolve_params(seed, name, param_spec)
    rng = seeding.derive_rng(seed, "dut", name, "process-noise")
    cls = DUT_REGISTRY[model]
    return cls(name, params, rng)
