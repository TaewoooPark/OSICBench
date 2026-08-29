# T06 - Closed-loop temperature hold

A sample stage sits on a heater (25.0 ohm element) powered by channel OUT1
of a Meridian MER-P330 supply. A Meridian MER-T115 reads the stage sensor.
There is no built-in temperature controller: your code IS the controller.

## Fixture data

- Heater element: 25.0 ohm, rated to 20 V (set the supply's OVP to 20 V).
- Stage thermal response: first-order; gain roughly 10 K per watt
  (+/- 20 %), time constant roughly 12 s (+/- 20 %). Ambient 293 K.
- **The sample is damaged above 335 K.** The run fails if the stage ever
  exceeds it.

## Goal

Bring the stage to **330.00 K** and hold it there:

- enter the +/- 0.25 K band within 75 s of starting,
- then remain in regulation for at least **60 s**: at least 90 % of that
  window inside the band, RMS error not much above 0.1 K,
- finish with the heater output OFF.

There are no file deliverables: grading works entirely from the recorded
true stage temperature and the recorded supply state.

## Environment contract

- `OSIC_ENDPOINTS`: path to a JSON file,
  `{"<device>": {"host": ..., "port": ..., "resource": ...}}`;
  `psu1` (heater supply), `tmon1` (thermometer).
- Manuals: `manuals/mer_p330.md`, `manuals/mer_t115.md`.

## Notes

- 150 s hard ceiling; exit once regulation is demonstrated and the heater
  is off.
