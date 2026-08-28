"""Statistics for benchmark reports - stdlib only.

Unit of analysis: one (task, seed) run. Condition comparisons are paired
on (task, seed).
"""
from __future__ import annotations

import math
import random
from typing import Dict, List, Sequence, Tuple


def wilson_ci(k: int, n: int, z: float = 1.959964) -> Tuple[float, float, float]:
    """Wilson score interval for a binomial proportion: (p, lo, hi)."""
    if n == 0:
        return 0.0, 0.0, 0.0
    p = k / n
    z2 = z * z
    denom = 1 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return p, max(0.0, center - half), min(1.0, center + half)


def mcnemar_exact_p(b: int, c: int) -> float:
    """Exact two-sided McNemar p-value on discordant pairs.

    b = pairs where condition A passed and B failed;
    c = pairs where B passed and A failed.
    """
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / (2 ** n)
    return min(1.0, 2.0 * tail)


def paired_bootstrap_ci(
    diffs: Sequence[float], n_boot: int = 10000, seed: int = 20260828,
    alpha: float = 0.05,
) -> Tuple[float, float, float]:
    """Mean difference with a bootstrap percentile CI: (mean, lo, hi)."""
    diffs = list(diffs)
    if not diffs:
        return 0.0, 0.0, 0.0
    rng = random.Random(seed)
    n = len(diffs)
    mean = sum(diffs) / n
    means: List[float] = []
    for _ in range(n_boot):
        sample = [diffs[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    lo = means[int((alpha / 2) * n_boot)]
    hi = means[int((1 - alpha / 2) * n_boot) - 1]
    return mean, lo, hi


def summarize_pass(results: Sequence[bool]) -> Dict[str, float]:
    k = sum(1 for r in results if r)
    n = len(results)
    p, lo, hi = wilson_ci(k, n)
    return {"passed": k, "total": n, "rate": p, "ci_lo": lo, "ci_hi": hi}
