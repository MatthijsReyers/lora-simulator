"""
The statistical interference model ("model" interference mode): instead of simulating the
interferers' transmissions, every FUOTA frame that the simulator delivers to a recipient is
subjected to a Monte Carlo draw of the paper's own interference model (Section IV and
Appendix B): Poisson frame arrivals from the interferers within the interference radius, each
with a random SF and channel, Rayleigh fading, and the dominant-interferer SIR criterion.

This is what the analytical model integrates over, so the two agree by construction; the
"simulated" mode is the independent check with real interferer radios on the air.
"""
from __future__ import annotations

import math
import random
from typing import Literal

from scenario import SPREADING_FACTORS, Scenario, poisson

Outcome = Literal["ok", "preamble", "payload"]


class InterferenceModel:
    def __init__(self, scenario: Scenario, interferer_distances: list[float], rng: random.Random):
        """
            :param interferer_distances: distances from this recipient to the interferers within
                its interference radius (one realisation of the point process).
        """
        self.scenario = scenario
        self.distances = interferer_distances
        self.rng = rng
        s = scenario
        # Frames per second on the recipient's channel per SF from all its interferers.
        self.rate_per_sf = {
            j: len(interferer_distances) * s.interferer_frame_rate * s.interferer_sf_probability(j) / s.channels
            for j in SPREADING_FACTORS
        }
        self.interferer_airtime = {j: s.interferer_airtime(j) for j in SPREADING_FACTORS}
        self.mean_power_db = s.tx_power - s.reference_loss_db

    def _interferer_power_dbm(self, distance: float) -> float:
        """ Received power of one interfering frame: γ0 pt A' u^-α with a fresh fading draw. """
        s = self.scenario
        fading = self.rng.expovariate(1.0) if s.rayleigh_fading else 1.0
        fading = max(fading, 1e-300)
        return self.mean_power_db - 10 * s.path_loss_exponent * math.log10(max(distance, 1e-3)) + 10 * math.log10(fading)

    def resolve(self, sf: int, rssi: float, frame_time: float, preamble_time: float) -> Outcome:
        """
            Draws the interfering frames that overlap a FUOTA frame with SF `sf` received at
            `rssi` dBm and decides whether it survives: "preamble" when a destructive frame
            overlaps the preamble (acquisition fails), "payload" when the preamble is clean but
            the payload is hit, "ok" otherwise.
        """
        if not self.distances:
            return "ok"
        s = self.scenario
        payload_hit = False
        for j in SPREADING_FACTORS:
            airtime_j = self.interferer_airtime[j]
            count = poisson(self.rate_per_sf[j] * (frame_time + airtime_j), self.rng)
            for _ in range(count):
                # Start of the interfering frame relative to the start of the FUOTA frame; any
                # start in (-l_j, l_fr) overlaps it.
                start = self.rng.uniform(-airtime_j, frame_time)
                if s.collision_rule == "sir":
                    power = self._interferer_power_dbm(self.rng.choice(self.distances))
                    if rssi - power >= s.capture_threshold_db(sf, j):
                        continue  # the FUOTA frame captures the receiver
                if start < preamble_time:
                    return "preamble"
                payload_hit = True
        return "payload" if payload_hit else "ok"


def distances_within(
    position: tuple[float, float], interferers: list[tuple[float, float]], radius: float
) -> list[float]:
    """ Distances from `position` to the interferers no farther than `radius`. """
    (x, y) = position
    result = []
    for (ix, iy) in interferers:
        d = math.hypot(ix - x, iy - y)
        if d <= radius:
            result.append(d)
    return result
