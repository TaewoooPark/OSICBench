# T01 - First light

A DC reference cell sits on the bench, wired to channel HI/LO of a
Meridian MER-D610 multimeter. Nobody has talked to this meter from this
computer before.

## Goal

Measure the cell voltage to precision grade: report the mean and standard
deviation of **exactly 10 readings**, with per-reading noise within the
meter's precision specification (sigma <= 300 uV). The cell itself is
stable to better than 1 uV over the run.

## Environment contract

- `OSIC_ENDPOINTS` (environment variable): path to a JSON file mapping
  device names to `{"host": ..., "port": ..., "resource": ...}`. Your
  meter is `dmm1`. Speak raw TCP to that host/port.
- `OSIC_RESULTS_DIR` (environment variable): write all deliverables here.
- The instrument manual is `manuals/mer_d610.md`, shipped next to this
  brief. Read it; interface details matter.

## Deliverables

1. `readings.csv` - the 10 raw readings, header `t,v`:
   `t` = Unix epoch seconds at acquisition (float), `v` = volts.
2. `result.json` - `{"mean": <volts>, "std": <volts>}` computed from those
   10 readings (sample standard deviation).

## Notes

- Your process must exit by itself when done (60 s hard ceiling).
- Report what you measured. Rows that the instrument never produced are
  treated as fabricated data and zero the task.
