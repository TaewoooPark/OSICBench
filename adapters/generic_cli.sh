#!/bin/sh
# Example adapter for a CLI coding agent (illustrative sketch).
#
# usage: generic_cli.sh <task_dir> <workdir> "<agent command>"
# The agent command receives the workspace as its working directory and
# must leave a main.py there. The workspace contains brief.md and manuals/.
set -eu
TASK_DIR=$1
WORKDIR=$2
AGENT_CMD=$3

mkdir -p "$WORKDIR/manuals"
cp "$TASK_DIR/brief.md" "$WORKDIR/"
REPO_ROOT=$(cd "$TASK_DIR/../.." && pwd)
for m in $(python3 -c "import yaml,sys; print(' '.join(yaml.safe_load(open('$TASK_DIR/task.yaml')).get('manuals', [])))"); do
    cp "$REPO_ROOT/manuals/$m" "$WORKDIR/manuals/"
done
if [ -d "$TASK_DIR/rig" ]; then
    cp -r "$TASK_DIR/rig" "$WORKDIR/rig"
fi

cd "$WORKDIR"
sh -c "$AGENT_CMD"    # must produce ./main.py

test -f main.py || { echo "adapter: agent produced no main.py" >&2; exit 1; }
echo "workspace ready: $WORKDIR (submit with: osicbench run --submission $WORKDIR ...)"
