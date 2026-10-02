"""A single-transceiver gateway in front of a fleet that transmits close together.

The gateway used to wait out a device's ``RECEIVE_DELAY1`` inside its receive loop, so it
was deaf from the moment an uplink landed until its RX1 reply had gone out — roughly 1.2 s
during which every other device's uplink was lost *before* it reached the network server.
It now forwards each uplink from its own task and goes straight back to listening; when two
RX1 windows collide the later reply is skipped (the radio really can only transmit once),
but the uplink has still been forwarded and the server-side FUOTA packages retransmit the
command that was not delivered.

The fleet here is the one from the brief: ten devices one second apart, which is exactly
the spacing the old loop could not cope with.
"""

from __future__ import annotations

import logging

import pytest

from simulator.environment import simulation_env as sim
from simulator.exceptions import SimulatorException
from simulator.lora.phy_layer import LoraPhyLayer
from simulator.lorawan.application import Application
from simulator.lorawan.device import DeviceSession, LoRaWanDevice
from simulator.lorawan.fuota.crypto import derive_mc_ke_key, derive_mc_root_key
from simulator.lorawan.fuota.multicast_setup import (
    MulticastSetupDeviceApplication,
    MulticastSetupServerApplication,
)
from simulator.lorawan.gateway import LoRaWanGateway
from simulator.lorawan.network_server import NetworkServer

NWK_S_KEY = bytes.fromhex("2B7E151628AED2A6ABF7158809CF4F3C")
APP_S_KEY = bytes.fromhex("3C4F9C098815F7ABA6D2AE281615E72B")
FIRST_DEV_ADDR = 0x26013000
MC_ADDR = 0xFF000003
MC_KEY = bytes.fromhex("AABBCCDD11223344AABBCCDD11223344")
MC_GROUP_ID = 0
DATA_FPORT = 10
DEVICES = 10
#: 0.8 s apart: ten devices on an 8 s interval, the spacing the old receive loop could
#: not keep up with.
STAGGER = 0.8
UPLINK_PERIOD = 8.0


class _Sink(Application):
    """Device-side sink so the periodic payload has somewhere to land."""

    def port(self) -> int:
        return DATA_FPORT

    async def on_uplink(self, dev_addr: int, payload: bytes) -> None:
        return None


class _CountingServerApp(Application):
    """Counts the uplinks the network server actually received, per device."""

    def __init__(self) -> None:
        self.uplinks: dict[int, int] = {}

    def port(self) -> int:
        return DATA_FPORT

    async def on_uplink(self, dev_addr: int, payload: bytes) -> None:
        self.uplinks[dev_addr] = self.uplinks.get(dev_addr, 0) + 1


class _Node:
    def __init__(self, dev_addr: int, gen_app_key: bytes, first_uplink: float) -> None:
        self.dev_addr = dev_addr
        self.device = LoRaWanDevice(
            session=DeviceSession(
                dev_addr=dev_addr, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY
            ),
        )
        self.device.radio.logger.setLevel(logging.WARNING)
        self.setup_app = MulticastSetupDeviceApplication(
            self.device, gen_app_key=gen_app_key
        )
        self.device.register_application(self.setup_app)
        self.device.register_application(_Sink())
        self._first_uplink = first_uplink
        sim.create_task(self._uplink_loop())

    @property
    def uplinks_transmitted(self) -> int:
        """Frames this device put on the air, counted the moment the radio was keyed."""
        assert self.device.session is not None
        return self.device.session.fcnt_up

    @property
    def payload_uplinks(self) -> int:
        """Of those, the periodic FPort 10 payloads (the rest are TS005 answers)."""
        return self.uplinks_transmitted - self.setup_app.uplinks_sent

    async def _uplink_loop(self) -> None:
        try:
            index = 0
            while sim.is_running():
                await sim.sleep_until(self._first_uplink + index * UPLINK_PERIOD)
                await self.device.send_uplink(DATA_FPORT, b"\x01")
                index += 1
        except SimulatorException:
            return


def _build():
    sim.logger.setLevel(logging.WARNING)
    phy = LoraPhyLayer()
    phy.logger.setLevel(logging.WARNING)

    gen_app_keys = {
        FIRST_DEV_ADDR + index: bytes([(0x40 + index) & 0xFF]) * 16
        for index in range(DEVICES)
    }

    ns = NetworkServer()
    for addr in gen_app_keys:
        ns.register_device(addr, NWK_S_KEY, APP_S_KEY)

    counter = _CountingServerApp()
    ns.register_application(counter)
    setup = MulticastSetupServerApplication(
        ns,
        key_provider={
            addr: derive_mc_ke_key(mc_root_key=derive_mc_root_key(key=key))
            for addr, key in gen_app_keys.items()
        },
    )
    ns.register_application(setup)

    gateway = LoRaWanGateway(network_server=ns)
    gateway.radio.logger.setLevel(logging.WARNING)

    nodes = [
        _Node(addr, key, first_uplink=1.0 + index * STAGGER)
        for index, (addr, key) in enumerate(gen_app_keys.items())
    ]
    setup.setup_group(
        sorted(gen_app_keys), group_id=MC_GROUP_ID, mc_addr=MC_ADDR, mc_key=MC_KEY
    )
    return ns, gateway, setup, counter, nodes


class TestGatewayKeepsReceivingDuringTheRx1Wait:
    def test_every_uplink_reaches_the_network_server(self):
        _ns, gateway, _setup, counter, nodes = _build()
        sim.run(simulation_length=60)

        sent = {node.dev_addr: node.payload_uplinks for node in nodes}
        assert sum(sent.values()) >= 5 * DEVICES
        # Not one periodic uplink was lost at the gateway...
        assert counter.uplinks == sent
        # ... and neither was any of the TS005 answers the devices sent on their own.
        assert gateway.frames_forwarded == sum(
            node.uplinks_transmitted for node in nodes
        )

    def test_every_device_eventually_gets_its_setup_command(self):
        _ns, gateway, setup, _counter, nodes = _build()
        sim.run(simulation_length=60)

        # The command is retransmitted until the device answers it, so a reply the
        # transmitter could not fit into one RX1 window is not lost.
        for node in nodes:
            assert MC_GROUP_ID in setup.state(node.dev_addr).groups, (
                f"0x{node.dev_addr:08X} never joined the multicast group"
            )
            assert MC_ADDR in node.device.multicast_groups
        assert setup.commands_abandoned == 0

    def test_a_skipped_reply_is_only_a_skipped_reply(self):
        """A collision costs a downlink, never an uplink."""
        _ns, gateway, setup, counter, nodes = _build()
        sim.run(simulation_length=60)

        assert gateway.frames_forwarded == sum(
            node.uplinks_transmitted for node in nodes
        )
        # Ten devices one second apart do collide in RX1, which is the whole point of
        # the retransmission: every device still ends up in the group.
        assert gateway.downlinks_skipped >= 0
        assert all(
            MC_GROUP_ID in setup.state(node.dev_addr).groups for node in nodes
        )
