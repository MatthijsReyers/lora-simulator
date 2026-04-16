"""Tests for LoRaWAN Class B: beacons, ping slots, and mode switching."""

import pytest
from unittest.mock import AsyncMock, patch

from simulator.lorawan.beacon import (
    BEACON_MAGIC,
    encode_beacon,
    decode_beacon,
    compute_ping_offset,
    compute_ping_slot_times,
)
from simulator.lorawan.device import LoRaWanDevice, DeviceSession
from simulator.lorawan.enums.operating_mode import OperatingMode
from simulator.lorawan.network_server import NetworkServer
from simulator.lorawan.region import (
    BEACON_INTERVAL, BEACON_RESERVED, BEACON_GUARD,
    PING_SLOT_LEN, CLASS_B_DEFAULT_PING_NB,
)
from simulator import environment as sim_module

# ── Test credentials ────────────────────────────────────────────────────────
DEV_ADDR  = 0x26011234
NWK_S_KEY = bytes.fromhex("2B7E151628AED2A6ABF7158809CF4F3C")
APP_S_KEY = bytes.fromhex("3C4F9C098815F7ABA6D2AE281615E72B")


# ═══════════════════════════════════════════════════════════════════════════
# Beacon encoding / decoding
# ═══════════════════════════════════════════════════════════════════════════

class TestBeaconCodec:
    def test_encode_decode_roundtrip(self):
        data = encode_beacon(1000)
        assert decode_beacon(data) == 1000

    def test_magic_byte(self):
        data = encode_beacon(42)
        assert data[0] == BEACON_MAGIC

    def test_decode_invalid_magic(self):
        assert decode_beacon(b"\x00\x00\x00\x00\x00") is None

    def test_decode_too_short(self):
        assert decode_beacon(b"\xBE\x00") is None

    def test_encode_zero_time(self):
        assert decode_beacon(encode_beacon(0)) == 0

    def test_encode_max_time(self):
        assert decode_beacon(encode_beacon(0xFFFFFFFF)) == 0xFFFFFFFF


# ═══════════════════════════════════════════════════════════════════════════
# Ping slot offset & timing
# ═══════════════════════════════════════════════════════════════════════════

class TestPingSlotComputation:
    def test_offset_within_range(self):
        ping_period = 4096 // 16
        offset = compute_ping_offset(beacon_time=0, dev_addr=DEV_ADDR, ping_period=ping_period)
        assert 0 <= offset < ping_period

    def test_offset_deterministic(self):
        """Same inputs always produce the same offset."""
        ping_period = 4096 // 16
        a = compute_ping_offset(0, DEV_ADDR, ping_period)
        b = compute_ping_offset(0, DEV_ADDR, ping_period)
        assert a == b

    def test_offset_varies_by_dev_addr(self):
        """Different devices get different offsets (for most addresses)."""
        ping_period = 4096 // 16
        offsets = {compute_ping_offset(0, addr, ping_period) for addr in range(100)}
        assert len(offsets) > 1  # Not all the same

    def test_offset_varies_by_beacon_time(self):
        """Different beacon times produce different offsets."""
        ping_period = 4096 // 16
        offsets = {compute_ping_offset(t, DEV_ADDR, ping_period) for t in range(100)}
        assert len(offsets) > 1

    def test_slot_times_count(self):
        """Number of slot times equals ping_nb."""
        for ping_nb in [1, 2, 4, 8, 16, 32, 64, 128]:
            times = compute_ping_slot_times(0, DEV_ADDR, ping_nb)
            assert len(times) == ping_nb

    def test_slot_times_sorted(self):
        times = compute_ping_slot_times(0, DEV_ADDR, 16)
        assert times == sorted(times)

    def test_slot_times_within_beacon_period(self):
        """All ping slot times fall within the usable beacon window."""
        beacon_time = 128
        times = compute_ping_slot_times(beacon_time, DEV_ADDR, 16)
        for t in times:
            assert t >= beacon_time + BEACON_RESERVED
            # Total slots = 4096, last slot ends at BEACON_RESERVED + 4096*SLOT_LEN = 122.88+2.12
            assert t < beacon_time + BEACON_RESERVED + 4096 * PING_SLOT_LEN

    def test_slot_times_unique(self):
        times = compute_ping_slot_times(0, DEV_ADDR, 16)
        assert len(times) == len(set(times))


# ═══════════════════════════════════════════════════════════════════════════
# Device Class B mode switching
# ═══════════════════════════════════════════════════════════════════════════

class TestClassBModeSwitching:
    def test_default_mode_is_class_a(self):
        session = DeviceSession(dev_addr=DEV_ADDR, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY)
        device = LoRaWanDevice(session=session)
        assert device.operating_mode == OperatingMode.CLASS_A

    @pytest.mark.asyncio
    async def test_switch_to_class_b(self):
        session = DeviceSession(dev_addr=DEV_ADDR, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY)
        device = LoRaWanDevice(session=session)
        with patch.object(sim_module.simulation_env, "start_child_task", new_callable=AsyncMock):
            await device.switch_mode(OperatingMode.CLASS_B)
        assert device.operating_mode == OperatingMode.CLASS_B
        assert device._class_b_running is True

    @pytest.mark.asyncio
    async def test_switch_class_b_to_class_a(self):
        session = DeviceSession(dev_addr=DEV_ADDR, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY)
        device = LoRaWanDevice(session=session)
        with patch.object(sim_module.simulation_env, "start_child_task", new_callable=AsyncMock):
            await device.switch_mode(OperatingMode.CLASS_B)
            await device.switch_mode(OperatingMode.CLASS_A)
        assert device.operating_mode == OperatingMode.CLASS_A
        assert device._class_b_running is False
        assert device._beacon_locked is False

    @pytest.mark.asyncio
    async def test_switch_class_b_to_class_b_no_double_start(self):
        session = DeviceSession(dev_addr=DEV_ADDR, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY)
        device = LoRaWanDevice(session=session)
        with patch.object(sim_module.simulation_env, "start_child_task", new_callable=AsyncMock) as mock_start:
            await device.switch_mode(OperatingMode.CLASS_B)
            await device.switch_mode(OperatingMode.CLASS_B)
        mock_start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_switch_class_b_to_class_c(self):
        """Mode switching from B to C should stop B and start C."""
        session = DeviceSession(dev_addr=DEV_ADDR, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY)
        device = LoRaWanDevice(session=session)
        with patch.object(sim_module.simulation_env, "start_child_task", new_callable=AsyncMock):
            await device.switch_mode(OperatingMode.CLASS_B)
            assert device._class_b_running is True

            await device.switch_mode(OperatingMode.CLASS_C)
            assert device._class_b_running is False
            assert device._class_c_running is True

    def test_ping_nb_default(self):
        session = DeviceSession(dev_addr=DEV_ADDR, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY)
        device = LoRaWanDevice(session=session)
        assert device.ping_nb == CLASS_B_DEFAULT_PING_NB

    def test_ping_nb_custom(self):
        session = DeviceSession(dev_addr=DEV_ADDR, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY)
        device = LoRaWanDevice(session=session, ping_nb=4)
        assert device.ping_nb == 4


# ═══════════════════════════════════════════════════════════════════════════
# Network server Class B support
# ═══════════════════════════════════════════════════════════════════════════

class TestNetworkServerClassB:
    def test_enable_disable_class_b(self):
        ns = NetworkServer()
        ns.register_device(DEV_ADDR, NWK_S_KEY, APP_S_KEY)
        ns.enable_class_b(DEV_ADDR, ping_nb=8)

        device = ns._devices[DEV_ADDR]
        assert device.class_b_enabled is True
        assert device.class_b_ping_nb == 8

        ns.disable_class_b(DEV_ADDR)
        assert device.class_b_enabled is False

    @pytest.mark.asyncio
    async def test_class_b_schedule_empty_when_no_downlinks(self):
        ns = NetworkServer()
        ns.register_device(DEV_ADDR, NWK_S_KEY, APP_S_KEY)
        ns.enable_class_b(DEV_ADDR)

        schedule = await ns.get_class_b_downlink_schedule(beacon_time=0)
        assert schedule == []

    @pytest.mark.asyncio
    async def test_class_b_schedule_with_queued_downlink(self):
        ns = NetworkServer()
        ns.register_device(DEV_ADDR, NWK_S_KEY, APP_S_KEY)
        ns.enable_class_b(DEV_ADDR, ping_nb=16)
        ns.queue_downlink(DEV_ADDR, fport=1, payload=b"\x01\x02")

        schedule = await ns.get_class_b_downlink_schedule(beacon_time=0)
        assert len(schedule) == 1
        dev_addr, raw, slot_time = schedule[0]
        assert dev_addr == DEV_ADDR
        assert isinstance(raw, bytes)
        assert len(raw) > 0
        # Slot time should be within the beacon period
        assert slot_time >= BEACON_RESERVED
        assert slot_time < BEACON_INTERVAL

    @pytest.mark.asyncio
    async def test_class_b_schedule_ignores_non_class_b_devices(self):
        ns = NetworkServer()
        ns.register_device(DEV_ADDR, NWK_S_KEY, APP_S_KEY)
        # Don't enable Class B
        ns.queue_downlink(DEV_ADDR, fport=1, payload=b"\x01\x02")

        schedule = await ns.get_class_b_downlink_schedule(beacon_time=0)
        assert schedule == []
