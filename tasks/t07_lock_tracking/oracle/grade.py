"""Oracle for T07 - continuous tracking quality from the recorder."""
from osicbench.grading import combine_min, param_score

WINDOW_START_S = 35.0
WINDOW_MIN_SPAN_S = 55.0
IN_BAND_TARGET = 0.85
RECOVERY_TOL_S = 8.0
KICK_TIMES_S = (45.0, 70.0)


def _pair(freq_series, f0_series):
    """Pair each frequency sample with the nearest-in-time truth sample."""
    pairs = []
    j = 0
    for t, f in freq_series:
        while j + 1 < len(f0_series) and abs(f0_series[j + 1][0] - t) <= abs(f0_series[j][0] - t):
            j += 1
        pairs.append((t, f, f0_series[j][1]))
    return pairs


def grade(ctx):
    freq = ctx.phys("l820", "freq")
    f0 = ctx.phys("dut1", "f0")
    if len(freq) < 100 or len(f0) < 100:
        ctx.notes.append("tracking record too short")
        return {"dfs": 0.0, "fabricated": False}
    gamma = ctx.truth_params("dut1")["gamma"]
    band = gamma / 3.0
    t0 = min(freq[0][0], f0[0][0])

    pairs = [(t, f, c) for t, f, c in _pair(freq, f0) if t - t0 >= WINDOW_START_S]
    if not pairs or (pairs[-1][0] - pairs[0][0]) < WINDOW_MIN_SPAN_S:
        ctx.notes.append("tracking window too short")
        return {"dfs": 0.0, "fabricated": False}

    in_band = [abs(f - c) <= band for _, f, c in pairs]
    frac = sum(in_band) / len(in_band)

    recoveries = {}
    for kick in KICK_TIMES_S:
        # Recovery = first instant after the kick from which the drive stays
        # in band for at least 2 s (a single boundary sample is not a lock).
        after = [(t, abs(f - c) <= band) for t, f, c in pairs if t - t0 > kick + 0.5]
        t_rec = None
        for idx, (t_i, ok_i) in enumerate(after):
            if not ok_i:
                continue
            sustained = True
            for t_j, ok_j in after[idx:]:
                if t_j - t_i > 2.0:
                    break
                if not ok_j:
                    sustained = False
                    break
            if sustained:
                t_rec = (t_i - t0) - kick
                break
        recoveries[kick] = t_rec if t_rec is not None else 1e9

    scores = {
        "in_band": param_score(max(0.0, IN_BAND_TARGET - frac), 0.05),
        "recovery_1": param_score(recoveries[KICK_TIMES_S[0]], RECOVERY_TOL_S),
        "recovery_2": param_score(recoveries[KICK_TIMES_S[1]], RECOVERY_TOL_S),
    }
    ctx.notes.append(
        f"gamma={gamma:.0f} frac_in_band={frac:.2%} recoveries={recoveries} {scores}"
    )
    return {"dfs": combine_min(scores), "fabricated": False}
