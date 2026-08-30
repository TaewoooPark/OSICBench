# Reproducible evaluation environment for OSIC-Bench.
#
#   docker build -t osicbench .
#   docker run --rm osicbench python3 -m pytest
#   docker run --rm osicbench osicbench validate --tasks tasks --seeds 2 --jobs 4 --out /tmp/validate
#
# The farm binds loopback TCP inside the container; no ports need to be
# published for validation or grading. Mount a host directory at /work
# to keep run artifacts:
#
#   docker run --rm -v "$PWD/runs:/work" osicbench \
#       osicbench run --task tasks/t01_first_light \
#       --submission tasks/t01_first_light/reference/ref_procedural.py \
#       --seed 7 --out /work/demo --label demo

FROM python:3.12-slim

WORKDIR /opt/osicbench
COPY pyproject.toml README.md LICENSE ./
COPY osicsim ./osicsim
COPY osicbench ./osicbench
COPY tasks ./tasks
COPY manuals ./manuals
COPY adapters ./adapters
COPY docs ./docs
COPY tests ./tests

RUN pip install --no-cache-dir -e ".[dev]"

# POSIX process semantics (process groups, SIGKILL) are part of the
# harness contract; this image is the reference host.
CMD ["python3", "-m", "pytest"]
