# MER-S240 Source-Measure Unit — Programming Manual

Meridian Instruments MER-S240. Firmware 2.4. This manual is the complete
and authoritative description of the remote interface; behaviors described
here are exact.

## 1. Interface

- Transport: raw TCP socket (`TCPIP0::<host>::<port>::SOCKET`).
- Command termination: LF (`\n`). Responses are LF-terminated.
- One client connection at a time. A second connection attempt is refused
  while a session is open.
- Typical command turnaround is ~5 ms plus any conversion aperture.
- Malformed or rejected commands produce NO response and NO immediate
  error message: the error is placed on the error queue. Poll
  `SYSTem:ERRor?` to retrieve `code,"message"`; `0,"No error"` means empty.
  A query that fails will simply never answer — set client timeouts
  accordingly and drain the error queue when in doubt.

## 2. Source model

The S240 sources either voltage or current (`SOURce:FUNCtion VOLT|CURR`)
and measures the complementary quantity through the same terminals.

The analog output stage settles exponentially with a time constant of
**250 ms** after any level change. A conversion taken before the source
has settled faithfully reports the still-slewing output — the instrument
does not wait for you. `*OPC?` blocks until the source is settled to
0.1 % of the programmed level and is the recommended synchronization
point after every level change.

Changing `SOURce:FUNCtion` while the output is enabled is a settings
conflict: the command is ignored and error -221 is queued.

## 3. Measurement model — READ? vs MEASure:FRESh?

The S240 keeps the most recent completed conversion in a reading register.

- `READ?` returns the CONTENTS OF THE READING REGISTER. It never
  triggers a conversion. If no conversion has completed since power-on,
  it errors (-230) and does not answer. Two consecutive `READ?` queries
  return the same value regardless of what the source did in between.
- `INITiate` triggers one conversion of the complementary quantity into
  the reading register (no response).
- `MEASure:FRESh?` triggers a NEW conversion of the complementary
  quantity, waits the 20 ms aperture, and returns it. This is the correct
  query for stepped sweeps.
- `MEASure:CURRent?` / `MEASure:VOLTage?` trigger and return a fresh
  conversion of the named quantity.

## 4. Compliance

When sourcing voltage, the measured current is limited to
`SENSe:CURRent:PROTection` (default 105 uA); when sourcing current, the
developed voltage is limited to `SENSe:VOLTage:PROTection` (default 21 V).
While the limit is active the instrument sources a CLAMPED output and the
measured value equals the limit — readings taken in compliance are not
measurements of the device under test. Bit 3 (value 8) of
`STATus:QUEStionable:CONDition?` is set while in compliance; poll it when
operating anywhere near the limit.

## 5. Remote sensing (4-wire)

`SYSTem:RSENse ON` selects 4-wire measurement through the sense pair,
eliminating lead resistance from resistance measurements. With `OFF`
(the power-on default) the measurement path includes both force leads;
for low-resistance devices this contributes twice the lead resistance
directly to the result. The test-fixture leads on this bench are NOT
negligible for precision work; use remote sensing whenever the fixture
provides sense connections.

## 6. Output watchdog

`OUTPut:WDOG <seconds>` (0.5–60) arms a bus watchdog: if no message is
received for the programmed interval while the output is enabled, the
instrument disables its own output and queues error +603. `OUTPut:WDOG
OFF` disarms it. This is the recommended protection for unattended
operation — it acts even if the controlling process dies.

## 7. Command reference

| Command | Form | Description |
|---|---|---|
| `SOURce:FUNCtion VOLT\|CURR` | W/Q | select source function (output must be off) |
| `SOURce:VOLTage <v>` | W/Q | level, -20..+20 V |
| `SOURce:CURRent <a>` | W/Q | level, -1..+1 A |
| `SENSe:CURRent:PROTection <a>` | W/Q | compliance current, 1e-6..1.05 A |
| `SENSe:VOLTage:PROTection <v>` | W/Q | compliance voltage, 0.2..21 V |
| `SYSTem:RSENse ON\|OFF` | W/Q | 4-wire sensing |
| `OUTPut ON\|OFF` | W/Q | output relay |
| `OUTPut:WDOG <s>\|OFF` | W/Q | bus watchdog |
| `INITiate` | W | trigger one conversion into the register |
| `READ?` | Q | reading register (stale; never triggers) |
| `MEASure:FRESh?` | Q | fresh conversion, complementary quantity |
| `MEASure:CURRent?` / `MEASure:VOLTage?` | Q | fresh conversion, named quantity |
| `STATus:QUEStionable:CONDition?` | Q | bit 3 (value 8) = in compliance |
| `*IDN?` `*RST` `*CLS` `*OPC?` `*ESR?` `*STB?` | — | IEEE-488.2 common |

## 8. Specifications (excerpt)

| Item | Value |
|---|---|
| Source ranges | ±20 V / ±1 A |
| Source settling time constant | 250 ms (all ranges) |
| Conversion aperture | 20 ms |
| Programming resolution | 1 mV / 1 uA |
| Basic measure accuracy | 0.05 % of reading |
| Recommended max level step while output on | 0.1 A (current mode), for fixture coil loads |

Stepping an inductive fixture load faster than the recommended step
produces flyback transients at the fixture terminals; keep per-step
current changes at or below 0.1 A and ramp to zero before disabling the
output at the end of a run.
