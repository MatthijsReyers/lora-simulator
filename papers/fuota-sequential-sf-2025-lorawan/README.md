# The sequential spreading factor scheme on the LoRaWAN stack

The FUOTA scheme of Borkotoky (2025), see `papers/fuota-sequential-sf-2025`, as a campaign on
the simulator's LoRaWAN stack instead of on bare radios. The reproduction next door follows the
paper: frames are the fragment and nothing else, every device knows the schedule from the
start, and what the protocol around it costs is a constant (`E_C`, 60 s of receiving). Here
that part is real: the frames are LoRaWAN multicast frames, the devices are set up with unicast
commands in the RX1 windows of their own uplinks, and the energy is what the radio of a device
spent.

| File | Contents |
| :--- | :------- |
| `ladder.py` | The schedule (rounds, frame slots) and the application package that hands it to a device: `LadderSessionReq` / `Ans`, with its server and device side. |
| `campaign.py` | `SequentialSfCampaign`: `FuotaCampaign` with the session setup and the broadcast replaced. Also the two rules LoRaWAN imposes on the data block (fragment size, padding). |
| `recipient.py` | `LadderDevice`, a `LoRaWanDevice` that can run a session over the ladder, and the device stack that acknowledges the image right away. |
| `scenario.py` | The parameters: scheme, firmware, devices, the paper's channel model. |
| `main.py` | One session → `data/*_nodes.csv` and `data/*_summary.csv`. |

## Running

From the repository root:

```bash
pipenv run python papers/fuota-sequential-sf-2025-lorawan/main.py --nodes 9 --radius 900 --no-fading --uplink-sf auto --uplink-interval 60 --max-hours 20
```

`main.py --help` lists the parameters. The scheme and channel parameters are those of the
reproduction (`-L`, `-M`, `-w`, `--firmware-kb`, `--duty-cycle`, `--exponent`,
`--reference-loss`, `--no-fading`, `--sensitivity`, `--shadowing`, `--frame-loss`,
`--layout-file`); new are `--listen`, `--uplink-interval`, `--uplink-sf`, `--setup-attempts`,
`--block-ack-delay` and a `--fragment-size` that defaults to the largest LoRaWAN allows.

`--max-hours` is how long the gateway transmits before it gives up, and it also sets the
length of the simulation, which cannot end early. An idle gateway costs about 2 s of wall time
per simulated hour, so give a bound that fits the scenario: without one the run covers the
whole ladder up to the last fragment index, hundreds of hours.

`*_nodes.csv` has per recipient its distance, uplink SF, whether and when it held the verified
image (`completed_at`, and `delivery_time` counted from the first frame), the SF of the round
it completed in, whether the server got its acknowledgement, the fragments it received, the
receive windows it opened, its uplinks, and the `cmp_*` columns of the protocol comparison
(time and energy per radio state from `started_at`, the uplink that brought it its first
FUOTA command, until it held the image).

## How the scheme maps onto LoRaWAN

| The paper | Here |
| :-- | :-- |
| Rateless coded fragments | TS004 `DataFragment`s: fragment N ≤ NbFrag is the N-th piece of the image, every later one a random XOR of half of them (TS004 Annex A). |
| A frame is a fragment | A multicast downlink of a TS005 group: 13 octets of LoRaWAN (MHDR, DevAddr, FCtrl, FCnt, FPort, MIC) and 3 of TS004 on top of the fragment, encrypted and signed with the group's keys. |
| The SF sequence is known a priori | `McGroupSetupReq` (TS005) for the group and its keys, then `LadderSessionReq`: start time and per round the data rate, the number of frames and their period. |
| Devices are synchronised | TS003 clock synchronisation on the first uplink. The simulator's clocks do not drift. |
| A device receives a frame or its preamble | `--listen slotted`: a receive window per frame slot, opened like an RX1 window (a guard time early, closed after a few symbols without a preamble). `--listen continuous`: Class C for the session, retuned per round. |
| A device stops when it has decoded | The session ends for a device when its TS004 data block is complete. |
| It acknowledges, the gateway stops when all have | `FragDataBlockReceivedReq` (TS004 §3.5) as a normal uplink right after the session, spread over the `BlockAckDelay` window and repeated until the server answers. The gateway stops at the first frame slot after the last one arrived. |
| Control overhead `E_C` | Whatever the above costs the radio. |

The campaign runs GROUP_SETUP, SESSION_SETUP, FRAG_SETUP, BROADCAST and CLEANUP. The three
setup phases and the cleanup are those of `FuotaCampaign`; there is no status or repair round,
because the ladder is the repair. As there, a device that does not answer a setup phase in time
is left out and the campaign carries on with the others.

## What LoRaWAN imposes on the scheme

1. **The last spreading factor of the ladder decides the fragment size.** A fragmentation
   session has one `FragSize`, and it has to fit a frame of every round. The EU868 payload
   limits leave 48 octets per fragment once SF10 is in the ladder, 112 with SF9 and 219 for
   SF7 and SF8 alone. The paper's 50-octet fragments are close to the first of these, the
   250 octets the protocol comparison gave the bare-radio version are not possible at any
   data rate. With 48-octet fragments a quarter of every frame is header, and a 100 kB image
   takes 2086 frames instead of 458.
2. **TS004's code is not rateless for every image.** A coded fragment is the XOR of
   `NbFrag // 2` pieces. When that number is even, coded fragments alone never reach full
   rank and a device needs at least one uncoded fragment, all of which are sent in the first
   round. A device out of reach of the first spreading factor would then never complete.
   `pad_for_coded_only_decoding` appends up to two zero fragments to the data block to make
   the number odd; the device gets the image with that padding behind it. The far devices of
   the example above complete from 107 to 109 coded fragments of a 106-fragment block.
3. **The setup is paced by the devices' uplinks.** A command reaches a device in the RX1
   window of an uplink, so every setup phase takes an uplink interval or more, and the first
   frame is sent `session_lead_time` after the session request (sized by `--uplink-interval`
   and `--setup-attempts` with the rules of `examples/lorawan_fuota`).
4. **Unicast replies take frame slots.** The gateway's transmitter and its duty cycle are
   shared with the RX1 replies. A slot the duty cycle does not allow is left empty
   (`slots_skipped`): a late frame is of no use to a device that listens for the slot only.
5. **A device needs an uplink that works.** Every device has an uplink SF (`--uplink-sf`,
   `auto`: the lowest that gets 90% of its frames across), and the gateway listens for all of
   them and answers each in kind. With the paper's channel (Rayleigh fading, 1 km) the SF12
   link at the edge delivers about a quarter of the frames in either direction, so a command
   and its answer take many uplinks there and the default timeouts drop those devices. The
   scheme itself copes with such a link, the signalling around it does not.

## Simulator choices

- `LadderSessionReq` is this repository's own command, on FPort 190. TS005 cannot express
  the schedule: a session has one data rate and a power-of-two length, and a device holds four
  groups at most.
- The LoRaWAN layer has one channel. Uplinks, RX1 replies and the multicast frames share
  868.1 MHz and one duty cycle budget.
- The devices keep sending their periodic uplinks until the session starts and after it, not
  during it.
- Clocks do not drift, so the 5 ms guard of a receive window is enough for a session of any
  length. A real device would have to resynchronise or widen its windows.
- The interference models of the reproduction are not part of this version, and neither are
  the FSF and GB benchmarks. A ladder of one SF (`-L 11 -M 11`) is the FSF scheme.

## Results

**Every device within reach of SF7** (the deployment of `papers/fuota-comparison`: 10 devices
within 400 m, 100 kB image, no fading, an uplink per hour, energy with the EWS sensor's power
levels from the uplink that brings a device its first setup command until it holds the image):

| | Energy per device | Receiver on | Fleet updated after |
| :-- | --: | --: | --: |
| Bare radios, 250 B fragments, the paper's energy model | 5.64 J | 157 s | 4.4 h |
| LoRaWAN, ladder SF7-8, 219 B fragments | 6.40 J | 172 s | 13.6 h |
| LoRaWAN, ladder SF7-12, 48 B fragments | 9.47 J | 257 s | 15.8 h |
| LoRaWAN, ladder SF7-12, Class C during the session | 876 J | 6.9 h | 15.8 h |

With 5% of all frames lost the three LoRaWAN rows become 6.80 J, 10.0 J and 925 J, and every
device still completes in the SF7 round. The first frame goes out 8.9 h after the start (10.8 h
with loss): one uplink interval for the group setup and the eight hours of lead time that three
attempts per setup command ask for.

**Devices out to the reach of SF12** (the command under *Running*: 9 devices at 100 to 900 m,
5 kB image, no fading, an uplink per minute): all nine complete, each in the round of the
first spreading factor that reaches it (SF7 up to 200 m, SF12 from 800 m), the far ones from
coded fragments alone. Energy per device runs from 0.57 J at SF7 to 23.2 J at SF12, delivery
time from 22 minutes to 16.3 h.
