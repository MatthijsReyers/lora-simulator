# FUOTA protocol comparison

Every FUOTA protocol in this repository on one and the same deployment: the same node
positions, firmware size, channel and energy model. It answers what one firmware update costs a
device under each protocol, and how long it takes to update the fleet.

## Running

From the repository root:

```bash
pipenv run python papers/fuota-comparison/main.py --nodes 10 --radius 400 --firmware-kb 100
```

Other options: `--exponent`, `--duty-cycle`, `--uplink-interval`, `--class-b-periodicity`,
`--seed`, `--only <protocol names>`, `--jobs`. The two LoRaWAN runs take about three minutes
each, the others seconds. Results go to `data/<run name>/`:

| File | Contents |
|---|---|
| `layout.csv` | the node positions handed to every protocol |
| `comparison_nodes.csv` | one row per protocol and device: time and energy per radio state |
| `comparison_summary.csv` | one row per protocol |
| `comparison.png` | energy per device and fleet update time per protocol |
| `<protocol>/` | that protocol's own output and `output.log` |

## Protocols

| Name | Protocol | Implementation |
|---|---|---|
| `ews_burst2`, `ews_burst12` | EWS multi-packet download, 2 or 12 fragments per burst | `examples/ews_fuota` |
| `lorawan_class_b`, `lorawan_class_c` | LoRaWAN FUOTA (TS003, TS004, TS005) | `examples/lorawan_fuota` |
| `sequential_sf`, `sequential_sf_always_on` | Rateless multicast over a spreading factor ladder (Borkotoky 2025) | `papers/fuota-sequential-sf-2025` |
| `broadcast_only`, `broadcast_unicast` | Raw LoRa broadcast with bitmap repair (Malumbres 2024) | `papers/fuota-unicast-broadcast-2024` |

## What is the same for all

- **Deployment:** nodes spread uniformly over a disc around one gateway, from one seed.
- **Firmware:** the same size, as arbitrary bytes.
- **Channel:** log-distance path loss (exponent 3.2 by default), no shadowing and no fading,
  SF7 at 125 kHz. With the default 400 m every link has more than 20 dB to spare, so nothing is
  lost and no repair mechanism is exercised.
- **Energy model:** worked out afterwards from the time every device's radio spent in each
  state (`simulator/lora/radio_usage.py`), with the EWS sensor's levels for every protocol:
  receive 17.3 mW, transmit 54 mW at 10 dBm and 162 mW at 14 dBm, 18 mW for the MCU whenever
  the radio is not off, 5 µW asleep.
- **What counts as the update:** from the first frame of the protocol a device sends or could
  receive until it holds the complete image. A device that never gets it is charged until the
  end of the run.

## What differs, on purpose

- **Duty cycle:** the gateways of the papers and of LoRaWAN keep to 1% per transmitter. EWS has
  its own rule, 100 s of bursts per channel before hopping. Class B ping periodicity 6 (two
  ping slots per beacon period) is what keeps that gateway under 1%.
- **Schedule:** EWS updates one sensor per hour, so its fleet time is the number of sensors in
  hours by design. For an EWS sensor the update starts with the ping that moves its ping slot,
  half an hour before the transfer; the other protocols start for every device at once.
- **Transmit power:** EWS sensors send at 10 dBm, every other device at 14 dBm.
- **Fragment size:** each protocol keeps its own frame format: 250 B of firmware per frame for
  EWS and the sequential SF scheme, 189 B for Malumbres, the largest that fits a DR5 frame for
  LoRaWAN.
- **Receivers:** the nodes of Malumbres listen without pause, as that paper assumes, which is
  what their energy consists of.
- **Sequential SF:** its recipients know the gateway's schedule and only need the receiver for
  the frames themselves. The simulation keeps their radios on throughout, so `sequential_sf`
  is charged the receive time of the paper's own energy model (a whole frame when its preamble
  was acquired, the preamble otherwise) and the rest counts as sleep. Waking up for a frame is
  free in that model: no guard time, no start-up. `sequential_sf_always_on` is the same run
  charged for all the time the simulated receiver was on.
- **LoRaWAN uplinks:** a device uplinks once per hour (`--uplink-interval`), which paces the
  setup phases: most of the LoRaWAN fleet time is waiting for those uplinks.

## Gaps

- Receiver sensitivities are each implementation's own (between -123 and -127.5 dBm at SF7);
  with the default deployment no link comes near them.
- The energy of an EWS radio session (opening and closing the radio, 752 µJ) is not in the
  shared energy model.
- One seed and no losses: the result is deterministic and says nothing about link quality.

## Reference result

10 nodes within 400 m, 100 kB image:

| Protocol | Energy per device | Receiver on | Fleet updated after |
|---|---|---|---|
| Sequential SF multicast | 5.64 J | 157 s | 4.4 h |
| EWS, 12 fragments per burst | 6.34 J | 177 s | 9.6 h |
| EWS, 2 fragments per burst | 6.81 J | 180 s | 9.6 h |
| LoRaWAN FUOTA, Class B | 11.2 J | 307 s | 13.0 h |
| Sequential SF multicast, receiver always on | 554 J | 4.4 h | 4.4 h |
| LoRaWAN FUOTA, Class C | 596 J | 4.7 h | 9.6 h |
| Broadcast only | 631 J | 5.0 h | 5.0 h |
| Broadcast + unicast | 631 J | 5.0 h | 5.0 h |

The two broadcast methods are identical here because nothing is lost.
