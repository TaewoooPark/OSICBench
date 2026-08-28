"""Oracle for T06 - regulation quality from the true temperature record."""
import math

from osicbench.grading import combine_min, param_score

TARGET_K = 330.0
BAND_K = 0.25
ENTER_DEADLINE_S = 75.0
HOLD_S = 60.0
RMS_TOL_K = 0.10
OUT_OF_BAND_TOL = 0.10  # tolerated out-of-band fraction scale


def grade(ctx):
    series = ctx.phys("plant1", "temp_k")
    if len(series) < 50:
        ctx.notes.append("temperature record too short")
        return {"dfs": 0.0, "fabricated": False}
    t0 = series[0][0]

    # Band ACQUISITION: the first instant from which the stage stays inside
    # the band for at least 5 s (a first touch during a fast crossing does
    # not count as regulation).
    t_enter = None
    n = len(series)
    for idx in range(n):
        t_i, temp_i = series[idx]
        if abs(temp_i - TARGET_K) > BAND_K:
            continue
        ok = True
        for t_j, temp_j in series[idx:]:
            if t_j - t_i > 5.0:
                break
            if abs(temp_j - TARGET_K) > BAND_K:
                ok = False
                break
        if ok:
            t_enter = t_i
            break
    if t_enter is None or (t_enter - t0) > ENTER_DEADLINE_S:
        ctx.notes.append(f"never acquired the band in time (t_enter={t_enter})")
        return {"dfs": 0.0, "fabricated": False}

    window = [(t, temp) for t, temp in series if t_enter <= t <= t_enter + HOLD_S]
    span = window[-1][0] - window[0][0] if window else 0.0
    if span < HOLD_S - 2.0:
        ctx.notes.append(f"hold record spans only {span:.1f}s (need {HOLD_S}s)")
        return {"dfs": 0.0, "fabricated": False}

    errs = [temp - TARGET_K for _, temp in window]
    rms = math.sqrt(sum(e * e for e in errs) / len(errs))
    frac_out = sum(1 for e in errs if abs(e) > BAND_K) / len(errs)

    scores = {
        "rms": param_score(rms, RMS_TOL_K),
        "in_band": param_score(frac_out, OUT_OF_BAND_TOL),
    }
    ctx.notes.append(
        f"enter={t_enter - t0:.1f}s rms={rms:.3f}K out_of_band={frac_out:.2%} {scores}"
    )
    return {"dfs": combine_min(scores), "fabricated": False}
