import math, random
from typing import Callable

from simulator.path_loss.log_distance_path_loss import log_distance_path_loss


def nakagami_path_loss(
        base: Callable[[float, float], float] | None = None,
        m0: float = 1.5,
        m1: float = 0.75,
        m2: float = 0.75,
        distance1: float = 80.0,
        distance2: float = 200.0,
    ) -> Callable[[float, float], float]:
    """
        Nakagami-m fast fading stacked on top of another path loss model, mirroring the way ns-3
        chains its `NakagamiPropagationLossModel` after a `LogDistancePropagationLossModel`.

        The received power is multiplied by a Gamma distributed random variable with shape `m`
        and mean 1, so on average the fading neither adds nor removes loss but individual packets
        can fade far below the mean. Like in ns-3 the shape parameter depends on the distance:
        `m0` up to `distance1`, `m1` up to `distance2` and `m2` beyond that. Values of `m < 1`
        (the ns-3 defaults beyond 80m) give fading that is more severe than Rayleigh fading.

        See:
            - https://www.nsnam.org/docs/models/html/propagation.html#nakagamipropagationlossmodel
            - https://en.wikipedia.org/wiki/Nakagami_distribution

        :param base: Path loss model to apply the fading to, defaults to a log-distance model.
        :param m0: Shape parameter for distances below `distance1`.
        :param m1: Shape parameter for distances between `distance1` and `distance2`.
        :param m2: Shape parameter for distances beyond `distance2`.
        :param distance1: In meters, first distance threshold for the shape parameter.
        :param distance2: In meters, second distance threshold for the shape parameter.
    """
    assert m0 > 0 and m1 > 0 and m2 > 0, "Nakagami shape parameters must be positive"
    assert 0 <= distance1 <= distance2, "Distance thresholds must be ordered"

    if base is None:
        base = log_distance_path_loss()

    def estimator(distance: float, frequency: float) -> float:
        loss = base(distance, frequency)
        if distance < distance1:
            m = m0
        elif distance < distance2:
            m = m1
        else:
            m = m2
        # A Gamma(m, 1/m) variable has mean 1, which makes it the power scaling factor of the
        # Nakagami-m amplitude distribution.
        fading = max(random.gammavariate(m, 1.0 / m), 1e-12)
        return loss - 10 * math.log10(fading)

    return estimator
