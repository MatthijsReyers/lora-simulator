"""End-to-end simulation tests for the TS005 Remote Multicast Setup package (FPort 200).

Unlike ``tests/test_lorawan_fuota_multicast_setup.py``, which drives the package's
state machines directly, every test here runs a **real simulation**: a
:class:`NetworkServer`, a :class:`LoRaWanGateway` and several
:class:`LoRaWanDevice` instances exchanging actual LoRa frames over the PHY layer.

What is covered:

- the full setup handshake (``McGroupSetupReq`` -> ``McGroupSetupAns``) carried on
  the devices' own periodic uplinks and the network server's RX1 replies;
- a Class C multicast session scheduled for a future ``SessionTime``: the devices
  switch to Class C at that instant and revert ``2**TimeOut`` seconds later
  (TS005-2.0.0 §4.5);
- a multicast payload transmitted inside the session window reaching every device,
  and one transmitted outside it reaching none;
- the Class B variant of the same flow, with beacons and ping slots
  (TS005-2.0.0 §4.6).
"""

from __future__ import annotations

import logging

import pytest

from simulator.environment import simulation_env as sim
from simulator.exceptions import SimulatorException
from simulator.lora.phy_layer import LoraPhyLayer
from simulator.lorawan.application import Application
from simulator.lorawan.device import DeviceSession, LoRaWanDevice
from simulator.lorawan.enums.operating_mode import OperatingMode
from simulator.lorawan.fuota.crypto import derive_mc_ke_key, derive_mc_root_key
from simulator.lorawan.fuota.multicast_setup import (
    MulticastSetupDeviceApplication,
    MulticastSetupServerApplication,
)
from simulator.lorawan.gateway import LoRaWanGateway
from simulator.lorawan.network_server import NetworkServer
from simulator.lorawan.beacon import compute_ping_slot_times
from simulator.lorawan.region import BEACON_INTERVAL


# ── Test credentials and radio parameters ───────────────────────────────────
NWK_S_KEY = bytes.fromhex("2B7E151628AED2A6ABF7158809CF4F3C")
APP_S_KEY = bytes.fromhex("3C4F9C098815F7ABA6D2AE281615E72B")

MC_ADDR = 0xFF000001
MC_KEY = bytes.fromhex("AABBCCDD11223344AABBCCDD11223344")
MC_GROUP_ID = 0

#: FPort the firmware payloads travel on, i.e. what TS004 would use in a real campaign.
DATA_FPORT = 10

#: Everything stays on the gateway's default channel and data rate so the devices'
#: session parameters and the gateway's transmitter agree (the simulator's LoRaWAN
#: layer is effectively single channel).
DL_FREQUENCY = 868_100_000
DL_DATA_RATE = 5

DEV_ADDRS = [0x26011001, 0x26011002, 0x26011003]
GEN_APP_KEYS = [
    bytes.fromhex("000102030405060708090A0B0C0D0E0F"),
    bytes.fromhex("0F0E0D0C0B0A09080706050403020100"),
    bytes.fromhex("A0A1A2A3A4A5A6A7A8A9AAABACADAEAF"),
]


def _mc_ke_key(gen_app_key: bytes) -> bytes:
    return derive_mc_ke_key(mc_root_key=derive_mc_root_key(key=gen_app_key))


class DummyApp(Application):
    """Device-side sink for the multicast firmware payloads."""

    def __init__(self, fport: int = DATA_FPORT) -> None:
        self._port = fport
        self.downlinks: list[bytes] = []
        #: ``(time, payload)`` for every downlink, so tests can check *when* it arrived.
        self.log: list[tuple[float, bytes]] = []

    def port(self) -> int:
        return self._port

    async def on_uplink(self, dev_addr: int, payload: bytes) -> None:
        return None

    async def on_downlink(self, payload: bytes) -> None:
        self.downlinks.append(payload)
        self.log.append((sim.current_time(), payload))


class FuotaNode:
    """A device with the TS005 package, a payload sink and a periodic uplink loop.

    The uplink loop is what carries the package's answers and pulls the network
    server's pending commands down in RX1 — exactly the way the clock-sync example's
    sensor loop drives TS003.
    """

    def __init__(
        self,
        dev_addr: int,
        gen_app_key: bytes,
        *,
        first_uplink: float,
        period: float,
        uplinks: int,
        class_b_at: float | None = None,
    ) -> None:
        self.dev_addr = dev_addr
        self.device = LoRaWanDevice(
            session=DeviceSession(
                dev_addr=dev_addr, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY,
            ),
            data_rate=DL_DATA_RATE,
        )
        self.device.radio.logger.setLevel(logging.WARNING)
        self.setup_app = MulticastSetupDeviceApplication(
            self.device, gen_app_key=gen_app_key,
        )
        self.data_app = DummyApp()
        self.device.register_application(self.setup_app)
        self.device.register_application(self.data_app)

        self.modes: list[tuple[float, OperatingMode]] = []
        self._first_uplink = first_uplink
        self._period = period
        self._uplinks = uplinks
        self._class_b_at = class_b_at

        sim.create_task(self._uplink_loop())
        sim.create_task(self._mode_sampler())
        if class_b_at is not None:
            sim.create_task(self._enter_class_b(class_b_at))

    async def _enter_class_b(self, at_time: float) -> None:
        """Acquire unicast Class B before the multicast session, as TS005 §4.6 assumes."""
        try:
            await sim.sleep_until(at_time)
            await self.device.switch_mode(OperatingMode.CLASS_B)
        except SimulatorException:
            return

    async def _uplink_loop(self) -> None:
        # The loop outlives the simulation; `sleep` raises when the clock stops.
        try:
            # Absolute wake-up times: a relative sleep would drift by however long the
            # RX windows and the package's own answer uplink took, and the devices would
            # eventually transmit on top of each other.
            for index in range(self._uplinks):
                await sim.sleep_until(self._first_uplink + index * self._period)
                await self.device.send_uplink(DATA_FPORT, bytes([index]))
        except SimulatorException:
            return

    async def _mode_sampler(self) -> None:
        try:
            while True:
                await sim.sleep(0.1)
                self.modes.append(
                    (round(sim.current_time(), 2), self.device.operating_mode)
                )
        except SimulatorException:
            return

    def mode_at(self, timestamp: float) -> OperatingMode:
        """The operating mode sampled closest to (and not after) *timestamp*."""
        best = self.modes[0][1]
        for (sampled_at, mode) in self.modes:
            if sampled_at > timestamp:
                break
            best = mode
        return best


def _quiet_logging() -> None:
    sim.logger.setLevel(logging.WARNING)
    phy = LoraPhyLayer()
    phy.logger.setLevel(logging.WARNING)


def _build_network(
    *,
    first_uplink: float = 0.5,
    stagger: float = 2.0,
    period: float = 6.0,
    uplinks: int = 2,
    class_b_enabled: bool = False,
    class_b_at: float | None = None,
    node_count: int = 3,
) -> tuple[NetworkServer, LoRaWanGateway, MulticastSetupServerApplication, list[FuotaNode]]:
    """Wire up a network server, a gateway, the TS005 server package and N nodes."""
    _quiet_logging()

    ns = NetworkServer(default_data_rate=DL_DATA_RATE)
    for dev_addr in DEV_ADDRS[:node_count]:
        ns.register_device(dev_addr, NWK_S_KEY, APP_S_KEY)

    server_app = MulticastSetupServerApplication(
        ns,
        key_provider={
            dev_addr: _mc_ke_key(key)
            for dev_addr, key in zip(DEV_ADDRS[:node_count], GEN_APP_KEYS)
        },
    )
    ns.register_application(server_app)

    gateway = LoRaWanGateway(network_server=ns, class_b_enabled=class_b_enabled)
    gateway.radio.logger.setLevel(logging.WARNING)

    nodes = [
        FuotaNode(
            dev_addr,
            gen_app_key,
            first_uplink=first_uplink + index * stagger,
            period=period,
            uplinks=uplinks,
            class_b_at=class_b_at,
        )
        for index, (dev_addr, gen_app_key) in enumerate(
            zip(DEV_ADDRS[:node_count], GEN_APP_KEYS)
        )
    ]
    return ns, gateway, server_app, nodes


# ═══════════════════════════════════════════════════════════════════════════
# Class C multicast session (TS005 §4.5)
# ═══════════════════════════════════════════════════════════════════════════

SESSION_TIME = 14
SESSION_TIMEOUT = 2          # TimeOut exponent: the session lasts 2**2 = 4 seconds
SESSION_END = SESSION_TIME + (1 << SESSION_TIMEOUT)

IN_SESSION_PAYLOAD = b"\xCA\xFE\xBA\xBE"
OUT_OF_SESSION_PAYLOAD = b"\xDE\xAD"


class TestClassCSessionEndToEnd:
    def test_setup_session_and_multicast_delivery(self) -> None:
        ns, gateway, server_app, nodes = _build_network()
        dev_addrs = [node.dev_addr for node in nodes]

        server_app.setup_group(
            dev_addrs,
            group_id=MC_GROUP_ID,
            mc_addr=MC_ADDR,
            mc_key=MC_KEY,
            data_rate=DL_DATA_RATE,
            frequency=DL_FREQUENCY,
        )

        async def orchestrator() -> None:
            # By t=6 every device has answered the setup request on its own uplink.
            await sim.sleep(6.0)
            assert server_app.all_devices_acked_group(MC_GROUP_ID), (
                f"only {sorted(server_app.device_state)} answered"
            )
            server_app.start_class_c_session(
                dev_addrs,
                group_id=MC_GROUP_ID,
                session_time=SESSION_TIME,
                session_timeout=SESSION_TIMEOUT,
                dl_frequency=DL_FREQUENCY,
                data_rate=DL_DATA_RATE,
            )

        # One frame inside the session window, one well after it closed.
        ns.schedule_multicast_downlink(
            MC_ADDR, fport=DATA_FPORT, payload=IN_SESSION_PAYLOAD, at_time=15.3,
        )
        ns.schedule_multicast_downlink(
            MC_ADDR, fport=DATA_FPORT, payload=OUT_OF_SESSION_PAYLOAD, at_time=20.0,
        )

        sim.create_task(orchestrator())
        sim.run(simulation_length=24)

        # ── The setup phase completed for every device ──
        assert server_app.all_devices_acked_group(MC_GROUP_ID)
        for node in nodes:
            group = node.device.get_multicast_group(MC_ADDR)
            assert group is not None, f"0x{node.dev_addr:08X} never joined the group"
            assert group.group_id == MC_GROUP_ID
            # Device and network server derived the same multicast session keys.
            record = ns.get_multicast_group(MC_ADDR)
            assert record is not None
            assert group.app_s_key == record.app_s_key
            assert group.nwk_s_key == record.nwk_s_key

        # ── Every device accepted the session and reported a sane TimeToStart ──
        assert server_app.devices_in_session(MC_GROUP_ID) == set(dev_addrs)
        for node in nodes:
            time_to_start = server_app.device_state[node.dev_addr].time_to_start
            assert MC_GROUP_ID in time_to_start
            assert 0 < time_to_start[MC_GROUP_ID] <= SESSION_TIME

            record = node.setup_app.session_history[-1]
            assert record.mode is OperatingMode.CLASS_C
            assert record.started is True
            assert record.timeout_seconds == (1 << SESSION_TIMEOUT)

        # ── Devices switched to Class C at SessionTime and reverted at the timeout ──
        for node in nodes:
            assert node.mode_at(SESSION_TIME - 1.0) is OperatingMode.CLASS_A
            assert node.mode_at(SESSION_TIME + 1.0) is OperatingMode.CLASS_C
            assert node.mode_at(SESSION_END - 0.5) is OperatingMode.CLASS_C
            assert node.mode_at(SESSION_END + 1.0) is OperatingMode.CLASS_A

        # ── The multicast frame inside the window reached every device... ──
        assert gateway.multicast_frames_sent == 2
        for node in nodes:
            assert node.data_app.downlinks == [IN_SESSION_PAYLOAD], (
                f"0x{node.dev_addr:08X} received {node.data_app.downlinks}"
            )
            received_at = node.data_app.log[0][0]
            assert SESSION_TIME <= received_at <= SESSION_END

    def test_session_switch_happens_within_a_second_of_session_time(self) -> None:
        """The mode flip must land on ``SessionTime``, not on the answer's uplink time."""
        ns, _, server_app, nodes = _build_network(node_count=2)
        dev_addrs = [node.dev_addr for node in nodes]

        server_app.setup_group(
            dev_addrs, group_id=MC_GROUP_ID, mc_addr=MC_ADDR, mc_key=MC_KEY,
            data_rate=DL_DATA_RATE, frequency=DL_FREQUENCY,
        )

        async def orchestrator() -> None:
            await sim.sleep(6.0)
            server_app.start_class_c_session(
                dev_addrs, group_id=MC_GROUP_ID, session_time=SESSION_TIME,
                session_timeout=SESSION_TIMEOUT, dl_frequency=DL_FREQUENCY,
                data_rate=DL_DATA_RATE,
            )

        sim.create_task(orchestrator())
        sim.run(simulation_length=24)

        for node in nodes:
            switches = [
                time for (time, mode), (_, previous) in zip(node.modes[1:], node.modes)
                if mode is OperatingMode.CLASS_C and previous is OperatingMode.CLASS_A
            ]
            reverts = [
                time for (time, mode), (_, previous) in zip(node.modes[1:], node.modes)
                if mode is OperatingMode.CLASS_A and previous is OperatingMode.CLASS_C
            ]
            assert len(switches) == 1 and len(reverts) == 1
            assert abs(switches[0] - SESSION_TIME) <= 1.0
            assert abs(reverts[0] - SESSION_END) <= 1.0

    def test_devices_outside_the_group_are_not_disturbed(self) -> None:
        """A device that was never set up stays Class A and hears nothing."""
        ns, gateway, server_app, nodes = _build_network()
        members = [nodes[0].dev_addr, nodes[1].dev_addr]
        outsider = nodes[2]

        server_app.setup_group(
            members, group_id=MC_GROUP_ID, mc_addr=MC_ADDR, mc_key=MC_KEY,
            data_rate=DL_DATA_RATE, frequency=DL_FREQUENCY,
        )

        async def orchestrator() -> None:
            await sim.sleep(6.0)
            server_app.start_class_c_session(
                members, group_id=MC_GROUP_ID, session_time=SESSION_TIME,
                session_timeout=SESSION_TIMEOUT, dl_frequency=DL_FREQUENCY,
                data_rate=DL_DATA_RATE,
            )

        ns.schedule_multicast_downlink(
            MC_ADDR, fport=DATA_FPORT, payload=IN_SESSION_PAYLOAD, at_time=15.3,
        )

        sim.create_task(orchestrator())
        sim.run(simulation_length=24)

        assert server_app.devices_in_session(MC_GROUP_ID) == set(members)
        for node in nodes[:2]:
            assert node.data_app.downlinks == [IN_SESSION_PAYLOAD]

        assert outsider.device.multicast_groups == {}
        assert outsider.data_app.downlinks == []
        assert outsider.mode_at(SESSION_TIME + 1.0) is OperatingMode.CLASS_A


# ═══════════════════════════════════════════════════════════════════════════
# Class B multicast session (TS005 §4.6)
# ═══════════════════════════════════════════════════════════════════════════

#: Devices acquire unicast Class B here, a beacon period before the session starts, so
#: they already hold beacon lock when it opens (TS005 §4.6 assumes exactly that).
CLASS_B_LOCK_TIME = 100.0
#: Two beacon periods in, and a multiple of 128 as TS005 §4.6 requires.
CLASS_B_SESSION_TIME = 2 * BEACON_INTERVAL
CLASS_B_TIMEOUT = 1          # TimeOut exponent: 128 * 2**1 = 256 seconds
CLASS_B_PERIODICITY = 4      # 128 >> 4 = 8 ping slots per beacon period
CLASS_B_SESSION_END = CLASS_B_SESSION_TIME + BEACON_INTERVAL * (1 << CLASS_B_TIMEOUT)


class TestClassBSessionEndToEnd:
    def test_class_b_session_delivers_in_a_ping_slot(self) -> None:
        ns, gateway, server_app, nodes = _build_network(
            node_count=2,
            first_uplink=1.0,
            stagger=2.0,
            period=20.0,
            uplinks=4,
            class_b_enabled=True,
            class_b_at=CLASS_B_LOCK_TIME,
        )
        dev_addrs = [node.dev_addr for node in nodes]

        server_app.setup_group(
            dev_addrs, group_id=MC_GROUP_ID, mc_addr=MC_ADDR, mc_key=MC_KEY,
            data_rate=DL_DATA_RATE, frequency=DL_FREQUENCY,
        )
        ns.enable_multicast_class_b(MC_ADDR, ping_nb=128 >> CLASS_B_PERIODICITY)

        async def orchestrator() -> None:
            # Setup is answered on the devices' uplinks at t=1 and t=3.
            await sim.sleep(30.0)
            assert server_app.all_devices_acked_group(MC_GROUP_ID)
            server_app.start_class_b_session(
                dev_addrs,
                group_id=MC_GROUP_ID,
                session_time=CLASS_B_SESSION_TIME,
                session_timeout=CLASS_B_TIMEOUT,
                periodicity=CLASS_B_PERIODICITY,
                dl_frequency=DL_FREQUENCY,
                data_rate=DL_DATA_RATE,
            )
            # Queue the payload once the session is open; the network server places it in
            # the group's ping slots of the next beacon period (TS005 §4.6).
            await sim.sleep_until(CLASS_B_SESSION_TIME + 10.0)
            ns.schedule_multicast_downlink(
                MC_ADDR, fport=DATA_FPORT, payload=IN_SESSION_PAYLOAD,
            )

        sim.create_task(orchestrator())
        sim.run(simulation_length=CLASS_B_SESSION_TIME + 2 * BEACON_INTERVAL)

        assert server_app.all_devices_acked_group(MC_GROUP_ID)
        assert server_app.devices_in_session(MC_GROUP_ID) == set(dev_addrs)

        for node in nodes:
            record = node.setup_app.session_history[-1]
            assert record.mode is OperatingMode.CLASS_B
            assert record.started is True
            assert record.session_time % BEACON_INTERVAL == 0
            # TS005 §4.6: TimeOut counts beacon periods, not seconds.
            assert record.timeout_seconds == BEACON_INTERVAL * (1 << CLASS_B_TIMEOUT)
            assert record.ping_periodicity == CLASS_B_PERIODICITY

            # The device stayed in Class B for the whole session.
            assert node.mode_at(CLASS_B_SESSION_TIME + 10.0) is OperatingMode.CLASS_B

            assert node.data_app.downlinks == [IN_SESSION_PAYLOAD], (
                f"0x{node.dev_addr:08X} received {node.data_app.downlinks}"
            )
            # It arrived in a ping slot inside the session window.
            received_at = node.data_app.log[0][0]
            assert CLASS_B_SESSION_TIME <= received_at <= CLASS_B_SESSION_END

    def test_ping_slots_follow_the_multicast_address(self) -> None:
        """The session's slots are computed from McAddr, not from the device's DevAddr."""
        _, _, server_app, nodes = _build_network(
            node_count=2,
            first_uplink=1.0,
            stagger=2.0,
            period=20.0,
            uplinks=3,
            class_b_enabled=True,
            class_b_at=CLASS_B_LOCK_TIME,
        )
        dev_addrs = [node.dev_addr for node in nodes]

        server_app.setup_group(
            dev_addrs, group_id=MC_GROUP_ID, mc_addr=MC_ADDR, mc_key=MC_KEY,
            data_rate=DL_DATA_RATE, frequency=DL_FREQUENCY,
        )

        async def orchestrator() -> None:
            await sim.sleep(30.0)
            server_app.start_class_b_session(
                dev_addrs, group_id=MC_GROUP_ID, session_time=CLASS_B_SESSION_TIME,
                session_timeout=CLASS_B_TIMEOUT, periodicity=CLASS_B_PERIODICITY,
                dl_frequency=DL_FREQUENCY, data_rate=DL_DATA_RATE,
            )

        captured: list[list[float]] = []

        async def sampler() -> None:
            try:
                await sim.sleep_until(CLASS_B_SESSION_TIME + 5.0)
                for node in nodes:
                    slots = node.device.collect_ping_slots(CLASS_B_SESSION_TIME)
                    captured.append(
                        [time for (time, session) in slots if session is not None]
                    )
            except SimulatorException:
                return

        sim.create_task(orchestrator())
        sim.create_task(sampler())
        sim.run(simulation_length=CLASS_B_SESSION_TIME + BEACON_INTERVAL)

        assert len(captured) == 2
        for slots in captured:
            assert len(slots) == 128 >> CLASS_B_PERIODICITY
        # Both devices listen in the *same* slots, because the slots come from McAddr.
        assert captured[0] == captured[1]
        assert captured[0] == compute_ping_slot_times(
            CLASS_B_SESSION_TIME, MC_ADDR, 128 >> CLASS_B_PERIODICITY,
        )
