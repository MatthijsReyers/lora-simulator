"""
FUOTA cryptographic operations (TS005 v2.0.0 and TS004 v2.0.0).

Implements the key hierarchy used by the LoRa Alliance FUOTA packages:

- TS005 §4.2.6 "Key derivation": McRootKey, McKEKey, McKey transport and the
  per-group session keys McAppSKey / McNwkSKey.
- TS004 §3.3 (FragSessionSetupReq): DataBlockIntKey and the data-block MIC.

All derivations are AES-128 ECB over a single 16-byte block. Following the
document-wide convention of both specifications ("the octet order for all
multi-octet fields is little endian"), every multi-octet value placed inside an
AES input block or inside the MIC B0 block is encoded little endian.

References:
    LoRaWAN Remote Multicast Setup Specification TS005-2.0.0 (April 2022).
    LoRaWAN Fragmented Data Block Transport Specification TS004-2.0.0 (April 2022).
"""

from __future__ import annotations

from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers import Cipher
from cryptography.hazmat.primitives.ciphers.algorithms import AES128
from cryptography.hazmat.primitives.ciphers.modes import ECB

from simulator.lorawan.crypto import _aes128_cmac, _aes128_encrypt

__all__ = [
    "MulticastKeyMaterial",
    "compute_data_block_mic",
    "decrypt_mc_key",
    "derive_data_block_int_key",
    "derive_mc_app_s_key",
    "derive_mc_ke_key",
    "derive_mc_nwk_s_key",
    "derive_mc_root_key",
    "derive_multicast_key_material",
    "encrypt_mc_key",
]

#: Length of every key handled by this module, in octets.
KEY_SIZE = 16


def _aes128_decrypt(key: bytes, block: bytes) -> bytes:
    """Decrypt a single 16-byte block with AES-128 ECB.

    Same idiom as :func:`simulator.lorawan.crypto.encrypt_join_accept`, which
    likewise uses the AES decrypt primitive as an "encrypt" operation.
    """
    cipher = Cipher(AES128(key), ECB())
    decryptor = cipher.decryptor()
    return decryptor.update(block) + decryptor.finalize()


def _check_key(name: str, key: bytes) -> None:
    if len(key) != KEY_SIZE:
        raise ValueError(f"{name} must be {KEY_SIZE} bytes, got {len(key)}")


def derive_mc_root_key(*, key: bytes, lorawan_1_1: bool = False) -> bytes:
    """Derive McRootKey, the lifetime per-device multicast root key (TS005 §4.2.6).

    LoRaWAN 1.0.x (``lorawan_1_1=False``), where ``key`` is the **GenAppKey**::

        McRootKey = aes128_encrypt(GenAppKey, 0x00 | pad16)

    LoRaWAN 1.1+ (``lorawan_1_1=True``), where ``key`` is the **AppKey**::

        McRootKey = aes128_encrypt(AppKey, 0x20 | pad16)

    This applies to both OTAA and ABP devices; TS005 notes that even a 1.1 ABP
    device, which does not otherwise need an AppKey, REQUIRES one here.

    Args:
        key: GenAppKey for LoRaWAN 1.0.x, AppKey for LoRaWAN 1.1+ (16 bytes).
        lorawan_1_1: Select the LoRaWAN 1.1+ scheme (prefix 0x20) instead of
            the 1.0.x scheme (prefix 0x00).

    Returns:
        The 16-byte McRootKey.
    """
    _check_key("key", key)
    prefix = 0x20 if lorawan_1_1 else 0x00
    return _aes128_encrypt(key, bytes([prefix]) + bytes(15))


def derive_mc_ke_key(*, mc_root_key: bytes) -> bytes:
    """Derive McKEKey, the lifetime per-device key-encryption key (TS005 §4.2.6).

    Identical in both the 1.0.x and the 1.1+ scheme::

        McKEKey = aes128_encrypt(McRootKey, 0x00 | pad16)

    Args:
        mc_root_key: McRootKey from :func:`derive_mc_root_key` (16 bytes).

    Returns:
        The 16-byte McKEKey.
    """
    _check_key("mc_root_key", mc_root_key)
    return _aes128_encrypt(mc_root_key, bytes(16))


def encrypt_mc_key(*, mc_ke_key: bytes, mc_key: bytes) -> bytes:
    """Encrypt an McKey for transport in McGroupSetupReq (TS005 §4.2.6, server side).

    TS005 deliberately inverts the primitives so that a constrained end-device
    only ever needs AES *encrypt*: the Application Server uses the AES **decrypt**
    operation in ECB mode to produce the ciphertext::

        McKey_encrypted = aes128_decrypt(McKEKey, McKey)

    Args:
        mc_ke_key: McKEKey of the target end-device (16 bytes).
        mc_key: The multicast group key to transport (16 bytes).

    Returns:
        The 16-byte ``McKey_encrypted`` field of McGroupSetupReq.
    """
    _check_key("mc_ke_key", mc_ke_key)
    _check_key("mc_key", mc_key)
    return _aes128_decrypt(mc_ke_key, mc_key)


def decrypt_mc_key(*, mc_ke_key: bytes, mc_key_encrypted: bytes) -> bytes:
    """Recover the McKey from McGroupSetupReq (TS005 §4.2.6, device side).

    The end-device uses the AES **encrypt** operation -- this is not a typo,
    it is the inverse of :func:`encrypt_mc_key`::

        McKey = aes128_encrypt(McKEKey, McKey_encrypted)

    Args:
        mc_ke_key: McKEKey of this end-device (16 bytes).
        mc_key_encrypted: The ``McKey_encrypted`` field received (16 bytes).

    Returns:
        The 16-byte McKey of the multicast group.
    """
    _check_key("mc_ke_key", mc_ke_key)
    _check_key("mc_key_encrypted", mc_key_encrypted)
    return _aes128_encrypt(mc_ke_key, mc_key_encrypted)


def _mc_session_key(*, mc_key: bytes, mc_addr: int, prefix: int) -> bytes:
    _check_key("mc_key", mc_key)
    if not 0 <= mc_addr <= 0xFFFFFFFF:
        raise ValueError(f"mc_addr must fit in 32 bits, got {mc_addr}")
    block = bytes([prefix]) + mc_addr.to_bytes(4, "little") + bytes(11)
    return _aes128_encrypt(mc_key, block)


def derive_mc_app_s_key(*, mc_key: bytes, mc_addr: int) -> bytes:
    """Derive McAppSKey for a multicast group (TS005 §4.2.6)::

        McAppSKey = aes128_encrypt(McKey, 0x01 | McAddr | pad16)

    The 16-byte AES input block is ``01 | McAddr[0..3] | 00 * 11`` with McAddr
    little endian, i.e. the same four octets as transmitted on air in
    McGroupSetupReq. TS005 does not restate the byte order locally; the
    document-wide little-endian rule is applied.

    Args:
        mc_key: The group's McKey (16 bytes).
        mc_addr: The 32-bit multicast address McAddr.

    Returns:
        The 16-byte McAppSKey.
    """
    return _mc_session_key(mc_key=mc_key, mc_addr=mc_addr, prefix=0x01)


def derive_mc_nwk_s_key(*, mc_key: bytes, mc_addr: int) -> bytes:
    """Derive McNwkSKey for a multicast group (TS005 §4.2.6)::

        McNwkSKey = aes128_encrypt(McKey, 0x02 | McAddr | pad16)

    Byte layout as in :func:`derive_mc_app_s_key` but with the 0x02 prefix.

    Args:
        mc_key: The group's McKey (16 bytes).
        mc_addr: The 32-bit multicast address McAddr.

    Returns:
        The 16-byte McNwkSKey.
    """
    return _mc_session_key(mc_key=mc_key, mc_addr=mc_addr, prefix=0x02)


def derive_data_block_int_key(*, key: bytes, lorawan_1_1: bool = False) -> bytes:
    """Derive DataBlockIntKey (TS004 §3.3).

    A lifetime, end-device-specific key used exclusively to compute the data
    block MIC carried in FragSessionSetupReq. Note that it is derived directly
    from the provisioned root key, **not** from McRootKey::

        LoRaWAN 1.0.x : DataBlockIntKey = aes128_encrypt(GenAppKey, 0x30 | pad16)
        LoRaWAN 1.1+  : DataBlockIntKey = aes128_encrypt(AppKey,    0x30 | pad16)

    The prefix is 0x30 in both schemes; only the source key differs. The
    ``lorawan_1_1`` flag is therefore purely documentary here, kept for symmetry
    with :func:`derive_mc_root_key` and to make call sites self-describing.

    Args:
        key: GenAppKey for LoRaWAN 1.0.x, AppKey for LoRaWAN 1.1+ (16 bytes).
        lorawan_1_1: Which scheme the caller believes it is using. Does not
            change the computation.

    Returns:
        The 16-byte DataBlockIntKey.
    """
    _check_key("key", key)
    del lorawan_1_1  # Same 0x30 prefix in both schemes; only the source key differs.
    return _aes128_encrypt(key, b"\x30" + bytes(15))


def compute_data_block_mic(*, data_block_int_key: bytes, data_block: bytes,
                           session_cnt: int, frag_index: int,
                           descriptor: int) -> bytes:
    """Compute the FragSessionSetupReq data block MIC (TS004 §3.3, Table 12)::

        cmac = aes128_cmac(DataBlockIntKey, B0 | msg)
        MIC  = cmac[0..3]

    B0 (16 octets):

    ===========  ====  ===================================================
    Offset       Size  Content
    ===========  ====  ===================================================
    0            1     0x49
    1            2     SessionCnt (little endian)
    3            1     FragIndex
    4            4     Descriptor (little endian)
    8            4     0x00 0x00 0x00 0x00
    12           4     len(data block) in octets, without padding (LE)
    ===========  ====  ===================================================

    ``msg`` is the data block itself, defined by TS004 as
    ``[B1 | B2 | ... | Bm]``, "the concatenation of all the uncoded fragments".

    Ambiguity (TS004 §3.3 vs. §A.1): the **length field is not in question** --
    Table 12 states it explicitly as the data-block length "without padding".
    The one genuinely unstated point is whether the *covered octets* ``msg =
    [B1|...|Bm]`` include the padding of the last uncoded fragment: §A.1
    describes ``[B1:B2:...:Bm]`` as the padded fragment sequence, while §3.3's
    parenthetical "(the data block)" points at the un-padded block. This
    function resolves it by **not** making the choice itself: the caller passes
    in exactly the octets to be covered, and ``len(data_block)`` is what goes
    into the length field -- so passing the un-padded block, as callers should,
    satisfies Table 12 by construction. The interoperable reading used by
    existing implementations is that un-padded block, i.e.
    ``NbFrag * FragSize - Padding`` octets. To experiment with the padded
    variant, pass the padded block; note that the length field then follows it
    and no longer matches Table 12.

    Byte order inside B0 is not restated by TS004; the document-wide
    little-endian convention is applied to SessionCnt, Descriptor and the length.

    Args:
        data_block_int_key: DataBlockIntKey of the target end-device (16 bytes).
        data_block: Exactly the octets covered by the MIC (normally the
            un-padded data block).
        session_cnt: The 16-bit SessionCnt of the fragmentation session.
        frag_index: The fragmentation session index, 0..3 (one octet in B0).
        descriptor: The 32-bit vendor-specific Descriptor field.

    Returns:
        The 4-byte MIC, as carried little endian in FragSessionSetupReq.
    """
    _check_key("data_block_int_key", data_block_int_key)
    if not 0 <= session_cnt <= 0xFFFF:
        raise ValueError(f"session_cnt must fit in 16 bits, got {session_cnt}")
    if not 0 <= frag_index <= 0xFF:
        raise ValueError(f"frag_index must fit in 8 bits, got {frag_index}")
    if not 0 <= descriptor <= 0xFFFFFFFF:
        raise ValueError(f"descriptor must fit in 32 bits, got {descriptor}")

    b0 = (
        b"\x49"
        + session_cnt.to_bytes(2, "little")
        + bytes([frag_index])
        + descriptor.to_bytes(4, "little")
        + bytes(4)
        + len(data_block).to_bytes(4, "little")
    )
    assert len(b0) == 16
    return _aes128_cmac(data_block_int_key, b0 + data_block)[:4]


@dataclass(frozen=True)
class MulticastKeyMaterial:
    """The full key set of one TS005 multicast group as held by a device/server.

    Attributes:
        mc_addr: The 32-bit multicast address of the group.
        mc_key: The group key McKey.
        mc_app_s_key: McAppSKey, used for multicast FRMPayload encryption.
        mc_nwk_s_key: McNwkSKey, used for the multicast downlink MIC.
    """

    mc_addr: int
    mc_key: bytes
    mc_app_s_key: bytes
    mc_nwk_s_key: bytes


def derive_multicast_key_material(*, mc_key: bytes, mc_addr: int) -> MulticastKeyMaterial:
    """Derive both session keys of a multicast group at once (TS005 §4.2.6).

    Args:
        mc_key: The group's McKey (16 bytes).
        mc_addr: The 32-bit multicast address McAddr.

    Returns:
        A :class:`MulticastKeyMaterial` holding McKey, McAppSKey and McNwkSKey.
    """
    return MulticastKeyMaterial(
        mc_addr=mc_addr,
        mc_key=mc_key,
        mc_app_s_key=derive_mc_app_s_key(mc_key=mc_key, mc_addr=mc_addr),
        mc_nwk_s_key=derive_mc_nwk_s_key(mc_key=mc_key, mc_addr=mc_addr),
    )
