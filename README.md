# OSIC-Bench

**Operating Scientific Instruments via Code — a benchmark for AI agents.**

Can an AI agent write the code that runs a real experiment? Not "does the
code look right" — did the instrument produce **correct data**, did the
hardware end in a **safe state**, did the run **survive the faults** that real
benches throw at you?

OSIC-Bench measures exactly that. Agents are given a physics goal and an
instrument manual; they write control code in any style they like; the code
runs against a simulated instrument farm with hidden ground-truth physics,
realistic protocol quirks, and scheduled fault injection. Scoring never looks
at the code — only at what physically happened.

> **Status: design & construction phase.** The specification below is the
> committed design; the simulator, tasks, and harness are being built in the
> open. Watch/star for the v0.1 release.

## Why this benchmark exists

Setting up and programming lab instruments is a notorious bottleneck of
experimental research: weeks lost to bring-up quirks, buried manual
constraints, silently-wrong data (stale buffers, unsettled readings,
setpoints stored as measurements), hardware-damage risk, and long runs that
die at hour nine taking their data with them. AI agents now write this code —
and increasingly operate physical devices directly — but no public benchmark
measures whether agent-written instrument code actually works where it
counts: in the data and on the hardware.

Existing evaluations measure adjacent things — scientific computation
(SciCode), research Q&A (LAB-Bench), repository bug-fixing (SWE-bench),
risky-code refusal (RedCode), embodied-command refusal (ASIMOV). The layer
"an agent wrote device-control code; was the experiment correct and safe?"
is unmeasured. OSIC-Bench fills that layer.

## What it measures

Seven task families, each grounded in a documented practitioner bottleneck:

| Family | Real-world bottleneck |
|---|---|
| **F1 Bring-up & First Light** | terminations, banners, timeouts — "the instrument won't talk" |
| **F2 Characterization** | open-loop measurements: sweeps, spectra, parameter extraction |
| **F3 Closed-loop Control** | hold a temperature, keep a lock, recover from drift |
| **F4 Forensics & Repair** | inherit a buggy rig script and a weird dataset; find and fix the cause |
| **F5 Endurance & Recovery** | multi-hour runs under link drops, power glitches, error floods |
| **F6 Multi-instrument Orchestration** | sequencing across devices without collisions |
| **F7 Throughput Engineering** | same data quality within a hard time/transaction budget |

## How scoring works

Every task runs against **osicsim**, a deterministic simulated instrument
farm: SCPI-1999 / IEEE-488.2 protocol engine, per-device physics models with
known ground truth (noise, settling dynamics, compliance clamping,
hysteresis, thermal plants), realistic interface quirks, a scheduled fault
injector, and a **flight recorder** that logs every transaction and every
physical state transition.

Four outcome metrics — computed **only** from the saved data files and the
flight recorder, never from the source code:

- **DFS — Data Fidelity Score.** Do the submitted results recover the
  simulator's ground-truth physics within tolerance? Unsettled readings,
  stale buffers, wrong ranges, and missing averaging show up here on their
  own, because the physics makes them wrong.
- **HSS — Hardware Safety Score.** Did every exit path (success, crash,
  timeout, even SIGKILL) leave outputs safe? Were slew limits, interlocks,
  and forbidden states respected during the run? Judged from recorded
  physical events.
- **RS — Robustness Score.** Under the fault schedule: how much valid data
  survived, and did the run recover? Submitted raw data is reconciled against
  the recorder's per-response reading counts (a buffered block transfer
  counts as its N readings) — data points that exceed what the instrument
  actually returned, or that fall inside a window when the instrument was
  provably dead, are flagged as **fabrication** and zero the task.
- **IE — Interaction Efficiency.** Bus transactions against the task budget
  (fully deterministic), with wall time reported alongside.

Two execution modes: **Mode A (one-shot)** — code is written from the brief
and manual alone, then executed once; **Mode B (live, k resets)** — the agent
may probe the farm but gets a hard budget of farm resets. The gap between
them quantifies how much an agent depends on trial-and-error against
hardware — the scarcest resource in a lab.

## Anti-gaming principles

1. **Outcome-only grading.** No AST checks, no style points, no pattern
   matching. Any idiom that produces correct data and safe hardware scores
   full marks — reference solutions are maintained in multiple distinct
   idioms to enforce this.
2. **Fictional instruments, fresh manuals.** Devices are original designs on
   top of public standards (SCPI-1999, IEEE 488.2), each with its own
   authored manual. Facts critical to success exist only in the manual.
3. **Seeded randomization of the answers.** Hidden physics parameters — the
   quantities a task asks you to recover — plus noise streams and fault
   timings vary per seed, while every fact stated in a device manual stays
   fixed (the manual never lies). Memorizing published answers does not pay.
4. **Self-proving graders.** Every task ships mutant (deliberately wrong)
   solutions; the grader's detection rate is published with every report.
5. **Calibrated difficulty.** Task traps are tuned so frontier baselines
   land mid-band, with the calibration history public.

## Repository layout (target)

```
osicsim/       simulator: transport, SCPI engine, physics, faults, recorder
osicbench/     runner, sandbox, grading, statistics, reports
instruments/   fictional device specs + physics bindings + manuals
tasks/         T01..T12 (v0.1): brief, farm config, faults, oracle, references
adapters/      agent-under-test adapters (any CLI harness can plug in)
docs/          grading math, anti-gaming design, task authoring guide
tests/         simulator units, idiom-invariance CI, mutant-detection CI
```

## Roadmap

- **v0.1** — simulator core, 5 instruments, 12 tasks, harness, grading,
  validation suite.
- **v0.2** — baseline numbers across public agent configurations.
- **v1.0** — 30+ tasks, difficulty tiers, community task submissions.

## License

MIT © Taewoo Park
