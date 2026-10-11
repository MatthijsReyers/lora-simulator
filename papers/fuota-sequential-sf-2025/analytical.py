"""
The analytical framework of Section V and Appendices A and B of the paper, as plain Python.

It predicts the normalised energy consumption and the delivery time of the proposed scheme for
a recipient at a given distance (the "Proposed (analytical)" curve of Figure 2) and is used
here for two things: calibrating the one channel constant the paper leaves ambiguous (see the
README) and cross-checking the simulation. Everything is driven by a `Scenario` so the two
never disagree about parameters.

Notation follows the paper: `i` is the spreading factor of the FUOTA frame, `j` that of an
interferer, `d0` the recipient's distance to the gateway, `a` the (Rayleigh) fading power gain
of the FUOTA frame, `n` the number of interferers within the interference radius.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache

from scipy.integrate import quad
from scipy.special import gammainc, gamma as gamma_fn

from scenario import Scenario, SPREADING_FACTORS


def lower_incomplete_gamma(s: float, x: float) -> float:
    """ γ(s, x) = ∫₀ˣ t^(s-1) e^(-t) dt (scipy's `gammainc` is the regularised version). """
    return gammainc(s, x) * gamma_fn(s)


def mean_fragments_to_decode(scenario: Scenario) -> float:
    """
        Appendix A: the mean number of received coded fragments N̄s needed to decode. For the
        ideal code this is k. For the raptor model the decoding attempt on the k-th fragment
        fails with probability 0.85 and every following one with 0.567 (independently), so the
        number of extra fragments is 0 with probability 0.15 and otherwise 1 + Geometric.
    """
    k = scenario.fragments
    if scenario.code == "ideal":
        return float(k)
    p_first, p_next = scenario.raptor_fail_first, scenario.raptor_fail_next
    return k + p_first * (1.0 + p_next / (1.0 - p_next))


@dataclass
class LinkModel:
    """ Everything about one recipient's link that does not depend on the spreading factor. """
    scenario: Scenario

    def __post_init__(self) -> None:
        s = self.scenario
        # Linear scale quantities of the received power model R = γ0 pt A d^-α, in milliwatts.
        self.gamma0_pt_mw = 10 ** ((s.tx_power - s.reference_loss_db) / 10)
        self.interference_radius = s.interference_radius()
        self.mean_interferers = s.interferer_density * math.pi * self.interference_radius ** 2
        # Mean airtimes of interfering frames per SF, l̄_j (fixed payload size here).
        self.interferer_airtime = {j: s.interferer_airtime(j) for j in SPREADING_FACTORS}

    def interferer_count_pmf(self, n: int) -> float:
        """ Equation (6). """
        m = self.mean_interferers
        return math.exp(-m + n * math.log(m) - math.lgamma(n + 1))

    def detection_threshold(self, sf: int, d0: float) -> float:
        """ The fading gain `a` below which the frame is below the sensitivity ζ_i, i.e. ζ_i d0^α / (γ0 pt). """
        zeta_mw = 10 ** (self.scenario.sensitivity[sf] / 10)
        return zeta_mw * d0 ** self.scenario.path_loss_exponent / self.gamma0_pt_mw

    def one_interferer_loss_probability(self, sf: int, d0: float, a: float, window: float) -> float:
        """
            Q_I of equation (28): the probability that one interferer, uniformly placed within
            the interference radius, sends a frame that overlaps a window of `window` seconds on
            the same channel with enough power (relative to the FUOTA frame's power `a d0^-α`)
            to destroy it.
        """
        s = self.scenario
        alpha = s.path_loss_exponent
        ri = self.interference_radius
        total = 0.0
        for j in SPREADING_FACTORS:
            collision = s.interferer_frame_rate * (window + self.interferer_airtime[j]) / s.channels
            if s.collision_rule == "any_overlap":
                power = 1.0
            else:
                xi = 10 ** (s.capture_threshold_db(sf, j) / 10)      # ξ_{i,j}, linear
                beta = a * d0 ** (-alpha) / xi                      # β_j of the paper
                power = (2 / (alpha * ri ** 2)) * lower_incomplete_gamma(2 / alpha, beta * ri ** alpha) / beta ** (2 / alpha)
            total += s.interferer_sf_probability(j) * collision * power
        return total

    def success_probability(self, sf: int, d0: float, n: int, window: float) -> float:
        """
            Equations (13)/(14) and (26): the probability that a frame with SF `sf` is detected
            (fading leaves it above the sensitivity) and that none of the `n` interferers destroys
            the given part of it (`window` = preamble or whole frame duration).
        """
        threshold = self.detection_threshold(sf, d0)

        def integrand(a: float) -> float:
            q = self.one_interferer_loss_probability(sf, d0, a, window)
            return (1.0 - q) ** n * math.exp(-a)

        if n == 0:
            return math.exp(-threshold)
        value, _ = quad(integrand, threshold, math.inf, limit=200)
        return value


@dataclass
class Prediction:
    distance: float
    energy: float                  # normalised, including the control overhead E_C
    energy_without_control: float  # normalised, E(d0) alone (what Tables II and III report)
    delivery_time: float           # seconds


def rounds_of(scenario: Scenario) -> list[int]:
    """ The SFs the gateway cycles through: {L..M} for the proposed scheme, one SF for FSF. """
    if scenario.scheme == "fsf":
        return [scenario.fixed_sf]
    return list(range(scenario.first_sf, scenario.last_sf + 1))


def predict(scenario: Scenario, d0: float, fast: bool = False) -> Prediction:
    """
        E(d0) and T(d0) of equations (7) and (18) for the proposed scheme with the scenario's
        (L, M, w), or for FSF with its single SF. The sum over the number of interferers is
        truncated where the Poisson pmf becomes negligible; `fast` replaces it by its mean (the
        distribution is narrow, this changes the results by well under a percent).
    """
    link = LinkModel(scenario)
    s = scenario
    n_s = mean_fragments_to_decode(scenario)
    rounds = rounds_of(scenario)
    frame = {i: s.frame_airtime(i) for i in rounds}
    preamble = {i: s.preamble_airtime(i) for i in rounds}
    pacing = 100.0 / s.duty_cycle_percent

    m = link.mean_interferers
    if fast:
        counts = [(round(m), 1.0)]
    else:
        counts = [(n, link.interferer_count_pmf(n)) for n in range(0, int(m + 8 * math.sqrt(m) + 10))]

    energy = 0.0
    time = 0.0
    for (n, p_n) in counts:
        if p_n < 1e-12:
            continue
        s_fr = {i: link.success_probability(i, d0, n, frame[i]) for i in rounds}
        s_pr = {i: link.success_probability(i, d0, n, preamble[i]) for i in rounds}
        # Equation (12): mean receiver-on time per reception attempt (energy per attempt / p_r).
        per_attempt = {
            i: s_fr[i] * frame[i] + (s_pr[i] - s_fr[i]) * frame[i] + (1 - s_pr[i]) * preamble[i]
            for i in rounds
        }
        # Equations (9)-(11): which round completes the decoding and how many attempts it takes.
        collected = 0.0
        e_n = 0.0
        t_n = 0.0
        for i in rounds:
            expected = s.transmissions_per_sf * s_fr[i]
            if i == rounds[-1] or collected + expected >= n_s:
                remaining = max(n_s - collected, 0.0)
                attempts = remaining / s_fr[i] if s_fr[i] > 0 else math.inf
                e_n += attempts * per_attempt[i]
                t_n += pacing * attempts * frame[i]
                break
            collected += expected
            e_n += s.transmissions_per_sf * per_attempt[i]
            t_n += pacing * s.transmissions_per_sf * frame[i]
        energy += p_n * e_n
        time += p_n * t_n

    # E_C = p_r l_c + p_t l_a, divided by p_r like everything else, then normalised by k l_f(7).
    control = s.control_airtime + s.tx_rx_power_ratio * s.ack_airtime(s.uplink_sf_for(d0))
    norm = s.energy_normalisation()
    return Prediction(
        distance=d0, energy=(energy + control) / norm, energy_without_control=energy / norm,
        delivery_time=time,
    )


def group_sf(scenario: Scenario, d0: float, energy_based: bool) -> int:
    """
        The SF assignment of the group based benchmarks, equations (19) and (20): the SF with
        the lowest expected cost per decoded image, k / S_i^(fr)(d0) times the cost of one
        reception attempt. For GB-L that cost is the frame's share of the gateway's time
        (l_i^(fr) × 100 / DC_max); for GB-E it is the mean energy of an attempt Ē_i^(fr) of
        equation (12), i.e. failed preamble acquisitions only cost the preamble (reading the
        e_i^(fr) of equation (19) literally would make the two criteria identical).
    """
    link = LinkModel(scenario)
    n = round(link.mean_interferers)
    best, best_cost = 7, math.inf
    for sf in SPREADING_FACTORS:
        frame = scenario.frame_airtime(sf)
        s_fr = link.success_probability(sf, d0, n, frame)
        if s_fr <= 0:
            continue
        if energy_based:
            s_pr = link.success_probability(sf, d0, n, scenario.preamble_airtime(sf))
            per_attempt = s_pr * frame + (1 - s_pr) * scenario.preamble_airtime(sf)
        else:
            per_attempt = frame * 100.0 / scenario.duty_cycle_percent
        cost = per_attempt / s_fr
        if cost < best_cost:
            best, best_cost = sf, cost
    return best
