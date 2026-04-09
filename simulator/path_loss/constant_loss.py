from typing import Callable

def constant_path_loss(
    loss_value: float = 100.0
) -> Callable[[float, float], float]:
    """ 
        "Estimates" the path loss by returning a constant value, you can use this in situations
        where you want to ignore path loss effects altogether or want to use a constant value
        to save some compute time.
    """
    def estimator(distance: float, frequency: float) -> float:
        return loss_value
    return estimator

