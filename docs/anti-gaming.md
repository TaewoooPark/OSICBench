# Anti-gaming design

## Training-data canary

Every task file and manual carries a canary line with a unique GUID
(see `CANARY.md`). Data curators are asked to exclude documents carrying
it from training corpora, and a model that can reproduce the GUID
without benchmark files in context has demonstrably trained on the
benchmark. `tests/test_canary.py` enforces the stamp on every data file,
so new tasks cannot land unstamped.

## Outcome-only grading

No AST checks, no style points, no pattern matching. The grader sees saved
files and recorded physics. Every task ships reference solutions in at
least two deliberately different idioms (straight-line procedural vs
class-based; instrument-side vs process-level defenses on the endurance
task), and `osicbench validate` asserts they all pass - if a grader ever
penalized structure, the multi-idiom gate would catch it.

## The manual never lies; the answers vary

Seeds vary hidden physics (the quantities tasks ask you to recover), noise
streams, and port assignment. Facts stated in an instrument manual -
terminations, command names, limits, quirk behaviors - are fixed. So
memorizing a published seed's answers does not transfer, while reading the
manual always pays.

## Self-proving graders

Every task ships mutants: deliberately wrong solutions embodying the
classic field mistakes (no settling, stale registers, missing remote
sense, compliance-blind fits, fabricated rows, unsafe exits, kelvin/
celsius confusion, polling storms, missing reconfiguration after a
brown-out, no defense against sudden process death). `osicbench validate`
asserts every reference passes and every mutant fails, across seeds. A
grader change that stops catching a mutant fails validation.

## Fabrication reconciliation

Data volume is bounded by what the instruments actually served (per-
response reading accounting, block transfers counted correctly),
timestamps are checked against recorded outage windows, and - for tasks
that declare raw-reading columns - submitted VALUES are matched against
the exact readings the instruments returned. Interpolating across an
outage, duplicating a sweep file, or substituting cleaner numbers than
the bench produced all fail reconciliation.

## Hidden-state isolation

The farm writes its flight recorder (which carries the seeded ground
truth) into a private temporary directory outside the run tree; the
submission receives only a copy of `endpoints.json` (host/port/resource)
and its results directory. Farm files are collected into the run tree
only after the farm stops. This is deliberate separation, not an OS
security boundary: submissions run as the same user, and a determined
process can hunt the filesystem. Two backstops make that unprofitable:
value reconciliation flags ground-truth substitution regardless of how
the truth was obtained, and adversarial evaluations should run each
submission in a container (see `adapters/`). Run directories are
single-use - the runner refuses a non-empty output directory - so stale
artifacts can never grade a fresh submission.

## Time discipline

Fault triggers are transaction-indexed wherever possible, physical time
constants sit at or above 100 ms (far above host jitter), efficiency is
counted in transactions, and every task has a wall-clock ceiling with at
least 3x headroom over the reference solutions. Scores do not depend on
host speed.

## Known limitations (stated, not hidden)

- The farm is a simulation. It reproduces protocol semantics, first-order
  physics, noise/settling economics, and failure modes - not analog
  subtleties (ground loops, EMI, thermal EMFs) or vendor firmware bugs
  beyond those modeled. OSIC-Bench is a pre-hardware gate, not a substitute
  for first contact with a real bench.
- Submissions are Python processes speaking raw TCP. Toolchains that
  require a VISA layer can still participate (the endpoints are standard
  socket resources), but no VISA backend ships with the bench.
- The sandbox provides process isolation, budgets, and kill semantics -
  not a network jail. Submissions are trusted not to attack the host;
  same-user filesystem separation of hidden state is best-effort (see
  "Hidden-state isolation"). Container-per-submission is the setting for
  adversarial evaluation.
- Subset selection is not detected: an agent that takes more readings
  than it submits and keeps the prettiest REAL ones passes reconciliation
  (every submitted value is genuine). Transaction budgets bound how much
  selection pressure is available; statistics graded from the submitted
  rows bound what selection can win.
- The harness relies on POSIX process semantics (process groups,
  SIGKILL); Linux and macOS are supported hosts. Windows is not.
- Mutants are authored by the task authors. The validation gate proves
  the graders catch the failure modes we thought of; the three-reference
  rule (two idioms plus one materially different measurement strategy on
  statistics-sensitive tasks) is the guard against tolerances tuned to a
  single canonical solution.
- Instrument manuals are short (60-150 lines). They model "the facts are
  in the manual and the manual is right" - not the real-world skill of
  mining a 500-page vendor PDF for the one paragraph that matters.
