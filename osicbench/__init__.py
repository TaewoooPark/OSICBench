"""osicbench - runner, sandbox, grading, statistics, and reports.

The agent-facing contract is deliberately tiny:

- The task hands the agent ``brief.md`` plus instrument manuals.
- At execution time the environment provides:
      OSIC_ENDPOINTS    path to endpoints.json ({device: {host, port, ...}})
      OSIC_RESULTS_DIR  directory where deliverables must be written
- The submission is plain Python (any style, stdlib or not); it talks raw
  TCP to the simulated instruments and writes its deliverables.

Grading never reads the submission's source code: only the flight recorder
and the files in OSIC_RESULTS_DIR.
"""

__version__ = "0.1.0.dev0"
