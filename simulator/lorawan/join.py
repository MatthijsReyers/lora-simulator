"""
LoRaWAN Over-The-Air Activation (OTAA) — Join procedure (§6.2).

Handles JoinRequest construction, JoinAccept parsing/generation, and
session key derivation. Works with the existing crypto primitives in
crypto.py and frame definitions in frame.py.

Reference: LoRaWAN L2 1.0.4 Specification §6.2.
"""

from __future__ import annotations
import logging
from dataclasses import dataclass, field

from simulator.lorawan.enums.frame_types import MType
from simulator.lorawan.frame import (
    MHDR, PHYPayload, JoinRequestPayload, JoinAcceptPayload,
)
from simulator.lorawan.crypto import (
    compute_join_request_mic,
    compute_join_accept_mic,
    encrypt_join_accept,
    decrypt_join_accept,
    derive_session_keys,
)

logger = logging.getLogger(__name__)


@dataclass
class OTAACredentials:
    """Root keys for OTAA activation."""
    app_eui: bytes   # 8 bytes (JoinEUI in 1.1)
    dev_eui: bytes   # 8 bytes
    app_key: bytes   # 16 bytes (root AES key)


@dataclass
class JoinResult:
    """Session parameters derived from a successful join."""
    dev_addr: int
    nwk_s_key: bytes
    app_s_key: bytes
    rx1_dr_offset: int
    rx2_data_rate: int
    rx_delay: int


@dataclass
class OTAADeviceRecord:
    """Network server's record for an OTAA-capable device."""
    app_eui: bytes
    dev_eui: bytes
    app_key: bytes
    used_dev_nonces: set[int] = field(default_factory=set)


def build_join_request(credentials: OTAACredentials, dev_nonce: int) -> bytes:
    """
    Build a complete JoinRequest PHYPayload.

    Returns the raw bytes ready for transmission.
    """
    mhdr = MHDR(mtype=MType.JOIN_REQUEST)
    join_req = JoinRequestPayload(
        app_eui=credentials.app_eui,
        dev_eui=credentials.dev_eui,
        dev_nonce=dev_nonce,
    )

    # MIC is computed over MHDR | JoinRequestPayload
    mhdr_and_payload = bytes([mhdr.encode()]) + join_req.encode()
    mic = compute_join_request_mic(credentials.app_key, mhdr_and_payload)

    phy = PHYPayload(mhdr=mhdr, join_request=join_req, mic=mic)
    return phy.encode()


def process_join_request(
    raw: bytes,
    device_db: dict[bytes, OTAADeviceRecord],
    net_id: int,
    assign_dev_addr: int,
    app_nonce: int,
    dl_settings: int = 0x00,
    rx_delay: int = 1,
) -> tuple[bytes, OTAADeviceRecord] | None:
    """
    Process a JoinRequest on the network server side.

    Verifies MIC, checks DevNonce for replay, generates JoinAccept.

    Args:
        raw: Raw JoinRequest PHYPayload bytes.
        device_db: Map of DevEUI → OTAADeviceRecord.
        net_id: 24-bit network identifier.
        assign_dev_addr: DevAddr to assign to the device.
        app_nonce: 24-bit application nonce (JoinNonce).
        dl_settings: DLSettings byte (RX1DRoffset | RX2DataRate).
        rx_delay: RX delay in seconds (0 maps to 1).

    Returns:
        Tuple of (JoinAccept raw bytes, device record) on success, None on failure.
    """
    # Decode the JoinRequest
    try:
        phy = PHYPayload.decode_join_request(raw)
    except (AssertionError, Exception) as e:
        logger.warning(f"Failed to decode JoinRequest: {e}")
        return None

    assert phy.join_request is not None
    join_req = phy.join_request

    # Look up device by DevEUI
    device = device_db.get(join_req.dev_eui)
    if device is None:
        logger.warning(f"Unknown DevEUI: {join_req.dev_eui.hex()}")
        return None

    # Verify MIC
    mhdr_and_payload = raw[:-4]
    expected_mic = compute_join_request_mic(device.app_key, mhdr_and_payload)
    if expected_mic != phy.mic:
        logger.warning(
            f"JoinRequest MIC mismatch for DevEUI={join_req.dev_eui.hex()}"
        )
        return None

    # Check DevNonce for replay protection
    if join_req.dev_nonce in device.used_dev_nonces:
        logger.warning(
            f"DevNonce replay: {join_req.dev_nonce} for DevEUI={join_req.dev_eui.hex()}"
        )
        return None
    device.used_dev_nonces.add(join_req.dev_nonce)

    # Build JoinAccept
    join_accept = JoinAcceptPayload(
        app_nonce=app_nonce,
        net_id=net_id,
        dev_addr=assign_dev_addr,
        dl_settings=dl_settings,
        rx_delay=rx_delay,
    )

    mhdr = MHDR(mtype=MType.JOIN_ACCEPT)

    # Compute MIC over MHDR | JoinAcceptPayload (plaintext)
    mhdr_and_accept = bytes([mhdr.encode()]) + join_accept.encode()
    mic = compute_join_accept_mic(device.app_key, mhdr_and_accept)

    # Encrypt the JoinAccept body + MIC with AppKey
    encrypted = encrypt_join_accept(device.app_key, join_accept.encode() + mic)

    # Final PHYPayload: MHDR | encrypted(JoinAccept | MIC)
    result = bytes([mhdr.encode()]) + encrypted

    logger.debug(
        f"JoinAccept generated for DevEUI={join_req.dev_eui.hex()} "
        f"DevAddr=0x{assign_dev_addr:08X}"
    )

    return result, device


def process_join_accept(
    raw: bytes,
    credentials: OTAACredentials,
    dev_nonce: int,
) -> JoinResult | None:
    """
    Process a JoinAccept on the device side.

    Decrypts the JoinAccept, verifies MIC, and derives session keys.

    Args:
        raw: Raw JoinAccept PHYPayload bytes (MHDR + encrypted body).
        credentials: Device's OTAA root keys.
        dev_nonce: The DevNonce used in the corresponding JoinRequest.

    Returns:
        JoinResult with session parameters on success, None on failure.
    """
    if len(raw) < 17:  # MHDR (1) + encrypted body (16 min)
        logger.warning(f"JoinAccept too short: {len(raw)} bytes")
        return None

    mhdr = MHDR.decode(raw[0])
    if mhdr.mtype != MType.JOIN_ACCEPT:
        logger.warning(f"Not a JoinAccept: MType={mhdr.mtype}")
        return None

    encrypted_body = raw[1:]

    # Decrypt
    decrypted = decrypt_join_accept(credentials.app_key, encrypted_body)

    # Last 4 bytes are MIC
    accept_bytes = decrypted[:-4]
    mic = decrypted[-4:]

    # Verify MIC over MHDR | plaintext JoinAccept
    mhdr_and_accept = bytes([raw[0]]) + accept_bytes
    expected_mic = compute_join_accept_mic(credentials.app_key, mhdr_and_accept)
    if expected_mic != mic:
        logger.warning("JoinAccept MIC mismatch")
        return None

    # Decode JoinAccept payload
    join_accept = JoinAcceptPayload.decode(accept_bytes)

    # Derive session keys
    nwk_s_key, app_s_key = derive_session_keys(
        credentials.app_key,
        join_accept.app_nonce,
        join_accept.net_id,
        dev_nonce,
    )

    return JoinResult(
        dev_addr=join_accept.dev_addr,
        nwk_s_key=nwk_s_key,
        app_s_key=app_s_key,
        rx1_dr_offset=join_accept.rx1_dr_offset,
        rx2_data_rate=join_accept.rx2_data_rate,
        rx_delay=max(1, join_accept.rx_delay),
    )
