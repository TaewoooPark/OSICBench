# T03 - Diode IV characterization

A packaged diode is mounted on the fixture of a Meridian MER-S240 SMU
(anode to force HI). The junction temperature is regulated at 300.0 K.

## Goal

Acquire a forward IV curve over **0.30 V to 0.55 V** and extract the
Shockley parameters, assuming I = Is * (exp(V / (n * Vt)) - 1) with
Vt = kT/q at 300.0 K:

- `i_s` - saturation current (target accuracy: within ~7 %)
- `n`   - ideality factor (target accuracy: +/- 0.02)

Use a voltage step no larger than 0.02 V. Set the current compliance
generously (currents up to ~15 mA can occur at the top of the range) and
make sure the points you fit are real measurements, not clamped ones.

## Environment contract

- `OSIC_ENDPOINTS`: device endpoints; the SMU is `smu1`.
- `OSIC_RESULTS_DIR`: deliverables directory.
- Instrument manual: `manuals/mer_s240.md`.

## Deliverables

1. `iv.csv` - the raw sweep, header `t,v,i`: epoch seconds, programmed (and
   settled) bias in volts, measured current in amperes. At least 13 points.
2. `params.json` - `{"i_s": <amperes>, "n": <dimensionless>}`

## Notes

- 90 s hard ceiling.
- Grading checks the recorded hardware state: source back at zero, output
  off at exit. Rows the instrument never produced are fabrication.
