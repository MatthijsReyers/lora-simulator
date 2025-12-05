
class SimulatorException(Exception):
    """Base exception for the simulator."""
    pass


class SimulationFinishedException(SimulatorException):
    """Exception raised when the simulation has finished."""
    def __init__(self):
        super().__init__(
            "Cannot schedule more events, simulation has finished."
        )
