import math, random, statistics

from simulator.path_loss.constant_loss import constant_path_loss
from simulator.path_loss.log_distance_path_loss import log_distance_path_loss
from simulator.path_loss.nakagami_path_loss import nakagami_path_loss


FREQUENCY = 868_100_000


def test_log_distance_matches_free_space_loss():
    # With exponent 2 the model is plain free space path loss, 868MHz over 1km is ~91.2dB.
    estimator = log_distance_path_loss(exponent=2.0)
    assert math.isclose(estimator(1000, FREQUENCY), 91.2, abs_tol=0.1)


def test_log_distance_grows_with_distance():
    estimator = log_distance_path_loss(exponent=2.0)
    assert estimator(0, FREQUENCY) == 0.0
    assert estimator(10, FREQUENCY) < estimator(100, FREQUENCY) < estimator(1000, FREQUENCY)
    # Doubling the distance with exponent 2 adds 20*log10(2) dB.
    delta = estimator(200, FREQUENCY) - estimator(100, FREQUENCY)
    assert math.isclose(delta, 20 * math.log10(2), rel_tol=1e-9)


def test_nakagami_has_unit_mean_power_gain():
    random.seed(1234)
    estimator = nakagami_path_loss(base=constant_path_loss(100.0), m2=0.75)
    # The extra loss must average out: the mean of the linear power scaling factor is one.
    gains = [10 ** ((100.0 - estimator(500.0, FREQUENCY)) / 10) for _ in range(20_000)]
    assert math.isclose(statistics.fmean(gains), 1.0, abs_tol=0.05)
    # ..but individual packets fade well below the mean.
    assert min(gains) < 0.01


def test_nakagami_shape_depends_on_distance():
    random.seed(1234)
    # A huge shape parameter means (almost) no fading, so the distance thresholds can be checked
    # by observing where the fading spread appears.
    estimator = nakagami_path_loss(
        base=constant_path_loss(100.0), m0=1e6, m1=0.75, m2=1e6, distance1=80, distance2=200,
    )
    near = [estimator(50, FREQUENCY) for _ in range(500)]
    mid = [estimator(150, FREQUENCY) for _ in range(500)]
    far = [estimator(1000, FREQUENCY) for _ in range(500)]
    assert statistics.pstdev(near) < 0.1
    assert statistics.pstdev(far) < 0.1
    assert statistics.pstdev(mid) > 3.0
