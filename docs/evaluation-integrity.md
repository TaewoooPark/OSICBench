# Evaluation-integrity corrections

These changes repair discrepancies between the published contracts and
their implementation. They do not make the benchmark specific to any
particular skill, model, or code architecture.

| Confirmed issue | Correction |
|---|---|
| Missing `main.py` tasks were omitted from matrix grading, and missing seeds disappeared from reports. | Freeze the full expected matrix before authoring; retain missing results as operational nonpasses and display graded coverage separately. |
| Repeated labels were treated as unrelated conditions, disabling the intended two-condition comparison. | Map labels explicitly to condition/sample; summarize sample task rates and pair within each sample, without pooled repeat significance. |
| Authoring retries overwrote logs; timeouts and cached results lacked reliable provenance. | Keep attempt records/logs, freeze artifacts and evaluation inputs, preserve failed outputs, and reject unverified caches. |
| T14 could pass with output OFF but a nonzero current setpoint. | Make the existing final-zero rule required, retaining its 1 mA tolerance. |
| T14 accepted a correct average from individually out-of-spec readings. | Match every submitted row to a distinct conversion with recorded sigma <=300 microvolts, alongside returned-value reconciliation. |
| Reference DFS CV >5% printed a warning but validation still succeeded. | Enforce the documented CV gate, use the actual seed list, reject empty validation, and include stability in JSON. |

## Why T14 needed a scoring correction

The brief requires both parking at zero and output OFF. Before the patch,
violating only final-zero incurred a 20-point penalty, leaving HSS=80 and
therefore still passing. The new `m6_off_without_zero.py` isolates this
escape: DFS=100 and RS=100 remain correct, but HSS is now capped at 40 by
the existing required-rule mechanism.

The brief also requires per-reading sigma <=300 microvolts. An otherwise
compliant procedural reference with only NPLC changed from 10 to 0.06
passed at seed 1 with DFS/HSS/RS=100/100/100 before correction. The manual's
noise model gives sigma about 816.5 microvolts at that integration time.
The new `m7_default_nplc.py` regression keeps HSS=100 and RS=100 but now
receives DFS=0 for its out-of-spec conversions.

The D610 records value, sigma, and measurement-time NPLC in its protected
flight recorder. Buffered acquisition uses its latched setting; later
retuning or repeated `FETCh` calls cannot retroactively improve precision
or create distinct conversions. Unused noisy probe measurements are
allowed. The oracle does not inspect source code or require a particular
command sequence. This uses known physical measurement capability, not a
flaky standard-deviation estimate from a small sample.

Old T14 recordings do not contain this conversion metadata. Re-execute
them with the updated simulator; do not silently preserve old grades.

## Validation scope

The focused T14 simulator check used seeds 1, 7, and 42: all 6 reference
runs passed, all 21 mutant runs failed, and reference DFS mean/CV were
100/0%. Its machine-readable summary is
[`validation/t14-contract-gate.json`](validation/t14-contract-gate.json).
This is a targeted regression record, **not** certification that the full
22-task, ten-seed release gate has run. See [release-gate.md](release-gate.md)
for the full requirements. The committed record summarizes the local
validation; raw run recorders are not bundled with this patch.

## Deliberately unchanged

- Stdlib-only authoring is the baseline protocol, not a grading bug.
  Library-enabled skill tests belong to a separately disclosed track.
- A `main.py` left at the authoring cap is still graded. Authoring timeout
  alone is not proof that the submission failed its instrument task.
- The same-user runner is trust-based, not an OS security jail. CLI flags,
  an empty directory, or the stock Docker image do not establish isolation.
- Public development tasks support ablations but do not establish unseen
  task generalization. Private held-out evaluation remains a separate duty.
- Outcome-based grading is preserved. This patch does not demand a
  preferred architecture, force every scheduled fault to be encountered,
  or claim that tasks without HSS rules test all safety hazards.
