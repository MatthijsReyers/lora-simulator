# Firmware Updates over the Air via LoRa: Unicast and Broadcast Combination (2024)

Reproduction of the simulations in:

> V. Malumbres, J. Saldana, G. Berné and J. Modrego, *Firmware Updates over the Air via LoRa:
> Unicast and Broadcast Combination for Boosting Update Speed*, Sensors 2024, 24, 2104.
> https://doi.org/10.3390/s24072104

The paper compares three ways of pushing a firmware image from one gateway to `N` nodes over raw
LoRa (MiWi frames, no LoRaWAN) and measures the total update time under a 1% duty cycle. This
folder currently implements the **only unicast** method (section 3.1), the other two methods are
planned as sibling folders that reuse `frames.py` and `scenario.py`.

| File | Contents |
| :--- | :------- |
| `frames.py` | MiWi frame header and the OTA payload types of Appendix A. |
| `scenario.py` | Parameters of Table 1, node placement (Figure 6), channel model, duty cycle rule and the device base class. |
| `unicast_only/gateway.py` | Gateway side of the only unicast method: sequential per node update with stop and wait. |
| `unicast_only/node.py` | Node side: `bin_frags`/`check_frags` counters, flashing and reboot. |
| `main.py` | Runs one update of a whole project and writes CSV results. |
| `run.sh` | The simulation batteries of section 5. |
| `graphs.py` | Renders `unicast_*.png` from the 10 node single runs: RSSI timelines (Figures 8/9), per node time and loss against the analytical model, totals next to the paper. |

## Running

From the repository root:

```bash
pipenv run python papers/fuota-unicast-broadcast-2024/main.py --nodes 10 --radius 400 --frame-log
REPEATS=5 ./papers/fuota-unicast-broadcast-2024/run.sh
```

`main.py --help` lists the parameters, the defaults are those of Table 1 of the paper (10 nodes,
400 m, 100 kB firmware, 215 byte frames, SF7, 125 kHz, 1% duty cycle, -125 dBm sensitivity).

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

**Protocol.** The five stages of Figure 1 are implemented as described in section 3.1 and
Appendix A: reset request/confirmation, binary fragments with a fragment number of 1, 2 or 3
bytes (the *last fragment* types mark the end), status request/response, checksum fragments,
flash command, and the final handshake plus status request after the reboot. Every gateway frame
except the flash command is acknowledged and retransmitted after a timeout. The acknowledgement of
a fragment is the `WAITING FOR BINARY FRAGMENT` message carrying the fragment number the node
expects next, so a lost acknowledgement simply results in a retransmission of the same fragment.

**Frames.** A unicast MiWi frame carries 23 bytes of header and FCS (Appendix A.1). Since the
appendix says the fragment number width shrinks the frame rather than growing the chunk, the
firmware chunk is fixed at `frame_len - 23 - 3` bytes (189 bytes for 215 byte frames, giving 542
fragments for 100 kB, Figure 1 shows 543). Frames with a one byte fragment number are one byte
shorter. The acknowledgement is 26 bytes. The status response carries an extra byte telling
whether the checksum verified (the paper's gateway also learns this from the status response but
the appendix does not list the field).

**Frames as simulated by the paper (`--paper-frames`).** The ns-3 simulations of the paper
evidently did not put the 23 byte MiWi header on the air: their 17,672 s per node at 400 m is
`534 * 100 * (307 ms + ~24 ms)`, i.e. 534 fragments of 192 bytes (`M = 534` in section 4) with the
time on air of a 192 byte LoRa payload and a nearly empty acknowledgement, and the 313 ms quoted
in section 4 is the time on air of 197 bytes. With `--paper-frames` the frames are sent with a
two byte source/destination header instead of the MiWi one and the chunk is the full 192 byte
payload, so a fragment is 197 bytes (313 ms) and an acknowledgement 5 bytes (31 ms). The default
(full header on the air, 189 byte chunks) is what the hardware of section 6 would do and is
about 20% slower.

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

Time until the last node finished the binary exchange, 10 nodes, seed 0, single runs (section
5.2.1 of the paper):

| Scenario | Paper | `--paper-frames` | Default frames |
| :------- | ----: | ---------------: | -------------: |
| 400 m radius | 176,725 s (49.1 h) | 189,888 s (52.7 h) | 224,295 s (62.3 h) |
| 2 km radius | 294,613 s (81.8 h) | 341,311 s (94.8 h) | 399,690 s (111.0 h) |
| ratio 2 km / 400 m | 1.66 | 1.80 | 1.78 |

The 7% left at 400 m with `--paper-frames` is the acknowledgement being 5 bytes instead of
(apparently) empty. The 2 km scenario depends strongly on the node placement of the seed, the
paper reports per node update times between 21,258 s and 35,664 s, this run has 23,433 s to
61,475 s (node 6 sits at 1,816 m where the fading loses half of the frames).

Over 20 seeds (1-20, `--paper-frames`, summaries in `data/`) the 2 km scenario takes 80.5 h on
average (standard deviation 14.5 h, 95% confidence interval ±6.3 h, range 59.6-116.7 h), so the
paper's single run of 81.8 h sits right at the mean and seed 0 above is simply a slow draw. The
400 m scenario is 52.6 h ±0.2 h, which puts the ratio of the means at 1.53. With the full MiWi
header on the air (10 seeds) the means are 62.0 h ±0.1 h and 94.3 h ±7.6 h (range 73.4-112.7 h),
the same ratio of 1.52.

**Not in the paper.** A few details had to be filled in: nodes take 50 ms to answer a frame,
the gateway gives a node 1 s to verify the checksum and 10 s to flash and reboot (the frames
involved are negligible next to the duty cycle waits), and a lost flash command is repeated
rather than restarting the whole update since the node still holds the verified image. If a
status response shows a wrong fragment count or a failed checksum the procedure restarts from
the reset stage as the paper describes.
