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

## Mode B (live sessions)

`osicbench live --task ... --seed ... --out ...` starts a farm the agent
may probe, with a hard reset budget (`reset` command, default 3 resets).
The final attempt is graded. The gap between Mode A and Mode B pass rates
measures how much an agent depends on trial-and-error against hardware.
