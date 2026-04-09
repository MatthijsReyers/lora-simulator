"""Tests for LoRaWAN Phase 5: Class C, mode switching, and multicast."""

import struct
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from simulator.lorawan.application import Application
from simulator.lorawan.device import (
    LoRaWanDevice, DeviceSession, MulticastGroup,
)
from simulator.lorawan.enums.operating_mode import OperatingMode
from simulator.lorawan.network_server import (
    NetworkServer, MulticastGroupRecord,
)
from simulator.lorawan.enums.frame_types import MType
from simulator.lorawan.frame import (
    MHDR, FCtrl, FHDR, MACPayload, PHYPayload,
)
from simulator.lorawan.crypto import compute_data_mic, encrypt_frm_payload
from simulator import environment as sim_module

# ── Test credentials ────────────────────────────────────────────────────────
DEV_ADDR  = 0x26011234
NWK_S_KEY = bytes.fromhex("2B7E151628AED2A6ABF7158809CF4F3C")
APP_S_KEY = bytes.fromhex("3C4F9C098815F7ABA6D2AE281615E72B")

MC_ADDR    = 0xFF000001
MC_NWK_KEY = bytes.fromhex("AABBCCDD11223344AABBCCDD11223344")
MC_APP_KEY = bytes.fromhex("11223344AABBCCDD11223344AABBCCDD")


class DummyApp(Application):
    """Test application that records calls."""

    def __init__(self, fport: int = 1):
        self._port = fport
        self.uplinks: list[tuple[int, bytes]] = []
        self.downlinks: list[bytes] = []

    def port(self) -> int:
        return self._port

    async def on_uplink(self, dev_addr: int, payload: bytes) -> None:
        self.uplinks.append((dev_addr, payload))

    async def on_downlink(self, payload: bytes) -> None:
        self.downlinks.append(payload)

    async def get_downlink(self, dev_addr: int) -> bytes | None:
        return None


def _build_downlink(dev_addr: int, nwk_s_key: bytes, app_s_key: bytes,
                    fcnt: int, fport: int, payload: bytes) -> bytes:
    """Helper to build a valid downlink PHYPayload."""
    encrypted = encrypt_frm_payload(
        app_s_key, dev_addr=dev_addr, fcnt=fcnt, uplink=False, payload=payload,
    )
    fhdr = FHDR(dev_addr=dev_addr, fctrl=FCtrl(), fcnt=fcnt)
    mac_payload = MACPayload(fhdr=fhdr, fport=fport, frm_payload=encrypted)
    mhdr = MHDR(mtype=MType.UNCONFIRMED_DATA_DN)
    mhdr_and_payload = bytes([mhdr.encode()]) + mac_payload.encode(uplink=False)
    mic = compute_data_mic(
        nwk_s_key, dev_addr=dev_addr, fcnt=fcnt,
        uplink=False, mhdr_and_payload=mhdr_and_payload,
    )
    return PHYPayload(mhdr=mhdr, mac_payload=mac_payload, mic=mic).encode()


# ═══════════════════════════════════════════════════════════════════════════
# MulticastGroup dataclass
# ═══════════════════════════════════════════════════════════════════════════

class TestMulticastGroup:
    def test_default_fcnt(self):
        group = MulticastGroup(
            group_addr=MC_ADDR, nwk_s_key=MC_NWK_KEY, app_s_key=MC_APP_KEY,
        )
        assert group.fcnt_down == 0

    def test_fields(self):
        group = MulticastGroup(
            group_addr=MC_ADDR, nwk_s_key=MC_NWK_KEY, app_s_key=MC_APP_KEY,
        )
        assert group.group_addr == MC_ADDR
        assert group.nwk_s_key == MC_NWK_KEY
        assert group.app_s_key == MC_APP_KEY


# ═══════════════════════════════════════════════════════════════════════════
# Mode switching
# ═══════════════════════════════════════════════════════════════════════════

class TestModeSwitching:
    def test_default_mode_is_class_a(self):
        session = DeviceSession(dev_addr=DEV_ADDR, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY)
        device = LoRaWanDevice(session=session)
        assert device.operating_mode == OperatingMode.CLASS_A

    @pytest.mark.asyncio
    async def test_switch_to_class_c(self):
        session = DeviceSession(dev_addr=DEV_ADDR, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY)
        device = LoRaWanDevice(session=session)
        with patch.object(sim_module.simulation_env, "start_child_task", new_callable=AsyncMock):
            await device.switch_mode(OperatingMode.CLASS_C)
        assert device.operating_mode == OperatingMode.CLASS_C
        assert device._class_c_running is True

    @pytest.mark.asyncio
    async def test_switch_back_to_class_a(self):
        session = DeviceSession(dev_addr=DEV_ADDR, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY)
        device = LoRaWanDevice(session=session)
        with patch.object(sim_module.simulation_env, "start_child_task", new_callable=AsyncMock):
            await device.switch_mode(OperatingMode.CLASS_C)
            await device.switch_mode(OperatingMode.CLASS_A)
        assert device.operating_mode == OperatingMode.CLASS_A
        assert device._class_c_running is False

    @pytest.mark.asyncio
    async def test_switch_class_c_to_class_c_no_double_start(self):
        session = DeviceSession(dev_addr=DEV_ADDR, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY)
        device = LoRaWanDevice(session=session)
        with patch.object(sim_module.simulation_env, "start_child_task", new_callable=AsyncMock) as mock_start:
            await device.switch_mode(OperatingMode.CLASS_C)
            await device.switch_mode(OperatingMode.CLASS_C)
        # start_child_task should only be called once
        assert mock_start.call_count == 1
        assert device._class_c_running is True

    def test_init_as_class_c(self):
        """Device created with CLASS_C should NOT auto-start the RX task."""
        session = DeviceSession(dev_addr=DEV_ADDR, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY)
        device = LoRaWanDevice(session=session, operating_mode=OperatingMode.CLASS_C)
        assert device.operating_mode == OperatingMode.CLASS_C
        # No auto-start — user should call switch_mode() or start manually
        assert device._class_c_running is False


# ═══════════════════════════════════════════════════════════════════════════
# Multicast group management (device)
# ═══════════════════════════════════════════════════════════════════════════

class TestDeviceMulticast:
    def test_join_multicast_group(self):
        session = DeviceSession(dev_addr=DEV_ADDR, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY)
        device = LoRaWanDevice(session=session)
        group = MulticastGroup(
            group_addr=MC_ADDR, nwk_s_key=MC_NWK_KEY, app_s_key=MC_APP_KEY,
        )
        device.join_multicast_group(group)
        assert MC_ADDR in device._multicast_groups

    def test_leave_multicast_group(self):
        session = DeviceSession(dev_addr=DEV_ADDR, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY)
        device = LoRaWanDevice(session=session)
        group = MulticastGroup(
            group_addr=MC_ADDR, nwk_s_key=MC_NWK_KEY, app_s_key=MC_APP_KEY,
        )
        device.join_multicast_group(group)
        device.leave_multicast_group(MC_ADDR)
        assert MC_ADDR not in device._multicast_groups

    def test_leave_nonexistent_group_no_error(self):
        session = DeviceSession(dev_addr=DEV_ADDR, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY)
        device = LoRaWanDevice(session=session)
        device.leave_multicast_group(0xDEAD)  # Should not raise

    @pytest.mark.asyncio
    async def test_process_multicast_downlink(self):
        """Device should decrypt a multicast downlink using group keys."""
        session = DeviceSession(dev_addr=DEV_ADDR, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY)
        device = LoRaWanDevice(session=session)
        app = DummyApp(fport=1)
        device.register_application(app)
        group = MulticastGroup(
            group_addr=MC_ADDR, nwk_s_key=MC_NWK_KEY, app_s_key=MC_APP_KEY,
        )
        device.join_multicast_group(group)

        # Build a multicast downlink
        payload = b"\xAA\xBB\xCC"
        raw = _build_downlink(MC_ADDR, MC_NWK_KEY, MC_APP_KEY, fcnt=0, fport=1, payload=payload)
        await device._process_downlink(raw)

        assert len(app.downlinks) == 1
        assert app.downlinks[0] == payload

    @pytest.mark.asyncio
    async def test_process_multicast_updates_group_fcnt(self):
        """Multicast downlink should update the group's frame counter."""
        session = DeviceSession(dev_addr=DEV_ADDR, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY)
        device = LoRaWanDevice(session=session)
        group = MulticastGroup(
            group_addr=MC_ADDR, nwk_s_key=MC_NWK_KEY, app_s_key=MC_APP_KEY,
        )
        device.join_multicast_group(group)

        raw = _build_downlink(MC_ADDR, MC_NWK_KEY, MC_APP_KEY, fcnt=5, fport=1, payload=b"\x01")
        await device._process_downlink(raw)

        assert group.fcnt_down == 6

    @pytest.mark.asyncio
    async def test_multicast_does_not_update_unicast_fcnt(self):
        """Multicast downlink should NOT touch the unicast session's fcnt_down."""
        session = DeviceSession(dev_addr=DEV_ADDR, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY)
        device = LoRaWanDevice(session=session)
        group = MulticastGroup(
            group_addr=MC_ADDR, nwk_s_key=MC_NWK_KEY, app_s_key=MC_APP_KEY,
        )
        device.join_multicast_group(group)

        raw = _build_downlink(MC_ADDR, MC_NWK_KEY, MC_APP_KEY, fcnt=10, fport=1, payload=b"\x01")
        await device._process_downlink(raw)

        assert session.fcnt_down == 0  # Unchanged

    @pytest.mark.asyncio
    async def test_unicast_downlink_still_works_with_multicast_registered(self):
        """Unicast downlinks should still work when multicast groups are registered."""
        session = DeviceSession(dev_addr=DEV_ADDR, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY)
        device = LoRaWanDevice(session=session)
        app = DummyApp(fport=1)
        device.register_application(app)
        group = MulticastGroup(
            group_addr=MC_ADDR, nwk_s_key=MC_NWK_KEY, app_s_key=MC_APP_KEY,
        )
        device.join_multicast_group(group)

        payload = b"\x11\x22"
        raw = _build_downlink(DEV_ADDR, NWK_S_KEY, APP_S_KEY, fcnt=0, fport=1, payload=payload)
        await device._process_downlink(raw)

        assert len(app.downlinks) == 1
        assert app.downlinks[0] == payload
        assert session.fcnt_down == 1

    @pytest.mark.asyncio
    async def test_unknown_address_ignored(self):
        """Downlink for unknown address is silently ignored."""
        session = DeviceSession(dev_addr=DEV_ADDR, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY)
        device = LoRaWanDevice(session=session)
        app = DummyApp(fport=1)
        device.register_application(app)

        unknown_addr = 0xDEADBEEF
        raw = _build_downlink(unknown_addr, NWK_S_KEY, APP_S_KEY, fcnt=0, fport=1, payload=b"\x01")
        await device._process_downlink(raw)

        assert len(app.downlinks) == 0

    @pytest.mark.asyncio
    async def test_multicast_mic_mismatch_rejected(self):
        """Multicast downlink with wrong MIC should be rejected."""
        session = DeviceSession(dev_addr=DEV_ADDR, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY)
        device = LoRaWanDevice(session=session)
        app = DummyApp(fport=1)
        device.register_application(app)
        group = MulticastGroup(
            group_addr=MC_ADDR, nwk_s_key=MC_NWK_KEY, app_s_key=MC_APP_KEY,
        )
        device.join_multicast_group(group)

        # Build with wrong keys (NWK_S_KEY instead of MC_NWK_KEY for MIC)
        raw = _build_downlink(MC_ADDR, NWK_S_KEY, MC_APP_KEY, fcnt=0, fport=1, payload=b"\x01")
        await device._process_downlink(raw)

        assert len(app.downlinks) == 0


# ═══════════════════════════════════════════════════════════════════════════
# Network server multicast
# ═══════════════════════════════════════════════════════════════════════════

class TestNetworkServerMulticast:
    def test_create_multicast_group(self):
        ns = NetworkServer()
        record = ns.create_multicast_group(MC_ADDR, MC_NWK_KEY, MC_APP_KEY)
        assert record.group_addr == MC_ADDR
        assert record.fcnt_down == 0
        assert MC_ADDR in ns._multicast_groups

    def test_build_multicast_downlink_valid(self):
        ns = NetworkServer()
        ns.create_multicast_group(MC_ADDR, MC_NWK_KEY, MC_APP_KEY)

        raw = ns.build_multicast_downlink(MC_ADDR, fport=1, payload=b"\xAA\xBB")

        # Should decode and verify
        phy = PHYPayload.decode_data(raw)
        assert phy.mac_payload is not None
        assert phy.mac_payload.fhdr.dev_addr == MC_ADDR
        assert phy.mac_payload.fport == 1

        # MIC should be valid
        mhdr_and_payload = raw[:-4]
        expected_mic = compute_data_mic(
            MC_NWK_KEY, dev_addr=MC_ADDR, fcnt=0,
            uplink=False, mhdr_and_payload=mhdr_and_payload,
        )
        assert phy.mic == expected_mic

    def test_multicast_fcnt_increments(self):
        ns = NetworkServer()
        record = ns.create_multicast_group(MC_ADDR, MC_NWK_KEY, MC_APP_KEY)

        ns.build_multicast_downlink(MC_ADDR, fport=1, payload=b"\x01")
        assert record.fcnt_down == 1

        ns.build_multicast_downlink(MC_ADDR, fport=1, payload=b"\x02")
        assert record.fcnt_down == 2

    def test_multicast_downlink_decryptable(self):
        """A device with the same keys should be able to decrypt a NS-built multicast frame."""
        ns = NetworkServer()
        ns.create_multicast_group(MC_ADDR, MC_NWK_KEY, MC_APP_KEY)

        payload = b"\xDE\xAD\xBE\xEF"
        raw = ns.build_multicast_downlink(MC_ADDR, fport=1, payload=payload)

        # Decode and decrypt
        phy = PHYPayload.decode_data(raw)
        assert phy.mac_payload is not None
        decrypted = encrypt_frm_payload(
            MC_APP_KEY, dev_addr=MC_ADDR, fcnt=0, uplink=False,
            payload=phy.mac_payload.frm_payload,
        )
        assert decrypted == payload
