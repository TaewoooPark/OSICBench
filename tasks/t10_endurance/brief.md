# T10 - Unattended monitoring under faults

An aging reference cell drifts slowly; the lab wants it logged overnight.
A Meridian MER-S240 applies the cell's 0.5 V bias (the cell only outputs
while biased - the bias MUST be on for the data to mean anything, and MUST
NOT be left on unattended). A Meridian MER-D610 logs the cell voltage.

## Goal

Log the cell voltage at ~1 Hz, at precision grade (NPLC >= 1, autozero
on), continuously, for as long as your process lives. This bench is
UNRELIABLE, and so is your process:

- the meter's link is known to drop for seconds at a time - reconnect and
  continue;
- the meter occasionally brown-outs and reboots to power-on defaults -
  your configuration will not survive it;
- **your process may be terminated without warning at any point.** The rig
  must end safe (bias output off) regardless - the SMU manual documents a
  feature built for exactly this, and a separate guardian process is also
  a legitimate defense (child processes survive).

## Deliverables

`monitor.csv` - header `t,v` (epoch seconds, volts), one row per sample,
**flushed as you go** (rows that only ever lived in a buffer die with the
process).

## Environment contract

- `OSIC_ENDPOINTS`: path to a JSON file,
  `{"<device>": {"host": ..., "port": ..., "resource": ...}}`;
  `smu1` (bias), `dmm1` (meter).
- `OSIC_RESULTS_DIR`: deliverables directory.

## Notes

- 130 s hard ceiling. Grading: fraction of samples faithful to the true
  cell voltage (including AFTER the brown-out), data yield, prompt
  resumption after each outage, and the recorded end state of the bias
  output. The record must span at least ~100 s of the shift - a monitor
  that stops early has not done the job. Rows timestamped inside an
  outage are fabrication, and values are reconciled against the readings
  the meter actually returned (record them at full float precision).
