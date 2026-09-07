# Host-suspension accounting amendment

This amendment defines a one-time, fail-closed accounting transition for an
authoring attempt whose host clock was discontinuous. It does not change the
benchmark, prompt, task inputs, model configuration, authoring budget, native
client command, containment policy, or grading implementation.

## Observed condition

The native process consumed the complete 40-minute active authoring budget but
the epoch clock advanced materially farther. The runtime recorded
`clock_discontinuity: true` and invalidated the provider audit solely with
`host_suspend_or_clock_discontinuity`. The attempt produced no `main.py`, no
frozen submission, and no grade. Its subscription guard was allowed, paid
overage was disabled, and the trace did not contain a provider refusal.

The original sealed author record remains `blocked` and is copied byte for
byte. This preserves the forensic distinction between what the runner observed
and the later accounting decision.

## Accounting rule

The migration creates one authoring-stage failure receipt for every planned
evaluation seed, using the reason
`host_suspend_or_clock_discontinuity`. These receipts are operational
nonpasses so the fixed plan denominator remains complete. They are
infrastructure evidence, not model-quality observations. DFS, HSS, RS, and
transaction counts remain unmeasured rather than being assigned zero.

The affected prompt is never submitted again and the target is never graded.
Only schedule rows with no prior authoring attempt may run after migration.
The public exporter retains the neutral failure reason and boolean terminal
evidence while excluding commands, paths, process identifiers, raw traces, and
other private runtime data.

## Evidence-preserving transition

Migration requires an exclusive lock on a quiescent source. It verifies the
exact plan, source commit, environment, target identity, record hashes, process
and audit receipts, timeout, independently recomputed clock discontinuity,
absence of a submission, native-call coverage, cached grades and failures, and
the full source inventory. Any conflicting evidence rejects the transition.

The destination is constructed under a temporary sibling and published only
after validation. Existing records, artifacts, logs, grades, failures, and
grading logs are copied byte for byte. The source manifest, orchestration state,
prior migration receipt, and prior amendment tree are archived privately. The
credential-bearing runtime tree is not copied. The destination starts with all
providers paused and requires explicit resume through the normal runner.

```bash
python -m experiments.model_matrix.migrate \
  --amendment host-suspend \
  --source /path/to/stopped-root \
  --out /path/to/new-accounted-root
```

The migration performs zero provider, subscription, and grading calls. This
rule is not a general retry policy and cannot be used for ordinary timeouts,
provider errors, invalid model identity, cleanup failures, produced artifacts,
or ambiguous audit evidence.
