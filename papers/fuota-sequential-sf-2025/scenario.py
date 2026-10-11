"""
Every parameter of the system model and the numerical evaluation of the paper (Sections IV
and VII), the two tables it borrows from other papers, the placement of the recipients and the
channel model. Nothing else in this reproduction hard-codes a number: the simulation, the
analytical model and the plots all read from one `Scenario`, so the same scenario can later be
handed to the reproductions of other papers for a like-for-like comparison.
"""
from __future__ import annotations

import csv
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Literal

from simulator.lora.airtime import estimate_airtime, preamble_airtime
from simulator.lora.phy_layer import LoraPhyLayer
from simulator.path_loss.nakagami_path_loss import nakagami_path_loss

SPREADING_FACTORS = tuple(range(7, 13))

# Receiver sensitivities in dBm at 125 kHz, Table I of Georgiou & Raza, "Low power wide area
# network analysis: Can LoRa scale?" (IEEE WCL 2017), the paper's reference [3].
GEORGIOU_RAZA_SENSITIVITY = {7: -123.0, 8: -126.0, 9: -129.0, 10: -132.0, 11: -134.5, 12: -137.0}

# Capture (SIR) thresholds ξ_{i,j} in dB: a frame with SF i (row) survives a concurrent frame with
# SF j (column) when it is received at least this many dB stronger. Matrix (1) of Mahmood et al.,
# "Scalability analysis of a LoRa network under imperfect orthogonality" (IEEE TII 2019), the
# paper's reference [14], which in turn takes it from Croce et al. (IEEE Comm. Letters 2018).
MAHMOOD_SIR_THRESHOLDS = {
    7:  {7: 1, 8: -8, 9: -9, 10: -9, 11: -9, 12: -9},
    8:  {7: -11, 8: 1, 9: -11, 10: -12, 11: -13, 12: -13},
    9:  {7: -15, 8: -13, 9: 1, 10: -13, 11: -14, 12: -15},
    10: {7: -19, 8: -18, 9: -17, 10: 1, 11: -17, 12: -18},
    11: {7: -22, 8: -22, 9: -21, 10: -20, 11: 1, 12: -20},
    12: {7: -25, 8: -25, 9: -25, 10: -24, 11: -23, 12: 1},
}

# The eight 125 kHz EU868 uplink channels, in hertz. The paper only says "eight 125-kHz
# channels"; which frequencies they sit on does not affect the results.
EU868_CHANNELS = tuple(867_100_000 + 200_000 * i for i in range(8))

# The paper writes the received power as γ0 pt A d^-α with γ0 = gt gr λ / 4π, which at 868 MHz
# and unity antenna gains is a reference loss of 15.6 dB (at 1 m). That value (or the usual
# free space (λ/4π)², 31.2 dB) puts the mean received power at the 1 km edge around -77 to
# -92 dBm, far above every sensitivity, which contradicts the paper's own results (SF 7-9 "cannot
# reach the farthest recipient", Figure 2). Fitting the analytical model to Tables I and II
# gives a reference loss of 77.0 dB (a mean received power of -138.0 dBm at 1 km). See the
# README; `Scenario.reference_loss_db` defaults to the fitted value.
PAPER_LITERAL_REFERENCE_LOSS_DB = -10 * math.log10((299_792_458 / 868e6) / (4 * math.pi))
CALIBRATED_REFERENCE_LOSS_DB = 77.0

Scheme = Literal["proposed", "fsf", "gb_e", "gb_l"]
# How an overlap with an interfering frame is resolved in "model" mode (and in the analytical
# model): "sir" applies the paper's dominant-interferer SIR matrix, "any_overlap" counts every
# overlapping frame on the same channel as a loss (see the README for why this exists).
CollisionRule = Literal["sir", "any_overlap"]
# What the energy is normalised by: k times the SF 7 frame airtime (as the paper says) or k
# times the SF 7 payload airtime without the preamble (what reproduces the paper's numbers).
Normalisation = Literal["frame", "payload"]
Code = Literal["ideal", "raptor"]
Interference = Literal["simulated", "model", "none"]
Layout = Literal["radial", "uniform"]


@dataclass
class Scenario:
    """ Parameters of the paper's evaluation (Section VII), with its defaults. """

    # ── Transmission scheme (Sections III and VI) ──────────────────────────────────────────
    scheme: Scheme = "proposed"
    first_sf: int = 7                   # L, proposed scheme
    last_sf: int = 12                   # M, proposed scheme
    transmissions_per_sf: int = 300     # w, proposed scheme
    fixed_sf: int = 11                  # the SF of the FSF benchmark
    # Decoding model: the paper assumes the raptor code of Appendix A for the proposed scheme and
    # an ideal code (any k fragments suffice) for the benchmarks. None picks that per scheme.
    code: Code | None = None
    raptor_fail_first: float = 0.85     # P̂f(k), Appendix A
    raptor_fail_next: float = 0.567     # P̂f(l) for l > k

    # ── Firmware and frames ───────────────────────────────────────────────────────────────
    firmware_size: int = 10_000         # bytes, "10 kB"
    fragment_size: int = 50             # b_k, bytes per fragment = LoRa payload per frame
    bandwidth: int = 125                # kHz
    code_rate: int = 5                  # 4/5, c = 1 in equation (3)
    preamble_len: int = 8               # n_pr
    tx_power: int = 14                  # dBm, gateway and interferers alike
    duty_cycle_percent: float = 1.0     # DC_max of the gateway

    # ── Recipients ────────────────────────────────────────────────────────────────────────
    nodes: int = 100
    radius: float = 1000.0              # R0, meters
    # "radial": node n sits at distance R0 (n+1)/N with a random angle, which samples every
    # distance equally (what Figure 2 and the "averaged over node distances" tables need).
    # "uniform": uniform over the disc, as the system model literally says.
    layout: Layout = "radial"

    # ── Channel (Section IV) ──────────────────────────────────────────────────────────────
    path_loss_exponent: float = 2.5     # α
    reference_loss_db: float = CALIBRATED_REFERENCE_LOSS_DB
    rayleigh_fading: bool = True        # block fading, i.i.d. per frame and link
    sensitivity: dict[int, float] = field(default_factory=lambda: dict(GEORGIOU_RAZA_SENSITIVITY))
    sir_thresholds: dict[int, dict[int, float]] = field(
        default_factory=lambda: {i: dict(row) for (i, row) in MAHMOOD_SIR_THRESHOLDS.items()}
    )
    channels: int = 8                   # n_f, equally likely for an interferer
    gateway_channel: int = 5            # index into EU868_CHANNELS (868.1 MHz)

    # ── Interferers (Section IV) ──────────────────────────────────────────────────────────
    interference: Interference = "model"
    interferer_density: float = 5e-5    # λ_I, devices per m²
    interferer_frame_rate: float = 1 / 600  # λ_f, frames per second per interferer
    interferer_payload: int = 5         # bytes
    interference_delta: float = 0.01    # δ of equation (5), defines the interference radius
    # In "simulated" mode the simulator's own collision model decides who survives an overlap,
    # in "model" mode `collision_rule` does. `capture_effect` only matters in simulated mode.
    capture_effect: bool = True
    collision_rule: CollisionRule = "sir"
    normalisation: Normalisation = "payload"

    # ── Control overhead E_C (Section V) ──────────────────────────────────────────────────
    control_airtime: float = 60.0       # l_c, seconds of downlink control frames received
    ack_payload: int = 5                # bytes of the acknowledgement (not given in the paper)
    tx_rx_power_ratio: float = 83 / 38  # p_t / p_r, the 83 mA and 38 mA of Section VII

    seed: int = 0

    def __post_init__(self) -> None:
        assert 7 <= self.first_sf <= self.last_sf <= 12, "SF set must satisfy 7 <= L <= M <= 12"
        assert self.transmissions_per_sf > 0
        assert self.fixed_sf in SPREADING_FACTORS
        assert 0 <= self.gateway_channel < len(EU868_CHANNELS)
        assert self.firmware_size % self.fragment_size == 0, (
            "The paper's image is an integer number of fragments; zero padding is not modelled"
        )

    # ── Derived quantities ────────────────────────────────────────────────────────────────

    @property
    def fragments(self) -> int:
        """ k, the number of source fragments. """
        return self.firmware_size // self.fragment_size

    @property
    def effective_code(self) -> Code:
        if self.code is not None:
            return self.code
        return "raptor" if self.scheme == "proposed" else "ideal"

    @property
    def gateway_frequency(self) -> int:
        return EU868_CHANNELS[self.gateway_channel]

    def airtime(self, sf: int, payload: int) -> float:
        """
            l_i(b) of equation (1): explicit header, CRC on, no low data rate optimisation (y = 0;
            Figure 2's FSF-12 delivery time near the gateway only fits without it, and the
            simulator's radios never use it either).
        """
        return estimate_airtime(
            payload_len=payload, bandwidth=self.bandwidth, spreading_factor=sf,
            code_rate=self.code_rate, preamble_len=self.preamble_len,
        )

    def frame_airtime(self, sf: int) -> float:
        """ l_i^(fr)(b_k), the airtime of a FUOTA frame. """
        return self.airtime(sf, self.fragment_size)

    def preamble_airtime(self, sf: int) -> float:
        """ l_i^(pr) of equation (2). """
        return preamble_airtime(bandwidth=self.bandwidth, spreading_factor=sf, preamble_len=self.preamble_len)

    def interferer_airtime(self, sf: int) -> float:
        """ l̄_j, the airtime of an interfering frame. """
        return self.airtime(sf, self.interferer_payload)

    def ack_airtime(self, sf: int) -> float:
        """ l_a, the airtime of the acknowledgement. """
        return self.airtime(sf, self.ack_payload)

    def frame_period(self, sf: int) -> float:
        """ Time between the starts of two FUOTA frames with this SF at the maximum duty cycle. """
        return self.frame_airtime(sf) * 100.0 / self.duty_cycle_percent

    def payload_airtime(self, sf: int) -> float:
        """ l_i^(pl)(b_k) of equation (3), the frame without its preamble. """
        return self.frame_airtime(sf) - self.preamble_airtime(sf)

    def energy_normalisation(self) -> float:
        """
            The receiver-on time the energy values are divided by: "the energy needed to receive
            the file using SF 7 over an erasure-free link", k l_f(7), or k l^(pl)_7 (see
            `Normalisation`).
        """
        per_frame = self.frame_airtime(7) if self.normalisation == "frame" else self.payload_airtime(7)
        return self.fragments * per_frame

    def capture_threshold_db(self, desired_sf: int, interferer_sf: int) -> float:
        return self.sir_thresholds[desired_sf][interferer_sf]

    def interferer_sf_probability(self, sf: int) -> float:
        """ η_j: every SF is equally likely. """
        return 1.0 / len(SPREADING_FACTORS)

    def mean_received_power_dbm(self, distance: float) -> float:
        """ 10 log10(γ0 pt d^-α): the received power before fading. """
        if distance <= 0:
            return float(self.tx_power)
        return self.tx_power - self.reference_loss_db - 10 * self.path_loss_exponent * math.log10(distance)

    def detection_probability(self, sf: int, distance: float) -> float:
        """ P(A ≥ ζ_i d^α / γ0 pt) = D^(pr) averaged over Rayleigh fading (no interference). """
        margin_db = self.mean_received_power_dbm(distance) - self.sensitivity[sf]
        if not self.rayleigh_fading:
            return 1.0 if margin_db >= 0 else 0.0
        return math.exp(-(10 ** (-margin_db / 10)))

    def interference_radius(self) -> float:
        """
            R_I of equation (5): the distance beyond which an interferer's frame exceeds the SF 12
            sensitivity with probability at most δ.
        """
        gamma0_pt_mw = 10 ** ((self.tx_power - self.reference_loss_db) / 10)
        zeta12_mw = 10 ** (self.sensitivity[12] / 10)
        return (-math.log(self.interference_delta) * gamma0_pt_mw / zeta12_mw) ** (1 / self.path_loss_exponent)

    def uplink_sf_for(self, distance: float) -> int:
        """
            The SF a recipient uses for its own uplinks, which the paper does not define except
            for Table I (SF 12 at the edge, SF 7 at a quarter of the radius). We use the SF with
            the lowest expected transmit time per delivered frame, l_i / D_i(d0), which gives the
            two values of Table I.
        """
        return min(
            SPREADING_FACTORS,
            key=lambda sf: self.frame_airtime(sf) / max(self.detection_probability(sf, distance), 1e-300),
        )

    # ── Placement ─────────────────────────────────────────────────────────────────────────

    def node_positions(self, rng: random.Random) -> list[tuple[float, float]]:
        positions = []
        for n in range(self.nodes):
            if self.layout == "radial":
                distance = self.radius * (n + 1) / self.nodes
            else:
                distance = self.radius * math.sqrt(rng.random())
            angle = rng.uniform(0, 2 * math.pi)
            positions.append((distance * math.cos(angle), distance * math.sin(angle)))
        return positions

    def interferer_positions(self, rng: random.Random) -> list[tuple[float, float]]:
        """
            A realisation of the Poisson point process of interferers over the disc that can
            matter to any recipient: radius R0 + R_I around the gateway (the paper's "infinite
            area" minus the devices it shows are negligible).
        """
        outer = self.radius + self.interference_radius()
        count = poisson(self.interferer_density * math.pi * outer ** 2, rng)
        positions = []
        for _ in range(count):
            distance = outer * math.sqrt(rng.random())
            angle = rng.uniform(0, 2 * math.pi)
            positions.append((distance * math.cos(angle), distance * math.sin(angle)))
        return positions

    # ── Simulator setup ───────────────────────────────────────────────────────────────────

    def path_loss(self) -> Callable[[float, float], float]:
        """ Power law loss `reference_loss_db + 10 α log10(d)` with Rayleigh fading on top. """
        exponent, reference = self.path_loss_exponent, self.reference_loss_db

        def power_law(distance: float, frequency: float) -> float:
            if distance <= 0:
                return 0.0
            return reference + 10 * exponent * math.log10(distance)

        if not self.rayleigh_fading:
            return power_law
        # Nakagami-m with m = 1 is Rayleigh fading: an exponentially distributed power gain.
        return nakagami_path_loss(base=power_law, m0=1.0, m1=1.0, m2=1.0)

    def setup_phy(self) -> LoraPhyLayer:
        phy = LoraPhyLayer(path_loss=self.path_loss(), sensitivity=self.sensitivity)
        phy.enable_capture_effect = self.capture_effect
        return phy


def poisson(mean: float, rng: random.Random) -> int:
    """ A Poisson distributed integer (inversion for small means, normal approximation beyond). """
    if mean > 500:
        return max(0, round(rng.gauss(mean, math.sqrt(mean))))
    threshold = math.exp(-mean)
    count, product = 0, rng.random()
    while product > threshold:
        count += 1
        product *= rng.random()
    return count


def write_layout(path: Path, positions: Iterable[tuple[float, float]]) -> None:
    with open(path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["node", "x", "y"])
        for (n, (x, y)) in enumerate(positions):
            writer.writerow([n, f"{x:.3f}", f"{y:.3f}"])


def read_layout(path: Path) -> list[tuple[float, float]]:
    with open(path, newline="") as handle:
        return [(float(row["x"]), float(row["y"])) for row in csv.DictReader(handle)]
