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

Requirements: Python 3.10+, PyYAML. No VISA stack, no GUI — instruments
are TCP endpoints (`TCPIP0::127.0.0.1::<port>::SOCKET`).

## The tasks (v0.1: 12)

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

Every task ships **reference solutions in two different idioms** (all must
pass) and **4+ mutants** embodying classic field mistakes (all must fail).
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
  returned (a block transfer counts as its N readings) and against outage
  windows — excess or gap-filling rows are **fabrication** and zero the
  task.
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
   validation gate, not a promise.
5. **Deterministic efficiency** — transaction-indexed faults, 100 ms-floor
   time constants, transaction-count budgets.

## Status

v0.1: simulator, five instruments with manuals, 12 tasks, harness
(Mode A + Mode B), grading, statistics, reports, and the self-validation
gate are implemented and passing. Baseline numbers across public agent
configurations are the next milestone.

## License

MIT © Taewoo Park
