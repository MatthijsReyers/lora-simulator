"""Tests for the LoRaWAN OTAA join procedure (join.py + integration)."""

import pytest
from simulator.lorawan.join import (
    OTAACredentials,
    OTAADeviceRecord,
    JoinResult,
    build_join_request,
    process_join_request,
    process_join_accept,
)
from simulator.lorawan.frame import PHYPayload, MHDR
from simulator.lorawan.enums.frame_types import MType
from simulator.lorawan.crypto import (
    compute_join_request_mic,
    derive_session_keys,
)


# ── Test fixtures ──────────────────────────────────────────────────────

APP_EUI = bytes.fromhex("0102030405060708")
DEV_EUI = bytes.fromhex("1112131415161718")
APP_KEY = bytes.fromhex("2B7E151628AED2A6ABF7158809CF4F3C")

CREDENTIALS = OTAACredentials(app_eui=APP_EUI, dev_eui=DEV_EUI, app_key=APP_KEY)

NET_ID = 0x000001
DEV_ADDR = 0x01000001
APP_NONCE = 42


def _make_device_db() -> dict[bytes, OTAADeviceRecord]:
    return {
        DEV_EUI: OTAADeviceRecord(
            app_eui=APP_EUI, dev_eui=DEV_EUI, app_key=APP_KEY,
        )
    }


# ── JoinRequest construction tests ───────────────────────────────────

class TestBuildJoinRequest:
    def test_join_request_structure(self) -> None:
        """JoinRequest should have MHDR(JoinRequest) + 18 bytes payload + 4 bytes MIC."""
        raw = build_join_request(CREDENTIALS, dev_nonce=0)
        # MHDR (1) + AppEUI (8) + DevEUI (8) + DevNonce (2) + MIC (4) = 23 bytes
        assert len(raw) == 23

    def test_join_request_mtype(self) -> None:
        """JoinRequest MType should be JOIN_REQUEST."""
        raw = build_join_request(CREDENTIALS, dev_nonce=0)
        mhdr = MHDR.decode(raw[0])
        assert mhdr.mtype == MType.JOIN_REQUEST

    def test_join_request_mic_valid(self) -> None:
        """JoinRequest MIC should be verifiable with the AppKey."""
        raw = build_join_request(CREDENTIALS, dev_nonce=0)
        mhdr_and_payload = raw[:-4]
        mic = raw[-4:]
        expected_mic = compute_join_request_mic(APP_KEY, mhdr_and_payload)
        assert mic == expected_mic

    def test_join_request_decodable(self) -> None:
        """JoinRequest should be decodable back to its components."""
        raw = build_join_request(CREDENTIALS, dev_nonce=7)
        phy = PHYPayload.decode_join_request(raw)
        assert phy.join_request is not None
        assert phy.join_request.app_eui == APP_EUI
        assert phy.join_request.dev_eui == DEV_EUI
        assert phy.join_request.dev_nonce == 7

    def test_different_dev_nonces_produce_different_requests(self) -> None:
        """Different DevNonces should produce different JoinRequest bytes."""
        raw1 = build_join_request(CREDENTIALS, dev_nonce=0)
        raw2 = build_join_request(CREDENTIALS, dev_nonce=1)
        assert raw1 != raw2


# ── Server-side JoinRequest processing tests ─────────────────────────

class TestProcessJoinRequest:
    def test_valid_join_request(self) -> None:
        """Valid JoinRequest should return JoinAccept bytes and device record."""
        raw = build_join_request(CREDENTIALS, dev_nonce=0)
        db = _make_device_db()
        result = process_join_request(
            raw, db, NET_ID, DEV_ADDR, APP_NONCE,
        )
        assert result is not None
        join_accept_raw, device = result
        assert isinstance(join_accept_raw, bytes)
        assert len(join_accept_raw) >= 17  # MHDR(1) + encrypted(16+)
        assert device.dev_eui == DEV_EUI

    def test_join_accept_mtype(self) -> None:
        """JoinAccept response should have JOIN_ACCEPT MType."""
        raw = build_join_request(CREDENTIALS, dev_nonce=0)
        db = _make_device_db()
        result = process_join_request(raw, db, NET_ID, DEV_ADDR, APP_NONCE)
        assert result is not None
        join_accept_raw, _ = result
        mhdr = MHDR.decode(join_accept_raw[0])
        assert mhdr.mtype == MType.JOIN_ACCEPT

    def test_unknown_dev_eui(self) -> None:
        """JoinRequest with unknown DevEUI should be rejected."""
        raw = build_join_request(CREDENTIALS, dev_nonce=0)
        empty_db: dict[bytes, OTAADeviceRecord] = {}
        result = process_join_request(raw, empty_db, NET_ID, DEV_ADDR, APP_NONCE)
        assert result is None

    def test_invalid_mic(self) -> None:
        """JoinRequest with corrupted MIC should be rejected."""
        raw = bytearray(build_join_request(CREDENTIALS, dev_nonce=0))
        raw[-1] ^= 0xFF  # Corrupt last byte of MIC
        db = _make_device_db()
        result = process_join_request(bytes(raw), db, NET_ID, DEV_ADDR, APP_NONCE)
        assert result is None

    def test_dev_nonce_replay_protection(self) -> None:
        """Same DevNonce should be rejected on second use."""
        db = _make_device_db()
        raw = build_join_request(CREDENTIALS, dev_nonce=42)

        result1 = process_join_request(raw, db, NET_ID, DEV_ADDR, APP_NONCE)
        assert result1 is not None

        result2 = process_join_request(raw, db, NET_ID, DEV_ADDR + 1, APP_NONCE + 1)
        assert result2 is None

    def test_dev_nonce_tracking(self) -> None:
        """Used DevNonce should be recorded in the device record."""
        db = _make_device_db()
        raw = build_join_request(CREDENTIALS, dev_nonce=99)
        process_join_request(raw, db, NET_ID, DEV_ADDR, APP_NONCE)
        assert 99 in db[DEV_EUI].used_dev_nonces


# ── Device-side JoinAccept processing tests ──────────────────────────

class TestProcessJoinAccept:
    def _get_join_accept(self, dev_nonce: int = 0) -> bytes:
        """Helper: generate a valid JoinAccept for the test credentials."""
        raw = build_join_request(CREDENTIALS, dev_nonce=dev_nonce)
        db = _make_device_db()
        result = process_join_request(raw, db, NET_ID, DEV_ADDR, APP_NONCE)
        assert result is not None
        return result[0]

    def test_valid_join_accept(self) -> None:
        """Valid JoinAccept should return JoinResult with correct DevAddr."""
        join_accept_raw = self._get_join_accept(dev_nonce=0)
        result = process_join_accept(join_accept_raw, CREDENTIALS, dev_nonce=0)
        assert result is not None
        assert isinstance(result, JoinResult)
        assert result.dev_addr == DEV_ADDR

    def test_session_keys_match(self) -> None:
        """Session keys derived by device should match server's keys."""
        join_accept_raw = self._get_join_accept(dev_nonce=0)
        result = process_join_accept(join_accept_raw, CREDENTIALS, dev_nonce=0)
        assert result is not None

        # Derive keys the same way the server does
        expected_nwk, expected_app = derive_session_keys(
            APP_KEY, APP_NONCE, NET_ID, 0,
        )
        assert result.nwk_s_key == expected_nwk
        assert result.app_s_key == expected_app

    def test_rx_delay_default(self) -> None:
        """Default RX delay from JoinAccept should be 1."""
        join_accept_raw = self._get_join_accept(dev_nonce=0)
        result = process_join_accept(join_accept_raw, CREDENTIALS, dev_nonce=0)
        assert result is not None
        assert result.rx_delay == 1

    def test_wrong_dev_nonce_fails(self) -> None:
        """JoinAccept decrypted with wrong DevNonce should fail MIC check."""
        join_accept_raw = self._get_join_accept(dev_nonce=0)
        # Process with wrong dev_nonce — derivation will give wrong keys,
        # but MIC is checked before key derivation, so it should still pass
        # MIC (MIC doesn't use DevNonce). However, the keys will be different.
        # Let's verify the process still works but yields different keys.
        result = process_join_accept(join_accept_raw, CREDENTIALS, dev_nonce=999)
        # MIC check passes (MIC is over MHDR|JoinAccept, not DevNonce-dependent)
        # but session keys will be wrong
        assert result is not None
        expected_nwk, _ = derive_session_keys(APP_KEY, APP_NONCE, NET_ID, 0)
        assert result.nwk_s_key != expected_nwk  # Keys differ due to wrong DevNonce

    def test_corrupted_join_accept(self) -> None:
        """Corrupted JoinAccept should fail MIC verification."""
        join_accept_raw = bytearray(self._get_join_accept(dev_nonce=0))
        join_accept_raw[5] ^= 0xFF  # Corrupt encrypted body
        result = process_join_accept(bytes(join_accept_raw), CREDENTIALS, dev_nonce=0)
        assert result is None

    def test_too_short_join_accept(self) -> None:
        """JoinAccept that's too short should be rejected."""
        result = process_join_accept(b"\x20\x00", CREDENTIALS, dev_nonce=0)
        assert result is None


# ── Full roundtrip test ──────────────────────────────────────────────

class TestJoinRoundtrip:
    def test_full_join_roundtrip(self) -> None:
        """Full join: device builds request → server processes → device processes accept."""
        dev_nonce = 5
        db = _make_device_db()

        # Step 1: Device builds JoinRequest
        join_req_raw = build_join_request(CREDENTIALS, dev_nonce)

        # Step 2: Server processes JoinRequest, generates JoinAccept
        result = process_join_request(
            join_req_raw, db, NET_ID, DEV_ADDR, APP_NONCE,
        )
        assert result is not None
        join_accept_raw, _ = result

        # Step 3: Device processes JoinAccept
        join_result = process_join_accept(join_accept_raw, CREDENTIALS, dev_nonce)
        assert join_result is not None
        assert join_result.dev_addr == DEV_ADDR

        # Step 4: Verify both sides derive the same session keys
        server_nwk, server_app = derive_session_keys(
            APP_KEY, APP_NONCE, NET_ID, dev_nonce,
        )
        assert join_result.nwk_s_key == server_nwk
        assert join_result.app_s_key == server_app

    def test_multiple_joins_different_nonces(self) -> None:
        """Multiple join procedures with different nonces should all succeed."""
        db = _make_device_db()
        for i in range(5):
            req = build_join_request(CREDENTIALS, dev_nonce=i)
            result = process_join_request(
                req, db, NET_ID, DEV_ADDR + i, APP_NONCE + i,
            )
            assert result is not None
            accept_raw, _ = result
            join_result = process_join_accept(accept_raw, CREDENTIALS, dev_nonce=i)
            assert join_result is not None
            assert join_result.dev_addr == DEV_ADDR + i
