# T04 - Hysteresis loop

A magnetic film sample sits inside a field coil driven by a Meridian
MER-S240 (current mode). A magneto-optic sensor reads the film's
magnetization as a voltage on a Meridian MER-D610 multimeter.

## Fixture data (calibration sheet)

- Coil constant: **50 mT per ampere** (field h_mT = 50 * I_A).
- Sensor constant: **0.2 V per unit magnetization** (v = 0.2 * M).
- The loop may be offset from zero field (exchange bias); do not assume
  symmetry about h = 0.
- Coil inductance: keep every programmed current step at or below the
  SMU manual's recommended maximum, and return the current to zero by
  ramping before disabling the output.

## Goal

Trace a full hysteresis loop (both branches, saturating both ways within
+/- 1 A of coil current) and report:

- `hc_mt`  - the coercive field in millitesla (half the separation of the
  two zero crossings; target accuracy ~1.5 mT)
- `ms`     - saturation magnetization in magnetization units
  (target accuracy ~5 %)

Pair every sensor reading with the coil current the SMU actually reports,
not with the programmed setpoint.

## Environment contract

- `OSIC_ENDPOINTS`: path to a JSON file,
  `{"<device>": {"host": ..., "port": ..., "resource": ...}}`;
  `smu1` (coil SMU), `dmm1` (sensor meter).
- `OSIC_RESULTS_DIR`: deliverables directory.
- Manuals: `manuals/mer_s240.md`, `manuals/mer_d610.md`.

## Deliverables

1. `loop.csv` - header `t,i_a,v`: epoch seconds, measured coil current in
   amperes, sensor voltage in volts. At least 60 points covering both
   sweep directions.
2. `params.json` - `{"hc_mt": <mT>, "ms": <units>}`

## Notes

- 150 s hard ceiling.
- Hardware-state grading: current-step limit respected throughout, source
  ramped to zero and output off at exit.
- Record measured values exactly as parsed from the instrument (full
  float precision); they are reconciled against the readings it
  actually returned.
