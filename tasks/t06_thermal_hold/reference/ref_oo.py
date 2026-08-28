"""T06 reference (OO): controller object, explicit anti-windup, safe teardown."""
import json
import math
import os
import socket
import time


class Client:
    def __init__(self, name):
        ep = json.load(open(os.environ["OSIC_ENDPOINTS"]))[name]
        self.s = socket.create_connection((ep["host"], ep["port"]), timeout=10)
        self.s.settimeout(10)

    def write(self, cmd):
        self.s.sendall(cmd.encode() + b"\n")

    def query(self, cmd):
        self.write(cmd)
        b = b""
        while not b.endswith(b"\n"):
            c = self.s.recv(4096)
            if not c:
                raise ConnectionError("closed")
            b += c
        return b[:-1].decode()

    def close(self):
        self.s.close()


class HeaterLoop:
    R = 25.0
    P_MAX = 6.0
    GUARD_K = 333.0

    def __init__(self, psu, tmon, target):
        self.psu, self.tmon, self.target = psu, tmon, target
        self.integral = 0.0

    def bring_up(self):
        self.psu.write("INST:SEL OUT1")
        self.psu.write("SOUR:VOLT:PROT 20")
        self.psu.write("SOUR:CURR 1.0")
        self.psu.write("SOUR:VOLT 0")
        self.psu.write("OUTP ON")
        assert self.psu.query("*OPC?") == "1"

    P_FF = 3.7

    def step(self, dt):
        temp = float(self.tmon.query("KRDG?"))
        err = self.target - temp
        if abs(err) < 3.0:              # integrate only near the setpoint
            self.integral = min(1.5, max(-1.5, self.integral + 0.05 * err * dt))
        power = self.P_FF + 0.5 * err + self.integral
        if err > 5.0:
            power = min(power, self.P_MAX)
        elif err > 1.5:
            power = min(power, self.P_FF + 0.6)
        else:
            power = min(power, self.P_FF + 1.6)
        power = max(0.0, power)
        if temp >= self.GUARD_K:
            power = 0.0
        self.psu.write(f"SOUR:VOLT {math.sqrt(power * self.R):.3f}")
        return err

    def shutdown(self):
        self.psu.write("SOUR:VOLT 0")
        self.psu.write("OUTP OFF")
        self.psu.query("*OPC?")


def main():
    psu, tmon = Client("psu1"), Client("tmon1")
    loop = HeaterLoop(psu, tmon, 330.0)
    try:
        loop.bring_up()
        t0 = time.monotonic()
        settled_since = None
        while time.monotonic() - t0 < 135.0:
            err = loop.step(0.5)
            now = time.monotonic()
            if abs(err) <= 0.20:
                settled_since = settled_since or now
                if now - settled_since >= 72.0:
                    break
            else:
                settled_since = None     # left the band: the clock restarts
            time.sleep(0.5)
    finally:
        loop.shutdown()
        psu.close()
        tmon.close()


if __name__ == "__main__":
    main()
