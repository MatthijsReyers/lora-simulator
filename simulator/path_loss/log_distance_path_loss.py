import math, random
from typing import Callable

SPEED_OF_LIGHT = 299_792_458  # in m/s
FOUR_PI = 4 * math.pi

def log_distance_path_loss(
        exponent: float = 2.0,
        sigma: float = 0.0
    ) -> Callable[[float, float], float]:
    """
        Python implementation of the Log-Distance Path Loss Model.

        See: 
            - https://en.wikipedia.org/wiki/Log-distance_path_loss_model
            - https://www.gaussianwaves.com/2013/09/log-distance-path-loss-or-log-normal-shadowing-model/
    
        :param frequency: In hertz, frequency of the signal
        :param path_loss_exponent: Environment specific path loss exponent
        :param sigma: Standard deviation of the Gaussian random variable to simulate shadowing
                      effects, can be set to 0.0 for no shadowing.
    """

    def free_space_path_loss(frequency: float, distance: float) -> float:
        return ((FOUR_PI * distance * frequency) / SPEED_OF_LIGHT) ** 2 

    def estimator(distance: float, frequency: float) -> float:
        # Taking log(0) is undefined and there is no meaningful path loss at zero distance anyway..
        if distance == 0:
            return 0.0
        fspl = free_space_path_loss(frequency, 1.0)
        lnpl = 10 * exponent * math.log10(distance)
        gamma = random.gauss(mu=0, sigma=sigma)
        return max(0, fspl + lnpl + gamma)
    
    return estimator
