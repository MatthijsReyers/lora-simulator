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

Six boards were measured in four runs (2026-11-21), the profile is the mean of four of them.

| Board (ST-LINK serial) | Run | Sleep | CPU running | Standby | RX 125 kHz | TX 14 dBm | TX 22 dBm |
|---|---|---|---|---|---|---|---|
| `0049002A3234510A33353533` | run-1 | 27.7 | +4.7 | +3.3 | +30.7 | +184.1 | +678.1 |
| `002200355553500B20393256` (left out) | run-1 | 28.3 | +32.8 | +31.5 | +63.3 | +189.0 | +658.5 |
| `003F003D3333511431363730` | run-2 | 27.7 | +4.8 | +3.3 | +30.3 | +182.8 | +670.5 |
| `000800195553500B20393256` | run-2 | 28.2 | +4.7 | +3.5 | +29.9 | +162.5 | +684.2 |
| `002D00333234510A33353533` | run-3 | 28.0 | +4.7 | +3.3 | +30.3 | +186.2 | +673.4 |
| `002F00383133510836303739` (left out) | run-3 | 28.1 | +142.5 | +141.0 | +187.2 | +346.6 | +843.6 |

Whole board in mW at 5 V when asleep (radio asleep, CPU stopped), the other columns are what the
state adds. TX as the ST middleware sets it up.

Boards `0022...` and `002F...` are left out. They match the others when everything sleeps, but
draw 32 and 141 mW extra whenever the CPU runs or the radio is not asleep, the same amount in
every state. The cause is not known.

## How well it repeats (run-4)

Boards `0049...` and `002D...` were measured a second time, `0049...` on the other Joulescope.

| | `0049...` run-1 / run-4 | `002D...` run-3 / run-4 |
|---|---|---|
| Sleep (whole board) | 27.66 / 27.85 | 27.97 / 28.01 |
| Standby | +3.35 / +3.35 | +3.31 / +3.35 |
| RX 125 kHz | +30.71 / +30.68 | +30.27 / +30.29 |
| TX 0 dBm | +63.4 / +69.8 | +67.3 / +73.7 |
| TX 7 dBm | +109.2 / +124.2 | +115.0 / +127.7 |
| TX 14 dBm | +184.1 / +196.1 | +186.2 / +187.3 |
| TX 16 dBm | +565.1 / +594.3 | +568.7 / +588.1 |
| TX 22 dBm | +678.1 / +683.8 | +673.4 / +674.9 |

Sleep, standby and receiving repeat within 0.1 mW (0.2 mW for the sleep level on the other
Joulescope), so the Joulescopes are not what sets the two left out boards apart. Transmitting
does not repeat: up to 14 % more in the second run on both boards, with the same firmware. The
boards were plugged in again between the runs, a likely cause is what the antenna sees, this was
not tested. The differences between boards on the low-power amplifier (`0008...` draws 12 % less
at 14 dBm) are of the same size, so they say little about the boards.

In the profile a board that was measured twice counts once, with the mean of its runs.
