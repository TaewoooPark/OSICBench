# Agent adapters

An adapter turns "an agent" into "a submission directory". The bench does
not care how the code was produced - by a person, a model, or a pipeline.

## Contract

Given a task directory, produce a Python submission (single file or a
directory with `main.py`) that will run with:

- `OSIC_ENDPOINTS` - path to endpoints.json (`{device: {host, port, ...}}`)
- `OSIC_RESULTS_DIR` - where deliverables must be written

The submission must be self-contained (stdlib or vendored code only) and
exit on its own. Then:

```
osicbench run --task tasks/<id> --submission <file-or-dir> --seed <n> \
    --out runs/<condition>/<id>_s<n> --label <condition>
```

Mode A discipline: the farm is NOT running while the agent writes code -
authoring happens from `brief.md` + `manuals/` alone. Local tooling and
self-written mocks are fine; the first contact with the farm is the graded
run.

## Example: a CLI coding agent

`generic_cli.sh` sketches the shape: copy the task's brief and manuals
into a fresh workspace, ask the agent CLI to produce `main.py`, then hand
the workspace to `osicbench run`. Label runs by condition
(`--label bare`, `--label with-context`, ...) and let
`osicbench report --runs runs/` aggregate per-condition pass rates and
paired statistics.

For a planned, repeated-sample comparison, use `matrix_runner.py`. Install
the repository (`pip install -e .`) first. The agents JSON maps condition
names to CLI commands and environment overrides; configure and verify
isolation separately as required by [SPEC.md](SPEC.md).

```bash
# Bash: use a new output directory and outside-repository work root.
matrix_args=(--agents agents.json
             --runs-dir "$PWD/runs/ablation-001"
             --workdir /tmp/osicbench-ablation-001
             --seeds 101,102,103,104,105)
python adapters/matrix_runner.py plan "${matrix_args[@]}" --samples 3
for condition in bare skilled; do
  for sample in 1 2 3; do
    python adapters/matrix_runner.py prep "${matrix_args[@]}" --agent "$condition" --sample "$sample"
    python adapters/matrix_runner.py author "${matrix_args[@]}" --agent "$condition" --sample "$sample"
    python adapters/matrix_runner.py grade "${matrix_args[@]}" --agent "$condition" --sample "$sample"
  done
done
python adapters/matrix_runner.py summary "${matrix_args[@]}"
```

The seeds above illustrate syntax only; choose unpublished seeds for an
actual evaluation. Planning includes every agent configured in the JSON,
so the condition loop must match that file. A missing `main.py` remains in
the report's denominator. Use `author --skip-done` only to resume tasks
not previously attempted; inspect unresolved attempts before proceeding.
Do not edit the manifest or reuse an old experiment directory to change
conditions, sources, artifacts, or budgets. Results without a manifest
remain supported but are marked coverage-unverified.

## Mode B (live sessions)

`osicbench live --task ... --seed ... --out ...` starts a farm the agent
may probe, with a hard reset budget (`reset` command, default 3 resets).
The final attempt is graded. The gap between Mode A and Mode B pass rates
measures how much an agent depends on trial-and-error against hardware.
