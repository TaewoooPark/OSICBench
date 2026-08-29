# Authoring a task

A task is a directory under `tasks/`:

```
tasks/<id>/
├── task.yaml          # farm, faults, budgets, HSS rules, limits
├── brief.md           # what the agent sees (with the environment contract)
├── rig/               # (forensics tasks) inherited scripts + sample output
├── oracle/grade.py    # grade(ctx) -> {"dfs": float, ...}
├── reference/*.py     # >= 2 solutions in DIFFERENT idioms; all must pass
└── mutants/*.py       # >= 4 wrong solutions; all must fail
```

## Design rules

1. **Ground the task in a practitioner bottleneck**, not in a feature of
   the simulator. Name the real-world failure the task exists to measure.
2. **Make the physics do the grading.** Prefer hidden quantities whose
   recovery is corrupted by the mistake you are testing (settling errors
   bend fits; wrong temperature bends Vt) over rule-based detection.
3. **Put the decisive facts in the manual.** If success requires knowing
   something, write it into the instrument manual - in prose, once. Never
   put per-seed values in any manual.
4. **State the deliverable schema exactly** (file names, headers, units)
   in the brief. Structure of the CODE is never constrained.
5. **Fix what seeds may vary**: hidden physics, noise, fault timing.
   Everything the brief or manuals state is seed-invariant.
6. **Give a 3x wall-clock margin** over the slowest reference, and prefer
   transaction-indexed fault triggers over timed ones.
7. **If the task SIGKILLs the agent, say so in the brief** ("your process
   may be terminated without warning") - it tests a design habit, not
   luck.

## Oracle contract

`grade(ctx)` receives a `GradeContext`:

- `ctx.truth_params(dut)` - re-derives hidden ground truth from the seed
- `ctx.read_rows(name)` / `ctx.read_json(name)` - deliverables (None and a
  note when missing)
- `ctx.fabrication(rows)` - reading-count + outage-window reconciliation
- `ctx.phys(node, field)` / `ctx.states(dev, field)` - recorder series
- `ctx.notes` - append human-readable grading detail

Return at least `{"dfs": float, "fabricated": bool}`; add `"rs"` where the
task grades robustness. HSS, budgets, and the pass rule are applied by the
harness from `task.yaml`.

Scoring style: use `param_score(err, tol)` and `combine_min` - and write a
one-line comment justifying every tolerance.

## Raw-value reconciliation

If the deliverable rows are raw instrument readings, declare the columns
in `task.yaml` under `grading.value_cols` and state in the brief that
readings must be recorded at full float precision. Only do this for
columns that ARE raw readings (never for programmed setpoints or values
the brief allows to be post-processed), and only when the reference
solutions write full precision - a `%.3f` in a reference will fail its
own reconciliation.

## Environment contract (every brief, verbatim shape)

Mode A agents write their endpoint parser blind - the farm is down while
they author, so they can never inspect `endpoints.json` before committing
to a format. A brief that names the devices but not the format therefore
makes the agent guess path-vs-inline JSON, and that guess decides the
task. Every brief must state both, e.g.

```
- `OSIC_ENDPOINTS`: path to a JSON file,
  `{"<device>": {"host": ..., "port": ..., "resource": ...}}`;
  the SMU is `smu1`.
```

References are written by the task author, who already knows the
contract, so `osicbench validate` cannot catch an unstated one. Check it
by reading the brief as if it were the only thing you had.

## Fault fairness

Every environment behavior that decides pass/fail must be announced in
the brief or the manual (the announcement can be qualitative - "the link
drops", "the display freezes" - the timing stays hidden). A fault the
references cannot survive is a task bug: the validation gate requires
references to pass WITH the fault schedule active.

## Validation gate (mandatory before merging a task)

```
osicbench validate --tasks tasks --task <id> --seeds 2 --jobs 6
```

- every reference passes on every seed;
- every mutant fails on every seed;
- if a mutant passes, the GRADER is wrong until proven otherwise - that is
  the point of shipping mutants.
