# Firmware Updates over the Air via LoRa: Unicast and Broadcast Combination (2024)

Reproduction of the simulations in:

> V. Malumbres, J. Saldana, G. Berné and J. Modrego, *Firmware Updates over the Air via LoRa:
> Unicast and Broadcast Combination for Boosting Update Speed*, Sensors 2024, 24, 2104.
> https://doi.org/10.3390/s24072104

The paper compares three ways of pushing a firmware image from one gateway to `N` nodes over raw
LoRa (MiWi frames, no LoRaWAN) and measures the total update time under a 1% duty cycle. All
three methods are implemented, each as a gateway/node pair in its own folder on top of the
shared frame definitions, scenario and base classes.

| File | Contents |
| :--- | :------- |
| `frames.py` | MiWi frame header and the OTA payload types of Appendix A (plus the bitmap messages of the broadcast methods). |
| `scenario.py` | Parameters of Table 1, node placement (Figure 6), channel model, duty cycle rule and the device base class. |
| `gateway_base.py` | Stop and wait primitives, per node results, and the checksum / flashing / confirmation stages shared by all methods. |
| `node_base.py` | Receive loop, chunk storage and the frames every node handles the same way. |
| `unicast_only/` | Section 3.1: sequential per node update, every fragment acknowledged. |
| `broadcast_unicast/` | Section 3.2: unicast announcement, `B` broadcast rounds, then the missing chunks per node in acknowledged unicast. |
| `broadcast_only/` | Section 3.3: as above, but the missing chunks are broadcast too and the node is asked for its bitmap until nothing is missing. |
| `main.py` | Runs one update of a whole project with one method and writes CSV results. |
| `run.sh` | The simulation batteries of section 5 for one method (`METHOD=...`). |
| `graphs.py` | Renders the figures from the 10 node runs: RSSI timelines (Figures 8-13), per node time and loss against the analytical model, totals of the three methods next to the paper, spread over seeds. |

## Running

From the repository root:

```bash
pipenv run python papers/fuota-unicast-broadcast-2024/main.py --method broadcast_unicast --nodes 10 --radius 400 --frame-log
REPEATS=5 METHOD=broadcast_unicast ./papers/fuota-unicast-broadcast-2024/run.sh
```

`main.py --help` lists the parameters, the defaults are those of Table 1 of the paper (10 nodes,
400 m, 100 kB firmware, 215 byte frames, SF7, 125 kHz, 1% duty cycle, -125 dBm sensitivity, one
broadcast round).

Every run writes three files to `data/`, named after the method, node count, radius, frame
length and seed:

- `*_summary.csv`: one line with the parameters, the time until the last node finished the
  binary exchange (the metric of the paper, which only simulates that stage) and the time of the
  whole procedure including checksum, flashing and confirmation.
- `*_nodes.csv`: per node distance, timestamps of the stages, number of frames and
  retransmissions.
- `*_frames.csv` (with `--frame-log`): every transmitted frame with its RSSI at the destination
  and whether it was delivered, the data behind Figures 8 and 9. Node radios keep a log of
  every frame they overhear for this, which costs a lot of memory for large node counts.

## How the paper maps onto the simulator

**Only unicast.** The five stages of Figure 1 are implemented as described in section 3.1 and
Appendix A: reset request/confirmation, binary fragments with a fragment number of 1, 2 or 3
bytes (the *last fragment* types mark the end), status request/response, checksum fragments,
flash command, and the final handshake plus status request after the reboot. Every gateway frame
except the flash command is acknowledged and retransmitted after a timeout. The acknowledgement of
a fragment is the `WAITING FOR BINARY FRAGMENT` message carrying the fragment number the node
expects next, so a lost acknowledgement simply results in a retransmission of the same fragment.

**Broadcast + unicast.** Stages (a) to (c) of Figure 2: a unicast `OTA BROADCAST START
REQUEST` (new version, number of chunks, chunk size) to every node, answered with the node's
current version; `B` rounds of every fragment as a broadcast frame (17 byte MiWi header with the
2 byte broadcast address, nothing is acknowledged, every frame is followed by its quiet time);
then per node `SEND ME THE BITMAP`, the `BITMAP` (one bit per chunk) and the missing chunks in
acknowledged unicast frames. The acknowledgement carries the number of the fragment it confirms
because the pending chunks are not sequential. Stages (d) and (e) are those of only unicast.

**Only broadcast.** Identical up to the bitmap; the missing chunks are then broadcast (so other
nodes fill their gaps too), the node is asked for its bitmap again, and so on until it answers
`ALL CHUNKS RECEIVED` (Figure 3). Nodes already complete answer `ALL CHUNKS RECEIVED` to the
first request in both broadcast methods.

**Frames.** A unicast MiWi frame carries 23 bytes of header and FCS (Appendix A.1). Since the
appendix says the fragment number width shrinks the frame rather than growing the chunk, the
firmware chunk is fixed at `frame_len - 23 - 3` bytes (189 bytes for 215 byte frames). Frames
with a one byte fragment number are one byte shorter. The acknowledgement is 26 bytes. The
status response carries an extra byte telling whether the checksum verified (the paper's gateway
also learns this from the status response but the appendix does not list the field).

**Firmware size and frames as simulated by the paper (`--paper-frames`).** The paper's numbers
only add up with a 100,000 byte image and nothing but the 192 byte payload on the air: the
broadcast stage of 16,097 s (section 5.2.2) is 521 fragments × 100 × 307 ms (the time on air of
192 bytes), and the only unicast 17,672 s per node is 521 × 100 × (307 ms + 31 ms), i.e. the
same fragments plus an acknowledgement of one to three bytes. (Section 4 uses `M = 534`, that
is 100 KiB / 192, in its worked example only.) The firmware is therefore 100,000 bytes here and
`--firmware-kb` counts 1000 bytes. With `--paper-frames` the frames are sent with a two byte
source/destination header instead of the MiWi one and the chunk is the full 192 byte payload, so
a fragment is 196-197 bytes (313-318 ms) and an acknowledgement 5 bytes (36 ms). The default
(full header on the air, 189 byte chunks) is what the hardware of section 6 would do and is
about 18% slower.

**Duty cycle.** Section 4 treats the 1% duty cycle as a quiet time of `99 * ToA` on the whole
medium after *every* frame, including acknowledgements, and the analytical update time
(equation 7) is `100 * (ToA_chunk + ToA_ack)` per successful fragment. `DutyCycle` reproduces
this with one instance shared by all devices (the default). `--per-device-duty-cycle` gives every
transmitter its own budget instead, which is what the regulations actually require and lets the
acknowledgement overlap the gateway's quiet time.

**Channel.** The paper uses ns-3's log-distance model with Nakagami-m fading and a receiver
sensitivity of -125 dBm, but gives neither the path loss exponent nor the reference loss.
`simulator/path_loss/nakagami_path_loss.py` ports the ns-3 fading model (Gamma distributed power
gain with `m = 1.5` below 80 m and `m = 0.75` beyond). The exponent was calibrated analytically
against the two single run results of the paper: with `n = 3.2` (and the free space loss at 1 m
as reference) the expected number of transmissions per fragment is 1.008 for 10 nodes within
400 m and the 2 km scenario takes 1.60 times as long as the 400 m one (paper: 176,725 s versus
294,613 s, a factor 1.66). The noise floor is set so demodulation fails exactly below the
sensitivity for the used spreading factor, fading is the only source of frame loss, as in the
paper.

## Results

Time until the last node holds the whole binary, 10 nodes, 100 kB, one broadcast round. The
paper's values are its single runs of section 5.2; ours are means over seeds 1-20
(`--paper-frames`) and 1-10 (full MiWi header) with 95% confidence intervals, all in hours.
Seed 0 of every configuration was run with `--frame-log` for the timeline figures; every
summary is in `data/`.

| Method | Radius | Paper | `--paper-frames` | Default frames |
| :----- | :----- | ----: | ---------------: | -------------: |
| Only unicast | 400 m | 49.1 | 51.3 ± 0.1 | 60.6 ± 0.1 |
| Only unicast | 2 km | 81.8 | 78.4 ± 6.1 (58-112) | 92.1 ± 7.3 |
| Broadcast + unicast | 400 m | 4.66 | 4.95 ± 0.05 | 5.47 ± 0.08 |
| Broadcast + unicast | 2 km | 37.9 | 21.9 ± 3.9 (9-44) | 25.3 ± 4.6 |
| Only broadcast | 400 m | 4.87 | 4.94 ± 0.04 | 5.45 ± 0.08 |
| Only broadcast | 2 km | 18.6 | 11.5 ± 1.0 (7.5-16.6) | 12.9 ± 1.2 |

Observations:

- The broadcast stage takes 16,392 s (paper: 16,097 s, +1.8%), and at 400 m a total of 19
  chunks per run need unicast repair afterwards (paper: 26). The remaining 4-6% at 400 m is the
  acknowledgement (5 bytes here, apparently a bare LoRa frame in ns-3) and the extra bytes of
  the compact header on every fragment.
- At 2 km everything depends on where the ten nodes land: the only unicast runs range from 58
  to 112 h, and a node beyond 1.5 km (22% of all nodes) can take three times as long as a
  near one. The paper's single unicast run sits right at our mean; its single broadcast +
  unicast run (37.9 h) is above our whole range but its own 40-run average in Figure 14b ("35%
  of only unicast" at 2 km, i.e. about 28 h) is close to our 28% (21.9 h of 78.4 h).
- Only broadcast benefits more here than in the paper at 2 km: 53% of the broadcast + unicast
  time versus 66% in Figure 14c. The repair broadcasts of the far nodes fill in the near
  nodes' gaps, which then answer `ALL CHUNKS RECEIVED` to their first bitmap request.
- The full MiWi header costs a constant 10-18% on top, purely airtime, for all three methods.

**Not in the paper.** A few details had to be filled in: nodes take 50 ms to answer a frame,
the gateway gives a node 1 s to verify the checksum and 10 s to flash and reboot (the frames
involved are negligible next to the duty cycle waits), and a lost flash command is repeated
rather than restarting the whole update since the node still holds the verified image. If a
status response shows a wrong fragment count or a failed checksum the procedure restarts from
the reset stage as the paper describes.
