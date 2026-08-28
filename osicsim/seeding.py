"""Deterministic seed derivation.

One task seed fans out into independent, reproducible random streams for
every hidden quantity in the farm (DUT parameters, noise streams, port
assignment). Graders re-derive ground truth from the same seed with the
same scope names, so nothing secret ever needs to be stored.

Policy (anti-gaming): the seed varies ONLY hidden physics, noise, fault
timing jitter, and ports. Every fact stated in an instrument manual is
fixed per task - the manual never lies.
"""
from __future__ import annotations

import hashlib
import random


def derive_rng(seed: int, *scope: str) -> random.Random:
    """Return an independent Random stream for (seed, scope...).

    Streams with different scopes are statistically independent; the same
    (seed, scope) always yields the same stream.
    """
    material = "|".join([str(int(seed)), *scope]).encode("utf-8")
    digest = hashlib.sha256(material).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def derive_uniform(seed: int, lo: float, hi: float, *scope: str) -> float:
    """One deterministic uniform draw in [lo, hi] for (seed, scope...)."""
    return derive_rng(seed, *scope).uniform(lo, hi)


def derive_loguniform(seed: int, lo: float, hi: float, *scope: str) -> float:
    """One deterministic log-uniform draw in [lo, hi] (lo, hi > 0)."""
    import math

    if lo <= 0 or hi <= lo:
        raise ValueError(f"log-uniform requires 0 < lo < hi, got [{lo}, {hi}]")
    u = derive_rng(seed, *scope).random()
    return math.exp(math.log(lo) + u * (math.log(hi) - math.log(lo)))


def derive_choice(seed: int, options: list, *scope: str):
    """One deterministic choice from options for (seed, scope...)."""
    return derive_rng(seed, *scope).choice(list(options))
