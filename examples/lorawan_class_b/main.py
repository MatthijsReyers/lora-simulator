#!/usr/bin/env python3
"""
LoRaWAN Class B Example.

Demonstrates:
  - ABP-activated device switching from Class A to Class B
  - Gateway broadcasting beacons every 128 seconds
  - Device acquiring beacon lock and opening ping slots
  - Network server queuing downlinks delivered via ping slots
  - Regular uplink still works alongside Class B ping slots
"""
import sys
import logging

sys.path.append(".")

from simulator.lora.phy_layer import LoraPhyLayer
from simulator.environment import simulation_env as sim
from simulator.lorawan.application import Application
from simulator.lorawan.device import LoRaWanDevice, DeviceSession
from simulator.lorawan.gateway import LoRaWanGateway
from simulator.lorawan.network_server import NetworkServer
from simulator.lorawan.enums.operating_mode import OperatingMode
from simulator.lorawan.region import BEACON_INTERVAL

logger = logging.getLogger(__name__)

# ── Device credentials ──────────────────────────────────────────────────────
DEV_ADDR  = 0x26011234
NWK_S_KEY = bytes.fromhex("2B7E151628AED2A6ABF7158809CF4F3C")
APP_S_KEY = bytes.fromhex("3C4F9C098815F7ABA6D2AE281615E72B")

FPORT = 10
PING_NB = 16  # 16 ping slots per beacon period (~8s between slots)
NUM_BEACON_PERIODS = 10  # Run for 10 beacon periods


class PingSlotApp(Application):
    """Simple application that logs received ping slot downlinks."""

    def __init__(self) -> None:
        self.received: list[bytes] = []

    def port(self) -> int:
        return FPORT

    async def on_uplink(self, dev_addr: int, payload: bytes) -> None:
        pass

    async def on_downlink(self, payload: bytes) -> None:
        logger.info(
            f"  >> DEVICE APP received downlink via ping slot: {payload.hex()}"
        )
        self.received.append(payload)


class ClassBSensor(LoRaWanDevice):
    """Sensor that switches to Class B and sends periodic uplinks."""

    def __init__(
        self,
        session: DeviceSession,
        app: PingSlotApp,
        ping_nb: int = PING_NB,
    ) -> None:
        super().__init__(session=session, ping_nb=ping_nb)
        self.register_application(app)
        self.app = app
        sim.create_task(self._sensor_loop())

    async def _sensor_loop(self) -> None:
        await self.switch_mode(OperatingMode.CLASS_B)

        # Send a regular uplink at beacon period 5 to show Class A TX still
        # works alongside Class B ping slots.
        await sim.sleep(5 * BEACON_INTERVAL + 10)
        logger.info(
            f"{sim.current_time():.2f}s  SENSOR TX  uplink 'hello'"
        )
        await self.send_uplink(fport=FPORT, payload=b"hello")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    sim.logger.setLevel(logging.WARNING)

    # Show every beacon at the device (DEBUG level from simulator.lorawan.device)
    logging.getLogger("simulator.lorawan.device").setLevel(logging.DEBUG)

    phy_layer = LoraPhyLayer()
    phy_layer.logger.setLevel(logging.WARNING)

    # ── Network server ──────────────────────────────────────────────────
    ns = NetworkServer()
    ns.register_device(DEV_ADDR, NWK_S_KEY, APP_S_KEY)
    ns.enable_class_b(DEV_ADDR, ping_nb=PING_NB)

    # ── Gateway with beacons enabled ────────────────────────────────────
    gateway = LoRaWanGateway(network_server=ns, class_b_enabled=True)
    gateway.radio.logger.setLevel(logging.WARNING)

    # ── Device ──────────────────────────────────────────────────────────
    app = PingSlotApp()
    sensor = ClassBSensor(session=DeviceSession(
        dev_addr=DEV_ADDR, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY,
    ), app=app)
    sensor.radio.logger.setLevel(logging.WARNING)

    # ── Queue downlinks at different beacon periods ─────────────────────
    async def schedule_downlinks() -> None:
        # Downlink 1: queue just before beacon period 2 (beacon at t=256)
        await sim.sleep(2 * BEACON_INTERVAL - 5)
        logger.info(
            f"{sim.current_time():.2f}s  NS  queuing downlink #1 (cafebabe)"
        )
        ns.queue_downlink(DEV_ADDR, fport=FPORT, payload=b"\xCA\xFE\xBA\xBE")

        # Downlink 2: queue just before beacon period 4 (beacon at t=512)
        await sim.sleep(2 * BEACON_INTERVAL)
        logger.info(
            f"{sim.current_time():.2f}s  NS  queuing downlink #2 (deadbeef)"
        )
        ns.queue_downlink(DEV_ADDR, fport=FPORT, payload=b"\xDE\xAD\xBE\xEF")

        # Downlink 3: queue just before beacon period 7 (beacon at t=896)
        await sim.sleep(3 * BEACON_INTERVAL)
        logger.info(
            f"{sim.current_time():.2f}s  NS  queuing downlink #3 (01020304)"
        )
        ns.queue_downlink(DEV_ADDR, fport=FPORT, payload=b"\x01\x02\x03\x04")

    sim.create_task(schedule_downlinks())

    # ── Run ─────────────────────────────────────────────────────────────
    total_time = NUM_BEACON_PERIODS * BEACON_INTERVAL + 10
    sim.run(total_time)

    logger.info(f"\n{'='*60}")
    logger.info(f"Simulation complete ({total_time:.0f}s)")
    logger.info(f"Device beacon locked: {sensor._beacon_locked}")
    logger.info(f"Downlinks received via ping slot: {len(app.received)}")
    for i, data in enumerate(app.received):
        logger.info(f"  [{i}] {data.hex()}")
    logger.info(f"Gateway frames forwarded: {gateway.frames_forwarded}")
