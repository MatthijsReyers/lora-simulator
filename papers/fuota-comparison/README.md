# FUOTA protocol comparison

Every FUOTA protocol in this repository on one and the same deployment: the same node
positions, firmware size, channel and energy model. It answers what one firmware update costs a
device under each protocol, and how long it takes to update the fleet.

## Running

From the repository root:

```bash
pipenv run python papers/fuota-comparison/main.py --nodes 10 --radius 400 --firmware-kb 100
```

Other options: `--exponent`, `--shadowing`, `--sensitivity`, `--loss`, `--duty-cycle`,
`--uplink-interval`, `--class-b-periodicity`, `--seed`, `--only <protocol names>` (reruns those
and keeps the earlier results of the others), `--jobs`. The two LoRaWAN runs take about three minutes
each, the three of the sequential SF scheme on LoRaWAN about two,
the others seconds. Results go to `data/<run name>/`, the run name ending in
`_loss<share>` when there is loss (and likewise for the exponent, shadowing and sensitivity):

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
| `sequential_sf_lorawan`, `sequential_sf_lorawan_sf7_8`, `sequential_sf_lorawan_class_c` | Rateless multicast over a spreading factor ladder (Borkotoky 2025), as a campaign on the LoRaWAN stack | `papers/fuota-sequential-sf-2025-lorawan` |
| `broadcast_only`, `broadcast_unicast` | Raw LoRa broadcast with bitmap repair (Malumbres 2024) | `papers/fuota-unicast-broadcast-2024` |

## What is the same for all

- **Deployment:** nodes spread uniformly over a disc around one gateway, from one seed.
- **Firmware:** the same size, as arbitrary bytes.
- **Transmit power:** 14 dBm for every device. The EWS sensors send at 10 dBm in the real
  system (`--ews-tx-power 10`), which costs them 4 dB of range on the uplink.
- **Channel:** log-distance path loss (exponent 3.2 by default) and no fading, SF7 at 125 kHz.
  `--shadowing 4` adds log-normal shadowing with that standard deviation in dB, drawn anew for
  every frame at every receiver.
- **Receivers:** every radio has the same sensitivity, `--sensitivity` in dBm at SF7 (-105 by
  default) and 2.5 dB better per spreading factor up. With the default 400 m and exponent a
  14 dBm frame still has 4.5 dB to spare at the edge, so without shadowing or `--loss` nothing
  is lost and no repair mechanism is exercised.
- **Frame loss:** `--loss 0.05` makes every receiver lose 5% of the frames, whatever the link
  budget. The radio channel applies it (`frame_loss` of `LoraPhyLayer`), so it is the same for
  every protocol: each receiver draws for itself, in both directions, and for every kind of
  frame, the setup commands, acknowledgements and beacons included. A lost frame is one the
  receiver never acquires, as if it had drowned in the noise.
- **Energy model:** worked out afterwards from the time every device's radio spent in each
  state (`simulator/lora/radio_usage.py`), with the EWS sensor's levels for every protocol:
  receive 17.3 mW, transmit 54 mW at 10 dBm and 162 mW at 14 dBm, 18 mW for the MCU whenever
  the radio is not off, 5 µW asleep.
- **Periodic traffic:** every device sends a frame of its own once per hour
  (`--uplink-interval`), update or no update: the ping of an EWS sensor, the application
  uplink of a LoRaWAN device, a small data frame of a node of Malumbres.
- **What counts as the update:** from the start of the FUOTA process for a device until it
  holds the complete image. For EWS and LoRaWAN that start is the periodic uplink that brings
  the device its first command of the update, for Malumbres the gateway's first frame. The
  periodic traffic before that is not counted, what falls inside is. A device that never gets
  the image is charged until the end of the run.

## What differs, on purpose

- **Duty cycle:** the gateways of the papers and of LoRaWAN keep to 1% per transmitter. EWS has
  its own rule, 100 s of bursts per channel before hopping. Class B ping periodicity 6 (two
  ping slots per beacon period) is what keeps that gateway under 1%.
- **Schedule:** EWS updates one sensor per hour, so its fleet time is the number of sensors in
  hours by design. For an EWS sensor the update starts with the ping that moves its ping slot,
  half an hour before the transfer; the other protocols start for every device at once.
- **Fragment size:** each protocol keeps its own frame format: 250 B of firmware per frame for
  EWS, 189 B for Malumbres, the largest that fits a DR5 frame for LoRaWAN FUOTA (219 B).
- **Receivers:** the nodes of Malumbres listen without pause, as that paper assumes, which is
  what their energy consists of.
- **Sequential SF on LoRaWAN:** the paper's scheme with what the paper leaves out: LoRaWAN
  multicast frames carrying TS004 fragments, the group and the schedule set up with unicast
  commands, an acknowledgement as an uplink, and a device that opens a receive window per
  frame, so its energy is that of its radio like for every other protocol. The fragments have
  to fit a frame of the last spreading factor of the ladder: 48 B for the paper's ladder up
  to SF12, 219 B for `_sf7_8`, which stops at SF8. `_class_c` has the devices listen
  continuously during the session instead. The gateway gives up after
  `--sequential-max-hours` (24 by default), which also bounds how long these runs take.
- **LoRaWAN uplinks:** a device uplinks once per hour (`--uplink-interval`), which paces the
  setup phases: most of the LoRaWAN fleet time is waiting for those uplinks. That goes for
  LoRaWAN FUOTA and for the sequential SF scheme on LoRaWAN alike.

The scheme of Borkotoky on bare radios, as the paper models it and as
`papers/fuota-sequential-sf-2025` reproduces it, is commented out in `main.py`: it has no
protocol around its frames, fragments larger than a LoRaWAN frame can carry and no traffic of
the devices' own, so its numbers do not compare with the others.

## Gaps

- The energy of an EWS radio session (opening and closing the radio, 752 µJ) is not in the
  shared energy model.
- One seed: a result says nothing about the spread between deployments or loss patterns.
- Frame loss is independent per frame and the same at every distance: no bursts of loss, no
  nodes with a worse link than others.
- What a lost frame costs a receiver follows from each protocol's own energy accounting. The
  sequential SF scheme is charged the preamble of a frame it does not acquire, a duty cycled
  receiver of EWS or LoRaWAN Class B keeps its window open as it would for any missing frame.

## Reference result

10 nodes within 400 m, 100 kB image:

| Protocol | Energy per device | Receiver on | Fleet updated after |
|---|---|---|---|
| Sequential SF on LoRaWAN, SF7-8 | 6.40 J | 172 s | 13.6 h |
| EWS, 12 fragments per burst | 6.46 J | 177 s | 9.6 h |
| EWS, 2 fragments per burst | 7.50 J | 180 s | 9.6 h |
| Sequential SF on LoRaWAN, SF7-12 | 9.47 J | 257 s | 15.8 h |
| LoRaWAN FUOTA, Class B | 11.3 J | 307 s | 17.0 h |
| LoRaWAN FUOTA, Class C | 596 J | 4.7 h | 13.6 h |
| Broadcast only | 633 J | 5.0 h | 5.1 h |
| Broadcast + unicast | 644 J | 5.1 h | 5.2 h |
| Sequential SF on LoRaWAN, SF7-12, Class C | 876 J | 6.9 h | 15.8 h |

- The sequential SF scheme on LoRaWAN costs 6.4 J or 9.5 J depending on whether the ladder
  stops at SF8 or goes up to SF12 as in the paper: the ladder's last spreading factor sets the
  fragment size, and with 48 B fragments the image takes 2086 frames of 64 B instead of 458
  of 235 B.
- For the LoRaWAN protocols the update starts with a device's first uplink, which brings it
  the first setup command, and the 13 to 17 hours until it holds the image are mostly the
  setup waiting for further uplinks. Those uplinks and the sleep in between are inside the
  measurement: about 0.5 s of transmitting and 0.15 to 0.3 J of sleep per device.
- Nothing is lost on the channel, yet the two broadcast methods differ: a node that sends its
  hourly uplink while a fragment is on the air misses that fragment (11 fragments in the one
  run, 1 in the other), which then has to be repaired.

The same deployment with `--loss 0.05`:

| Protocol | Energy per device | Worst device | Receiver on | Fleet updated after |
|---|---|---|---|---|
| Sequential SF on LoRaWAN, SF7-8 | 6.80 J | 6.96 J | 182 s | 15.9 h |
| EWS, 12 fragments per burst | 7.16 J | 7.94 J | 195 s | 12.6 h |
| EWS, 2 fragments per burst | 9.36 J | 9.93 J | 228 s | 13.6 h |
| Sequential SF on LoRaWAN, SF7-12 | 10.0 J | 10.1 J | 271 s | 18.2 h |
| LoRaWAN FUOTA, Class B | 16.0 J | 16.6 J | 440 s | 19.5 h |
| LoRaWAN FUOTA, Class C | 631 J | 639 J | 5.0 h | 15.8 h |
| Broadcast only | 792 J | 910 J | 6.2 h | 7.2 h |
| Broadcast + unicast | 839 J | 1020 J | 6.6 h | 8.0 h |
| Sequential SF on LoRaWAN, SF7-12, Class C | 925 J | 935 J | 7.3 h | 18.2 h |

- The sequential SF scheme hardly notices: its tenth of spare fragments at SF7 covers a loss
  of 5%, so no device needs the second spreading factor.
- The energy of an EWS update includes the transfers that failed before the one that
  completed.
