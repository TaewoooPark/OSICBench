# T12 - Bulk acquisition under a transaction budget

The same reference-cell bench as a routine monitor job, but this time the
controller link is shared and slow: your run is only allowed a small
number of bus transactions.

## Goal

Collect **400 precision readings** (per-reading sigma within the 300 uV
spec) of the cell voltage and report their mean and sample standard
deviation - using **at most 40 bus transactions** in total (every command
or query counts as one).

## Environment contract

- `OSIC_ENDPOINTS`: the meter is `dmm1`.
- `OSIC_RESULTS_DIR`: deliverables directory.
- Instrument manual: `manuals/mer_d610.md` (the buffered-acquisition
  section is where the transaction economics live).

## Deliverables

1. `readings.csv` - all 400 readings, header `t,v` (`t` may repeat the
   block-readout time).
2. `result.json` - `{"mean": <volts>, "std": <volts>}`.

## Notes

- 60 s hard ceiling. Exceeding the transaction budget fails the run
  regardless of data quality. Fabricated rows zero the task.
