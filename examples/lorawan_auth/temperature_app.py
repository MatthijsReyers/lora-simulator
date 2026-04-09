"""Server-side temperature application.

Receives temperature uplinks and optionally queues downlink config commands.
"""
import logging
import struct

from simulator.environment import simulation_env as sim
from simulator.lorawan.application import Application
from simulator.lorawan.network_server import NetworkServer

logger = logging.getLogger(__name__)


class TemperatureApp(Application):
    """Server-side application that receives temperature readings and
    optionally queues a downlink configuration command."""

    def __init__(self, network_server: NetworkServer, fport: int = 1):
        self.ns = network_server
        self._fport = fport
        self.uplink_count = 0

    def port(self) -> int:
        return self._fport

    async def on_uplink(self, dev_addr: int, payload: bytes) -> None:
        counter, temp_raw = struct.unpack("<Hh", payload)
        temperature = temp_raw / 100.0
        self.uplink_count += 1
        logger.info(
            f"{sim.current_time():.2f}s  APP  uplink #{self.uplink_count} from "
            f"0x{dev_addr:08X}: counter={counter}, temp={temperature:.1f}°C"
        )

        # Queue a downlink every 3rd uplink
        if self.uplink_count % 3 == 0:
            config_payload = struct.pack("<BH", 0x01, 15)  # command=1, interval=15s
            self.ns.queue_downlink(dev_addr, fport=self._fport, payload=config_payload)
            logger.info(
                f"{sim.current_time():.2f}s  APP  queued downlink config for 0x{dev_addr:08X}"
            )

    async def on_downlink(self, payload: bytes) -> None:
        pass
