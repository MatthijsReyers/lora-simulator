"""Tests for FUOTA key derivation and MIC computation (TS004/TS005 v2.0.0).

The known-answer tests deliberately rebuild the AES input blocks with the
``cryptography`` library directly, so that the expected values are reproduced
independently of the implementation under test. This checks the exact byte
layout (prefix octet, little-endian McAddr placement, zero padding) rather than
just self-consistency.

Neither TS004-2.0.0 nor TS005-2.0.0 publishes official cryptographic test
vectors, so no vectors from the specifications are reproduced here.
"""

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher
from cryptography.hazmat.primitives.ciphers.algorithms import AES128
from cryptography.hazmat.primitives.ciphers.modes import ECB
from cryptography.hazmat.primitives.cmac import CMAC

from simulator.lorawan.fuota.crypto import (
    MulticastKeyMaterial,
    compute_data_block_mic,
    decrypt_mc_key,
    derive_data_block_int_key,
    derive_mc_app_s_key,
    derive_mc_ke_key,
    derive_mc_nwk_s_key,
    derive_mc_root_key,
    derive_multicast_key_material,
    encrypt_mc_key,
)

APP_KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
GEN_APP_KEY = bytes.fromhex("0f0e0d0c0b0a09080706050403020100")
MC_KEY = bytes.fromhex("112233445566778899aabbccddeeff00")
MC_ADDR = 0x01234567


def aes_ecb_encrypt(key: bytes, block: bytes) -> bytes:
    """Reference single-block AES-128 ECB encryption, built in the test."""
    enc = Cipher(AES128(key), ECB()).encryptor()
    return enc.update(block) + enc.finalize()


def aes_ecb_decrypt(key: bytes, block: bytes) -> bytes:
    """Reference single-block AES-128 ECB decryption, built in the test."""
    dec = Cipher(AES128(key), ECB()).decryptor()
    return dec.update(block) + dec.finalize()


def aes_cmac(key: bytes, msg: bytes) -> bytes:
    """Reference AES-128 CMAC, built in the test."""
    c = CMAC(AES128(key))
    c.update(msg)
    return c.finalize()


class TestMcRootKey:
    def test_lorawan_10x_known_answer(self):
        """TS005 §4.2.6: McRootKey = aes128_encrypt(GenAppKey, 0x00 | pad16)."""
        expected = aes_ecb_encrypt(GEN_APP_KEY, bytes(16))
        assert derive_mc_root_key(key=GEN_APP_KEY) == expected

    def test_lorawan_11_known_answer(self):
        """TS005 §4.2.6: McRootKey = aes128_encrypt(AppKey, 0x20 | pad16)."""
        expected = aes_ecb_encrypt(APP_KEY, b"\x20" + bytes(15))
        assert derive_mc_root_key(key=APP_KEY, lorawan_1_1=True) == expected

    def test_schemes_differ_for_same_key(self):
        a = derive_mc_root_key(key=APP_KEY, lorawan_1_1=False)
        b = derive_mc_root_key(key=APP_KEY, lorawan_1_1=True)
        assert a != b

    def test_length(self):
        assert len(derive_mc_root_key(key=APP_KEY)) == 16

    def test_rejects_wrong_key_size(self):
        with pytest.raises(ValueError):
            derive_mc_root_key(key=bytes(8))


class TestMcKEKey:
    def test_known_answer(self):
        """TS005 §4.2.6: McKEKey = aes128_encrypt(McRootKey, 0x00 | pad16)."""
        mc_root_key = derive_mc_root_key(key=GEN_APP_KEY)
        expected = aes_ecb_encrypt(mc_root_key, bytes(16))
        assert derive_mc_ke_key(mc_root_key=mc_root_key) == expected

    def test_length(self):
        mc_root_key = derive_mc_root_key(key=GEN_APP_KEY)
        assert len(derive_mc_ke_key(mc_root_key=mc_root_key)) == 16

    def test_rejects_wrong_key_size(self):
        with pytest.raises(ValueError):
            derive_mc_ke_key(mc_root_key=b"short")


class TestMcKeyTransport:
    def _mc_ke_key(self) -> bytes:
        return derive_mc_ke_key(mc_root_key=derive_mc_root_key(key=GEN_APP_KEY))

    def test_server_encrypt_uses_aes_decrypt(self):
        """TS005 §4.2.6: McKey_encrypted = aes128_decrypt(McKEKey, McKey)."""
        kek = self._mc_ke_key()
        assert encrypt_mc_key(mc_ke_key=kek, mc_key=MC_KEY) == aes_ecb_decrypt(kek, MC_KEY)

    def test_device_decrypt_uses_aes_encrypt(self):
        """TS005 §4.2.6: McKey = aes128_encrypt(McKEKey, McKey_encrypted)."""
        kek = self._mc_ke_key()
        ciphertext = encrypt_mc_key(mc_ke_key=kek, mc_key=MC_KEY)
        assert decrypt_mc_key(mc_ke_key=kek, mc_key_encrypted=ciphertext) == \
            aes_ecb_encrypt(kek, ciphertext)

    def test_roundtrip(self):
        kek = self._mc_ke_key()
        ciphertext = encrypt_mc_key(mc_ke_key=kek, mc_key=MC_KEY)
        assert decrypt_mc_key(mc_ke_key=kek, mc_key_encrypted=ciphertext) == MC_KEY

    def test_ciphertext_differs_from_plaintext(self):
        kek = self._mc_ke_key()
        assert encrypt_mc_key(mc_ke_key=kek, mc_key=MC_KEY) != MC_KEY

    def test_wrong_kek_does_not_recover_key(self):
        kek = self._mc_ke_key()
        other = derive_mc_ke_key(mc_root_key=derive_mc_root_key(key=APP_KEY))
        ciphertext = encrypt_mc_key(mc_ke_key=kek, mc_key=MC_KEY)
        assert decrypt_mc_key(mc_ke_key=other, mc_key_encrypted=ciphertext) != MC_KEY

    def test_lengths(self):
        kek = self._mc_ke_key()
        ciphertext = encrypt_mc_key(mc_ke_key=kek, mc_key=MC_KEY)
        assert len(ciphertext) == 16
        assert len(decrypt_mc_key(mc_ke_key=kek, mc_key_encrypted=ciphertext)) == 16

    def test_rejects_wrong_sizes(self):
        kek = self._mc_ke_key()
        with pytest.raises(ValueError):
            encrypt_mc_key(mc_ke_key=kek, mc_key=bytes(15))
        with pytest.raises(ValueError):
            decrypt_mc_key(mc_ke_key=kek, mc_key_encrypted=bytes(17))


class TestMcSessionKeys:
    def test_mc_app_s_key_known_answer(self):
        """TS005 §4.2.6: McAppSKey = aes128_encrypt(McKey, 0x01 | McAddr | pad16)."""
        block = b"\x01" + MC_ADDR.to_bytes(4, "little") + bytes(11)
        assert derive_mc_app_s_key(mc_key=MC_KEY, mc_addr=MC_ADDR) == \
            aes_ecb_encrypt(MC_KEY, block)

    def test_mc_nwk_s_key_known_answer(self):
        """TS005 §4.2.6: McNwkSKey = aes128_encrypt(McKey, 0x02 | McAddr | pad16)."""
        block = b"\x02" + MC_ADDR.to_bytes(4, "little") + bytes(11)
        assert derive_mc_nwk_s_key(mc_key=MC_KEY, mc_addr=MC_ADDR) == \
            aes_ecb_encrypt(MC_KEY, block)

    def test_mc_addr_is_little_endian_in_the_aes_block(self):
        """The four McAddr octets appear LSB-first at offsets 1..4."""
        expected_block = bytes([0x01, 0x67, 0x45, 0x23, 0x01]) + bytes(11)
        assert derive_mc_app_s_key(mc_key=MC_KEY, mc_addr=MC_ADDR) == \
            aes_ecb_encrypt(MC_KEY, expected_block)
        # A big-endian layout would give a different key.
        be_block = b"\x01" + MC_ADDR.to_bytes(4, "big") + bytes(11)
        assert derive_mc_app_s_key(mc_key=MC_KEY, mc_addr=MC_ADDR) != \
            aes_ecb_encrypt(MC_KEY, be_block)

    def test_app_and_nwk_keys_differ(self):
        assert derive_mc_app_s_key(mc_key=MC_KEY, mc_addr=MC_ADDR) != \
            derive_mc_nwk_s_key(mc_key=MC_KEY, mc_addr=MC_ADDR)

    def test_different_mc_addr_gives_different_keys(self):
        other = MC_ADDR + 1
        assert derive_mc_app_s_key(mc_key=MC_KEY, mc_addr=MC_ADDR) != \
            derive_mc_app_s_key(mc_key=MC_KEY, mc_addr=other)
        assert derive_mc_nwk_s_key(mc_key=MC_KEY, mc_addr=MC_ADDR) != \
            derive_mc_nwk_s_key(mc_key=MC_KEY, mc_addr=other)

    def test_different_mc_key_gives_different_keys(self):
        other_key = bytes(16)
        assert derive_mc_app_s_key(mc_key=MC_KEY, mc_addr=MC_ADDR) != \
            derive_mc_app_s_key(mc_key=other_key, mc_addr=MC_ADDR)

    def test_lengths(self):
        assert len(derive_mc_app_s_key(mc_key=MC_KEY, mc_addr=MC_ADDR)) == 16
        assert len(derive_mc_nwk_s_key(mc_key=MC_KEY, mc_addr=MC_ADDR)) == 16

    def test_max_mc_addr_accepted(self):
        assert len(derive_mc_app_s_key(mc_key=MC_KEY, mc_addr=0xFFFFFFFF)) == 16

    def test_rejects_out_of_range_mc_addr(self):
        with pytest.raises(ValueError):
            derive_mc_app_s_key(mc_key=MC_KEY, mc_addr=0x1_0000_0000)
        with pytest.raises(ValueError):
            derive_mc_nwk_s_key(mc_key=MC_KEY, mc_addr=-1)

    def test_rejects_wrong_key_size(self):
        with pytest.raises(ValueError):
            derive_mc_app_s_key(mc_key=bytes(4), mc_addr=MC_ADDR)


class TestMulticastKeyMaterial:
    def test_matches_individual_derivations(self):
        material = derive_multicast_key_material(mc_key=MC_KEY, mc_addr=MC_ADDR)
        assert isinstance(material, MulticastKeyMaterial)
        assert material.mc_addr == MC_ADDR
        assert material.mc_key == MC_KEY
        assert material.mc_app_s_key == derive_mc_app_s_key(mc_key=MC_KEY, mc_addr=MC_ADDR)
        assert material.mc_nwk_s_key == derive_mc_nwk_s_key(mc_key=MC_KEY, mc_addr=MC_ADDR)


class TestDataBlockIntKey:
    def test_known_answer_10x(self):
        """TS004 §3.3: DataBlockIntKey = aes128_encrypt(GenAppKey, 0x30 | pad16)."""
        expected = aes_ecb_encrypt(GEN_APP_KEY, b"\x30" + bytes(15))
        assert derive_data_block_int_key(key=GEN_APP_KEY) == expected

    def test_known_answer_11(self):
        """TS004 §3.3: DataBlockIntKey = aes128_encrypt(AppKey, 0x30 | pad16)."""
        expected = aes_ecb_encrypt(APP_KEY, b"\x30" + bytes(15))
        assert derive_data_block_int_key(key=APP_KEY, lorawan_1_1=True) == expected

    def test_scheme_flag_does_not_change_result(self):
        """Both schemes use the 0x30 prefix; only the source key differs."""
        assert derive_data_block_int_key(key=APP_KEY, lorawan_1_1=False) == \
            derive_data_block_int_key(key=APP_KEY, lorawan_1_1=True)

    def test_differs_from_mc_root_key(self):
        """The 0x30 prefix must not collide with the 0x00/0x20 McRootKey prefixes."""
        assert derive_data_block_int_key(key=APP_KEY) != derive_mc_root_key(key=APP_KEY)
        assert derive_data_block_int_key(key=APP_KEY) != \
            derive_mc_root_key(key=APP_KEY, lorawan_1_1=True)

    def test_length(self):
        assert len(derive_data_block_int_key(key=APP_KEY)) == 16

    def test_rejects_wrong_key_size(self):
        with pytest.raises(ValueError):
            derive_data_block_int_key(key=bytes(16) + b"\x00")


class TestDataBlockMIC:
    KEY = derive_data_block_int_key(key=APP_KEY)
    BLOCK = bytes(range(100))
    ARGS = dict(session_cnt=0x1234, frag_index=2, descriptor=0xAABBCCDD)

    def _mic(self, **overrides) -> bytes:
        args = dict(self.ARGS)
        args.update(overrides)
        return compute_data_block_mic(
            data_block_int_key=self.KEY,
            data_block=overrides.get("data_block", self.BLOCK),
            session_cnt=args["session_cnt"],
            frag_index=args["frag_index"],
            descriptor=args["descriptor"],
        )

    def test_known_answer(self):
        """TS004 §3.3 Table 12 B0 layout, all multi-octet fields little endian."""
        b0 = (
            b"\x49"
            + (0x1234).to_bytes(2, "little")
            + bytes([2])
            + (0xAABBCCDD).to_bytes(4, "little")
            + bytes(4)
            + len(self.BLOCK).to_bytes(4, "little")
        )
        assert b0 == bytes.fromhex("49341202ddccbbaa0000000064000000")
        expected = aes_cmac(self.KEY, b0 + self.BLOCK)[:4]
        assert self._mic() == expected

    def test_b0_is_16_octets(self):
        b0 = (
            b"\x49"
            + (0x1234).to_bytes(2, "little")
            + bytes([2])
            + (0xAABBCCDD).to_bytes(4, "little")
            + bytes(4)
            + len(self.BLOCK).to_bytes(4, "little")
        )
        assert len(b0) == 16

    def test_mic_is_4_bytes(self):
        assert len(self._mic()) == 4

    def test_mic_changes_with_session_cnt(self):
        assert self._mic() != self._mic(session_cnt=0x1235)

    def test_mic_changes_with_frag_index(self):
        assert self._mic() != self._mic(frag_index=3)

    def test_mic_changes_with_descriptor(self):
        assert self._mic() != self._mic(descriptor=0xAABBCCDE)

    def test_mic_changes_with_data(self):
        mutated = bytearray(self.BLOCK)
        mutated[7] ^= 0x01
        assert self._mic() != self._mic(data_block=bytes(mutated))

    def test_mic_changes_with_key(self):
        other = compute_data_block_mic(
            data_block_int_key=derive_data_block_int_key(key=GEN_APP_KEY),
            data_block=self.BLOCK, **self.ARGS)
        assert self._mic() != other

    def test_padded_and_unpadded_blocks_differ(self):
        """The caller chooses the MIC scope; padding changes both msg and len."""
        padded = self.BLOCK + bytes(4)
        assert self._mic() != self._mic(data_block=padded)

    def test_empty_data_block(self):
        assert len(self._mic(data_block=b"")) == 4

    def test_deterministic(self):
        assert self._mic() == self._mic()

    def test_rejects_out_of_range_fields(self):
        with pytest.raises(ValueError):
            self._mic(session_cnt=0x10000)
        with pytest.raises(ValueError):
            self._mic(frag_index=256)
        with pytest.raises(ValueError):
            self._mic(descriptor=0x1_0000_0000)

    def test_rejects_wrong_key_size(self):
        with pytest.raises(ValueError):
            compute_data_block_mic(data_block_int_key=bytes(15),
                                   data_block=self.BLOCK, **self.ARGS)


class TestFullChain:
    def test_server_and_device_agree(self):
        """End-to-end: server provisions a group, device recovers the same keys."""
        # Device-side lifetime keys (LoRaWAN 1.0.x scheme, GenAppKey).
        mc_root_key = derive_mc_root_key(key=GEN_APP_KEY)
        mc_ke_key = derive_mc_ke_key(mc_root_key=mc_root_key)

        # Server wraps McKey for McGroupSetupReq.
        wrapped = encrypt_mc_key(mc_ke_key=mc_ke_key, mc_key=MC_KEY)

        # Device unwraps and derives the session keys.
        recovered = decrypt_mc_key(mc_ke_key=mc_ke_key, mc_key_encrypted=wrapped)
        assert recovered == MC_KEY

        device = derive_multicast_key_material(mc_key=recovered, mc_addr=MC_ADDR)
        server = derive_multicast_key_material(mc_key=MC_KEY, mc_addr=MC_ADDR)
        assert device == server

        # TS004 MIC agrees on both sides.
        int_key = derive_data_block_int_key(key=GEN_APP_KEY)
        block = bytes(range(64))
        mic = compute_data_block_mic(data_block_int_key=int_key, data_block=block,
                                     session_cnt=1, frag_index=0, descriptor=0)
        assert compute_data_block_mic(data_block_int_key=int_key, data_block=block,
                                      session_cnt=1, frag_index=0, descriptor=0) == mic


# ═══════════════════════════════════════════════════════════════════════════
# The package's public surface (simulator.lorawan.fuota)
# ═══════════════════════════════════════════════════════════════════════════

class TestFuotaPackageExports:
    """``__all__`` must be importable, and the TS004/TS005 name clash must be resolved.

    Both modules define ``PackageVersionReq``/``Ans``, ``encode_commands``,
    ``parse_downlink_commands``, ``parse_uplink_commands`` and
    ``PACKAGE_IDENTIFIER``/``PACKAGE_VERSION`` with different meanings, so the package
    re-exports them only under ``Mc``/``Frag`` prefixed aliases.
    """

    def test_every_exported_name_exists(self):
        import simulator.lorawan.fuota as fuota

        missing = [name for name in fuota.__all__ if not hasattr(fuota, name)]
        assert missing == []

    def test_the_clashing_names_are_not_exported_unprefixed(self):
        import simulator.lorawan.fuota as fuota

        for name in (
            "PackageVersionReq",
            "PackageVersionAns",
            "encode_commands",
            "parse_downlink_commands",
            "parse_uplink_commands",
            "PACKAGE_IDENTIFIER",
            "PACKAGE_VERSION",
        ):
            assert name not in fuota.__all__

    def test_the_prefixed_aliases_point_at_the_right_package(self):
        import simulator.lorawan.fuota as fuota
        from simulator.lorawan.fuota import frag_transport, multicast_setup

        assert fuota.McPackageVersionReq is multicast_setup.PackageVersionReq
        assert fuota.FragPackageVersionReq is frag_transport.PackageVersionReq
        assert fuota.McPackageVersionAns is multicast_setup.PackageVersionAns
        assert fuota.FragPackageVersionAns is frag_transport.PackageVersionAns
        assert fuota.encode_mc_commands is multicast_setup.encode_commands
        assert fuota.encode_frag_commands is frag_transport.encode_commands
        assert fuota.parse_mc_uplink_commands is multicast_setup.parse_uplink_commands
        assert fuota.parse_frag_uplink_commands is frag_transport.parse_uplink_commands
        assert (fuota.MC_PACKAGE_IDENTIFIER, fuota.FRAG_PACKAGE_IDENTIFIER) == (2, 3)

    def test_the_previously_missing_names_are_exported(self):
        import simulator.lorawan.fuota as fuota

        for name in (
            "MAX_FRAG_SESSIONS",
            "MAX_MISSING_FRAG",
            "DATA_FRAGMENT_HEADER_SIZE",
            "McGroupStatusEntry",
            "MAX_MULTICAST_GROUPS",
            "TIME_TO_START_UNSYNCHRONIZED",
        ):
            assert name in fuota.__all__
