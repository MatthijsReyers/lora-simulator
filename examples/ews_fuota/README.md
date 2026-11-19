# EWS FUOTA energy scenario

What a firmware update over the air costs an EWS sensor. Sensors ping the gateway once per hour,
each in its own static slot at the start of the hour. Once per hour the gateway updates one
sensor, in three steps, so that no ping falls inside a transfer:

1. The ACK of the sensor's regular ping carries `SET_PING_SCHEDULE`, which moves its ping to
   the FUOTA moment, well away from the ping slots (half past by default). The sensor confirms
   and pings again at that moment, in the same hour: a sleeping sensor has to start every
   exchange itself.
2. The gateway answers that ping with `MP_START` and pushes the firmware image with the
   multi-packet protocol.
3. An hour later the sensor pings at the FUOTA moment once more; that ACK carries
   `SET_PING_SCHEDULE` back to its static slot.

This is not a functional copy of the sensor and gateway. It puts the same bytes on air, in the
same order, on the same channels, and keeps the sensor's receiver on for the same windows.

## Running

From the repository root:

```bash
pipenv run python examples/ews_fuota/main.py --sensors 10 --hours 24 --image-bytes 100000
```

Other options: `--burst-packets` (fragments per burst, default as many as fit in 1 s),
`--fuota-offset`, `--ping-interval`, `--ping-slot`, `--sf`, `--downlink-loss`, `--uplink-loss`,
`--frame-loss`, `--seed`, `--out`.
Results go to `examples/ews_fuota/data/<run name>/`. To draw who transmits when and on which
channel (`timeline.png` in the run directory):

```bash
pipenv run python examples/ews_fuota/timeline.py examples/ews_fuota/data/<run name>
```

| File | Contents |
|---|---|
| `radio_log/gateway.csv`, `radio_log/sensor_NN.csv` | every radio event of one device |
| `cycles.csv` | one row per sensor wake-up: kind, outcome, time awake, energy |
| `sensors.csv` | per sensor totals: TX/RX time and the energy split |

### Radio log

Columns: `time_s, end_s, channel_mhz, event, bytes, frame, duration_ms`.

- `tx` and `rx` are frames. `time_s` is the first symbol and `end_s` the last, so a capture that
  timestamps received frames at their end compares against `end_s`.
- `rx_window` (sensors) is the receiver being on, from opening to closing.
- `cca` (sensors) is a clear channel assessment, `frame` says `free` or `busy`.

## Files

| File | Contents |
|---|---|
| `frames.py` | frame layouts and sizes |
| `scenario.py` | all parameters, the sensor power model, the radio log |
| `sensor.py` | ping exchange and the receiving side of the download |
| `gateway.py` | ACKs and the sending side of the download |
| `main.py` | builds the run and writes the results |
| `timeline.py` | chart of the transmissions per device |
| `burst_sweep.py` | runs a range of burst sizes and charts the energy per update |

## What is modelled

All frames: SF7, 125 kHz, CR 4/5, 8 symbol preamble, explicit header, CRC, no IQ inversion.
Sensor 10 dBm, gateway 14 dBm. No encryption or MIC.

| Frame | Direction | Bytes | Channel |
|---|---|---|---|
| PING | sensor to gateway | 3 | 868.3 MHz |
| ACK | gateway to sensor | 3 | 868.3 MHz, 60 ms after the uplink |
| ACK + SET_PING_SCHEDULE | gateway to sensor | 11 | 868.3 MHz, in place of the ACK |
| ACK (confirmation) | sensor to gateway | 3 | 868.3 MHz |
| MP_START | gateway to sensor | 6 | 868.3 MHz, in place of the ACK |
| ACK (accept) | sensor to gateway | 3 | 868.3 MHz |
| FRAGMENT | gateway to sensor | 5 + up to 250 | pool |
| ACK / MP_MISSING | sensor to gateway | 3 / 4 + 2 per ID | pool |
| MP_HIGHEST | gateway to sensor | 6 | pool |
| MP_CANCEL | gateway to sensor | 4 | pool |

- Ping: CCA, transmit, 200 ms window that closes at the first frame. Up to 5 attempts with a
  backoff of 100 ms times the attempt plus up to 100 ms jitter.
- Download: bursts of at most 1 s with 20 ms between fragments (2 fragments at SF7), an answer
  after every burst, 3.4 s windows restarted at every frame, three empty windows give up.
- Both sides move to the next of the seven pool channels (863.1 to 864.3 MHz) after 100 s of
  burst time on one channel; the sensor mirrors the gateway's count as the firmware does.
- Without an answer for 1.5 s the gateway sends MP_HIGHEST, three times, then MP_CANCEL.
- After its last answer the sensor listens for one more full window.

Sensor energy follows the firmware's own model (`libs/energy`, 3.6 V): receive 17.3 mW,
transmit 54 mW at 10 dBm, MCU 18 mW while awake, 5 µW asleep, 752 µJ per radio session.

## Assumptions and gaps

- **The gateway's FUOTA sender was not available.** Burst size, the 20 ms gap, the 1.5 s answer
  wait, three asks and the 100 s dwell are the sensor firmware's mirror of it
  (`libs/transport/src/download.rs`).
- Not fixed by any code, set in `scenario.py`: the gateway waits 60 ms after an answer before the
  next burst, the sensor needs 10 ms to reopen its radio before the accept, a CCA takes 2 ms.
- The image size is a parameter; neither repository defines one.
- How a sensor gets back to its static slot after the update is an assumption: it pings at the
  FUOTA moment once more, an hour later, and is moved back in that ACK. To keep that ping out of
  the next sensor's transfer, sensors are staggered by one ping slot at the FUOTA moment as well.
- With `--burst-packets` the sensor's receive window grows with the burst (answer wait + burst
  + one packet + margin), as it would have to; the real firmware has the 1 s burst built in.
- A ping that does arrive while a transfer is running (a transfer that outlasts the hour) gets
  no ACK.
- `--downlink-loss` drops frames at the sensor and `--uplink-loss` at the gateway, as if they
  were never heard: a flat share of the frames, whatever their length or signal strength.
- `--frame-loss` is the same kind of loss applied by the radio channel instead, to every frame
  at every receiver. It is what the protocol comparison (`papers/fuota-comparison`) uses, so
  that all protocols lose frames in the same way.
- Flash writes, image verification and the reboot cost nothing: the firmware does none of them
  yet.
- The simulator does not apply low data rate optimisation, so SF11 and SF12 airtimes are short.

## Reference result

10 sensors, 24 h, 100 kB image (400 fragments), no loss:

| Wake-up | Time awake | Energy |
|---|---|---|
| Ping, acknowledged | 0.124 s | 6.27 mJ |
| Ping, slot moved (twice per update) | 0.178 s | 9.87 mJ |
| Update | 186.1 s | 6.80 J |

Fragments per burst, same run otherwise:

| Burst | Receive window | Time awake | Energy per update |
|---|---|---|---|
| 1 | 3.40 s | 200.7 s | 7.54 J |
| 2 (default) | 3.40 s | 186.1 s | 6.80 J |
| 4 | 4.06 s | 179.4 s | 6.45 J |
| 8 | 5.74 s | 177.4 s | 6.32 J |
| 16 | 9.09 s | 178.9 s | 6.35 J |
| 32 | 15.81 s | 184.8 s | 6.54 J |

Larger bursts save answers and turnarounds, but the last window, which runs to its timeout,
grows with the burst.

To rerun that sweep and draw it, one line per level of frame loss (applied in both directions)
(`data/burst_sweep/burst_sweep.png`):

```bash
pipenv run python examples/ews_fuota/burst_sweep.py --bursts 1 2 4 8 16 32 --losses 0 0.05 --seeds 3
```

Under loss the energy per update includes the transfers that failed on the way to it.
