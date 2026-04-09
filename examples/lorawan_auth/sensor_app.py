"""Device-side sensor application.

Handles downlink configuration commands received by the sensor.
"""
import logging
import struct

from simulator.environment import simulation_env as sim
from simulator.lorawan.application import Application

logger = logging.getLogger(__name__)


class SensorApp(Application):
    """Device-side application that receives downlink configuration commands."""

    def __init__(self, fport: int = 1):
        self._fport = fport

    def port(self) -> int:
        return self._fport

    async def on_uplink(self, dev_addr: int, payload: bytes) -> None:
        pass  # Device-side, not used

    async def on_downlink(self, payload: bytes) -> None:
        command = payload[0]
        value = struct.unpack("<H", payload[1:3])[0]
        logger.info(
            f"{sim.current_time():.2f}s  SENSOR APP  received downlink: "
            f"command={command}, value={value}"
        )
