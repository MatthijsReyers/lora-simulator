# Nucleo-WL55JC1 power profile at the 5 V supply

Source of `NucleoWL55PowerProfile`. One Joulescope JS220 per board in the board's 5 V supply, so
the board's 3.3 V regulator is part of what is measured.

- `firmware/main.c` walks the radio through sleep, standby, receiving and transmitting.
- `capture.py <run>` builds it, flashes every connected board and records the Joulescopes.
- `extract_power_profile.py <run>` reads the levels back out of the recording.
- `combine.py` averages the boards into the values of the profile.

The raw recordings (`joulescope.jls`, 2.8 GB each) are not committed, `power_1khz.npz` is the
same trace averaged down to 1 kHz.

## Boards

Four boards were measured in two runs (2026-11-21), the profile is the mean of three of them.

| Board (ST-LINK serial) | Run | Sleep | Standby | RX 125 kHz | TX 14 dBm | TX 22 dBm |
|---|---|---|---|---|---|---|
| `0049002A3234510A33353533` | run-1 | 27.7 | +3.3 | +30.7 | +184.1 | +678.1 |
| `003F003D3333511431363730` | run-2 | 27.7 | +3.3 | +30.3 | +182.8 | +670.5 |
| `000800195553500B20393256` | run-2 | 28.2 | +3.5 | +29.9 | +162.5 | +684.2 |
| `002200355553500B20393256` (left out) | run-1 | 28.3 | +31.5 | +63.3 | +189.0 | +658.5 |

Whole board in mW at 5 V when asleep, the other columns are what the state adds. TX as the ST
middleware sets it up.

Board `0022...` is left out: it draws about 32 mW extra whenever its CPU runs or its radio is not
asleep, while it matches the others when everything sleeps. The cause is not known.

The three other boards agree within 5 % except on the low-power amplifier, where `0008...` draws
up to 12 % less than the other two with the middleware setup (and up to 25 % more at low power
with the ping pong setup).
