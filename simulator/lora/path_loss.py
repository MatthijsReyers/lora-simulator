import math, random
from typing import Tuple

SPEED_OF_LIGHT = 299_792_458  # in m/s
FOUR_PI = 4 * math.pi


def log_distance_path_loss(
        distance: float,
        frequency: float,
        path_loss_exponent: float = 2.0,
        sigma: float = 0.0
    ):
    """
        Python implementation of the Log-Distance Path Loss Model.

        See: 
            - https://en.wikipedia.org/wiki/Log-distance_path_loss_model
            - https://www.gaussianwaves.com/2013/09/log-distance-path-loss-or-log-normal-shadowing-model/
    
        :param distance: In meters, distance between transmitter and receiver
        :param frequency: In hertz, frequency of the signal
        :param path_loss_exponent: Environment specific path loss exponent
        :param sigma: Standard deviation of the Gaussian random variable to simulate shadowing
                      effects, can be set to 0.0 for no shadowing.
    """
    # Taking log(0) is undefined and there is no meaningful path loss at zero distance anyway..
    if distance == 0:
        return 0.0

    fspl = free_space_path_loss(frequency, 1.0)
    lnpl = 10 * path_loss_exponent * math.log10(distance)
    gamma = random.gauss(mu=0, sigma=sigma)
    
    return max(0, fspl + lnpl + gamma)




def free_space_path_loss(frequency: float, distance: float) -> float:
    """
        Calculates the Free Space Path Loss (FSPL) in decibels.
    
        :param frequency: Frequency in hertz
        :param distance: Distance in meters
        :return: Path loss in decibels
    """
    return ((FOUR_PI * distance * frequency) / SPEED_OF_LIGHT) ** 2 

