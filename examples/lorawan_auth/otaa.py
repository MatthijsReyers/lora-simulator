#!/usr/bin/env python3
"""
LoRaWAN OTAA Example — Over-The-Air Activation with key exchange.

Demonstrates:
  - OTAA join procedure (JoinRequest → JoinAccept)
  - Session key derivation from AppKey + JoinNonce
  - Sensor sending uplinks after successful join
  - Network server handling join and data frames
"""
import sys
import logging

sys.path.append(".")

from simulator.lora.phy_layer import LoraPhyLayer
from simulator.lora.radio import LoraRadio
from simulator.environment import simulation_env as sim
from simulator.lorawan.join import OTAACredentials
from simulator.lorawan.gateway import LoRaWanGateway
from simulator.lorawan.network_server import NetworkServer

from examples.lorawan_auth.temperature_app import TemperatureApp
from examples.lorawan_auth.temperature_sensor import TemperatureSensor

logger = logging.getLogger(__name__)

# ── OTAA root keys (pre-provisioned, shared between device and NS) ───
APP_EUI = bytes.fromhex("0102030405060708")
DEV_EUI = bytes.fromhex("1112131415161718")
APP_KEY = bytes.fromhex("2B7E151628AED2A6ABF7158809CF4F3C")

TX_INTERVAL = 30  # seconds

if __name__ == "__main__":
    level = logging.INFO
    logging.basicConfig(level=level, format="%(message)s")

    sim.logger.setLevel(logging.WARNING)

    phy_layer = LoraPhyLayer()
    phy_layer.logger.setLevel(logging.WARNING)

    # ── Network server setup ────────────────────────────────────────────
    ns = NetworkServer()
    ns.register_otaa_device(APP_EUI, DEV_EUI, APP_KEY)

    temp_app = TemperatureApp(ns)
    ns.register_application(temp_app)

    # ── Gateway setup ───────────────────────────────────────────────────
    gw_radio = LoraRadio()
    gw_radio.logger.setLevel(logging.WARNING)
    gateway = LoRaWanGateway(radio=gw_radio, network_server=ns)

    # ── Sensor setup (OTAA — no session yet, will join) ─────────────────
    credentials = OTAACredentials(app_eui=APP_EUI, dev_eui=DEV_EUI, app_key=APP_KEY)
    sensor = TemperatureSensor(otaa_credentials=credentials)
    sensor.radio.logger.setLevel(logging.WARNING)

    # ── Run ─────────────────────────────────────────────────────────────
    print("=" * 70)
    print("LoRaWAN Example: OTAA Sensor → Gateway → Network Server")
    print(f"  DevEUI:      {DEV_EUI.hex()}")
    print(f"  AppEUI:      {APP_EUI.hex()}")
    print(f"  TX Interval: {TX_INTERVAL}s (after join)")
    print("=" * 70)

    sim.run(simulation_length=180)

    print("=" * 70)
    print(f"Simulation complete.")
    print(f"  Gateway forwarded: {gateway.frames_forwarded} frames")
    print(f"  App received:      {temp_app.uplink_count} uplinks")
    print("=" * 70)
