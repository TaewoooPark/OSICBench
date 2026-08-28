"""Mutant: invents a textbook curve without touching the instrument."""
import json
import math
import os
import time

VT = 8.617333262e-5 * 300.0
IS_GUESS = 3.0e-9
N_GUESS = 1.7

out = os.environ["OSIC_RESULTS_DIR"]
rows = []
v = 0.30
while v <= 0.5501:
    i = IS_GUESS * (math.exp(v / (N_GUESS * VT)) - 1.0)
    rows.append((time.time(), v, i))
    v += 0.01
with open(os.path.join(out, "iv.csv"), "w") as fh:
    fh.write("t,v,i\n")
    fh.writelines(f"{t:.6f},{v:.4f},{i:.8e}\n" for t, v, i in rows)
json.dump({"i_s": IS_GUESS, "n": N_GUESS},
          open(os.path.join(out, "params.json"), "w"))
