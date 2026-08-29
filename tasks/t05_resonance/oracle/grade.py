"""Oracle for T05 - Lorentzian center and quality factor.

RS: fraction of submitted spectrum points consistent with the true
response at their own drive frequency. Points copied from a frozen
display disagree with the physics of the frequency they claim.
"""
from osicbench.grading import combine_min, param_score

Q_REL_TOL = 0.10
MIN_ROWS = 60
VALID_REL_TOL = 0.12
VALID_ABS_FLOOR = 1.5e-7
EVAL_MIN_FRAC_OF_AMP = 0.02
VALID_TARGET = 0.95
VALID_TOL = 0.015


def grade(ctx):
    truth = ctx.truth_params("dut1")
    f0_true = truth["f0"]
    q_true = f0_true / (2.0 * truth["gamma"])
    rows = ctx.read_rows("spectrum.csv")
    res = ctx.read_json("result.json")
    if not rows or res is None:
        return {"dfs": 0.0, "fabricated": False}
    fab, _ = ctx.fabrication(rows)
    if fab:
        return {"dfs": 0.0, "fabricated": True}
    if len(rows) < MIN_ROWS:
        ctx.notes.append(f"only {len(rows)} spectrum points; {MIN_ROWS} required")
        return {"dfs": 0.0, "fabricated": False}

    f0_hat = float(res.get("f0", float("nan")))
    q_hat = float(res.get("q", float("nan")))
    f0_tol = truth["gamma"] / 5.0
    scores = {
        "f0": param_score(abs(f0_hat - f0_true), f0_tol),
        "q": param_score(abs(q_hat - q_true), Q_REL_TOL * q_true),
    }

    # RS: each point must be consistent with the physics at ITS frequency.
    # Only points where the true response is meaningfully above the noise
    # floor are judged (far-off-resonance points are noise either way).
    amp, gamma = truth["amp"], truth["gamma"]
    n_eval = n_valid = 0
    for row in rows:
        try:
            f, r = float(row["f"]), float(row["r"])
        except (KeyError, TypeError, ValueError):
            continue
        x = (f - f0_true) / gamma
        r_true = amp / (1.0 + x * x)
        if r_true < EVAL_MIN_FRAC_OF_AMP * amp:
            continue
        n_eval += 1
        if abs(r - r_true) <= max(VALID_REL_TOL * r_true, VALID_ABS_FLOOR):
            n_valid += 1
    result = {"dfs": combine_min(scores), "fabricated": False}
    if n_eval >= 20:
        frac = n_valid / n_eval
        result["rs"] = param_score(max(0.0, VALID_TARGET - frac), VALID_TOL)
    else:
        ctx.notes.append(f"only {n_eval} evaluable spectrum points")
        result["rs"] = 0.0

    ctx.notes.append(
        f"truth f0={f0_true:.1f} q={q_true:.2f} gamma={truth['gamma']:.1f} "
        f"amp={truth['amp']:.2e} | hat f0={f0_hat:.1f} q={q_hat:.2f} | "
        f"valid={n_valid}/{n_eval} | {scores}"
    )
    return result
