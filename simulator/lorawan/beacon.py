"""LoRaWAN Class B beacon payload and ping slot utilities.

Reference: LoRaWAN L2 1.0.4 Specification §12 (Class B — Beacon synchronization).
"""

from __future__ import annotations

import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from simulator.lorawan.region import BEACON_RESERVED, PING_SLOT_LEN


# Beacon frame identifier (simulator-specific).
# Real LoRaWAN beacons use a fixed frequency and distinct preamble;
# in our PHY model a one-byte magic tag serves the same purpose.
BEACON_MAGIC = 0xBE


def encode_beacon(beacon_time: int) -> bytes:
    """Encode a beacon payload: ``[magic(1)] [beacon_time(4 LE)]``."""
    return struct.pack("<BI", BEACON_MAGIC, beacon_time)


def decode_beacon(data: bytes) -> int | None:
    """Decode a beacon payload.  Returns *beacon_time* or ``None`` if invalid."""
    if len(data) < 5 or data[0] != BEACON_MAGIC:
        return None
    return struct.unpack("<I", data[1:5])[0]


# ---- Ping slot scheduling (§12.1) -----------------------------------------

_AES_ZERO_KEY = b"\x00" * 16


def compute_ping_offset(beacon_time: int, dev_addr: int, ping_period: int) -> int:
    """Compute the ping slot offset per LoRaWAN L2 1.0.4 §12.1.

    Uses AES-128-ECB with a 16-byte zero key to produce a deterministic
    pseudo-random offset so different devices open at different times.

    Args:
        beacon_time: Beacon timestamp (integer seconds).
        dev_addr: Device address (32-bit).
        ping_period: Period in *slot units* (``4096 / ping_nb``).

    Returns:
        Slot offset in ``[0, ping_period)``.
    """
    buf = struct.pack("<II", beacon_time, dev_addr) + b"\x00" * 8
    cipher = Cipher(algorithms.AES(_AES_ZERO_KEY), modes.ECB())
    enc = cipher.encryptor()
    rand = enc.update(buf) + enc.finalize()
    return (rand[0] + rand[1] * 256) % ping_period


def compute_ping_slot_times(
    beacon_time: int,
    dev_addr: int,
    ping_nb: int,
) -> list[float]:
    """Return the absolute opening times of all ping slots in one beacon period.

    Args:
        beacon_time: Beacon timestamp (integer seconds).
        dev_addr: Device address.
        ping_nb: Number of ping slots per beacon period (1–128, power of 2).

    Returns:
        Sorted list of absolute times (seconds) at which ping slot RX windows
        should be opened.
    """
    ping_period = 4096 // ping_nb
    offset = compute_ping_offset(beacon_time, dev_addr, ping_period)

    times: list[float] = []
    for i in range(ping_nb):
        slot_index = offset + i * ping_period
        t = beacon_time + BEACON_RESERVED + slot_index * PING_SLOT_LEN
        times.append(t)
    return sorted(times)
