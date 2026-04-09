"""Multicast-capable sensor device.

A sensor that starts in Class A, switches to Class C after its first uplink,
and can receive multicast downlinks on the group address.
"""
import logging
import struct

from simulator.environment import simulation_env as sim
from simulator.lorawan.device import LoRaWanDevice, DeviceSession, MulticastGroup
from simulator.lorawan.enums.operating_mode import OperatingMode

from examples.lorawan_auth.sensor_app import SensorApp

logger = logging.getLogger(__name__)

FPORT = 1


class MulticastSensor(LoRaWanDevice):
    """Sensor that switches to Class C and joins a multicast group."""

    TX_INTERVAL = 30  # seconds between transmissions

    def __init__(
        self,
        session: DeviceSession,
        multicast_group: MulticastGroup,
        sensor_id: int = 0,
        start_delay: float = 0.0,
    ):
        super().__init__(session=session)
        self._mc_group = multicast_group
        self._id = sensor_id
        self._start_delay = start_delay
        self.register_application(SensorApp(fport=FPORT))
        sim.create_task(self._sensor_loop())

    def _tag(self) -> str:
        return f"SENSOR-{self._id}" if self._id else "SENSOR"

    async def _sensor_loop(self) -> None:
        # Initial delay to let gateway enter RX mode + stagger sensors
        await sim.sleep(1.0 + self._start_delay)

        assert self.session is not None
        tag = self._tag()

        # Send one uplink in Class A first
        temperature = 21.5
        payload = struct.pack("<Hh", 0, int(temperature * 100))
        logger.info(
            f"{sim.current_time():.2f}s  {tag} TX  FCnt={self.session.fcnt_up}  "
            f"temp={temperature:.1f}°C  (Class A)"
        )
        await self.send_uplink(fport=FPORT, payload=payload)

        # Switch to Class C and join multicast group
        logger.info(f"{sim.current_time():.2f}s  {tag}  Switching to Class C")
        await self.switch_mode(OperatingMode.CLASS_C)
        self.join_multicast_group(self._mc_group)
        logger.info(
            f"{sim.current_time():.2f}s  {tag}  Joined multicast group "
            f"0x{self._mc_group.group_addr:08X}"
        )

        # Continue sending periodic uplinks in Class C
        counter = 1
        while sim.is_running():
            await sim.sleep(self.TX_INTERVAL)
            temperature = 21.5 + (counter % 10) * 0.1
            payload = struct.pack("<Hh", counter, int(temperature * 100))
            logger.info(
                f"{sim.current_time():.2f}s  {tag} TX  FCnt={self.session.fcnt_up}  "
                f"temp={temperature:.1f}°C  (Class C)"
            )
            await self.send_uplink(fport=FPORT, payload=payload)
            counter += 1
