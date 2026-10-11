# Balancing the Energy Consumption and Latency of OTA Firmware Updates in LoRaWAN (2025)

Reproduction of the numerical evaluation in:

> S. S. Borkotoky, *Balancing the Energy Consumption and Latency of Over-the-Air Firmware
> Updates in LoRaWAN*, IEEE Transactions on Industrial Informatics 21(10), pp. 7403-7411,
> October 2025. https://doi.org/10.1109/TII.2025.3563539

The paper proposes a FUOTA multicast scheme in which the gateway sends rateless coded
fragments with the spreading factors in sequence (`w` frames at SF `L`, `w` at `L+1`, ..., then
SF `M` until every recipient has decoded) and compares its energy consumption (EC) and delivery
time (DT) with fixed-SF multicast (FSF) and the group-based scheme of Mao et al. (GB-E, GB-L),
by MATLAB simulation and by an analytical model. Both are reproduced here on the simulator.

| File | Contents |
| :--- | :------- |
| `scenario.py` | `Scenario`: every parameter of Sections IV and VII with the paper's defaults, the borrowed sensitivity and SIR tables, node and interferer placement, the channel model and the airtime helpers. Node layouts are written to and can be read from CSV so other reproductions can run on identical positions. |
| `session.py` | The SF schedule (frame `n` → SF, round switch times), the group bookkeeping of the GB schemes and the "all recipients done" flag. |
| `gateway.py` | The gateway: coded fragments at the maximum duty cycle, cycling through the SFs, at one SF, or group by group. |
| `recipient.py` | A recipient: follows the SF schedule, accounts every FUOTA frame per the paper's energy model (hooked into the radio so lost frames are counted too), decodes, acknowledges. |
| `coding.py` | The ideal and raptor decoding models (Section VI, Appendix A). |
| `interference.py` | The statistical interference model: a Monte Carlo draw of the paper's interferer model per received frame (see *Interference* below). |
| `interferers.py` | Real interferer radios for the `simulated` interference mode. |
| `analytical.py` | Sections V, Appendices A and B: `predict()` gives E(d0) and T(d0) for the proposed and FSF schemes, `group_sf()` the SF assignment of GB-E/GB-L (equations 19 and 20). |
| `paper_values.py` | Tables I-III verbatim and the Figure 2 and 3 curves read off the paper's plots. |
| `main.py` | One session (one scheme, one seed) → `data/*_nodes.csv` and `data/*_summary.csv`. |
| `run.sh` | The batteries for Figure 2 / Tables I-II, Table III and Figure 3, run in parallel. |
| `graphs.py` | Figures 2 and 3 with the paper's curves overlaid, a reduced Figure 2 with only the proposed scheme (paper next to simulation, `fig2_proposed.png`), and Tables I-III next to the paper's numbers, into `graphs/`. |

## Running

From the repository root:

```bash
pipenv run python papers/fuota-sequential-sf-2025/main.py --scheme proposed --seed 1 --analytical
pipenv run python papers/fuota-sequential-sf-2025/main.py --scheme fsf --sf 11 --interference simulated
REPEATS=20 JOBS=10 ./papers/fuota-sequential-sf-2025/run.sh fig2 tables fig3
pipenv run python papers/fuota-sequential-sf-2025/graphs.py
```

`main.py --help` lists the parameters; the defaults are those of Section VII (100 recipients
within 1 km, 10 kB image in 200 fragments of 50 B, 1% duty cycle, 14 dBm, α = 2.5, Rayleigh
fading, 5·10⁻⁵ interferers/m² sending a 5 B frame every 10 min on one of 8 channels with a
random SF, L = 7, M = 12, w = 300). A 100-node session takes 5-20 s of wall time in the default
`model` interference mode.

Every run writes two files to `data/`:

- `*_nodes.csv`: per recipient its distance, assigned SF (GB), whether and when it decoded
  (`delivery_time`, seconds after the first frame), the normalised energy with and without the
  control overhead (`ec`, `ec_without_control`), the receiver-on time of the paper's model
  (`rx_time`), the simulator's own measured radio energy at the moment of decoding
  (`radio_energy_j`, Nucleo-WL55 profile, receiver on throughout), and the reception
  statistics: attempts, acquired preambles, received fragments and the losses by cause.
  With `--analytical`, the analytical prediction per node.
- `*_summary.csv`: the scenario parameters and the means over the recipients.

The node layout of a run is written to `data/layout_*.csv`; `--layout-file` replays it.

## How the paper maps onto the simulator

**Gateway and recipients.** The gateway is a `LoraClientRadio` at the origin that transmits
50-byte frames at the SF given by the schedule, each followed by 99 times its airtime of
silence (equation 16). Recipients keep their receiver on the gateway's channel for the whole
session (Class C); for the proposed scheme they retune to the next SF 1 ms before the first
frame of each round, which they can compute from the parameters since the gateway's timing is
deterministic. GB recipients are tuned to their assigned SF from the start and the gateway
serves the groups lowest SF first, moving on when the last member has decoded; the gateway
learns this directly from the session state (the recipients do transmit their
acknowledgement, but it does not have to be received).

**Channel.** `γ0 pt A d^-α` is a power law path loss `reference_loss_db + 10 α log10(d)` with
Rayleigh fading on top (the simulator's Nakagami model with m = 1, drawn independently per
frame and link). Sensitivities are the Georgiou & Raza values (-123 ... -137 dBm), applied
through the simulator's per-SF sensitivity override. Airtimes follow equation (3) with an
explicit header and CRC, no low data rate optimisation (see *Gaps*).

**Energy model.** The paper only counts receiver time spent on FUOTA frames: a frame whose
preamble was acquired costs the frame duration, one whose preamble was not acquired (too weak
or hit during the preamble) only the preamble duration (equation 12), plus `E_C = p_r l_c +
p_t l_a` with `l_c = 60 s` and the acknowledgement. `recipient.py` reproduces this from the
per-frame metadata the radio hands it (`received_preamble`, `demodulate_failure`,
`collision`), so the normalised EC is independent of the simulator's power profile. The
simulator's measured energy is recorded alongside but is dominated by idle listening, since
the recipients do not sleep between frames.

**Decoding.** The proposed scheme uses the raptor model of Appendix A (the attempt upon the
k-th fragment fails with probability 0.85, later ones with 0.567), the benchmarks the ideal
code, as the paper states; `--code` overrides this.

**Interference.** The interferers form a Poisson point process over a disc of radius
`R0 + R_I` around the gateway, where `R_I` is the paper's interference radius of equation (5)
(1.6 km here, beyond which an interferer's frame exceeds the SF 12 sensitivity with
probability under 1%); that realisation is drawn per seed. Two modes:

- `--interference model` (default): no interferer transmissions are simulated. For every
  FUOTA frame the simulator delivers, the recipient draws the number of interfering frames
  overlapping it (Poisson, per SF, on its channel, from its interferers within `R_I`), a start
  time for each, the interferer's distance and fading, and applies the dominant-interferer
  SIR matrix ξ of Mahmood et al.; a destructive frame over the preamble fails the acquisition,
  one over the payload loses the frame. This is exactly the model the paper's analysis
  integrates over and what its MATLAB simulation most plausibly did.
- `--interference simulated`: ~1500 interferer radios really transmit through the PHY layer.
  Whether an overlap destroys a FUOTA frame is then decided by the simulator's own capture
  model (same-SF frames need 6 dB, other SFs the SF's SNR requirement, the 2017 measurement
  based preamble rule), not by the paper's SIR matrix, so this mode is a different collision
  model rather than a second implementation of the same one. It is also two orders of
  magnitude slower, see *Results*.

## Gaps in the paper and how they were filled

1. **Link budget constant.** The paper defines `γ0 = gt gr λ / 4π`, which at 868 MHz is a
   15.6 dB loss at 1 m (the usual Friis `(λ/4π)²` would be 31.2 dB). Either value puts the
   mean received power at the 1 km edge between -77 and -92 dBm, far above every sensitivity,
   so every SF would reach every node, contradicting the paper's own statement that SF 7-9
   "cannot reach the farthest recipient" and the shape of Figure 2. Fitting `analytical.py` to
   Tables I and II gives a reference loss of **77.0 dB** (mean received power -138.0 dBm at
   1 km), which reproduces Table II to within 0.3 normalised units and 0.7 h for every scheme.
   `--reference-loss` changes it; `scenario.PAPER_LITERAL_REFERENCE_LOSS_DB` holds the literal
   value.
2. **Energy normalisation.** The text normalises by `k p_r l_f(7)`, the SF 7 *frame* airtime
   (0.1027 s). The numbers in Figure 2 and Table II only come out if the normalisation is the
   SF 7 *payload* airtime without the preamble (0.0902 s): that gives 4.5 at the gateway for
   the proposed scheme (0.1027 s would give 3.9), 16.1 and 27.0 for FSF-11/12 (vs 14.2 and
   23.7), and Table II's 11.6 and 26.5. `--normalisation frame` uses the literal definition.
3. **Table II and III exclude `E_C`.** Averaging the Figure 2 curves over distance gives
   values 3.3 above Table II for every scheme, and 3.3 is exactly `l_c / (k l_7^(pl))`. So the
   tables report `E(d0)` alone while the figure includes the control overhead;
   `ec_without_control` is the table quantity.
4. **Equation (11)** divides by `1 - S^(fr)_{m0}`; the expected number of attempts needed to
   collect the remaining fragments is the remainder divided by `S^(fr)_{m0}`, which is what
   `analytical.py` uses (and what matches the paper's own analytical curve).
5. **Equation (12)** mixes the conditional payload failure probability `F^(pl)` with the
   unconditional others, so its three probabilities do not sum to one. `analytical.py` uses
   the consistent unconditional form `S^(fr) e^(fr) + (S^(pr) - S^(fr)) e^(fr) + (1 - S^(pr))
   e^(pr)`, which is also what the simulation measures.
6. **Equations (19) and (20)** are identical up to a constant if `e_i^(fr)` in (19) is the
   full-frame energy, yet GB-E and GB-L differ in Figure 2. Reading (19) with the mean energy
   per attempt of (12) (failed acquisitions only cost the preamble) makes GB-E prefer lower SFs
   than GB-L, which reproduces the figure. The assignment uses the interference-aware
   `S^(fr)_i(d0)` with the mean number of interferers.
7. **Low data rate optimisation** (`y` in equation 3) is not specified. FSF-12's delivery time
   near the gateway (≈12.5 h) fits `y = 0` (12.0 h without interference; 13.7 h with LDRO on),
   so it is off. The simulator's radios never enable it either.
8. **Acknowledgement.** Its size and SF are not given. It is a 5-byte frame at the uplink SF
   the recipient would use for its own traffic, chosen as the SF with the lowest expected
   airtime per delivered uplink, which yields the SF 7 and SF 12 of Table I at R0/4 and R0.
   `p_t / p_r` is 83/38 from the battery lifetime section. Its share of the EC is below 0.15.
9. **Node placement for Figure 2 and the "averages over node distances".** Section VII says
   uniform over the disc, but Table II is a distance-uniform average of the Figure 2 curves
   (an area-uniform one would weigh the far nodes and come out around 21 instead of 11.6).
   The default layout therefore places the 100 recipients at distances `R0 n/100` with random
   angles; `--layout uniform` gives the literal placement. Positions vary per seed and
   Figure 2 is drawn from 50 m distance bins.
10. **Interferers over an infinite plane** are truncated at `R0 + R_I`, using the paper's own
    argument that devices beyond `R_I` are negligible.
11. **"Any overlap is a loss"**: the losses the paper shows near the gateway (FSF-11's 7.2 h
    against 6.4 h without any loss) looked like every overlap might count as a collision.
    `--collision-rule any_overlap` implements that hypothesis for the model mode; it fits
    Tables I and II clearly worse than the SIR matrix (FSF-12's average EC becomes 32 instead
    of 26.5), so the SIR rule stays the default and the near-gateway discrepancy is left as a
    residual.
12. **Group completion** in GB is known to the gateway instantly (ideal acknowledgements), and
    the session cap (`--max-hours`, 500 h) only exists to bound pathological runs.
13. **The paper's interference losses are larger than its model gives.** With the SIR matrix,
    Rayleigh fading and the stated densities, an interferer must be within a few metres of a
    recipient near the gateway to destroy an SF 11 frame, so the model (analytically and in
    both simulation modes) loses well under 1% of the frames there, while Figure 2 implies
    5-10% (FSF-11 at 7.2 h instead of 6.4-6.6 h) and Table III reacts 2-4 times as strongly to
    the interferer density. Hypotheses that were tried and do not fit: counting every overlap
    as a loss (gap 11, far too pessimistic for SF 12), and the literal `γ0`. Not tried:
    interferers with longer frames (LDRO on), no channel split (`n_f = 1` in the collision
    probability, 8 times the collisions), or a collision criterion on mean rather than faded
    powers. `--collision-rule`, `--interferer-density` and `--interferer-period` are the knobs
    for exploring this.

## Results

All numbers below are from `REPEATS=20` (20 seeds per configuration against the paper's 100
runs) in the `model` interference mode; `graphs/tables.md` is regenerated by `graphs.py`.

**Figure 2** (`graphs/fig2.png`, solid: this reproduction, dotted: the paper's curves): every
scheme follows the paper's curve over the whole range in both panels. The analytical squares
sit on the simulated proposed curve just like in the paper. The visible differences are at the
edge, where the paper's FSF-11 is a little slower (61.9 h vs 58.7 h) and its GB-E a little
faster (137.7 h vs 141 h) than here, and near the gateway, where the paper shows 5-10% more
frame loss for SF 11/12 than its own interference model yields (FSF-11 at 7.2 h where the
noise-free value is 6.4 h and ours is 6.6 h).

**Table II**, averaged over the 100 node distances (EC without `E_C` / DT in hours):

| | Proposed | FSF-10 | FSF-11 | FSF-12 | GB-E | GB-L |
| :-- | --: | --: | --: | --: | --: | --: |
| This reproduction | 11.9 / 15.4 | 13.7 / 24.5 | 16.1 / 15.8 | 26.3 / 18.6 | 8.9 / 40.4 | 11.2 / 28.0 |
| Paper | 11.6 / 15.3 | 13.4 / 24.2 | 16.3 / 17.0 | 26.5 / 19.5 | 8.7 / 36.4 | 10.7 / 28.3 |

The ranking and the trade-off the paper argues for come out the same: the proposed scheme has
the lowest delivery time of all and an energy consumption between the FSF and GB schemes.

**Table I**, delivery times at the edge and at R0/4 (hours):

| | Prop. edge | FSF-11 edge | GB-E edge | Prop. R0/4 | FSF-11 R0/4 | GB-E R0/4 |
| :-- | --: | --: | --: | --: | --: | --: |
| This reproduction | 54.0 | 58.7 | 141.1 | 1.6 | 6.8 | 1.6 |
| Paper | 54.5 | 61.9 | 137.7 | 1.44 | 7.18 | 1.41 |

The battery lifetimes of Table I could **not** be reproduced from equation (21) with the
stated parameters (1200 mAh, 83/38 mA, 45 µA, a 50-byte uplink every 30 min at SF 12 or SF 7,
one update per month). The uplink transmissions alone cost 863 mAh per year at SF 12 (10.4 h
of airtime at 83 mA), which caps the lifetime at 1.39 years before any reception or sleep
current, yet the paper reports 1.42-1.47 years at the edge; at R0/4 the same formula gives
2.5-2.7 years where the paper has 1.66-1.82. `graphs.py` prints equation (21) as written
(0.89 / 0.90 / 0.90 and 2.68 / 2.54 / 2.69 years); the relative ordering of the three schemes
matches the paper, the absolute values do not.

**Table III**, other interferer densities (EC without `E_C` / DT in hours):

| λ_I | Prop. | FSF-11 | GB-L | Prop. paper | FSF-11 paper | GB-L paper |
| :-- | --: | --: | --: | --: | --: | --: |
| 0.0005 | 12.4 / 16.1 | 16.4 / 16.3 | 11.3 / 28.8 | 13.69 / 18.85 | 18.02 / 21.78 | 12.78 / 32.17 |
| 0.001 | 13.1 / 16.9 | 16.7 / 16.8 | 11.4 / 29.6 | 14.50 / 20.04 | 18.45 / 22.57 | 12.90 / 34.21 |
| 0.002 | 14.5 / 18.9 | 17.5 / 18.0 | 11.4 / 30.0 | 16.35 / 22.83 | 19.33 / 24.24 | 13.51 / 36.02 |

The trends agree (every scheme gets worse with density, the proposed scheme stays fastest and
GB-L cheapest) but the paper is 2-4 times as sensitive to the interferers as the model
implemented here: going from 5·10⁻⁵ to 5·10⁻⁴ devices/m² costs the proposed scheme 2.1
normalised units and 3.5 h in the paper against 0.5 and 0.7 h here. This is the same residual
as the extra near-gateway losses in Figure 2 and the one open point of this reproduction, see
gap 13.

**Figure 3** (`graphs/fig3.png`): the energy curves reproduce the paper's shape, levels and
ordering (lower L is cheaper for every w, the curves flatten beyond w ≈ 400 at 10.7-11.3 for
L ≤ 9). The delivery time curves are convex with their minima at the same w (100-200 for
L = 7-9, 250-350 for L = 10) and the same 14.2-14.5 h floor; the L = 11 curve is the one that
differs, with its minimum at w ≈ 400 and 15.2 h here against w ≈ 500 and 16.0 h in the paper,
and the rise beyond the minimum is a little steeper here for L = 7-9 (21.9 h vs 21.8 h at
w = 1000 for L = 7, but 18.6 h vs 18.1 h for L = 9).

**Interference modes.** On a 20-recipient session (seed 1) the `simulated` mode with 1170
real interferer radios and 404k interfering frames gave a mean EC of 16.5 and DT of 17.3 h
against 16.1 and 16.8 h for the `model` mode, with the simulator's capture model losing about
1% of the FUOTA frames (nearly all during the preamble) where the paper's SIR matrix loses
0.1%. It took 497 s of wall time against 2 s, so the batteries use the `model` mode;
`INTERFERENCE=simulated ./run.sh fig2` reruns Figure 2 the slow way.

