"""
The parameters of a session: the scheme, the firmware, the devices and the channel. The scheme
and the channel model are those of the paper (Sections III, IV and VII) and of its reproduction
in `papers/fuota-sequential-sf-2025`; what the paper leaves to "the control overhead" is here
the LoRaWAN stack itself, so its parameters are part of the scenario too.
"""
from __future__ import annotations

import csv
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Literal

from simulator.lora.phy_layer import LoraPhyLayer
from simulator.path_loss.nakagami_path_loss import nakagami_path_loss

SPREADING_FACTORS = tuple(range(7, 13))

# Receiver sensitivities in dBm at 125 kHz, Table I of Georgiou & Raza, "Low power wide area
# network analysis: Can LoRa scale?" (IEEE WCL 2017), the paper's reference [3].
GEORGIOU_RAZA_SENSITIVITY = {7: -123.0, 8: -126.0, 9: -129.0, 10: -132.0, 11: -134.5, 12: -137.0}

# The reference loss at 1 m that reproduces the paper's results, see the README of the
# reproduction.
CALIBRATED_REFERENCE_LOSS_DB = 77.0

#: The one channel of the simulator's LoRaWAN layer: uplinks, RX1 and the multicast frames.
FREQUENCY = 868_100_000

#: Share of its uplinks a device should get across with the spreading factor it is given.
UPLINK_RELIABILITY = 0.9

Layout = Literal["radial", "uniform"]
Listen = Literal["slotted", "continuous"]


@dataclass
class Scenario:
    # ── Transmission scheme (Section III) ─────────────────────────────────────────────────
    first_sf: int = 7                   # L
    last_sf: int = 12                   # M
    # w, frames per spreading factor. None: a tenth more than the image has fragments, so
    # that one round can deliver the image over a link that loses a few frames.
    transmissions_per_sf: int | None = None
    duty_cycle_percent: float = 1.0     # DC_max of the gateway
    # Seconds of transmitting after which the gateway gives up. None: until the fragment
    # index of TS004 runs out (16383 frames).
    max_broadcast_time: float | None = None

    # ── Firmware and frames ───────────────────────────────────────────────────────────────
    firmware_size: int = 10_000         # bytes, "10 kB"
    # FragSize of TS004. None: the largest that fits a frame of the last spreading factor.
    fragment_size: int | None = None
    tx_power: int = 14                  # dBm, gateway and devices alike

    # ── Devices ───────────────────────────────────────────────────────────────────────────
    nodes: int = 100
    radius: float = 1000.0              # R0, meters
    layout: Layout = "radial"
    listen: Listen = "slotted"
    # Seconds between a device's own uplinks ("every 30 min" in Section VII). Every unicast
    # command reaches a device in the RX1 window of one of them.
    uplink_interval: float = 1800.0
    # The spreading factor of a device's uplinks and RX1 window. None: per device, by its
    # link budget (`uplink_sf_for`).
    uplink_sf: int | None = 7
    # How often a setup command can be tried on a device; sizes the setup phases.
    setup_attempts: int | None = None
    block_ack_delay: int = 0            # TS004: acknowledgements spread over 2^(4 + this) s

    # ── Channel (Section IV) ──────────────────────────────────────────────────────────────
    path_loss_exponent: float = 2.5     # α
    reference_loss_db: float = CALIBRATED_REFERENCE_LOSS_DB
    rayleigh_fading: bool = True        # block fading, i.i.d. per frame and link
    shadowing_db: float = 0.0           # log-normal shadowing per frame, not in the paper
    sensitivity: dict[int, float] = field(default_factory=lambda: dict(GEORGIOU_RAZA_SENSITIVITY))

    seed: int = 0

    def __post_init__(self) -> None:
        assert 7 <= self.first_sf <= self.last_sf <= 12, "SF set must satisfy 7 <= L <= M <= 12"

    @property
    def spreading_factors(self) -> tuple[int, ...]:
        return tuple(range(self.first_sf, self.last_sf + 1))

    # ── Link budget ───────────────────────────────────────────────────────────────────────

    def mean_received_power_dbm(self, distance: float) -> float:
        """ 10 log10(γ0 pt d^-α): the received power before fading. """
        if distance <= 0:
            return float(self.tx_power)
        return self.tx_power - self.reference_loss_db - 10 * self.path_loss_exponent * math.log10(distance)

    def detection_probability(self, sf: int, distance: float) -> float:
        """ The share of the frames with this SF that arrive above the sensitivity. """
        margin_db = self.mean_received_power_dbm(distance) - self.sensitivity[sf]
        if not self.rayleigh_fading:
            return 1.0 if margin_db >= 0 else 0.0
        return math.exp(-(10 ** (-margin_db / 10)))

    def uplink_sf_for(self, distance: float) -> int:
        """
            The SF of a device's own uplinks and of the RX1 replies to them: the lowest one
            that gets `UPLINK_RELIABILITY` of its frames across, as rate adaptation would pick
            it, or SF12 where none does.
        """
        if self.uplink_sf is not None:
            return self.uplink_sf
        for sf in SPREADING_FACTORS:
            if self.detection_probability(sf, distance) >= UPLINK_RELIABILITY:
                return sf
        return SPREADING_FACTORS[-1]

    # ── Placement ─────────────────────────────────────────────────────────────────────────

    def node_positions(self, rng: random.Random) -> list[tuple[float, float]]:
        """
            "radial": node n sits at distance R0 (n+1)/N with a random angle, which samples
            every distance equally. "uniform": uniform over the disc.
        """
        positions = []
        for n in range(self.nodes):
            if self.layout == "radial":
                distance = self.radius * (n + 1) / self.nodes
            else:
                distance = self.radius * math.sqrt(rng.random())
            angle = rng.uniform(0, 2 * math.pi)
            positions.append((distance * math.cos(angle), distance * math.sin(angle)))
        return positions

    # ── Simulator setup ───────────────────────────────────────────────────────────────────

    def path_loss(self) -> Callable[[float, float], float]:
        """ Power law loss `reference_loss_db + 10 α log10(d)` with Rayleigh fading on top. """
        exponent, reference = self.path_loss_exponent, self.reference_loss_db
        sigma = self.shadowing_db

        def power_law(distance: float, frequency: float) -> float:
            if distance <= 0:
                return 0.0
            shadowing = random.gauss(0.0, sigma) if sigma else 0.0
            return max(0.0, reference + 10 * exponent * math.log10(distance) + shadowing)

        if not self.rayleigh_fading:
            return power_law
        # Nakagami-m with m = 1 is Rayleigh fading: an exponentially distributed power gain.
        return nakagami_path_loss(base=power_law, m0=1.0, m1=1.0, m2=1.0)

    def setup_phy(self, frame_loss: float = 0.0) -> LoraPhyLayer:
        return LoraPhyLayer(
            path_loss=self.path_loss(), sensitivity=self.sensitivity,
            frame_loss=frame_loss, frame_loss_seed=self.seed,
        )


def write_layout(path: Path, positions: Iterable[tuple[float, float]]) -> None:
    with open(path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["node", "x", "y"])
        for (n, (x, y)) in enumerate(positions):
            writer.writerow([n, f"{x:.3f}", f"{y:.3f}"])


def read_layout(path: Path) -> list[tuple[float, float]]:
    with open(path, newline="") as handle:
        return [(float(row["x"]), float(row["y"])) for row in csv.DictReader(handle)]
