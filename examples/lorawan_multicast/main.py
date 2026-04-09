#!/usr/bin/env python3
"""
LoRaWAN Class C + Multicast Example.

Demonstrates:
  - Multiple ABP-activated sensors switching from Class A to Class C
  - Network server creating a multicast group
  - All sensors joining the same multicast group
  - Gateway broadcasting a multicast downlink received by all sensors
"""
import sys
import logging

sys.path.append(".")

from simulator.lora.phy_layer import LoraPhyLayer
from simulator.environment import simulation_env as sim
from simulator.lorawan.device import DeviceSession, MulticastGroup
from simulator.lorawan.gateway import LoRaWanGateway
from simulator.lorawan.network_server import NetworkServer
from simulator.lorawan.enums.operating_mode import OperatingMode

from examples.lorawan_auth.temperature_app import TemperatureApp
from examples.lorawan_multicast.multicast_sensor import MulticastSensor

logger = logging.getLogger(__name__)

# ── Sensor credentials (3 ABP devices) ──────────────────────────────────────
SENSORS = [
    {
        "dev_addr": 0x26011001,
        "nwk_s_key": bytes.fromhex("2B7E151628AED2A6ABF7158809CF4F3C"),
        "app_s_key": bytes.fromhex("3C4F9C098815F7ABA6D2AE281615E72B"),
    },
    {
        "dev_addr": 0x26011002,
        "nwk_s_key": bytes.fromhex("1122334455667788AABBCCDDEEFF0011"),
        "app_s_key": bytes.fromhex("FFEEDDCCBBAA99887766554433221100"),
    },
    {
        "dev_addr": 0x26011003,
        "nwk_s_key": bytes.fromhex("DEADBEEF12345678DEADBEEF12345678"),
        "app_s_key": bytes.fromhex("CAFEBABE87654321CAFEBABE87654321"),
    },
]

# ── Multicast group credentials (shared by all sensors) ─────────────────────
MC_ADDR    = 0xFF000001
MC_NWK_KEY = bytes.fromhex("AABBCCDD11223344AABBCCDD11223344")
MC_APP_KEY = bytes.fromhex("11223344AABBCCDD11223344AABBCCDD")

FPORT = 1


if __name__ == "__main__":
    level = logging.INFO
    logging.basicConfig(level=level, format="%(message)s")

    sim.logger.setLevel(logging.WARNING)

    phy_layer = LoraPhyLayer()
    phy_layer.logger.setLevel(logging.WARNING)

    # ── Network server setup ────────────────────────────────────────────
    ns = NetworkServer()
    temp_app = TemperatureApp(ns)
    ns.register_application(temp_app)

    # Create a multicast group on the server
    ns.create_multicast_group(MC_ADDR, MC_NWK_KEY, MC_APP_KEY)

    # ── Gateway setup ───────────────────────────────────────────────────
    gateway = LoRaWanGateway(network_server=ns)

    # ── Sensor setup (3 ABP sensors, all joining the same multicast group) ──
    mc_group = MulticastGroup(
        group_addr=MC_ADDR, nwk_s_key=MC_NWK_KEY, app_s_key=MC_APP_KEY,
    )
    sensors: list[MulticastSensor] = []
    for i, creds in enumerate(SENSORS):
        ns.register_device(creds["dev_addr"], creds["nwk_s_key"], creds["app_s_key"])  # type: ignore[arg-type]
        session = DeviceSession(
            dev_addr=creds["dev_addr"],  # type: ignore[arg-type]
            nwk_s_key=creds["nwk_s_key"],  # type: ignore[arg-type]
            app_s_key=creds["app_s_key"],  # type: ignore[arg-type]
        )
        # Each sensor gets its own MulticastGroup instance (same keys, independent fcnt)
        sensor_mc = MulticastGroup(
            group_addr=MC_ADDR, nwk_s_key=MC_NWK_KEY, app_s_key=MC_APP_KEY,
        )
        sensor = MulticastSensor(
            session=session, multicast_group=sensor_mc, sensor_id=i + 1,
            start_delay=i * 5.0,
        )
        sensor.radio.logger.setLevel(logging.WARNING)
        sensors.append(sensor)

    # ── Schedule a multicast downlink via the gateway ───────────────────
    async def send_multicast_broadcast() -> None:
        # Wait until all sensors have switched to Class C
        await sim.sleep(20.0)

        firmware_chunk = b"\xCA\xFE\xBA\xBE\x00\x01\x02\x03"
        raw = ns.build_multicast_downlink(MC_ADDR, fport=FPORT, payload=firmware_chunk)

        logger.info(
            f"{sim.current_time():.2f}s  BROADCAST  Sending multicast downlink "
            f"to group 0x{MC_ADDR:08X} ({len(firmware_chunk)} bytes payload)"
        )
        await gateway.radio.transmit_data_blocking(raw)

    sim.create_task(send_multicast_broadcast())

    # ── Run ─────────────────────────────────────────────────────────────
    print("=" * 70)
    print("LoRaWAN Example: Class C + Multicast (3 sensors)")
    print(f"  Sensors:        {len(sensors)}")
    for i, creds in enumerate(SENSORS):
        print(f"    Sensor {i+1}:     0x{creds['dev_addr']:08X}")
    print(f"  MulticastAddr:  0x{MC_ADDR:08X}")
    print("=" * 70)

    sim.run(simulation_length=120)

    print("=" * 70)
    print(f"Simulation complete.")
    print(f"  Gateway forwarded: {gateway.frames_forwarded} frames")
    print(f"  App received:      {temp_app.uplink_count} uplinks")
    print("=" * 70)
