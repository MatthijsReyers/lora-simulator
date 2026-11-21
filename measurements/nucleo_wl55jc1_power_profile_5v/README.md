# Nucleo-WL55JC1 power profile at the 5 V supply

Source of `NucleoWL55PowerProfile`. One Joulescope JS220 per board in the board's 5 V supply, so
the board's 3.3 V regulator is part of what is measured.

- `firmware/main.c` walks the radio through sleep, standby, receiving and transmitting.
- `capture.py <run>` builds it, flashes every connected board and records the Joulescopes.
- `extract_power_profile.py <run>` reads the levels back out of the recording.

The raw recording (`joulescope.jls`, 2.8 GB) is not committed, `power_1khz.npz` is the same
trace averaged down to 1 kHz.

## run-1 (2026-11-21, two boards)

The profile uses board `0049002A3234510A33353533` only. The other board
(`002200355553500B20393256`) draws about 32 mW extra whenever its CPU runs or its radio is not
asleep (standby 59.8 mW against 31.0 mW, with the same 28 mW when everything sleeps), and its
transmit levels differ by up to 15 %. The cause is not known, so its values are in
`run-1/power_profile.json` but not in the profile.
