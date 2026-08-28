"""Oracle for T05 - Lorentzian center and quality factor."""
from osicbench.grading import combine_min, param_score

Q_REL_TOL = 0.10
MIN_ROWS = 60


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
    ctx.notes.append(
        f"truth f0={f0_true:.1f} q={q_true:.2f} gamma={truth['gamma']:.1f} "
        f"amp={truth['amp']:.2e} | hat f0={f0_hat:.1f} q={q_hat:.2f} | {scores}"
    )
    return {"dfs": combine_min(scores), "fabricated": False}
