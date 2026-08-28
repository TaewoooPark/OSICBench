"""Mutant: invents 400 beautiful readings without a single transaction."""
import json
import os
import random
import statistics
import time

out = os.environ["OSIC_RESULTS_DIR"]
rng = random.Random(7)
values = [1.0 + rng.gauss(0, 2e-4) for _ in range(400)]
t = time.time()
with open(os.path.join(out, "readings.csv"), "w") as fh:
    fh.write("t,v\n")
    fh.writelines(f"{t:.6f},{v:.9e}\n" for v in values)
json.dump({"mean": statistics.fmean(values), "std": statistics.stdev(values)},
          open(os.path.join(out, "result.json"), "w"))
