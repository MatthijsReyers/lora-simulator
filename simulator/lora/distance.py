from typing import Tuple
import math

def distance(p1: Tuple[float, float], p2: Tuple[float, float]) -> float:
    """ Calculate the Euclidean distance given delta x and delta y. """
    return math.sqrt(
        (p1[0] - p2[0]) ** 2 + (p1[1] - p2[1]) ** 2
    )
