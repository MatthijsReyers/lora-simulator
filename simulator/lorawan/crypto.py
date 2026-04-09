"""
LoRaWAN 1.0.4 cryptographic operations.

- MIC calculation for data frames (§4.4) using AES-128 CMAC
- MIC calculation for Join Request (§6.2.4)
- MIC calculation for Join Accept (§6.2.5)
- FRMPayload encryption/decryption (§4.3.3) using AES-128 in CTR-like mode
- Join Accept encryption/decryption (§6.2.5) using AES-128 ECB (decrypt-to-encrypt)
- Session key derivation from Join Accept (§6.2.5)

Reference: LoRaWAN L2 1.0.4 Specification §4.3.3, §4.4, §6.2.
"""

from __future__ import annotations
import struct
from cryptography.hazmat.primitives.ciphers import Cipher
from cryptography.hazmat.primitives.ciphers.algorithms import AES128
from cryptography.hazmat.primitives.ciphers.modes import ECB
from cryptography.hazmat.primitives.cmac import CMAC


def _aes128_encrypt(key: bytes, block: bytes) -> bytes:
    """Encrypt a single 16-byte block with AES-128 ECB."""
    cipher = Cipher(AES128(key), ECB())
    encryptor = cipher.encryptor()
    return encryptor.update(block) + encryptor.finalize()


def _aes128_cmac(key: bytes, msg: bytes) -> bytes:
    """Compute AES-128 CMAC over msg, returning the full 16-byte tag."""
    c = CMAC(AES128(key))
    c.update(msg)
    return c.finalize()


def compute_data_mic(key: bytes, *, dev_addr: int, fcnt: int, uplink: bool,
                     mhdr_and_payload: bytes) -> bytes:
    """Compute MIC for a data frame (§4.4).

    B0 block:
        0x49 | 0x00 0x00 0x00 0x00 | dir | DevAddr (4, LE) | FCntUp/Down (4, LE) | 0x00 | len(msg)

    MIC = cmac[0:4]

    Args:
        key: NwkSKey (16 bytes).
        dev_addr: 32-bit device address.
        fcnt: Full 32-bit frame counter.
        uplink: True for uplink, False for downlink.
        mhdr_and_payload: MHDR | MACPayload (everything except MIC).

    Returns:
        4-byte MIC.
    """
    direction = 0x00 if uplink else 0x01
    b0 = struct.pack("<BIB4sIBB",
                     0x49,           # constant
                     0x00000000,     # 4 zero bytes
                     direction,
                     dev_addr.to_bytes(4, "little"),
                     fcnt,
                     0x00,
                     len(mhdr_and_payload))
    return _aes128_cmac(key, b0 + mhdr_and_payload)[:4]


def compute_join_request_mic(app_key: bytes, mhdr_and_payload: bytes) -> bytes:
    """Compute MIC for a Join Request frame (§6.2.4).

    cmac = aes128_cmac(AppKey, MHDR | AppEUI | DevEUI | DevNonce)
    MIC = cmac[0:4]
    """
    return _aes128_cmac(app_key, mhdr_and_payload)[:4]


def compute_join_accept_mic(app_key: bytes, mhdr_and_payload: bytes) -> bytes:
    """Compute MIC for a Join Accept frame (§6.2.5).

    cmac = aes128_cmac(AppKey, MHDR | AppNonce | NetID | DevAddr | DLSettings | RxDelay | CFList)
    MIC = cmac[0:4]
    """
    return _aes128_cmac(app_key, mhdr_and_payload)[:4]


def encrypt_frm_payload(key: bytes, *, dev_addr: int, fcnt: int, uplink: bool,
                        payload: bytes) -> bytes:
    """Encrypt or decrypt FRMPayload (§4.3.3).

    The operation is symmetric (encrypt == decrypt).

    Uses AES-128 in a CTR-like mode with block Ai:
        0x01 | 0x00 0x00 0x00 0x00 | dir | DevAddr (4, LE) | FCntUp/Down (4, LE) | 0x00 | i

    S = S1 | S2 | ... | Sk
    encrypted = payload XOR S (truncated to payload length)

    Args:
        key: AppSKey (FPort > 0) or NwkSKey (FPort == 0), 16 bytes.
        dev_addr: 32-bit device address.
        fcnt: Full 32-bit frame counter.
        uplink: True for uplink, False for downlink.
        payload: Plaintext (encryption) or ciphertext (decryption).

    Returns:
        Encrypted/decrypted payload (same length as input).
    """
    if len(payload) == 0:
        return b""

    direction = 0x00 if uplink else 0x01
    k = (len(payload) + 15) // 16  # number of blocks needed

    s = bytearray()
    for i in range(1, k + 1):
        ai = struct.pack("<BIB4sIBB",
                         0x01,
                         0x00000000,
                         direction,
                         dev_addr.to_bytes(4, "little"),
                         fcnt,
                         0x00,
                         i)
        s.extend(_aes128_encrypt(key, ai))

    # XOR payload with S stream, truncated to payload length
    return bytes(p ^ s[j] for j, p in enumerate(payload))


def encrypt_join_accept(app_key: bytes, join_accept_bytes: bytes) -> bytes:
    """Encrypt a Join Accept message (§6.2.5).

    LoRaWAN uses AES-128 *decrypt* to encrypt the Join Accept so that the
    device can use AES-128 *encrypt* (which is always available) to decrypt it.

    The input is: AppNonce | NetID | DevAddr | DLSettings | RxDelay | CFList | MIC
    (i.e., everything after MHDR).

    The output is the encrypted version of the same, which replaces the
    plaintext in the PHYPayload.
    """
    assert len(join_accept_bytes) in (16, 32), \
        f"Join Accept body must be 16 or 32 bytes, got {len(join_accept_bytes)}"

    # Use AES decrypt for "encryption" per spec
    cipher = Cipher(AES128(app_key), ECB())
    decryptor = cipher.decryptor()
    return decryptor.update(join_accept_bytes) + decryptor.finalize()


def decrypt_join_accept(app_key: bytes, encrypted: bytes) -> bytes:
    """Decrypt a Join Accept message (§6.2.5).

    Uses AES-128 *encrypt* to decrypt (inverse of encrypt_join_accept).
    """
    assert len(encrypted) in (16, 32), \
        f"Encrypted Join Accept body must be 16 or 32 bytes, got {len(encrypted)}"

    result = bytearray()
    for i in range(0, len(encrypted), 16):
        result.extend(_aes128_encrypt(app_key, encrypted[i:i + 16]))
    return bytes(result)


def derive_session_keys(app_key: bytes, app_nonce: int, net_id: int,
                        dev_nonce: int) -> tuple[bytes, bytes]:
    """Derive NwkSKey and AppSKey from Join Accept parameters (§6.2.5).

    NwkSKey = aes128_encrypt(AppKey, 0x01 | AppNonce | NetID | DevNonce | pad_16)
    AppSKey = aes128_encrypt(AppKey, 0x02 | AppNonce | NetID | DevNonce | pad_16)

    Returns:
        Tuple of (NwkSKey, AppSKey), each 16 bytes.
    """
    base = bytearray()
    base.extend(app_nonce.to_bytes(3, "little"))
    base.extend(net_id.to_bytes(3, "little"))
    base.extend(dev_nonce.to_bytes(2, "little"))

    nwk_input = bytearray(16)
    nwk_input[0] = 0x01
    nwk_input[1:1 + len(base)] = base
    nwk_s_key = _aes128_encrypt(app_key, bytes(nwk_input))

    app_input = bytearray(16)
    app_input[0] = 0x02
    app_input[1:1 + len(base)] = base
    app_s_key = _aes128_encrypt(app_key, bytes(app_input))

    return nwk_s_key, app_s_key
