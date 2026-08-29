# T05 - Resonance spectrum

A mechanical resonator is driven by the internal oscillator of a Meridian
MER-L820 lock-in amplifier, which also demodulates the response.

## Fixture data

- The resonance lies somewhere between **25 kHz and 75 kHz**.
- On-resonance response amplitude: expected in the **5 to 20 uV** range.
- The line shape is Lorentzian: R(f) = A / (1 + ((f - f0)/gamma)^2).

## Goal

Locate the resonance and report:

- `f0` - center frequency in Hz (target accuracy: about gamma/5)
- `q`  - quality factor, defined as f0 / FWHM = f0 / (2*gamma)
  (target accuracy: 10 %)

Choose the sensitivity and filter deliberately: the manual documents how
reading noise scales with the full-scale range, what an overloaded input
returns, and how long the output filter needs after any change.

## Environment contract

- `OSIC_ENDPOINTS`: the lock-in is `l820`.
- `OSIC_RESULTS_DIR`: deliverables directory.
- Instrument manual: `manuals/mer_l820.md`.

## Deliverables

1. `spectrum.csv` - every point you measured, header `t,f,r`
   (epoch seconds, drive frequency in Hz, response in volts).
2. `result.json` - `{"f0": <Hz>, "q": <dimensionless>}`

## Notes

- 90 s hard ceiling. Rows the instrument never produced are fabrication;
  record responses at full float precision - values are reconciled against
  what the lock-in actually returned.
- This unit has a known display fault: readings occasionally FREEZE for a
  few seconds (byte-identical repeats while the drive moves). Data taken
  from a frozen display is not data; detect it and re-measure. Grading
  includes the fraction of spectrum points consistent with the physics.
