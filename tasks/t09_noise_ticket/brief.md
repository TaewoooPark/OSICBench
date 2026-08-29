# T09 - Forensics: the noisy monitor

You inherit a production monitor script and a complaint ticket. Read both,
find the root causes, and ship a fixed monitor.

## Materials (in this task directory)

- `rig/monitor.py` - the current production script (runnable as-is).
- `rig/TICKET.md`  - the operator's complaint.
- `manuals/mer_d610.md` - the meter's manual.

## Goal

Deliver a corrected monitor whose output meets the meter's precision
specification: 10 readings, per-reading sigma within 300 uV, mean faithful
to the cell voltage. Same deliverables as the original script:

1. `readings.csv` (`t,v`, 10 rows)
2. `result.json` (`{"mean":..., "std":...}` from those rows)

## Environment contract

Same as the rig: `OSIC_ENDPOINTS` (meter `dmm1`), `OSIC_RESULTS_DIR`.

## Notes

- Record readings exactly as parsed from the instrument (full float
  precision). Submitted values are reconciled against the readings the
  instruments actually returned; heavy rounding fails that check.
- 60 s hard ceiling. Fabricated rows zero the task.
- Your submission is the fixed script itself (it will be executed once).
