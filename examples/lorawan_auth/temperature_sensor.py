"""LoRaWAN temperature sensor device.

A sensor node that periodically sends temperature readings. Can be used
with both ABP (pre-shared session) and OTAA (join procedure) activation.
"""
import logging
import struct

from simulator.environment import simulation_env as sim
from simulator.lorawan.device import LoRaWanDevice, DeviceSession
from simulator.lorawan.join import OTAACredentials

from examples.lorawan_auth.sensor_app import SensorApp

logger = logging.getLogger(__name__)

FPORT = 1


class TemperatureSensor(LoRaWanDevice):
    """A sensor node that periodically sends temperature readings."""

    TX_INTERVAL = 30  # seconds between transmissions

    def __init__(
        self,
        session: DeviceSession | None = None,
        otaa_credentials: OTAACredentials | None = None,
    ):
        super().__init__(session=session, otaa_credentials=otaa_credentials)
        self.register_application(SensorApp(fport=FPORT))
        sim.create_task(self._sensor_loop())

    async def _sensor_loop(self) -> None:
        # Initial delay to let gateway enter RX mode
        await sim.sleep(1.0)

        # If OTAA, perform join first
        if self.session is None:
            logger.info(f"{sim.current_time():.2f}s  SENSOR  Attempting OTAA join...")
            joined = await self.join()
            if not joined:
                logger.error(f"{sim.current_time():.2f}s  SENSOR  Join failed!")
                return
            assert self.session is not None
            logger.info(
                f"{sim.current_time():.2f}s  SENSOR  Join successful!  "
                f"DevAddr=0x{self.session.dev_addr:08X}"
            )

        assert self.session is not None
        counter = 0
        while sim.is_running():
            temperature = 21.5 + (counter % 10) * 0.1
            payload = struct.pack("<Hh", counter, int(temperature * 100))

            logger.info(
                f"{sim.current_time():.2f}s  SENSOR TX  FCnt={self.session.fcnt_up}  "
                f"temp={temperature:.1f}°C"
            )

            got_downlink = await self.send_uplink(fport=FPORT, payload=payload)
            if got_downlink:
                logger.info(f"{sim.current_time():.2f}s  SENSOR  received downlink in RX window")

            counter += 1
            await sim.sleep(self.TX_INTERVAL)
