"""Tests for LoRaWAN cryptographic operations."""

import os
from simulator.lorawan.crypto import (
    compute_data_mic, compute_join_request_mic, compute_join_accept_mic,
    encrypt_frm_payload, encrypt_join_accept, decrypt_join_accept,
    derive_session_keys,
)
from simulator.lorawan.enums.frame_types import MType
from simulator.lorawan.frame import (
    MHDR, FCtrl, FHDR, MACPayload, PHYPayload,
    JoinRequestPayload, JoinAcceptPayload,
)


class TestDataMIC:
    def test_mic_is_4_bytes(self):
        key = bytes(16)
        msg = b"\x40" + bytes(11)  # minimal MHDR + MACPayload
        mic = compute_data_mic(key, dev_addr=0, fcnt=0, uplink=True, mhdr_and_payload=msg)
        assert len(mic) == 4

    def test_mic_changes_with_key(self):
        msg = b"\x40" + bytes(11)
        mic1 = compute_data_mic(bytes(16), dev_addr=0, fcnt=0, uplink=True, mhdr_and_payload=msg)
        mic2 = compute_data_mic(bytes.fromhex("01" * 16), dev_addr=0, fcnt=0, uplink=True, mhdr_and_payload=msg)
        assert mic1 != mic2

    def test_mic_changes_with_direction(self):
        key = bytes(16)
        msg = b"\x40" + bytes(11)
        mic_up = compute_data_mic(key, dev_addr=0, fcnt=0, uplink=True, mhdr_and_payload=msg)
        mic_dn = compute_data_mic(key, dev_addr=0, fcnt=0, uplink=False, mhdr_and_payload=msg)
        assert mic_up != mic_dn

    def test_mic_changes_with_fcnt(self):
        key = bytes(16)
        msg = b"\x40" + bytes(11)
        mic_0 = compute_data_mic(key, dev_addr=0, fcnt=0, uplink=True, mhdr_and_payload=msg)
        mic_1 = compute_data_mic(key, dev_addr=0, fcnt=1, uplink=True, mhdr_and_payload=msg)
        assert mic_0 != mic_1

    def test_mic_deterministic(self):
        key = bytes(16)
        msg = b"\x40" + bytes(11)
        mic1 = compute_data_mic(key, dev_addr=0, fcnt=0, uplink=True, mhdr_and_payload=msg)
        mic2 = compute_data_mic(key, dev_addr=0, fcnt=0, uplink=True, mhdr_and_payload=msg)
        assert mic1 == mic2

    def test_full_frame_mic(self):
        """Build a complete frame, compute MIC, and verify it matches on decode."""
        nwk_s_key = os.urandom(16)
        fhdr = FHDR(dev_addr=0x01020304, fctrl=FCtrl(), fcnt=7)
        mac = MACPayload(fhdr=fhdr, fport=1, frm_payload=b"test")
        mhdr = MHDR(mtype=MType.UNCONFIRMED_DATA_UP)

        # Build MHDR | MACPayload for MIC input
        mhdr_and_payload = bytes([mhdr.encode()]) + mac.encode(uplink=True)
        mic = compute_data_mic(nwk_s_key, dev_addr=0x01020304, fcnt=7,
                               uplink=True, mhdr_and_payload=mhdr_and_payload)

        phy = PHYPayload(mhdr=mhdr, mac_payload=mac, mic=mic)
        encoded = phy.encode()

        # Decode and verify MIC
        decoded = PHYPayload.decode_data(encoded)
        assert decoded.mac_payload is not None
        recomputed_mhdr_and_payload = encoded[:-4]
        recomputed_mic = compute_data_mic(
            nwk_s_key, dev_addr=decoded.mac_payload.fhdr.dev_addr,
            fcnt=decoded.mac_payload.fhdr.fcnt, uplink=True,
            mhdr_and_payload=recomputed_mhdr_and_payload,
        )
        assert recomputed_mic == decoded.mic


class TestJoinRequestMIC:
    def test_mic_is_4_bytes(self):
        app_key = bytes(16)
        jr = JoinRequestPayload(app_eui=bytes(8), dev_eui=bytes(8), dev_nonce=0)
        mhdr = MHDR(mtype=MType.JOIN_REQUEST)
        msg = bytes([mhdr.encode()]) + jr.encode()
        mic = compute_join_request_mic(app_key, msg)
        assert len(mic) == 4

    def test_mic_deterministic(self):
        app_key = os.urandom(16)
        jr = JoinRequestPayload(app_eui=bytes(8), dev_eui=bytes(8), dev_nonce=0x5678)
        mhdr = MHDR(mtype=MType.JOIN_REQUEST)
        msg = bytes([mhdr.encode()]) + jr.encode()
        assert compute_join_request_mic(app_key, msg) == compute_join_request_mic(app_key, msg)


class TestFRMPayloadEncryption:
    def test_encrypt_decrypt_roundtrip(self):
        key = os.urandom(16)
        plaintext = b"Hello LoRaWAN!"
        encrypted = encrypt_frm_payload(key, dev_addr=0xAABBCCDD, fcnt=1,
                                        uplink=True, payload=plaintext)
        assert encrypted != plaintext
        decrypted = encrypt_frm_payload(key, dev_addr=0xAABBCCDD, fcnt=1,
                                        uplink=True, payload=encrypted)
        assert decrypted == plaintext

    def test_empty_payload(self):
        key = bytes(16)
        result = encrypt_frm_payload(key, dev_addr=0, fcnt=0, uplink=True, payload=b"")
        assert result == b""

    def test_preserves_length(self):
        key = os.urandom(16)
        for length in [1, 15, 16, 17, 32, 100]:
            plaintext = os.urandom(length)
            encrypted = encrypt_frm_payload(key, dev_addr=0, fcnt=0,
                                            uplink=True, payload=plaintext)
            assert len(encrypted) == length

    def test_different_keys_produce_different_output(self):
        plaintext = b"test data"
        enc1 = encrypt_frm_payload(bytes(16), dev_addr=0, fcnt=0, uplink=True, payload=plaintext)
        enc2 = encrypt_frm_payload(bytes.fromhex("01" * 16), dev_addr=0, fcnt=0,
                                   uplink=True, payload=plaintext)
        assert enc1 != enc2

    def test_different_direction_produces_different_output(self):
        key = os.urandom(16)
        plaintext = b"test data"
        enc_up = encrypt_frm_payload(key, dev_addr=0, fcnt=0, uplink=True, payload=plaintext)
        enc_dn = encrypt_frm_payload(key, dev_addr=0, fcnt=0, uplink=False, payload=plaintext)
        assert enc_up != enc_dn


class TestJoinAcceptEncryption:
    def test_encrypt_decrypt_roundtrip(self):
        app_key = os.urandom(16)
        # 12 bytes payload + 4 bytes MIC = 16 bytes
        ja = JoinAcceptPayload(
            app_nonce=0x112233, net_id=0x000013,
            dev_addr=0xAABBCCDD, dl_settings=0x00, rx_delay=1,
        )
        plaintext = ja.encode() + b"\x00\x00\x00\x00"  # 12 + 4 = 16 bytes
        encrypted = encrypt_join_accept(app_key, plaintext)
        assert encrypted != plaintext
        decrypted = decrypt_join_accept(app_key, encrypted)
        assert decrypted == plaintext

    def test_roundtrip_with_cflist(self):
        app_key = os.urandom(16)
        ja = JoinAcceptPayload(
            app_nonce=0x112233, net_id=0x000013,
            dev_addr=0xAABBCCDD, dl_settings=0x00, rx_delay=1,
            cf_list=bytes(16),
        )
        plaintext = ja.encode() + b"\x00\x00\x00\x00"  # 28 + 4 = 32 bytes
        encrypted = encrypt_join_accept(app_key, plaintext)
        decrypted = decrypt_join_accept(app_key, encrypted)
        assert decrypted == plaintext


class TestSessionKeyDerivation:
    def test_keys_are_16_bytes(self):
        app_key = bytes(16)
        nwk_s_key, app_s_key = derive_session_keys(app_key, app_nonce=0, net_id=0, dev_nonce=0)
        assert len(nwk_s_key) == 16
        assert len(app_s_key) == 16

    def test_nwk_and_app_keys_differ(self):
        app_key = os.urandom(16)
        nwk_s_key, app_s_key = derive_session_keys(app_key, app_nonce=0x123456,
                                                     net_id=0x000013, dev_nonce=0xABCD)
        assert nwk_s_key != app_s_key

    def test_deterministic(self):
        app_key = os.urandom(16)
        keys1 = derive_session_keys(app_key, app_nonce=1, net_id=2, dev_nonce=3)
        keys2 = derive_session_keys(app_key, app_nonce=1, net_id=2, dev_nonce=3)
        assert keys1 == keys2

    def test_different_inputs_produce_different_keys(self):
        app_key = os.urandom(16)
        keys1 = derive_session_keys(app_key, app_nonce=1, net_id=2, dev_nonce=3)
        keys2 = derive_session_keys(app_key, app_nonce=2, net_id=2, dev_nonce=3)
        assert keys1 != keys2
