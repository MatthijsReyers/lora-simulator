#!/usr/bin/env python3
"""
LoRaWAN Clock Sync example (TS003-style AppTime over FPort 202).

Demonstrates:
- Device-side clock sync application sending AppTimeReq
- Server-side clock sync application replying with AppTimeAns
- End-to-end encrypted LoRaWAN transport via gateway + network server
"""
import logging
import sys

sys.path.append(".")

from simulator.environment import simulation_env as sim
from simulator.lora.phy_layer import LoraPhyLayer
from simulator.lorawan.applications.clock_sync import (
    CLOCK_SYNC_FPORT,
    ClockSyncApplication,
    ClockSyncServerApplication,
)
from simulator.lorawan.device import DeviceSession, LoRaWanDevice
from simulator.lorawan.gateway import LoRaWanGateway
from simulator.lorawan.network_server import NetworkServer


logger = logging.getLogger(__name__)

DEV_ADDR = 0x26011234
NWK_S_KEY = bytes.fromhex("2B7E151628AED2A6ABF7158809CF4F3C")
APP_S_KEY = bytes.fromhex("3C4F9C098815F7ABA6D2AE281615E72B")
SYNC_INTERVAL = 30  # seconds


class ClockSyncSensor(LoRaWanDevice):
    """Sensor node that periodically requests network time sync."""

    def __init__(self, session: DeviceSession, sync_app: ClockSyncApplication):
        super().__init__(session=session)
        self.sync_app = sync_app
        self.register_application(sync_app)
        sim.create_task(self._run())

    async def _run(self) -> None:
        await sim.sleep(2.0)
        while True:
            # Example device timestamp source: simulation time in whole seconds.
            device_time = int(sim.current_time())
            req_payload = self.sync_app.build_time_request(device_time)
            got_downlink = await self.send_uplink(CLOCK_SYNC_FPORT, req_payload)
            logger.info(
                f"{sim.current_time():.2f}s  SENSOR  AppTimeReq sent "
                f"(device={device_time}s, downlink={got_downlink})"
            )
            await sim.sleep(SYNC_INTERVAL)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sim.logger.setLevel(logging.WARNING)

    phy_layer = LoraPhyLayer()
    phy_layer.logger.setLevel(logging.WARNING)

    ns = NetworkServer()
    ns.register_device(DEV_ADDR, NWK_S_KEY, APP_S_KEY)

    server_app = ClockSyncServerApplication(time_provider=lambda: int(sim.current_time()))
    ns.register_application(server_app)

    gateway = LoRaWanGateway(network_server=ns)

    session = DeviceSession(dev_addr=DEV_ADDR, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY)
    sensor = ClockSyncSensor(session=session, sync_app=ClockSyncApplication())
    sensor.radio.logger.setLevel(logging.WARNING)

    print("=" * 70)
    print("LoRaWAN Applications Example: Clock Sync (FPort 202)")
    print(f"  DevAddr:       0x{DEV_ADDR:08X}")
    print(f"  Sync Interval: {SYNC_INTERVAL}s")
    print("=" * 70)

    sim.run(simulation_length=120)

    print("=" * 70)
    print("Simulation complete.")
    print(f"  Gateway forwarded: {gateway.frames_forwarded} frames")
    print(f"  Clock sync cycles: {server_app.sync_count}")
    print("=" * 70)
