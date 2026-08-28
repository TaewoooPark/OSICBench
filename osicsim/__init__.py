"""osicsim - a deterministic simulated instrument farm for OSIC-Bench.

Layers:
    transport  - per-device TCP servers with realistic interface quirks
    scpi       - SCPI-1999 / IEEE-488.2 message engine (tree, chaining, units)
    device     - base class for simulated instruments (errors, status, OPC)
    physics    - device-under-test models with hidden ground truth
    circuit    - wiring between source instruments, DUTs, and meters
    faults     - scheduled fault injection (transaction-indexed or timed)
    recorder   - the flight recorder: every transaction and state transition
    farm       - assembles a task's instrument farm and runs it
"""

__version__ = "0.1.0.dev0"
