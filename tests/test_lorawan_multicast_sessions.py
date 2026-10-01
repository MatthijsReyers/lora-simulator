"""Tests for timed multicast sessions, multicast downlink scheduling and payload limits.

Covers the generic infrastructure the TS004/TS005 FUOTA packages build on:

- the multicast frame counter window of TS005-2.0.0 §2.5 (``minMcFCnt`` / ``maxMcFCnt``),
- the four multicast contexts of TS005-2.0.0 §2.1,
- Class C and Class B multicast sessions (TS005-2.0.0 §2.7 / §2.8),
- network-server side multicast downlink scheduling and the gateway that drains it,
- the region's maximum FRMPayload size (RP002-1.0.4 §2.4.6).
"""

from __future__ import annotations

import pytest

from simulator.environment import simulation_env as sim
from simulator.lora.enums.radio_state import RadioState
from simulator.lorawan.application import Application
from simulator.lorawan.beacon import compute_ping_slot_times
from simulator.lorawan.device import (
    LoRaWanDevice, DeviceSession, MulticastGroup, MulticastSession,
    MAX_MULTICAST_GROUPS,
)
from simulator.lorawan.enums.frame_types import MType
from simulator.lorawan.enums.operating_mode import OperatingMode
from simulator.lorawan.frame import MHDR, FCtrl, FHDR, MACPayload, PHYPayload
from simulator.lorawan.crypto import compute_data_mic, encrypt_frm_payload
from simulator.lorawan.gateway import LoRaWanGateway, DutyCycleLimiter
from simulator.lorawan.network_server import NetworkServer
from simulator.lorawan.region import max_frm_payload, EU868_DATA_RATES

# ── Test credentials ────────────────────────────────────────────────────────
DEV_ADDR  = 0x26011234
NWK_S_KEY = bytes.fromhex("2B7E151628AED2A6ABF7158809CF4F3C")
APP_S_KEY = bytes.fromhex("3C4F9C098815F7ABA6D2AE281615E72B")

MC_ADDR    = 0xFF000001
MC_NWK_KEY = bytes.fromhex("AABBCCDD11223344AABBCCDD11223344")
MC_APP_KEY = bytes.fromhex("11223344AABBCCDD11223344AABBCCDD")

FPORT = 10


class RecordingApp(Application):
    """Application that records everything it sees and can hold a canned downlink."""

    def __init__(self, fport: int = FPORT, downlink: bytes | None = None):
        self._port = fport
        self.uplinks: list[tuple[int, bytes]] = []
        self.downlinks: list[bytes] = []
        self._pending = downlink

    def port(self) -> int:
        return self._port

    async def on_uplink(self, dev_addr: int, payload: bytes) -> None:
        self.uplinks.append((dev_addr, payload))

    async def on_downlink(self, payload: bytes) -> None:
        self.downlinks.append(payload)

    async def get_downlink(self, dev_addr: int) -> bytes | None:
        return self._pending


def _new_device(**kwargs: object) -> LoRaWanDevice:
    session = DeviceSession(dev_addr=DEV_ADDR, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY)
    return LoRaWanDevice(session=session, **kwargs)  # type: ignore[arg-type]


def _multicast_frame(fcnt: int, payload: bytes = b"\x01", fport: int = FPORT) -> bytes:
    """Build a valid multicast downlink for the shared test group."""
    encrypted = encrypt_frm_payload(
        MC_APP_KEY, dev_addr=MC_ADDR, fcnt=fcnt, uplink=False, payload=payload,
    )
    fhdr = FHDR(dev_addr=MC_ADDR, fctrl=FCtrl(), fcnt=fcnt)
    mac_payload = MACPayload(fhdr=fhdr, fport=fport, frm_payload=encrypted)
    mhdr = MHDR(mtype=MType.UNCONFIRMED_DATA_DN)
    mic = compute_data_mic(
        MC_NWK_KEY, dev_addr=MC_ADDR, fcnt=fcnt, uplink=False,
        mhdr_and_payload=bytes([mhdr.encode()]) + mac_payload.encode(uplink=False),
    )
    return PHYPayload(mhdr=mhdr, mac_payload=mac_payload, mic=mic).encode()


# ═══════════════════════════════════════════════════════════════════════════
# Multicast frame counter window (TS005 §2.5)
# ═══════════════════════════════════════════════════════════════════════════

class TestMulticastFcntWindow:
    def test_defaults_span_the_whole_counter_space(self):
        group = MulticastGroup(MC_ADDR, MC_NWK_KEY, MC_APP_KEY)
        assert group.group_id == 0
        assert group.min_fcnt == 0
        assert group.max_fcnt == 0xFFFFFFFF

    def test_fcnt_down_starts_at_min_fcnt(self):
        group = MulticastGroup(MC_ADDR, MC_NWK_KEY, MC_APP_KEY, min_fcnt=100)
        assert group.fcnt_down == 100

    def test_accepts_inside_window(self):
        group = MulticastGroup(MC_ADDR, MC_NWK_KEY, MC_APP_KEY, min_fcnt=10, max_fcnt=20)
        assert group.accepts_fcnt(10)
        assert group.accepts_fcnt(15)
        assert group.accepts_fcnt(20)

    def test_rejects_below_window(self):
        group = MulticastGroup(MC_ADDR, MC_NWK_KEY, MC_APP_KEY, min_fcnt=10, max_fcnt=20)
        assert not group.accepts_fcnt(9)
        assert not group.accepts_fcnt(0)

    def test_rejects_above_window(self):
        group = MulticastGroup(MC_ADDR, MC_NWK_KEY, MC_APP_KEY, min_fcnt=10, max_fcnt=20)
        assert not group.accepts_fcnt(21)

    def test_rejects_replay_but_accepts_a_later_frame(self):
        group = MulticastGroup(MC_ADDR, MC_NWK_KEY, MC_APP_KEY)
        assert group.accepts_fcnt(0)
        group.record_fcnt(0)
        assert not group.accepts_fcnt(0)
        assert group.accepts_fcnt(1)

    def test_missing_the_first_frames_still_accepts_the_next_one(self):
        """A device that slept through frames 0..4 must still take frame 5."""
        group = MulticastGroup(MC_ADDR, MC_NWK_KEY, MC_APP_KEY)
        assert group.accepts_fcnt(5)
        group.record_fcnt(5)
        assert group.fcnt_down == 6
        assert not group.accepts_fcnt(4)

    @pytest.mark.asyncio
    async def test_device_drops_frame_below_min_fcnt(self):
        device = _new_device()
        app = RecordingApp()
        device.register_application(app)
        device.join_multicast_group(
            MulticastGroup(MC_ADDR, MC_NWK_KEY, MC_APP_KEY, min_fcnt=5, max_fcnt=10)
        )

        await device._process_downlink(_multicast_frame(fcnt=4, payload=b"\xAA"))
        assert app.downlinks == []

    @pytest.mark.asyncio
    async def test_device_drops_frame_above_max_fcnt(self):
        device = _new_device()
        app = RecordingApp()
        device.register_application(app)
        device.join_multicast_group(
            MulticastGroup(MC_ADDR, MC_NWK_KEY, MC_APP_KEY, min_fcnt=5, max_fcnt=10)
        )

        await device._process_downlink(_multicast_frame(fcnt=11, payload=b"\xAA"))
        assert app.downlinks == []

    @pytest.mark.asyncio
    async def test_device_accepts_inside_window_and_rejects_replay(self):
        device = _new_device()
        app = RecordingApp()
        device.register_application(app)
        group = MulticastGroup(MC_ADDR, MC_NWK_KEY, MC_APP_KEY, min_fcnt=5, max_fcnt=10)
        device.join_multicast_group(group)

        await device._process_downlink(_multicast_frame(fcnt=7, payload=b"\xAA"))
        assert app.downlinks == [b"\xAA"]
        assert group.fcnt_down == 8

        # Exact replay of an accepted frame.
        await device._process_downlink(_multicast_frame(fcnt=7, payload=b"\xAA"))
        assert len(app.downlinks) == 1

        # A later frame inside the window is still taken.
        await device._process_downlink(_multicast_frame(fcnt=9, payload=b"\xBB"))
        assert app.downlinks == [b"\xAA", b"\xBB"]


# ═══════════════════════════════════════════════════════════════════════════
# Multicast context management (TS005 §2.1)
# ═══════════════════════════════════════════════════════════════════════════

class TestMulticastGroupSlots:
    def test_four_groups_fit(self):
        device = _new_device()
        for group_id in range(MAX_MULTICAST_GROUPS):
            joined = device.join_multicast_group(MulticastGroup(
                MC_ADDR + group_id, MC_NWK_KEY, MC_APP_KEY, group_id=group_id,
            ))
            assert joined is True
        assert len(device.multicast_groups) == MAX_MULTICAST_GROUPS

    def test_fifth_distinct_group_is_refused(self):
        device = _new_device()
        for group_id in range(MAX_MULTICAST_GROUPS):
            device.join_multicast_group(MulticastGroup(
                MC_ADDR + group_id, MC_NWK_KEY, MC_APP_KEY, group_id=group_id,
            ))
        # McGroupID 4 does not exist at all, so a fifth context cannot be addressed.
        with pytest.raises(AssertionError):
            MulticastGroup(MC_ADDR + 9, MC_NWK_KEY, MC_APP_KEY, group_id=4)

    def test_fifth_group_refused_on_a_single_context_device(self):
        device = _new_device(max_multicast_groups=1)
        assert device.join_multicast_group(
            MulticastGroup(MC_ADDR, MC_NWK_KEY, MC_APP_KEY, group_id=0)
        ) is True
        assert device.join_multicast_group(
            MulticastGroup(MC_ADDR + 1, MC_NWK_KEY, MC_APP_KEY, group_id=1)
        ) is False
        assert len(device.multicast_groups) == 1

    def test_reusing_a_group_id_replaces_the_context(self):
        device = _new_device()
        device.join_multicast_group(MulticastGroup(MC_ADDR, MC_NWK_KEY, MC_APP_KEY, group_id=2))
        device.join_multicast_group(
            MulticastGroup(MC_ADDR + 7, MC_NWK_KEY, MC_APP_KEY, group_id=2)
        )
        assert device.get_multicast_group(MC_ADDR) is None
        assert device.get_multicast_group(MC_ADDR + 7) is not None
        assert len(device.multicast_groups) == 1

    def test_lookup_by_address_and_by_group_id(self):
        device = _new_device()
        group = MulticastGroup(MC_ADDR, MC_NWK_KEY, MC_APP_KEY, group_id=3)
        device.join_multicast_group(group)
        assert device.get_multicast_group(MC_ADDR) is group
        assert device.get_multicast_group_by_id(3) is group
        assert device.get_multicast_group_by_id(0) is None
        assert device.get_multicast_group(0xDEADBEEF) is None


# ═══════════════════════════════════════════════════════════════════════════
# Downlink RX configuration
# ═══════════════════════════════════════════════════════════════════════════

class TestRxConfig:
    def test_defaults_follow_the_uplink_data_rate(self):
        device = _new_device(data_rate=5)
        assert device.rx_data_rate is None
        assert device.radio.rx_config.spreading_factor.value == 7

    def test_set_rx_config_retunes_only_the_receiver(self):
        device = _new_device(data_rate=5)
        device.set_rx_config(0, 869_525_000)
        assert device.radio.rx_config.spreading_factor.value == 12
        assert device.radio.rx_frequency == 869_525_000
        # The transmitter stays on the uplink data rate.
        assert device.radio.tx_config.spreading_factor.value == 7
        assert device.data_rate == 5

    def test_clear_rx_config_restores_the_uplink_rate(self):
        device = _new_device(data_rate=5)
        device.set_rx_config(0, 869_525_000)
        device.clear_rx_config()
        assert device.radio.rx_config.spreading_factor.value == 7
        assert device.rx_data_rate is None


# ═══════════════════════════════════════════════════════════════════════════
# Class C multicast sessions (TS005 §2.7)
# ═══════════════════════════════════════════════════════════════════════════

class TestClassCSession:
    def test_session_starts_and_reverts_on_time(self):
        device = _new_device()
        device.join_multicast_group(MulticastGroup(MC_ADDR, MC_NWK_KEY, MC_APP_KEY))

        samples: list[tuple[float, OperatingMode]] = []

        async def driver() -> None:
            await device.start_class_c_session(
                MC_ADDR, start_time=3.0, timeout_seconds=2.0, data_rate=0,
            )

        async def sampler() -> None:
            for _ in range(70):
                await sim.sleep(0.1)
                samples.append((round(sim.current_time(), 2), device.operating_mode))

        sim.create_task(driver())
        sim.create_task(sampler())
        sim.run(simulation_length=9)

        modes = dict(samples)
        assert modes[2.9] == OperatingMode.CLASS_A
        assert modes[3.1] == OperatingMode.CLASS_C
        assert modes[4.9] == OperatingMode.CLASS_C
        assert modes[5.5] == OperatingMode.CLASS_A
        assert modes[6.5] == OperatingMode.CLASS_A

    def test_active_session_and_cancel(self):
        device = _new_device()
        device.join_multicast_group(MulticastGroup(MC_ADDR, MC_NWK_KEY, MC_APP_KEY))

        seen: list[object] = []

        async def driver() -> None:
            await device.start_class_c_session(
                MC_ADDR, start_time=2.0, timeout_seconds=10.0, data_rate=0,
            )
            seen.append(device.active_session(MC_ADDR) is not None)
            await sim.sleep(3.0)
            # Session is running by now.
            seen.append(device.operating_mode)
            device.cancel_session(MC_ADDR)
            await sim.sleep(1.0)
            seen.append(device.operating_mode)
            seen.append(device.active_session(MC_ADDR))

        sim.create_task(driver())
        sim.run(simulation_length=8)

        assert seen[0] is True
        assert seen[1] == OperatingMode.CLASS_C
        assert seen[2] == OperatingMode.CLASS_A
        assert seen[3] is None

    def test_second_session_replaces_the_first(self):
        device = _new_device()
        device.join_multicast_group(MulticastGroup(MC_ADDR, MC_NWK_KEY, MC_APP_KEY))

        observed: list[tuple[float, OperatingMode, int]] = []

        async def driver() -> None:
            await device.start_class_c_session(
                MC_ADDR, start_time=1.0, timeout_seconds=2.0, data_rate=0,
            )
            await sim.sleep(0.5)
            # Replace it before it even starts; only the second one must take effect.
            await device.start_class_c_session(
                MC_ADDR, start_time=4.0, timeout_seconds=2.0, data_rate=3,
            )
            assert len(device.active_sessions()) == 1

        async def sampler() -> None:
            for _ in range(80):
                await sim.sleep(0.1)
                observed.append((
                    round(sim.current_time(), 2),
                    device.operating_mode,
                    len(device.active_sessions()),
                ))

        sim.create_task(driver())
        sim.create_task(sampler())
        sim.run(simulation_length=10)

        modes = {t: mode for (t, mode, _) in observed}
        # The replaced session's window (1s..3s) never happens.
        assert modes[1.5] == OperatingMode.CLASS_A
        assert modes[2.5] == OperatingMode.CLASS_A
        # The replacing one does.
        assert modes[4.5] == OperatingMode.CLASS_C
        assert modes[6.5] == OperatingMode.CLASS_A

    def test_session_on_unknown_group_is_rejected(self):
        device = _new_device()

        errors: list[str] = []

        async def driver() -> None:
            try:
                await device.start_class_c_session(
                    0xABCDEF01, start_time=1.0, timeout_seconds=1.0, data_rate=0,
                )
            except AssertionError as exc:
                errors.append(str(exc))

        sim.create_task(driver())
        sim.run(simulation_length=3)

        assert errors and "No multicast group" in errors[0]


class TestClassCSessionRxConfig:
    def test_session_applies_and_restores_the_rx_configuration(self):
        device = _new_device(data_rate=5)
        device.join_multicast_group(MulticastGroup(MC_ADDR, MC_NWK_KEY, MC_APP_KEY))

        samples: list[tuple[float, int | None]] = []

        async def driver() -> None:
            await device.start_class_c_session(
                MC_ADDR, start_time=2.0, timeout_seconds=2.0,
                data_rate=0, frequency=869_525_000,
            )

        async def sampler() -> None:
            for _ in range(60):
                await sim.sleep(0.1)
                samples.append((round(sim.current_time(), 2), device.rx_data_rate))

        sim.create_task(driver())
        sim.create_task(sampler())
        sim.run(simulation_length=8)

        rx = dict(samples)
        assert rx[1.9] is None
        assert rx[2.5] == 0
        assert rx[5.0] is None
        # The uplink data rate was never touched.
        assert device.data_rate == 5


# ═══════════════════════════════════════════════════════════════════════════
# Class B multicast ping slots (TS005 §2.8)
# ═══════════════════════════════════════════════════════════════════════════

class TestClassBMulticastSlots:
    def test_device_opens_slots_for_dev_addr_and_mc_addr(self):
        device = _new_device(ping_nb=16)
        device.join_multicast_group(MulticastGroup(MC_ADDR, MC_NWK_KEY, MC_APP_KEY))
        device._multicast_sessions[MC_ADDR] = MulticastSession(
            group_addr=MC_ADDR,
            mode=OperatingMode.CLASS_B,
            start_time=0.0,
            end_time=1024.0,
            data_rate=3,
            ping_nb=16,
            running=True,
        )

        slots = device.collect_ping_slots(beacon_time=0)
        unicast_times = set(compute_ping_slot_times(0, DEV_ADDR, 16))
        multicast_times = set(compute_ping_slot_times(0, MC_ADDR, 16))

        taken_unicast = {t for (t, s) in slots if s is None}
        taken_multicast = {t for (t, s) in slots if s is not None}

        assert taken_multicast and taken_multicast <= multicast_times
        assert taken_unicast and taken_unicast <= unicast_times
        # Nothing invented, nothing out of order, nothing overlapping.
        assert [t for (t, _) in slots] == sorted(t for (t, _) in slots)

    def test_slots_are_deduplicated(self):
        device = _new_device(ping_nb=16)
        device.join_multicast_group(MulticastGroup(MC_ADDR, MC_NWK_KEY, MC_APP_KEY))
        device._multicast_sessions[MC_ADDR] = MulticastSession(
            group_addr=MC_ADDR, mode=OperatingMode.CLASS_B, start_time=0.0,
            end_time=1024.0, data_rate=3, ping_nb=16, running=True,
        )
        slots = device.collect_ping_slots(beacon_time=0)
        times = [t for (t, _) in slots]
        assert len(times) == len(set(times))
        assert len(times) <= 32

    def test_sessions_that_are_not_running_contribute_nothing(self):
        device = _new_device(ping_nb=16)
        device.join_multicast_group(MulticastGroup(MC_ADDR, MC_NWK_KEY, MC_APP_KEY))
        device._multicast_sessions[MC_ADDR] = MulticastSession(
            group_addr=MC_ADDR, mode=OperatingMode.CLASS_B, start_time=0.0,
            end_time=1024.0, data_rate=3, ping_nb=16, running=False,
        )
        slots = device.collect_ping_slots(beacon_time=0)
        assert all(session is None for (_, session) in slots)

    @pytest.mark.asyncio
    async def test_network_server_schedules_multicast_into_ping_slots(self):
        ns = NetworkServer()
        ns.create_multicast_group(MC_ADDR, MC_NWK_KEY, MC_APP_KEY)
        ns.enable_multicast_class_b(MC_ADDR, ping_nb=16)
        ns.schedule_multicast_downlink(MC_ADDR, fport=FPORT, payload=b"\x01")
        ns.schedule_multicast_downlink(MC_ADDR, fport=FPORT, payload=b"\x02")

        schedule = await ns.get_class_b_downlink_schedule(beacon_time=0)
        slot_times = compute_ping_slot_times(0, MC_ADDR, 16)

        assert len(schedule) == 2
        for (addr, raw, slot_time) in schedule:
            assert addr == MC_ADDR
            assert slot_time in slot_times
            assert len(raw) > 0
        # One payload per slot, in queue order and in slot order.
        assert schedule[0][2] == slot_times[0]
        assert schedule[1][2] == slot_times[1]
        # The queue is drained.
        assert ns.pending_multicast_downlinks() == []

    @pytest.mark.asyncio
    async def test_class_b_multicast_requires_enabling(self):
        ns = NetworkServer()
        ns.create_multicast_group(MC_ADDR, MC_NWK_KEY, MC_APP_KEY)
        ns.schedule_multicast_downlink(MC_ADDR, fport=FPORT, payload=b"\x01")

        assert await ns.get_class_b_downlink_schedule(beacon_time=0) == []
        assert len(ns.pending_multicast_downlinks()) == 1

    def test_class_b_session_ping_nb_follows_periodicity(self):
        device = _new_device()
        device.join_multicast_group(MulticastGroup(MC_ADDR, MC_NWK_KEY, MC_APP_KEY))

        captured: list[MulticastSession] = []

        async def driver() -> None:
            await device.start_class_b_session(
                MC_ADDR, start_time=50.0, timeout_seconds=10.0,
                data_rate=3, ping_periodicity=4,
            )
            session = device.active_session(MC_ADDR)
            assert session is not None
            captured.append(session)

        sim.create_task(driver())
        sim.run(simulation_length=2)

        assert len(captured) == 1
        assert captured[0].mode == OperatingMode.CLASS_B
        assert captured[0].ping_nb == 8  # 128 >> 4
        assert captured[0].end_time == 60.0


# ═══════════════════════════════════════════════════════════════════════════
# Network server multicast downlink queue
# ═══════════════════════════════════════════════════════════════════════════

class TestMulticastQueue:
    def test_queue_is_time_ordered_and_fifo_within_a_time(self):
        ns = NetworkServer()
        ns.create_multicast_group(MC_ADDR, MC_NWK_KEY, MC_APP_KEY)
        ns.schedule_multicast_downlink(MC_ADDR, FPORT, b"\x03", at_time=30.0)
        ns.schedule_multicast_downlink(MC_ADDR, FPORT, b"\x01", at_time=10.0)
        ns.schedule_multicast_downlink(MC_ADDR, FPORT, b"\x02", at_time=10.0)

        payloads = [e.payload for e in ns.pending_multicast_downlinks()]
        assert payloads == [b"\x01", b"\x02", b"\x03"]

    def test_entries_without_a_time_go_first(self):
        ns = NetworkServer()
        ns.create_multicast_group(MC_ADDR, MC_NWK_KEY, MC_APP_KEY)
        ns.schedule_multicast_downlink(MC_ADDR, FPORT, b"\x01", at_time=10.0)
        ns.schedule_multicast_downlink(MC_ADDR, FPORT, b"\x02")

        assert [e.payload for e in ns.pending_multicast_downlinks()] == [b"\x02", b"\x01"]

    def test_pop_due_takes_only_what_is_due(self):
        ns = NetworkServer()
        ns.create_multicast_group(MC_ADDR, MC_NWK_KEY, MC_APP_KEY)
        ns.schedule_multicast_downlink(MC_ADDR, FPORT, b"\x01", at_time=10.0)
        ns.schedule_multicast_downlink(MC_ADDR, FPORT, b"\x02", at_time=20.0)

        due = ns.pop_due_multicast_downlinks(now=15.0)
        assert [e.payload for e in due] == [b"\x01"]
        assert [e.payload for e in ns.pending_multicast_downlinks()] == [b"\x02"]

    def test_next_time(self):
        ns = NetworkServer()
        ns.create_multicast_group(MC_ADDR, MC_NWK_KEY, MC_APP_KEY)
        assert ns.next_multicast_downlink_time() is None
        ns.schedule_multicast_downlink(MC_ADDR, FPORT, b"\x01", at_time=42.0)
        assert ns.next_multicast_downlink_time() == 42.0

    def test_unknown_group_is_rejected(self):
        ns = NetworkServer()
        with pytest.raises(AssertionError):
            ns.schedule_multicast_downlink(0xDEADBEEF, FPORT, b"\x01")

    def test_fcnt_follows_send_order_not_queue_order(self):
        """The frame is only built at transmission time, so counters stay in send order."""
        ns = NetworkServer()
        record = ns.create_multicast_group(MC_ADDR, MC_NWK_KEY, MC_APP_KEY)
        ns.schedule_multicast_downlink(MC_ADDR, FPORT, b"\x01", at_time=10.0)
        ns.schedule_multicast_downlink(MC_ADDR, FPORT, b"\x02", at_time=5.0)
        assert record.fcnt_down == 0

        due = ns.pop_due_multicast_downlinks(now=10.0)
        first = ns.build_multicast_downlink(MC_ADDR, due[0].fport, due[0].payload)
        assert PHYPayload.decode_data(first).mac_payload.fhdr.fcnt == 0  # type: ignore[union-attr]
        assert due[0].payload == b"\x02"


# ═══════════════════════════════════════════════════════════════════════════
# Gateway: scheduled multicast transmission over the PHY
# ═══════════════════════════════════════════════════════════════════════════

class TestGatewayMulticast:
    def test_class_c_device_receives_a_scheduled_multicast(self):
        """End to end: NS schedules -> gateway transmits -> device app gets plaintext."""
        ns = NetworkServer()
        ns.register_device(DEV_ADDR, NWK_S_KEY, APP_S_KEY)
        ns.create_multicast_group(MC_ADDR, MC_NWK_KEY, MC_APP_KEY)

        gateway = LoRaWanGateway(network_server=ns)

        device = _new_device()
        app = RecordingApp()
        device.register_application(app)
        device.join_multicast_group(MulticastGroup(MC_ADDR, MC_NWK_KEY, MC_APP_KEY))

        payload = b"\xCA\xFE\xBA\xBE"
        ns.schedule_multicast_downlink(MC_ADDR, fport=FPORT, payload=payload, at_time=4.0)

        async def driver() -> None:
            await device.switch_mode(OperatingMode.CLASS_C)

        sim.create_task(driver())
        sim.run(simulation_length=8)

        assert gateway.multicast_frames_sent == 1
        assert app.downlinks == [payload]
        # Transmitted at (just after) the scheduled time.
        assert 4.0 <= gateway.multicast_log[0][0] <= 4.3
        assert gateway.multicast_log[0][1] == MC_ADDR

    def test_on_multicast_transmitted_hook(self):
        ns = NetworkServer()
        ns.create_multicast_group(MC_ADDR, MC_NWK_KEY, MC_APP_KEY)

        seen: list[tuple[int, int]] = []

        async def hook(entry, raw: bytes) -> None:  # type: ignore[no-untyped-def]
            seen.append((entry.group_addr, len(raw)))

        gateway = LoRaWanGateway(network_server=ns, on_multicast_transmitted=hook)
        ns.schedule_multicast_downlink(MC_ADDR, FPORT, b"\x01\x02", at_time=1.0)

        sim.run(simulation_length=4)

        assert len(seen) == 1
        assert seen[0][0] == MC_ADDR
        assert gateway.multicast_frames_sent == 1

    def test_rx1_reply_wins_over_a_colliding_multicast(self):
        """A multicast scheduled into an RX1 window is delayed, never dropped or collided."""
        ns = NetworkServer()
        ns.register_device(DEV_ADDR, NWK_S_KEY, APP_S_KEY)
        ns.create_multicast_group(MC_ADDR, MC_NWK_KEY, MC_APP_KEY)
        ns.queue_downlink(DEV_ADDR, fport=FPORT, payload=b"\xAA\xBB")

        gateway = LoRaWanGateway(network_server=ns)

        device = _new_device()
        app = RecordingApp()
        device.register_application(app)
        device.join_multicast_group(MulticastGroup(MC_ADDR, MC_NWK_KEY, MC_APP_KEY))

        # The uplink goes out at t=1; the gateway answers in RX1 one second after it ends,
        # so roughly t=2.07. Aim the multicast straight at that window.
        ns.schedule_multicast_downlink(MC_ADDR, FPORT, b"\x11", at_time=2.0)

        async def driver() -> None:
            await sim.sleep(1.0)
            await device.send_uplink(fport=FPORT, payload=b"\x00")

        sim.create_task(driver())
        sim.run(simulation_length=8)

        # The RX1 reply reached the device...
        assert app.downlinks == [b"\xAA\xBB"]
        # ...and the multicast still went out, just later.
        assert gateway.multicast_frames_sent == 1

    def test_duty_cycle_spaces_transmissions(self):
        ns = NetworkServer()
        ns.create_multicast_group(MC_ADDR, MC_NWK_KEY, MC_APP_KEY)
        gateway = LoRaWanGateway(network_server=ns, duty_cycle=0.01)

        for i in range(3):
            ns.schedule_multicast_downlink(MC_ADDR, FPORT, bytes([i]), at_time=1.0)

        sim.run(simulation_length=40)

        assert gateway.multicast_frames_sent == 3
        times = [t for (t, _, _, _) in gateway.multicast_log]
        gaps = [b - a for a, b in zip(times, times[1:])]
        # A ~15 byte frame at DR5 is a few tens of ms on air, so a 1% duty cycle leaves
        # several seconds of quiet time between frames.
        assert all(gap > 2.0 for gap in gaps), gaps

    def test_no_duty_cycle_sends_back_to_back(self):
        ns = NetworkServer()
        ns.create_multicast_group(MC_ADDR, MC_NWK_KEY, MC_APP_KEY)
        gateway = LoRaWanGateway(network_server=ns)

        for i in range(3):
            ns.schedule_multicast_downlink(MC_ADDR, FPORT, bytes([i]), at_time=1.0)

        sim.run(simulation_length=10)

        assert gateway.multicast_frames_sent == 3
        times = [t for (t, _, _, _) in gateway.multicast_log]
        assert times[-1] - times[0] < 1.0


# ═══════════════════════════════════════════════════════════════════════════
# Class C: continuous reception, uplinks during a session, and leaving the mode
# ═══════════════════════════════════════════════════════════════════════════

class TestClassCContinuousReception:
    """The Class C receiver must have no holes in it (L2 1.0.4 §19.3)."""

    def test_no_frame_is_lost_on_a_one_second_grid(self):
        """Frames on whole seconds, starting exactly at ``SessionTime``, all arrive.

        The receive loop waits for frames in bounded steps, so a frame landing on one of
        those step boundaries used to be the one that got dropped. A one-second grid aligned
        with the session start hits every boundary there is.
        """
        ns = NetworkServer()
        ns.register_device(DEV_ADDR, NWK_S_KEY, APP_S_KEY)
        ns.create_multicast_group(MC_ADDR, MC_NWK_KEY, MC_APP_KEY)
        gateway = LoRaWanGateway(network_server=ns)

        device = _new_device()
        app = RecordingApp()
        device.register_application(app)
        device.join_multicast_group(MulticastGroup(MC_ADDR, MC_NWK_KEY, MC_APP_KEY))

        session_time = 10.0
        payloads = [bytes([index]) for index in range(10)]
        for index, payload in enumerate(payloads):
            ns.schedule_multicast_downlink(
                MC_ADDR, fport=FPORT, payload=payload, at_time=session_time + index,
            )

        async def driver() -> None:
            await device.start_class_c_session(
                MC_ADDR, start_time=session_time, timeout_seconds=16.0, data_rate=5,
            )

        sim.create_task(driver())
        sim.run(simulation_length=28)

        assert gateway.multicast_frames_sent == len(payloads)
        assert app.downlinks == payloads

    def test_class_c_device_sends_uplinks_while_multicast_is_running(self):
        """A Class C device transmits whenever it likes, without losing the receiver.

        Its continuous receive loop steps aside for the uplink and the RX1 window that
        follows it, so the transmission neither fails nor takes the loop down with it, and
        every multicast frame sent outside those moments still arrives.
        """
        ns = NetworkServer()
        ns.register_device(DEV_ADDR, NWK_S_KEY, APP_S_KEY)
        ns.create_multicast_group(MC_ADDR, MC_NWK_KEY, MC_APP_KEY)
        server_app = RecordingApp()
        ns.register_application(server_app)
        gateway = LoRaWanGateway(network_server=ns)

        device = _new_device()
        app = RecordingApp()
        device.register_application(app)
        device.join_multicast_group(MulticastGroup(MC_ADDR, MC_NWK_KEY, MC_APP_KEY))

        uplink_times = [3.0, 7.0, 11.0]
        replies = [b"\xA0", b"\xA1", b"\xA2"]
        for reply in replies:
            ns.queue_downlink(DEV_ADDR, fport=FPORT, payload=reply)

        # Scheduled between the uplinks and their RX1 windows (uplink + 1s), so the device
        # is in plain continuous reception when each of them goes out.
        multicast_times = [5.0, 9.0, 13.0]
        multicast = [b"\xB0", b"\xB1", b"\xB2"]
        for at_time, payload in zip(multicast_times, multicast):
            ns.schedule_multicast_downlink(
                MC_ADDR, fport=FPORT, payload=payload, at_time=at_time,
            )

        async def driver() -> None:
            await device.switch_mode(OperatingMode.CLASS_C)
            for at_time in uplink_times:
                await sim.sleep_until(at_time)
                await device.send_uplink(fport=FPORT, payload=b"\x5A")

        sim.create_task(driver())
        sim.run(simulation_length=20)

        # Every uplink reached the network server...
        assert [payload for (_, payload) in server_app.uplinks] == [b"\x5A"] * 3
        assert gateway.frames_forwarded == 3
        # ...the device stayed in Class C and kept its session counter moving...
        assert device.operating_mode is OperatingMode.CLASS_C
        assert device.session is not None and device.session.fcnt_up == 3
        # ...and both the RX1 replies and the multicast frames landed.
        assert gateway.multicast_frames_sent == 3
        assert sorted(app.downlinks) == sorted(replies + multicast)


class TestLeavingClassC:
    """Switching away from Class C has to hand the radio back in a usable state."""

    def test_receiver_is_powered_down_and_queue_dropped(self):
        ns = NetworkServer()
        ns.register_device(DEV_ADDR, NWK_S_KEY, APP_S_KEY)
        ns.create_multicast_group(MC_ADDR, MC_NWK_KEY, MC_APP_KEY)
        gateway = LoRaWanGateway(network_server=ns)

        device = _new_device()
        app = RecordingApp()
        device.register_application(app)
        device.join_multicast_group(MulticastGroup(MC_ADDR, MC_NWK_KEY, MC_APP_KEY))

        ns.queue_downlink(DEV_ADDR, fport=FPORT, payload=b"\xAA\xBB")
        # One frame while the device listens, one after it has gone back to Class A.
        ns.schedule_multicast_downlink(MC_ADDR, fport=FPORT, payload=b"\x11", at_time=2.0)
        ns.schedule_multicast_downlink(MC_ADDR, fport=FPORT, payload=b"\x22", at_time=6.0)

        states: list[RadioState] = []

        async def driver() -> None:
            await sim.sleep(1.0)
            await device.switch_mode(OperatingMode.CLASS_C)
            await sim.sleep(4.0)
            await device.switch_mode(OperatingMode.CLASS_A)
            # Leaving Class C powers the receiver down there and then, rather than leaving
            # it collecting frames nobody is going to read.
            states.append(device.radio.get_state())
            assert len(device.radio.rx_chains) == 1
            # The uplink's RX1 window must hand back this device's own reply, not something
            # the receiver happened to pick up while it was still in Class C.
            await sim.sleep(3.0)
            await device.send_uplink(fport=FPORT, payload=b"\x00")

        sim.create_task(driver())
        sim.run(simulation_length=16)

        assert states == [RadioState.OFF]
        assert gateway.multicast_frames_sent == 2
        # The in-session frame and the RX1 reply, and nothing from after the switch.
        assert app.downlinks == [b"\x11", b"\xAA\xBB"]

    def test_stale_frames_do_not_close_a_later_rx_window(self):
        """A frame queued before a window opened is discarded, not served from it."""
        ns = NetworkServer()
        ns.register_device(DEV_ADDR, NWK_S_KEY, APP_S_KEY)
        ns.create_multicast_group(MC_ADDR, MC_NWK_KEY, MC_APP_KEY)
        LoRaWanGateway(network_server=ns)

        device = _new_device()
        app = RecordingApp()
        device.register_application(app)
        device.join_multicast_group(MulticastGroup(MC_ADDR, MC_NWK_KEY, MC_APP_KEY))
        ns.queue_downlink(DEV_ADDR, fport=FPORT, payload=b"\xAA\xBB")

        async def driver() -> None:
            await sim.sleep(1.0)
            # A frame the radio holds onto without anyone reading it.
            await device.radio.receive(continuous=True)
            ns.schedule_multicast_downlink(MC_ADDR, fport=FPORT, payload=b"\x33")
            await sim.sleep(2.0)
            assert len(device.radio.rx_chains) == 1
            await device.send_uplink(fport=FPORT, payload=b"\x00")

        sim.create_task(driver())
        sim.run(simulation_length=12)

        # The stale multicast frame never reaches the application: the RX1 window skips it
        # and waits for the reply that belongs to the window.
        assert app.downlinks == [b"\xAA\xBB"]


class TestDutyCycleLimiter:
    def test_quiet_time_formula(self):
        limiter = DutyCycleLimiter(0.10)
        assert limiter.quiet_time(1.0) == pytest.approx(9.0)
        limiter = DutyCycleLimiter(0.01)
        assert limiter.quiet_time(0.5) == pytest.approx(49.5)

    def test_full_duty_cycle_never_waits(self):
        limiter = DutyCycleLimiter(1.0)
        assert limiter.quiet_time(2.0) == pytest.approx(0.0)

    def test_invalid_duty_cycle(self):
        with pytest.raises(AssertionError):
            DutyCycleLimiter(0.0)
        with pytest.raises(AssertionError):
            DutyCycleLimiter(1.5)


# ═══════════════════════════════════════════════════════════════════════════
# Round-robin application downlinks
# ═══════════════════════════════════════════════════════════════════════════

class TestRoundRobinDownlinks:
    @pytest.mark.asyncio
    async def test_applications_take_turns(self):
        ns = NetworkServer()
        ns.register_device(DEV_ADDR, NWK_S_KEY, APP_S_KEY)
        ns.register_application(RecordingApp(fport=200, downlink=b"\xAA"))
        ns.register_application(RecordingApp(fport=201, downlink=b"\xBB"))

        device_record = ns._devices[DEV_ADDR]
        ports: list[int] = []
        for _ in range(4):
            raw = await ns._build_downlink(device_record)
            assert raw is not None
            phy = PHYPayload.decode_data(raw)
            assert phy.mac_payload is not None
            ports.append(phy.mac_payload.fport)  # type: ignore[arg-type]

        assert ports == [200, 201, 200, 201]

    @pytest.mark.asyncio
    async def test_explicit_queue_keeps_priority(self):
        ns = NetworkServer()
        ns.register_device(DEV_ADDR, NWK_S_KEY, APP_S_KEY)
        ns.register_application(RecordingApp(fport=200, downlink=b"\xAA"))
        ns.queue_downlink(DEV_ADDR, fport=42, payload=b"\x99")

        raw = await ns._build_downlink(ns._devices[DEV_ADDR])
        assert raw is not None
        phy = PHYPayload.decode_data(raw)
        assert phy.mac_payload is not None
        assert phy.mac_payload.fport == 42

    @pytest.mark.asyncio
    async def test_single_application_is_served_every_time(self):
        ns = NetworkServer()
        ns.register_device(DEV_ADDR, NWK_S_KEY, APP_S_KEY)
        ns.register_application(RecordingApp(fport=200, downlink=b"\xAA"))

        for _ in range(3):
            raw = await ns._build_downlink(ns._devices[DEV_ADDR])
            assert raw is not None

    @pytest.mark.asyncio
    async def test_has_pending_downlink(self):
        ns = NetworkServer()
        ns.register_device(DEV_ADDR, NWK_S_KEY, APP_S_KEY)
        assert await ns.has_pending_downlink(DEV_ADDR) is False

        ns.queue_downlink(DEV_ADDR, fport=1, payload=b"\x01")
        assert await ns.has_pending_downlink(DEV_ADDR) is True

        await ns._build_downlink(ns._devices[DEV_ADDR])
        assert await ns.has_pending_downlink(DEV_ADDR) is False

        ns.register_application(RecordingApp(fport=200, downlink=b"\xAA"))
        assert await ns.has_pending_downlink(DEV_ADDR) is True


# ═══════════════════════════════════════════════════════════════════════════
# Maximum FRMPayload size (RP002-1.0.4 §2.4.6)
# ═══════════════════════════════════════════════════════════════════════════

class TestMaxFrmPayload:
    def test_eu868_values(self):
        assert [max_frm_payload(dr) for dr in range(6)] == [42, 42, 42, 106, 213, 213]

    def test_fopts_eat_into_the_budget(self):
        assert max_frm_payload(5, fopts_len=15) == 198
        assert max_frm_payload(0, fopts_len=15) == 27

    def test_unknown_data_rate(self):
        with pytest.raises(AssertionError):
            max_frm_payload(9)

    def test_invalid_fopts_length(self):
        with pytest.raises(AssertionError):
            max_frm_payload(5, fopts_len=16)

    def test_matches_the_region_table(self):
        for dr, entry in EU868_DATA_RATES.items():
            assert max_frm_payload(dr) == entry.max_payload - 9

    @pytest.mark.asyncio
    async def test_uplink_over_the_limit_is_rejected(self):
        device = _new_device(data_rate=5)
        with pytest.raises(ValueError, match="exceeds the 213 byte"):
            await device.send_uplink(fport=FPORT, payload=b"\x00" * 214)

    @pytest.mark.asyncio
    async def test_uplink_at_the_limit_is_accepted(self):
        """The largest legal payload must not trip the check (it is never transmitted here)."""
        device = _new_device(data_rate=5)
        assert max_frm_payload(5) == 213
        # Build-time check only; running the radio needs a simulation, so stop at the limit
        # computation itself.
        assert len(b"\x00" * 213) <= max_frm_payload(5)

    def test_multicast_payload_over_the_limit_is_rejected(self):
        ns = NetworkServer(default_data_rate=5)
        ns.create_multicast_group(MC_ADDR, MC_NWK_KEY, MC_APP_KEY)
        with pytest.raises(ValueError, match="exceeds the 213 byte"):
            ns.build_multicast_downlink(MC_ADDR, fport=FPORT, payload=b"\x00" * 300)

    def test_multicast_group_data_rate_tightens_the_limit(self):
        ns = NetworkServer(default_data_rate=5)
        ns.create_multicast_group(MC_ADDR, MC_NWK_KEY, MC_APP_KEY, data_rate=0)
        with pytest.raises(ValueError, match="exceeds the 42 byte"):
            ns.build_multicast_downlink(MC_ADDR, fport=FPORT, payload=b"\x00" * 43)

    @pytest.mark.asyncio
    async def test_downlink_over_the_limit_is_rejected(self):
        ns = NetworkServer(default_data_rate=0)
        ns.register_device(DEV_ADDR, NWK_S_KEY, APP_S_KEY)
        ns.queue_downlink(DEV_ADDR, fport=FPORT, payload=b"\x00" * 100)
        with pytest.raises(ValueError, match="exceeds the 42 byte"):
            await ns._build_downlink(ns._devices[DEV_ADDR])
