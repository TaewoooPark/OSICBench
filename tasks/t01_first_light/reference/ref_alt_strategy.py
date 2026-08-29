"""T01 reference (alternate strategy): buffered burst instead of 10 READs.

NPLC 2 with a 10-sample buffered acquisition and a single block readout -
a different integration/transfer policy from the NPLC-10 single-reading
references. Exists to prove the grader scores outcomes, not one canonical
acquisition style.
"""
import json
import os
import socket
import statistics
import time

TERM = b"\r\n"


def rl(s):
    b = b""
    while not b.endswith(TERM):
        c = s.recv(65536)
        if not c:
            raise ConnectionError("closed")
        b += c
    return b[:-2]


def main():
    ep = json.load(open(os.environ["OSIC_ENDPOINTS"]))["dmm1"]
    out = os.environ["OSIC_RESULTS_DIR"]
    s = socket.create_connection((ep["host"], ep["port"]), timeout=15)
    s.settimeout(15)
    rl(s)  # greeting banner (documented interface quirk)

    s.sendall(b"SENS:VOLT:DC:NPLC 2" + TERM)   # sigma = 200uV/sqrt(2) < spec
    s.sendall(b"SYST:AZER ON" + TERM)
    s.sendall(b"SAMP:COUN 10" + TERM)
    s.sendall(b"INIT" + TERM)
    s.sendall(b"*OPC?" + TERM)
    assert rl(s) == b"1"
    s.sendall(b"TRAC:DATA?" + TERM)
    block = rl(s)
    assert block.startswith(b"#")
    ndig = int(block[1:2])
    values = [float(x) for x in block[2 + ndig:].decode().split(",")]
    assert len(values) == 10
    s.close()

    t = time.time()
    with open(os.path.join(out, "readings.csv"), "w") as fh:
        fh.write("t,v\n")
        fh.writelines(f"{t:.6f},{v:.9e}\n" for v in values)
    json.dump({"mean": statistics.fmean(values), "std": statistics.stdev(values)},
              open(os.path.join(out, "result.json"), "w"))


if __name__ == "__main__":
    main()
