"""Tests for LoRaWAN Phase 2: device, network server, gateway, and application."""

import struct
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from simulator.lorawan.application import Application
from simulator.lorawan.device import LoRaWanDevice, DeviceSession
from simulator.lorawan.enums.operating_mode import OperatingMode
from simulator.lorawan.network_server import NetworkServer, DeviceRecord, PendingDownlink
from simulator.lorawan.gateway import LoRaWanGateway
from simulator.lorawan.enums.frame_types import MType
from simulator.lorawan.frame import (
    MHDR, FCtrl, FHDR, MACPayload, PHYPayload,
)
from simulator.lorawan.crypto import compute_data_mic, encrypt_frm_payload

# ── Test ABP credentials ────────────────────────────────────────────────────
DEV_ADDR  = 0x26011234
NWK_S_KEY = bytes.fromhex("2B7E151628AED2A6ABF7158809CF4F3C")
APP_S_KEY = bytes.fromhex("3C4F9C098815F7ABA6D2AE281615E72B")


class DummyApp(Application):
    """Test application that records calls."""

    def __init__(self, fport: int = 1):
        self._port = fport
        self.uplinks: list[tuple[int, bytes]] = []
        self.downlinks: list[bytes] = []
        self._pending_downlink: bytes | None = None

    def port(self) -> int:
        return self._port

    async def on_uplink(self, dev_addr: int, payload: bytes) -> None:
        self.uplinks.append((dev_addr, payload))

    async def on_downlink(self, payload: bytes) -> None:
        self.downlinks.append(payload)

    async def get_downlink(self, dev_addr: int) -> bytes | None:
        dl = self._pending_downlink
        self._pending_downlink = None
        return dl


def _build_uplink(dev_addr: int, nwk_s_key: bytes, app_s_key: bytes,
                  fcnt: int, fport: int, payload: bytes) -> bytes:
    """Helper to build a valid uplink PHYPayload."""
    encrypted = encrypt_frm_payload(
        app_s_key, dev_addr=dev_addr, fcnt=fcnt, uplink=True, payload=payload,
    )
    fhdr = FHDR(dev_addr=dev_addr, fctrl=FCtrl(), fcnt=fcnt)
    mac_payload = MACPayload(fhdr=fhdr, fport=fport, frm_payload=encrypted)
    mhdr = MHDR(mtype=MType.UNCONFIRMED_DATA_UP)
    mhdr_and_payload = bytes([mhdr.encode()]) + mac_payload.encode(uplink=True)
    mic = compute_data_mic(
        nwk_s_key, dev_addr=dev_addr, fcnt=fcnt,
        uplink=True, mhdr_and_payload=mhdr_and_payload,
    )
    return PHYPayload(mhdr=mhdr, mac_payload=mac_payload, mic=mic).encode()


# ═══════════════════════════════════════════════════════════════════════════
# DeviceSession
# ═══════════════════════════════════════════════════════════════════════════

class TestDeviceSession:
    def test_default_counters(self):
        session = DeviceSession(dev_addr=DEV_ADDR, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY)
        assert session.fcnt_up == 0
        assert session.fcnt_down == 0

    def test_counter_mutation(self):
        session = DeviceSession(dev_addr=DEV_ADDR, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY)
        session.fcnt_up = 42
        assert session.fcnt_up == 42


# ═══════════════════════════════════════════════════════════════════════════
# OperatingMode
# ═══════════════════════════════════════════════════════════════════════════

class TestOperatingMode:
    def test_values(self):
        assert OperatingMode.CLASS_A.value == "A"
        assert OperatingMode.CLASS_B.value == "B"
        assert OperatingMode.CLASS_C.value == "C"


# ═══════════════════════════════════════════════════════════════════════════
# Application registration
# ═══════════════════════════════════════════════════════════════════════════

class TestApplicationRegistration:
    def test_device_register_valid_port(self):
        session = DeviceSession(dev_addr=DEV_ADDR, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY)
        device = LoRaWanDevice(session=session)

        app = DummyApp(fport=10)
        device.register_application(app)
        assert 10 in device._applications

    def test_device_register_invalid_port(self):
        session = DeviceSession(dev_addr=DEV_ADDR, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY)
        device = LoRaWanDevice(session=session)

        app = DummyApp(fport=0)
        with pytest.raises(AssertionError):
            device.register_application(app)

    def test_ns_register_application(self):
        ns = NetworkServer()
        app = DummyApp(fport=5)
        ns.register_application(app)
        assert 5 in ns._applications


# ═══════════════════════════════════════════════════════════════════════════
# Network Server — uplink processing
# ═══════════════════════════════════════════════════════════════════════════

class TestNetworkServerUplink:
    def setup_method(self):
        self.ns = NetworkServer()
        self.ns.register_device(DEV_ADDR, NWK_S_KEY, APP_S_KEY)
        self.app = DummyApp(fport=1)
        self.ns.register_application(self.app)

    @pytest.mark.asyncio
    async def test_valid_uplink_processed(self):
        """NS should verify MIC, decrypt, and route a valid uplink."""
        payload = struct.pack("<Hh", 0, 2150)
        raw = _build_uplink(DEV_ADDR, NWK_S_KEY, APP_S_KEY, fcnt=0, fport=1, payload=payload)

        result = await self.ns.handle_uplink(raw)

        # No pending downlink, so result should be None
        assert result is None
        # Application should have received the decrypted payload
        assert len(self.app.uplinks) == 1
        assert self.app.uplinks[0][0] == DEV_ADDR
        assert self.app.uplinks[0][1] == payload

    @pytest.mark.asyncio
    async def test_mic_mismatch_rejected(self):
        """NS should reject a frame with invalid MIC."""
        raw = _build_uplink(DEV_ADDR, NWK_S_KEY, APP_S_KEY, fcnt=0, fport=1, payload=b"\x01\x02")
        # Corrupt the MIC (last 4 bytes)
        corrupted = raw[:-4] + b"\xff\xff\xff\xff"

        result = await self.ns.handle_uplink(corrupted)

        assert result is None
        assert len(self.app.uplinks) == 0

    @pytest.mark.asyncio
    async def test_unknown_device_rejected(self):
        """NS should reject frames from unknown devices."""
        unknown_addr = 0xDEADBEEF
        raw = _build_uplink(unknown_addr, NWK_S_KEY, APP_S_KEY, fcnt=0, fport=1, payload=b"\x01")

        result = await self.ns.handle_uplink(raw)

        assert result is None
        assert len(self.app.uplinks) == 0

    @pytest.mark.asyncio
    async def test_frame_counter_increments(self):
        """NS should track the expected frame counter."""
        for i in range(3):
            raw = _build_uplink(DEV_ADDR, NWK_S_KEY, APP_S_KEY, fcnt=i, fport=1, payload=b"\x00")
            await self.ns.handle_uplink(raw)

        assert self.ns._devices[DEV_ADDR].fcnt_up == 3
        assert len(self.app.uplinks) == 3

    @pytest.mark.asyncio
    async def test_frame_counter_replay_rejected(self):
        """NS should reject frames with a too-low frame counter."""
        raw0 = _build_uplink(DEV_ADDR, NWK_S_KEY, APP_S_KEY, fcnt=0, fport=1, payload=b"\x00")
        await self.ns.handle_uplink(raw0)

        # Re-send with same fcnt=0 (replay)
        result = await self.ns.handle_uplink(raw0)

        assert result is None
        assert len(self.app.uplinks) == 1  # Only the first one was accepted

    @pytest.mark.asyncio
    async def test_unregistered_fport_still_verifies(self):
        """NS should accept valid frames for unregistered FPorts (just no routing)."""
        raw = _build_uplink(DEV_ADDR, NWK_S_KEY, APP_S_KEY, fcnt=0, fport=99, payload=b"\x01")

        result = await self.ns.handle_uplink(raw)

        # MIC was valid, so it should be accepted (no error)
        assert result is None
        # But application on port 1 should not have received it
        assert len(self.app.uplinks) == 0


# ═══════════════════════════════════════════════════════════════════════════
# Network Server — downlink
# ═══════════════════════════════════════════════════════════════════════════

class TestNetworkServerDownlink:
    def setup_method(self):
        self.ns = NetworkServer()
        self.ns.register_device(DEV_ADDR, NWK_S_KEY, APP_S_KEY)
        self.app = DummyApp(fport=1)
        self.ns.register_application(self.app)

    @pytest.mark.asyncio
    async def test_queued_downlink_sent(self):
        """A queued downlink should be returned after the next uplink."""
        dl_payload = b"\xAA\xBB\xCC"
        self.ns.queue_downlink(DEV_ADDR, fport=1, payload=dl_payload)

        raw = _build_uplink(DEV_ADDR, NWK_S_KEY, APP_S_KEY, fcnt=0, fport=1, payload=b"\x00")
        result = await self.ns.handle_uplink(raw)

        assert result is not None
        # Decode the downlink and verify it contains our payload
        phy = PHYPayload.decode_data(result)
        assert phy.mac_payload is not None
        assert phy.mhdr.mtype == MType.UNCONFIRMED_DATA_DN
        assert phy.mac_payload.fport == 1
        # Decrypt and verify
        plaintext = encrypt_frm_payload(
            APP_S_KEY, dev_addr=DEV_ADDR, fcnt=0,
            uplink=False, payload=phy.mac_payload.frm_payload,
        )
        assert plaintext == dl_payload

    @pytest.mark.asyncio
    async def test_downlink_mic_valid(self):
        """The downlink built by the NS should have a valid MIC."""
        self.ns.queue_downlink(DEV_ADDR, fport=1, payload=b"\x01\x02")

        raw = _build_uplink(DEV_ADDR, NWK_S_KEY, APP_S_KEY, fcnt=0, fport=1, payload=b"\x00")
        dl_raw = await self.ns.handle_uplink(raw)

        assert dl_raw is not None
        phy = PHYPayload.decode_data(dl_raw)
        assert phy.mac_payload is not None

        # Recompute MIC
        mhdr_and_payload = dl_raw[:-4]
        expected_mic = compute_data_mic(
            NWK_S_KEY, dev_addr=DEV_ADDR, fcnt=phy.mac_payload.fhdr.fcnt,
            uplink=False, mhdr_and_payload=mhdr_and_payload,
        )
        assert phy.mic == expected_mic

    @pytest.mark.asyncio
    async def test_downlink_fcnt_increments(self):
        """Each downlink should increment the downlink frame counter."""
        for i in range(3):
            self.ns.queue_downlink(DEV_ADDR, fport=1, payload=b"\x01")
            raw = _build_uplink(DEV_ADDR, NWK_S_KEY, APP_S_KEY, fcnt=i, fport=1, payload=b"\x00")
            dl_raw = await self.ns.handle_uplink(raw)
            assert dl_raw is not None
            phy = PHYPayload.decode_data(dl_raw)
            assert phy.mac_payload is not None
            assert phy.mac_payload.fhdr.fcnt == i

        assert self.ns._devices[DEV_ADDR].fcnt_down == 3

    @pytest.mark.asyncio
    async def test_no_downlink_without_queue(self):
        """NS should return None when no downlinks are pending."""
        raw = _build_uplink(DEV_ADDR, NWK_S_KEY, APP_S_KEY, fcnt=0, fport=1, payload=b"\x00")
        result = await self.ns.handle_uplink(raw)
        assert result is None

    @pytest.mark.asyncio
    async def test_application_get_downlink(self):
        """NS should check applications for pending downlinks."""
        self.app._pending_downlink = b"\xDE\xAD"

        raw = _build_uplink(DEV_ADDR, NWK_S_KEY, APP_S_KEY, fcnt=0, fport=1, payload=b"\x00")
        result = await self.ns.handle_uplink(raw)

        assert result is not None
        phy = PHYPayload.decode_data(result)
        assert phy.mac_payload is not None
        plaintext = encrypt_frm_payload(
            APP_S_KEY, dev_addr=DEV_ADDR, fcnt=0,
            uplink=False, payload=phy.mac_payload.frm_payload,
        )
        assert plaintext == b"\xDE\xAD"

    def test_queue_downlink_unknown_device(self):
        """Queueing a downlink for an unknown device should fail."""
        with pytest.raises(AssertionError):
            self.ns.queue_downlink(0xDEADBEEF, fport=1, payload=b"\x01")


# ═══════════════════════════════════════════════════════════════════════════
# End-to-end: NS processes uplink and sensor decodes downlink
# ═══════════════════════════════════════════════════════════════════════════

class TestEndToEndFrames:
    """Test that frames built by the NS can be decoded by the device logic,
    and vice versa (without the simulation environment — frame-level only)."""

    @pytest.mark.asyncio
    async def test_ns_downlink_decodable_by_device(self):
        """A downlink frame built by NS should be decodable and have valid MIC."""
        ns = NetworkServer()
        ns.register_device(DEV_ADDR, NWK_S_KEY, APP_S_KEY)

        dl_payload = struct.pack("<BH", 0x01, 15)  # config command
        ns.queue_downlink(DEV_ADDR, fport=1, payload=dl_payload)

        # Trigger downlink via uplink
        raw = _build_uplink(DEV_ADDR, NWK_S_KEY, APP_S_KEY, fcnt=0, fport=1, payload=b"\x00")
        dl_raw = await ns.handle_uplink(raw)
        assert dl_raw is not None

        # Decode as the device would
        phy = PHYPayload.decode_data(dl_raw)
        assert phy.mac_payload is not None
        mac = phy.mac_payload

        # Verify MIC
        mhdr_and_payload = dl_raw[:-4]
        expected_mic = compute_data_mic(
            NWK_S_KEY, dev_addr=mac.fhdr.dev_addr, fcnt=mac.fhdr.fcnt,
            uplink=False, mhdr_and_payload=mhdr_and_payload,
        )
        assert phy.mic == expected_mic

        # Decrypt payload
        plaintext = encrypt_frm_payload(
            APP_S_KEY, dev_addr=mac.fhdr.dev_addr, fcnt=mac.fhdr.fcnt,
            uplink=False, payload=mac.frm_payload,
        )
        assert plaintext == dl_payload

    @pytest.mark.asyncio
    async def test_multiple_uplinks_processed_correctly(self):
        """Process multiple uplinks with increasing frame counters."""
        ns = NetworkServer()
        ns.register_device(DEV_ADDR, NWK_S_KEY, APP_S_KEY)
        app = DummyApp(fport=1)
        ns.register_application(app)

        payloads = [struct.pack("<Hh", i, 2000 + i * 10) for i in range(5)]
        for i, p in enumerate(payloads):
            raw = _build_uplink(DEV_ADDR, NWK_S_KEY, APP_S_KEY, fcnt=i, fport=1, payload=p)
            await ns.handle_uplink(raw)

        assert len(app.uplinks) == 5
        for i, (addr, payload) in enumerate(app.uplinks):
            assert addr == DEV_ADDR
            assert payload == payloads[i]
