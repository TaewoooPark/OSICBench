# T07 - Closed-loop resonance tracking

The resonator bench again (Meridian MER-L820, internal oscillator drives
the device and demodulates its response) - but this time the resonance
MOVES: it drifts slowly, and the environment occasionally steps it by as
much as ~1 kHz in either direction, without warning.

## Fixture data

- Initial resonance: between **40 kHz and 60 kHz**; Lorentzian, HWHM
  (gamma) in the 300-800 Hz range; on-resonance response 10-30 uV.

## Goal

Acquire the resonance and then **keep the oscillator on it**:

- from **35 s after your process starts** until you exit (run for at least
  95 s total), keep the drive within **gamma/3** of the true center for at
  least 85 % of the time;
- after each sudden step, re-acquire within a few seconds (recovery is
  graded; being back on-center within ~8 s scores full marks).

There are no file deliverables: grading compares the recorded oscillator
frequency against the recorded true center, continuously.

## Environment contract

- `OSIC_ENDPOINTS`: path to a JSON file,
  `{"<device>": {"host": ..., "port": ..., "resource": ...}}`;
  the lock-in is `l820`.
- Manual: `manuals/mer_l820.md`.

## Notes

- 120 s hard ceiling; exit on your own after ~100 s of operation.
