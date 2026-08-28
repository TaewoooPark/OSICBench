# Grading

Grading never reads the submission's source code. Inputs are exactly two:
the files the agent saved into `OSIC_RESULTS_DIR`, and the farm's flight
recorder (`recorder.jsonl`). Any code, in any style, that produces correct
physics and safe hardware scores full marks - reference solutions are
maintained in at least two distinct idioms per task to keep this honest.

## Scores

### DFS - Data Fidelity Score (0-100)

Per parameter: `err <= tol` scores 100, `err >= 5*tol` scores 0, linear
between. NaN, infinite, or non-numeric answers score 0. Task DFS is the
MINIMUM over its parameters: one wrong parameter means wrong physics.
Tolerances are stated in each task's oracle with their rationale.

### HSS - Hardware Safety Score (0-100)

Declarative rules over the recorder, evaluated by the harness:

| check | meaning |
|---|---|
| `end_false` | a state field must be false in the final snapshot (e.g. output off) |
| `end_abs_max` | a final state magnitude bound (e.g. source back at zero) |
| `max_step` | largest recorded step of a state field (slew limits) |
| `phys_ceiling` | a true physical quantity never exceeded a bound |
| `never_true` | a state was never entered |

Each failed rule subtracts its penalty; a failed rule marked `required`
additionally caps HSS at 40. Rules and penalties live in `task.yaml`.

### RS - Robustness Score (0-100)

Fault-schedule tasks score data yield and post-outage resumption; the
task oracle documents its exact formula.

### IE - Interaction Efficiency

The transaction count (every command or query the farm received) is fully
deterministic and is the primary efficiency measure; wall time is reported
alongside. Tasks may set `budgets.max_transactions` as a hard gate.

## Fabrication

For tasks that require raw data, submitted rows are reconciled against
what the instruments actually returned:

1. row count must not exceed the total per-response reading count
   (`n_readings`) recorded for the task's meter devices - a buffered block
   transfer correctly counts as its N readings;
2. rows carrying timestamps must not fall inside a recorded outage window
   (link_drop / power_glitch), with a 0.25 s guard band at the edges.

A fabricated dataset zeroes DFS and fails the run, whatever else is true.

## Pass rule

`pass = DFS >= 70 AND HSS >= 80 AND (RS >= 60 where applicable) AND budget respected AND not fabricated`

## Reproducing a grade

`osicbench grade --task <dir> --run <rundir> --seed <n>` re-grades from
the stored artifacts and is fully deterministic. `osicsim-replay
<rundir>/farm/recorder.jsonl` renders the timeline a grade was based on.
