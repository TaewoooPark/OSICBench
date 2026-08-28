"""T07 reference (OO): Tracker object with explicit reacquire logic."""
import json
import os
import socket
import time


class LockIn:
    def __init__(self):
        ep = json.load(open(os.environ["OSIC_ENDPOINTS"]))["l820"]
        self.s = socket.create_connection((ep["host"], ep["port"]), timeout=15)
        self.s.settimeout(15)

    def _q(self, cmd):
        self.s.sendall(cmd.encode() + b"\n")
        b = b""
        while not b.endswith(b"\n"):
            c = self.s.recv(4096)
            if not c:
                raise ConnectionError("closed")
            b += c
        return b[:-1].decode()

    def _w(self, cmd):
        self.s.sendall(cmd.encode() + b"\n")

    def configure(self):
        self._w("SENS 6")
        self._w("OFLT 1")
        err = self._q("SYST:ERR?")
        assert err.startswith("0,"), err

    def response_at(self, f, settle=0.16):
        self._w(f"SOUR:FREQ {f:.1f}")
        time.sleep(settle)
        return float(self._q("OUTP? 3"))

    def close(self):
        self.s.close()


class Tracker:
    MAX_STEP = 300.0

    def __init__(self, li):
        self.li = li
        self.f = None
        self.r_peak = 0.0
        self.gamma_est = 600.0
        self.dither = 100.0

    def acquire(self, lo, hi, step):
        points = []
        f = lo
        while f <= hi:
            points.append((f, self.li.response_at(f)))
            f += step
        self.f, best_r = max(points, key=lambda p: p[1])
        self.r_peak = max(self.r_peak, best_r)
        above = [f for f, r in points if r >= 0.5 * best_r]
        if len(above) >= 2:
            self.gamma_est = max(150.0, min(900.0, (max(above) - min(above)) / 2.0))
        # keep the dither INSIDE the gamma/3 tolerance: use gamma/4
        self.dither = max(50.0, self.gamma_est / 4.0)
        return self.f

    def update(self):
        lo = self.li.response_at(self.f - self.dither)
        mid = self.li.response_at(self.f)
        hi = self.li.response_at(self.f + self.dither)
        self.r_peak = max(self.r_peak, mid)
        if max(lo, mid, hi) < 0.4 * self.r_peak:
            self.acquire(self.f - 2500.0, self.f + 2500.0, self.gamma_est / 2.0)
            return
        denom = lo - 2.0 * mid + hi
        step = 0.5 * self.dither * (lo - hi) / denom if abs(denom) > 1e-18 else 0.0
        self.f += max(-self.MAX_STEP, min(self.MAX_STEP, step))


def main():
    li = LockIn()
    t0 = time.monotonic()
    try:
        li.configure()
        tracker = Tracker(li)
        tracker.acquire(40000.0, 60000.0, 250.0)
        tracker.acquire(tracker.f - 900.0, tracker.f + 900.0, 60.0)
        while time.monotonic() - t0 < 100.0:
            tracker.update()
    finally:
        li.close()


if __name__ == "__main__":
    main()
