# Model and native-harness evaluation protocol

This directory defines a **protocol, not a results announcement**. The requested
configurations live in [`cases.json`](cases.json). A configuration must pass its
runtime preflight before scored authoring begins. No final scores, successful
model resolutions, or completed release gate are implied by this document.

The experiment evaluates the public OSICBench development tasks in Mode A:
an agent writes a program from the brief and manuals before any instrument is
reachable. The frozen program is then executed against the simulated bench.
It is not a private held-out leaderboard evaluation.

## Configurations and matrix size

| Native CLI | Requested model | Requested reasoning settings |
|---|---|---|
| Codex | `gpt-5.6-sol` | `low`, `ultra` |
| Codex | `gpt-5.6-terra` | `low`, `ultra` |
| Claude Code | `claude-opus-5` | `low`, `max` |
| Claude Code | `claude-sonnet-5` | `low`, `max` |

The two settings for each model are separate conditions, giving eight
conditions in total. These are requested identifiers, not permission to use
aliases, silent fallbacks, or unsupported settings. Availability and resolved
identity must be checked using the installed CLI and the authenticated account.
An unsupported case is corrected before the plan is frozen, never silently
replaced during evaluation.

The complete design is:

- 8 conditions × 22 tasks × 3 independent authoring samples = **528 artifacts**;
- each artifact evaluated on the same 5 declared seeds = **2,640 graded runs**;
- per condition: 66 artifacts and 330 graded runs.

The seed list, schedule seed, conditions, and eight planned contrasts are in
[`cases.json`](cases.json). Evaluation seeds must be disjoint from published
example seeds. Seed variation tests a frozen program; it does not create more
independent authoring samples.

The first scheduling wave completes sample 1 across all conditions before
samples 2 and 3. That wave contains 176 artifacts and 880 graded runs. If an
intermediate snapshot is published, it must be labeled **single-sample and
preliminary**. It cannot establish authoring variability or satisfy the
three-sample reporting requirement. Models, tasks, contrasts, or later samples
must not be selected using intermediate scores.

## What the comparisons mean

Cross-provider comparisons concern **model-plus-native-harness configurations**.
Native system prompts, tool implementations, execution policies, and internal
auxiliary model use can differ. This experiment does not identify a pure model
effect with those factors removed.

Reasoning settings are native endpoint settings, not a common unit of compute.
`ultra` and `max` are not asserted to use equal tokens, latency, or inference
resources. Effort comparisons keep the requested model and native harness
fixed. Comparisons between models at their higher settings are descriptive
configuration comparisons, not equal-compute trials.

Record the CLI version, requested and resolved model, requested effort,
observed effort evidence, and any fallback or auxiliary-model observations.
When effective server effort is not exposed, label the evidence as launch
configuration only; do not turn that absence into a claim of verification.

## Subscription access and clean contexts

Use existing native subscription authentication only. API-key billing routes
and paid overage are outside this protocol. Account limits are not bypassed or
converted into a different payment route.

Every authoring call receives a fresh task workspace and clean isolated CLI
context, with no external custom instructions, memory, hooks, plugins, or tool
servers. Only the task brief, named manuals, and task-supplied rig files are
provided. The authored submission follows the standard-library-only contract
and the identical frozen prompt in [`adapters/SPEC.md`](../../adapters/SPEC.md).

Provider adapters alone are not a containment boundary. The evaluated runtime
requires outer OS filesystem and network controls that prevent access to other
tasks, benchmark internals, and known local customization stores. This runtime
does not claim to hide every unrelated file on the host. Provider-authentication
and inference traffic is permitted through a restricted transport; arbitrary
web access and live instrument access during authoring are not.

Preflight checks cover local file creation and execution, protected-parent
reads, blocked direct network connections, authentication, tool configuration,
model identity, and effort evidence. A failed check blocks that configuration.
These checks are empirical checks of the declared boundary, not proof of a
general-purpose adversarial security sandbox. Preflight traces are not scored
benchmark attempts.

## Planning, schedule, and budgets

Before scored calls, freeze the source revision and fingerprint, CLI/runtime
policy, case definitions, full expected-run manifest, contrasts, task inputs,
evaluation seeds, sample schedule, and authoring budget. Use fresh neutral
evaluation directories outside the benchmark repository. A source or policy
change after planning requires a new experiment identity, not a modified plan
with earlier scores carried forward.

A narrowly scoped audit-only amendment may preserve already collected,
ungraded first attempts in a new plan when a migration receipt proves that
prompts, task inputs, models, native launch configuration, budgets, schedule,
and grading code are unchanged. The original plan, records, and artifacts
remain immutable. Reclassification is reported explicitly; it must not cause
new generations or select a better artifact. See the
[native-refusal amendment](amendments/2026-09-04-native-refusal.md). A sealed
host-suspension attempt may instead be accounted without retry under the
[host-suspension amendment](amendments/2026-09-07-host-suspension.md); it
remains infrastructure evidence and never receives instrument-quality scores.

The schedule is balanced across conditions within task/sample blocks, using
the predeclared randomization seed. Begin with at most one active authoring
call per provider. Keep grading concurrency fixed and conservative because
some simulated tasks depend on wall-clock behavior. No agent sees graded
results while authoring a scored submission.

The authoring cap is **40 minutes per task**, identical across conditions.
Task execution limits and transaction budgets come from the task definitions.
Preserve the first artifact and its fingerprint, all attempt statuses, and
their timing. There are no content retries or best-of-N artifact selection.
Any permitted infrastructure retry follows the frozen
[`adapter specification`](../../adapters/SPEC.md) and retains its original
attempt record.

Quota exhaustion pauses scheduling. It does not justify a model downgrade,
effort change, alternate billing route, or erasing an unsuccessful attempt.
Interrupted attempts remain explicit until adjudicated under the declared
retry policy. Resume unattempted work only after the account permits it.
Record queue pauses separately from active authoring time.

Keep the host awake and its lid open. On macOS, launch long phases through
`caffeinate -i`; this prevents idle sleep, not lid-closed sleep. Authoring
records compare epoch and monotonic elapsed time and reject discontinuities
greater than two seconds. A suspension can also corrupt simulated timing:
invalidate every affected validation run, including passing runs, retain its
original evidence, and repeat the complete affected task block while awake.

## Scoring and statistical comparisons

For each condition and authoring sample, a task passes only if **every planned
seed** passes. The primary sample score is the number of passing tasks divided
by 22. Across the three samples, report the mean, sample standard deviation,
and range. Pooled task-sample totals and seed-level totals are descriptive;
they are not independent binomial trials.

The eight contrasts are predeclared in `cases.json`: four within-model effort
contrasts, two within-provider higher-setting contrasts, and two cross-provider
configuration contrasts. For each matched sample separately:

1. Pair conditions by task, after requiring all seeds for that task to pass.
2. Report discordant task counts and the task-pass difference, B minus A.
3. Compute the exact task-level McNemar p-value.
4. Apply Holm correction across the eight declared contrasts in that sample.

Report raw and adjusted p-values and the correction family size. There is no
cross-sample pooled significance test. Sample-level confidence intervals and
tests do not remove public-task exposure, task dependence, or native-harness
confounding. Effect sizes and failure mechanisms accompany any statistical
claims.

DFS, HSS, RS, transaction counts, and authoring cost evidence accompany the
primary metric. HSS-applicable summaries are derived from nonempty `hss` rules
in each task's `task.yaml`—currently 12 of the 22 tasks. An empty safety rule
set is not evidence of safe behavior on an untested hazard. Publish required
rule violations separately from the aggregate HSS score. RS is reported only
where the oracle supplies it, with its observed coverage; missing RS is not
assigned zero.

## Missing results, failures, and timeouts

The expected-run manifest fixes the denominator before execution. Missing
artifacts, missing seeds, absent grades, and recorded infrastructure failures
remain operational nonpasses. Distinguish them from observed graded failures.
DFS, HSS, RS, and transaction means use observed grades only, with their
coverage shown; ungraded rows do not acquire invented zero measurements.

Authoring timeout and CLI exit status are separate from artifact quality. An
existing artifact is eligible for grading under the frozen deadline rule once
identity and containment requirements are satisfied. A good artifact cannot
override an invalid identity or containment audit. Preserve deadline evidence
and partial traces; do not replace the primary result using a later revision.

Infrastructure incompleteness is not evidence of poor instrument control.
Incomplete snapshots must display coverage prominently and must not be
presented as fully observed model-quality rankings. No failed or missing cell
may disappear from a published denominator.

A verified native provider policy refusal without a submission is a completed
provider outcome and an operational nonpass, not a successful answer or an
observed instrument-control failure. Preserve the first refusal and report its
count separately. Do not resubmit, rephrase, weaken safeguards, or change the
requested model in response to it. Other unattempted, predeclared cases may
continue. A synthetic error message is not a substitute model identity; a
refusal requires consistent native initialization and terminal evidence.

## Release gate and publication

Before scored execution, the exact task-set revision must pass the full
[`release gate`](../../docs/release-gate.md): unit/hardening tests, a multi-seed
sweep of every reference and mutant, and reference-stability checks. Archive
the gate record. A partial gate or a single-seed check is not a substitute.
Do not mix grades from different grader or simulator revisions.

The separate `author` phase may collect and freeze programs while validation
is pending, because no evaluated program can reach the bench. It does not
grade those programs or certify the task set. Keep the source fingerprint
unchanged, and wait for the gate before starting the `run` phase.

Keep raw provider logs, credential-bearing runtime state, account information,
private paths, and arbitrary diagnostic text outside public exports. Public
analysis uses allowlisted identities and numeric fields. Publish source and
artifact hashes, the sanitized plan, resolved-configuration evidence, attempt
and failure counts, grades, run metadata, and flight recorders needed for
regrading and replay. Review every exported artifact; the analysis exporter
does not sanitize an entire raw run directory or establish complete provenance
on its own. If an artifact requires redaction, preserve the private original
and record the redaction and hashes without publishing secrets.

Report observed wall time and provider-exposed token counters with field-level
coverage. Token counters may overlap and must not be added indiscriminately.
Provider list-price estimates are not subscription charges. Actual billing is
not inferred from tokens or elapsed time.

### Available analysis command

After a planned matrix has observations, create a fresh publication snapshot:

```bash
python -m experiments.model_matrix.analyze \
  --runs-dir /path/to/private-evaluation/runs \
  --tasks-dir tasks \
  --out-dir /path/to/new-publication-snapshot
```

The analyzer reads `evaluation_manifest.json`, including its predeclared
`contrasts`, and creates `analysis.json` and `analysis.md`. It refuses to
overwrite either existing output. It can describe an incomplete snapshot but
does not certify the runtime, finish missing calls, or convert the development
set into a held-out evaluation.

### Planning and execution

Run the eight nonbenchmark preflight checks before planning. Their private
root must be separate from the fresh scoring output:

```bash
python -m experiments.model_matrix.runner plan \
  --out /path/to/new-scoring-root \
  --preflight-root /path/to/private-preflight-root

# Optional: collect frozen programs without running them against the bench.
caffeinate -i python -m experiments.model_matrix.runner author --out /path/to/new-scoring-root

# After the release gate passes, grade existing programs and finish the matrix.
caffeinate -i python -m experiments.model_matrix.runner run --out /path/to/new-scoring-root
python -m experiments.model_matrix.runner status --out /path/to/new-scoring-root
```

The plan embeds the complete expected matrix and matching passed preflight
evidence. Source, CLI, or plan changes stop execution. A stopped provider is
not silently restarted: inspect its retained records and current subscription
state before using the explicit `--resume-provider` option. Interrupted
in-progress authoring records require manual integrity review.

To switch phases or pause scheduling without terminating an active call:

```bash
python -m experiments.model_matrix.runner request-stop --out /path/to/scoring-root
# Wait for active calls to finish and the evaluator to exit, then explicitly resume.
caffeinate -i python -m experiments.model_matrix.runner run \
  --out /path/to/scoring-root --resume-provider openai --resume-provider anthropic
```

Stop requests are data-only receipts bound to the frozen plan. They take effect
at call boundaries. Resuming does not replay completed attempts, and every new
call still requires the subscription guard to pass.
