# Native-refusal classification amendment

This amendment changes audit classification and scheduler control only. It is
not a benchmark result, a prompt revision, or a permission to bypass a provider
restriction. It was prepared before any model-generated program was graded.

## Observed defect

Claude Code returned an ordered native refusal envelope: initialization for the
requested model, a `model_refusal_no_fallback` event, a synthetic refusal error,
and a terminal result with `is_error: true`, `stop_reason: refusal`, and
`terminal_reason: api_error`. Its process exited with code 1 and produced no
submission. The model's refusal is an observed outcome, not a network failure.

The original audit mistakenly treated the substring `fallback` in
`model_refusal_no_fallback` as evidence of a replacement model. It also treated
the `<synthetic>` error marker as a primary model identity. Consequently, the
scheduler stopped all remaining work for that provider.

## Correction and limits

The corrected audit recognizes only a consistent native envelope with matching
requested initialization, original-model metadata, model-usage identity,
refusal category, synthetic API-error fields, and terminal refusal fields.
Authentication, tool isolation, effort evidence, and genuine model-fallback
checks remain in force. Raw refusal explanations and request identifiers are
private and are not exported.

The scheduler records a verified refusal with no artifact as
`outcome: provider_refusal`, retains `completion_successful: false`, and counts
every associated seed as an operational nonpass. Instrument metrics are not
invented for an unexecuted program. Inconsistent evidence, a conflicting
artifact, a timeout, or a cleanup, containment, or subscription error still
blocks the provider for review.

The refused call is never repeated. Its prompt, model, effort, safeguards,
authentication route, and tool configuration are not changed. Only other
unattempted cases in the original schedule can proceed.

The same amendment adds plan-bound, data-only pause requests. They pause new
calls after an active call has completed; they do not terminate generation or
select artifacts by quality. Explicit resume preserves pause history and
remains subject to the per-call subscription guard.

## Evidence-preserving transition

The original run uses source commit
`bc4feac317ed009672ae1a20793e0cbb74ff5c4f`. A new run identity is required for
the corrected harness. The migration must reject active or unresolved attempts,
changed record seals or artifact hashes, preexisting grades, and changes to
models, effort, prompts, task inputs, schedule, seeds, budgets, native launch
configuration, or benchmark grading code.

Migration retains every existing first call, including the refusal, without
calling either provider. Private evidence includes the old manifest and
original record bytes and hashes. Completed submissions retain their original
content hashes and timing. The refusal is re-audited against its original raw
trace and linked to its original blocked record. The amendment receipt records
both source identities and the inherited call identities. New generation may
begin only after the amended plan has been sealed.

After the original evaluator exits and the reviewed amendment is committed:

```bash
python -m experiments.model_matrix.migrate \
  --source /path/to/original-author-only-root \
  --out /path/to/new-amended-root
```

The destination must not exist. Migration performs no provider, subscription,
or grading calls, and initializes both providers as paused. Inspect its receipt
and status before explicitly resuming through the normal runner. Use `author`
while the release gate is pending and `run` only after it passes.

This exception cannot be generalized to prompt improvements, task changes,
model substitutions, or post-score selection. Such changes require a separate
comparison design, not reuse under this audit-only amendment.
