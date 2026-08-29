# T08 - Forensics: the rig whose diodes read wrong

Your group's diode-screening rig has been quietly producing ideality
factors that vendors dispute. You inherit the script and one example of
its output. Diagnose it, fix it, and produce trustworthy parameters.

## Materials (in this task directory)

- `rig/sweep.py` - the production sweep script (runnable as-is).
- `rig/iv_sample_bad.csv`, `rig/params_sample_bad.json` - one recent run's
  output, produced by this exact script on a same-model bench (different
  diode than yours; the vendor's datasheet disagrees with both parameters, badly).
- `manuals/mer_s240.md` - the SMU manual. The measurement-model section
  repays close reading.

## Goal

Same contract as the rig: sweep 0.30-0.55 V (steps <= 0.02 V) and deliver

1. `iv.csv` (`t,v,i`, at least 13 points)
2. `params.json` (`{"i_s":..., "n":...}`), targets: n within +/- 0.02,
   i_s within ~7 %.

## Environment contract

`OSIC_ENDPOINTS` (path to a JSON file,
`{"<device>": {"host": ..., "port": ..., "resource": ...}}`;
the SMU is `smu1`), `OSIC_RESULTS_DIR`. 90 s hard ceiling.
Hardware-state grading: source zeroed and output off at exit.
- Record measured values exactly as parsed from the instrument (full
  float precision); they are reconciled against the readings it
  actually returned.
