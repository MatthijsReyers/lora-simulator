#!/usr/bin/env python3
"""
LoRaWAN ABP Example — Pre-Shared Key activation.

Demonstrates:
  - ABP-activated sensor sending periodic temperature uplinks
  - Network server processing uplinks and routing to applications
  - Downlink application data (config commands every 3rd uplink)
"""
import sys
import logging

sys.path.append(".")

from simulator.lora.phy_layer import LoraPhyLayer
from simulator.environment import simulation_env as sim
from simulator.lorawan.device import DeviceSession
from simulator.lorawan.gateway import LoRaWanGateway
from simulator.lorawan.network_server import NetworkServer

from examples.lorawan_auth.temperature_app import TemperatureApp
from examples.lorawan_auth.temperature_sensor import TemperatureSensor

logger = logging.getLogger(__name__)

# ── Pre-shared ABP credentials ──────────────────────────────────────────────
DEV_ADDR  = 0x26011234
NWK_S_KEY = bytes.fromhex("2B7E151628AED2A6ABF7158809CF4F3C")
APP_S_KEY = bytes.fromhex("3C4F9C098815F7ABA6D2AE281615E72B")

FPORT = 1
TX_INTERVAL = 30  # seconds


if __name__ == "__main__":
    level = logging.INFO
    logging.basicConfig(level=level, format="%(message)s")

    sim.logger.setLevel(logging.WARNING)

    phy_layer = LoraPhyLayer()
    phy_layer.logger.setLevel(logging.WARNING)

    # ── Network server setup ────────────────────────────────────────────
    ns = NetworkServer()
    ns.register_device(DEV_ADDR, NWK_S_KEY, APP_S_KEY)

    temp_app = TemperatureApp(ns)
    ns.register_application(temp_app)

    # ── Gateway setup ───────────────────────────────────────────────────
    gateway = LoRaWanGateway(network_server=ns)

    # ── Sensor setup ────────────────────────────────────────────────────
    session = DeviceSession(dev_addr=DEV_ADDR, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY)
    sensor = TemperatureSensor(session=session)
    sensor.radio.logger.setLevel(logging.WARNING)

    # ── Run ─────────────────────────────────────────────────────────────
    print("=" * 70)
    print("LoRaWAN Example: ABP Sensor -> Gateway -> Network Server")
    print(f"  DevAddr:     0x{DEV_ADDR:08X}")
    print(f"  TX Interval: {TX_INTERVAL}s")
    print(f"  Downlink:    config command every 3rd uplink")
    print("=" * 70)

    sim.run(simulation_length=180)

    print("=" * 70)
    print(f"Simulation complete.")
    print(f"  Gateway forwarded: {gateway.frames_forwarded} frames")
    print(f"  App received:      {temp_app.uplink_count} uplinks")
    print("=" * 70)