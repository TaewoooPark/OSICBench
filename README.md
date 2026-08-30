# OSIC-Bench

**Operating Scientific Instruments via Code — a benchmark for AI agents.**

Can an AI agent write the code that runs a real experiment? Not "does the
code look right" — did the instrument produce **correct data**, did the
hardware end in a **safe state**, did the run **survive the faults** that
real benches throw at you?

OSIC-Bench measures exactly that. Agents get a physics goal and an
instrument manual; they write control code in any style they like; the code
runs against a simulated instrument farm with hidden ground-truth physics,
realistic protocol quirks, and scheduled fault injection. **Scoring never
looks at the code — only at what physically happened.**

## Quick start

```bash
pip install -e .

# run one reference solution against one task, then grade it
osicbench run --task tasks/t03_diode_iv \
    --submission tasks/t03_diode_iv/reference/ref_procedural.py \
    --seed 7 --out runs/demo --label demo

# the whole self-validation gate: references must pass, mutants must fail
osicbench validate --tasks tasks --seeds 2 --jobs 6 --out runs/validate

# aggregate labeled runs into a report (pass rates, CIs, paired stats)
osicbench report --runs runs/ --out reports/

# replay any run's flight recorder as a human-readable timeline
osicsim-replay runs/demo/farm/recorder.jsonl
```

Requirements: Python 3.10+, PyYAML, a POSIX host (Linux/macOS). No VISA
stack, no GUI — instruments are TCP endpoints
(`TCPIP0::127.0.0.1::<port>::SOCKET`). Hidden ground truth stays in a
farm-private directory during a run; submissions see only endpoints and
their results directory.

## The tasks (22)

| id | family | the practitioner bottleneck it measures |
|---|---|---|
| t01_first_light | Bring-up | terminations, greeting banners, integration-time vs the noise spec |
| t02_four_wire | Bring-up | lead resistance vs remote sensing; safe source shutdown |
| t03_diode_iv | Characterization | settling, stale reading registers, compliance-aware fitting |
| t04_hysteresis | Characterization | history-dependent physics, measured-axis pairing, coil step limits |
| t05_resonance | Characterization | sensitivity/full-scale trades, filter settling, overload sentinels |
| t06_thermal_hold | Closed loop | your code IS the controller: acquisition, regulation, damage ceiling |
| t07_lock_tracking | Closed loop | tracking a drifting resonance through sudden environment steps |
| t08_iv_forensics | Forensics | inherit a rig script + its wrong output; find both defects |
| t09_noise_ticket | Forensics | a 10x-too-noisy monitor and an operator ticket |
| t10_endurance | Endurance | link drops, a configuration-erasing brown-out, and SIGKILL mid-run |
| t11_temp_iv_orchestration | Orchestration | three instruments, one experiment; physics convicts scheduling shortcuts |
| t12_bulk_budget | Throughput | 400 precision readings under a 40-transaction budget |
| t13_interlock | Safety | no hardware interlock: the agent's polling loop must trip within a grace window and re-arm |
| t14_coil_park | Safety | slew-compliant energize and park through a mid-ramp link drop; recovery is not a license to slam |
| t15_dead_leg | Integrity | one of two cells failed open; report no_signal honestly instead of always delivering a number |
| t16_source_trim | Metrology | deliver the level at the fixture through an uncertain pad; the setpoint is not the measurement |
| t17_restart | Endurance | SIGKILLed mid-shift and restarted cold: resume your own log without loss or duplication |
| t18_esr_sync | Protocol | firmware batch where *OPC? no longer blocks; synchronize on the status register within a budget |
| t19_duty_thermal | Safety | the measurement current heats the sample toward a damage ceiling; duty-cycle or destroy |
| t20_wrong_ticket | Forensics | the operator's confident diagnosis is wrong; fix what the data says is broken |
| t21_hostile_link | Protocol | junk bytes, spurious error floods, response stalls; re-read, never repair or panic |
| t22_loading_correction | Metrology | the meter loads the source; two input impedances separate EMF from source resistance |

Every task ships **reference solutions in two different idioms** (all must
pass) — statistics-sensitive tasks add a **third reference with a
materially different measurement strategy** — and **4-5 mutants**
embodying classic field mistakes and known grader exploits (all must
fail).
`osicbench validate` enforces both, across seeds — the benchmark grades its
own graders.

## How scoring works

Every task runs against **osicsim**, a deterministic simulated instrument
farm: an SCPI-1999 / IEEE-488.2 protocol engine, per-device physics with
hidden seeded ground truth (noise, settling dynamics, compliance clamping,
branch-switching hysteresis, thermal plants, drifting resonances),
documented interface quirks, a scheduled fault injector, and a **flight
recorder** that logs every transaction (with per-response reading counts)
and every physical state transition.

Four outcome metrics — computed **only** from the saved data files and the
flight recorder:

- **DFS — Data Fidelity.** Do the results recover the hidden ground truth
  within tolerance? Unsettled readings, stale buffers, wrong ranges and
  missing averaging show up on their own, because the physics makes them
  wrong. Weakest parameter decides.
- **HSS — Hardware Safety.** Did every exit path (success, crash, timeout,
  SIGKILL) leave outputs safe? Were slew limits and damage ceilings
  respected during the run? Judged from recorded physical events.
- **RS — Robustness.** Under the fault schedule: how much valid data
  survived, and did the run resume promptly? Submitted rows are reconciled
  against the per-response reading totals the instruments actually
  returned (a block transfer counts as its N readings), against outage
  windows, and — where tasks declare raw-reading columns — against the
  exact reading VALUES the instruments served. Excess rows, gap-filling
  rows, and numbers the bench never produced are **fabrication** and zero
  the task.
- **IE — Interaction Efficiency.** Bus transactions against the task
  budget (fully deterministic), with wall time reported alongside.

Two execution modes: **Mode A (one-shot)** — code is written blind from the
brief and manuals, then executed once (`osicbench run`); **Mode B (live)**
— the agent may probe the farm under a hard reset budget
(`osicbench live`). The gap between them measures dependence on
trial-and-error against hardware — the scarcest resource in a lab.

Details: [docs/grading.md](docs/grading.md) ·
[docs/anti-gaming.md](docs/anti-gaming.md) ·
[docs/task-authoring.md](docs/task-authoring.md) ·
[adapters/README.md](adapters/README.md)

## Anti-gaming principles

1. **Outcome-only grading**, enforced by multi-idiom references.
2. **Fictional instruments, authored manuals** on top of public standards
   (SCPI-1999, IEEE 488.2). Facts that decide success live in the manuals;
   the manuals never lie.
3. **Seeded randomization of the answers** — hidden physics, noise, and
   fault timing vary per seed; manual-stated facts do not.
4. **Self-proving graders** — mutant detection is part of the released
   validation gate, not a promise ([docs/release-gate.md](docs/release-gate.md)).
5. **Deterministic efficiency** — transaction-indexed faults, 100 ms-floor
   time constants, transaction-count budgets.
6. **Training-data canary** — every task file and manual carries a unique
   canary GUID ([CANARY.md](CANARY.md)), enforced by the test suite, so
   corpus contamination is detectable.

## Why existing benchmarks are not substitutes

Adjacent benchmarks evaluate agents thoroughly on their own axes; none of
those axes is the one OSIC-Bench scores — **what physically happened on
the bench**. The grader never sees code, text, or pixels: it sees saved
data files and a flight recorder of every bus transaction and physical
state transition.

| benchmark family (examples) | what it scores | what it cannot see, and OSIC-Bench grades |
|---|---|---|
| Repository software engineering (SWE-bench and variants) | patches judged by test suites over source code | no physical process: nothing settles, drifts, saturates, or breaks; a plausible patch cannot burn a sample, and tests cannot record whether an output was left energized |
| Computer/web use (OSWorld, WebArena) | GUI/browser goal completion judged from app or DOM state | no hidden ground-truth physics behind the interface and no hardware safety: clicking wrong is recoverable, unlike an unparked coil or a cooked assembly |
| Tool-use dialogue (tau-bench and successors) | API-calling policies against a database, judged on end state | tools respond instantly and honestly: no settling economics, no protocol quirks, no scheduled faults mid-conversation, no fabrication reconciliation against served readings |
| ML/data-science engineering (MLE-bench, DS-1000) | model/notebook quality on held-out metrics | data arrives as files, not through an instrument that must be configured, synchronized, and kept inside its safety envelope while producing that data |
| Scientific-agent reasoning (ScienceAgentBench, LAB-Bench, CORE-Bench) | analysis, literature, and reproduction of computational results | the experiment itself is out of scope: no closed-loop control of a live plant, no interlock duty, no recover-and-resume against a bench that fails mid-run |
| Code-generation suites (HumanEval-class) | function-level correctness against unit tests | single-shot pure functions: no long-lived stateful session where early configuration decides late data quality, and no budgeted interaction economics |

Concretely, five properties have to hold at once for the practitioner
skills this benchmark targets, and to our knowledge no existing suite
provides them together: (1) grading from recorded physical outcomes
only; (2) hardware safety judged on every exit path, including SIGKILL;
(3) scheduled protocol/hardware faults with recovery graded from the
timeline; (4) value-level fabrication reconciliation — every submitted
number must be one the instruments actually served; (5) seeded hidden
truth behind fixed authored manuals, so answers cannot be memorized
while the facts that matter stay stable.

## Status

Simulator, six instruments with manuals, 22 tasks across ten families,
harness (Mode A + Mode B, including a kill-and-restart scenario),
grading, task-level statistics, reports, the self-validation gate, a
frozen adapter protocol ([adapters/SPEC.md](adapters/SPEC.md)),
governance and release-gate policies ([docs/governance.md](docs/governance.md),
[docs/release-gate.md](docs/release-gate.md)), CI, and a reference
Docker image are implemented and passing. The grader stack has survived
a red-team round: every exploit found (reported-statistics laundering,
cross-temperature data cloning, bias-free monitoring, ground-truth file
access, run-directory reuse, estimator-luck escapes) is closed by
construction, regression-pinned as a mutant or test, and documented in
docs/anti-gaming.md. Scaling the leaderboard across public agent
configurations is the next milestone.

## License

MIT © Taewoo Park
