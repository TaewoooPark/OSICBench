"""T04 reference (procedural): ramped, measured-axis, two-branch loop."""
import json
import os
import socket
import time

LF = b"\n"
CRLF = b"\r\n"


def connect(name, term, banner=False):
    ep = json.load(open(os.environ["OSIC_ENDPOINTS"]))[name]
    s = socket.create_connection((ep["host"], ep["port"]), timeout=15)
    s.settimeout(15)
    if banner:
        _readline(s, term)
    return s


def _readline(s, term):
    b = b""
    while not b.endswith(term):
        c = s.recv(4096)
        if not c:
            raise ConnectionError("closed")
        b += c
    return b[:-len(term)].decode()


def w(s, cmd, term=LF):
    s.sendall(cmd.encode() + term)


def q(s, cmd, term=LF):
    w(s, cmd, term)
    return _readline(s, term)


def ramp(smu, target, current, step=0.1):
    """Move the coil current in manual-compliant steps.

    The sweep pairs sensor readings with the MEASURED coil current, so a
    fixed dwell is sufficient here; *OPC?-level settling is not required
    for a measured-axis acquisition.
    """
    while abs(target - current) > 1e-9:
        current += max(-step, min(step, target - current))
        w(smu, f"SOUR:CURR {current:.4f}")
        time.sleep(0.12)
    return current


def main():
    out = os.environ["OSIC_RESULTS_DIR"]
    smu = connect("smu1", LF)
    dmm = connect("dmm1", CRLF, banner=True)
    rows = []
    level = 0.0
    try:
        w(smu, "SOUR:FUNC CURR")
        w(smu, "SENS:VOLT:PROT 21")
        w(smu, "SOUR:CURR 0")
        w(smu, "OUTP ON")
        q(smu, "*OPC?")
        w(dmm, "SENS:VOLT:DC:NPLC 1", CRLF)
        q(dmm, "*OPC?", CRLF)

        level = ramp(smu, 1.0, level)          # saturate positive first
        time.sleep(0.5)

        def sweep(points):
            nonlocal level
            for target in points:
                level = ramp(smu, target, level, step=0.05)
                time.sleep(0.30)               # dwell; the axis is MEASURED
                i_meas = float(q(smu, "MEAS:CURR?"))
                v = float(q(dmm, "READ?", CRLF))
                rows.append((time.time(), i_meas, v))

        down = [1.0 - 0.05 * k for k in range(0, 41)]      # +1 ... -1
        up = [-1.0 + 0.05 * k for k in range(1, 41)]       # ... back to +1
        sweep(down)
        sweep(up)

        # crossings of v against zero on each branch, measured axis
        def crossing(seq):
            for (i0, v0), (i1, v1) in zip(seq, seq[1:]):
                if v0 == v1:
                    continue
                if v0 >= 0 > v1 or v0 <= 0 < v1:
                    frac = -v0 / (v1 - v0)
                    return i0 + frac * (i1 - i0)
            return None

        n_down = len(down)
        branch_down = [(i, v) for _, i, v in rows[:n_down]]
        branch_up = [(i, v) for _, i, v in rows[n_down:]]
        i_dn = crossing(branch_down)
        i_up = crossing(branch_up)
        hc_mt = 50.0 * abs(i_up - i_dn) / 2.0

        v_sat = (abs(rows[0][2]) + abs(rows[n_down - 1][2])) / 2.0
        ms = v_sat / 0.2

        with open(os.path.join(out, "loop.csv"), "w") as fh:
            fh.write("t,i_a,v\n")
            fh.writelines(f"{t:.6f},{i:.6f},{v:.6e}\n" for t, i, v in rows)
        json.dump({"hc_mt": hc_mt, "ms": ms},
                  open(os.path.join(out, "params.json"), "w"))
    finally:
        try:
            ramp(smu, 0.0, level)
            w(smu, "OUTP OFF")
            q(smu, "*OPC?")
        finally:
            smu.close()
            dmm.close()


if __name__ == "__main__":
    main()
