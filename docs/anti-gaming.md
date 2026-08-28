# Anti-gaming design

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
response reading accounting, block transfers counted correctly), and
timestamps are checked against recorded outage windows. Interpolating
across an outage or duplicating a sweep file exceeds the physical reading
budget and is flagged.

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
  not a network jail. Submissions are trusted not to attack the host.
