"""Mutant: renders a beautiful textbook loop without any instrument I/O."""
import json
import math
import os
import time

out = os.environ["OSIC_RESULTS_DIR"]
HC, MS, W = 25.0, 1.0, 4.0
rows = []
for k in range(41):
    i = 1.0 - 0.05 * k
    m = MS * math.tanh((50 * i + HC) / W)
    rows.append((time.time(), i, 0.2 * m))
for k in range(1, 41):
    i = -1.0 + 0.05 * k
    m = MS * math.tanh((50 * i - HC) / W)
    rows.append((time.time(), i, 0.2 * m))
with open(os.path.join(out, "loop.csv"), "w") as fh:
    fh.write("t,i_a,v\n")
    fh.writelines(f"{t:.6f},{i:.6f},{v:.6e}\n" for t, i, v in rows)
json.dump({"hc_mt": HC, "ms": MS}, open(os.path.join(out, "params.json"), "w"))
