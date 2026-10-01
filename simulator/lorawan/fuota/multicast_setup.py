"""TS005 v2.0.0 Remote Multicast Setup package — wire codecs (FPort 200).

Pure, dependency-free encoders/decoders for the application-layer commands of
the LoRa Alliance specification *LoRaWAN Remote Multicast Setup*,
**TS005-2.0.0** (FINAL, April 2022):

- §4.1 ``PackageVersionReq`` / ``PackageVersionAns``      (CID 0x00)
- §4.2 ``McGroupStatusReq`` / ``McGroupStatusAns``        (CID 0x01)
- §4.3 ``McGroupSetupReq`` / ``McGroupSetupAns``          (CID 0x02)
- §4.4 ``McGroupDeleteReq`` / ``McGroupDeleteAns``        (CID 0x03)
- §4.5 ``McClassCSessionReq`` / ``McClassCSessionAns``    (CID 0x04)
- §4.6 ``McClassBSessionReq`` / ``McClassBSessionAns``    (CID 0x05)

This module contains **no simulator state**: no devices, no frames, no keys.
Key derivation lives in :mod:`simulator.lorawan.fuota.crypto` (TS005 §4.3), and
the device/server state machines are built on top of these codecs elsewhere.

Conventions (normative, TS005-2.0.0 §1.5 "Conventions"):

- All multi-octet fields are **little endian** (``McAddr``, ``minMcFCnt``,
  ``maxMcFCnt``, ``SessionTime``, ``DLFreq``, ``TimeToStart``).
- RFU bits **SHALL** be set to 0 by the transmitter and **silently ignored** by
  the receiver.  The one documented exception is bits 7:6 of
  ``McClassB/CSessionAns.Status&McGroupID`` ("Reserved for future errors",
  Tables 20 and 25), which SHALL be treated as *errors* when set — modelled
  here by :attr:`McClassCSessionAns.reserved_errors`.
- A single application payload MAY carry several commands concatenated as
  ``CID | payload | CID | payload | …`` (§3); they are executed first to last
  and each is individually acknowledged on the same FPort.
- All TS005 messages SHALL be sent unicast; a device SHALL silently drop them
  when received on a multicast address (§3).  That rule is enforced by the
  callers of this module, not by the codecs.

Implementation decisions where TS005-2.0.0 is silent (see also the digest of
ambiguities):

1. ``McGroupStatusAns`` record ordering is not stated normatively.  The
   "discard the highest ``McGroupID`` first" rule of §4.2 implies **ascending
   ``McGroupID``**; :meth:`McGroupStatusAns.encode_payload` does not reorder
   the caller's list but :func:`parse_uplink_commands` preserves wire order.
2. ``DLFreq`` is a frequency *in units of 100 Hz* on the wire.  This module
   stores and exposes ``dl_frequency`` in **Hz** and rejects values that are
   not an exact multiple of 100 Hz, rather than silently rounding.
3. Decoders raise :class:`ValueError` on a wrong payload length.  Encoders
   raise :class:`ValueError` on out-of-range field values.
"""

from __future__ import annotations

import inspect
import logging
import random
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import IntEnum
from typing import ClassVar

from simulator.environment import simulation_env as sim
from simulator.lorawan.application import Application
from simulator.lorawan.device import LoRaWanDevice, MulticastGroup
from simulator.lorawan.enums.operating_mode import OperatingMode
from simulator.lorawan.fuota.crypto import (
    decrypt_mc_key,
    derive_mc_ke_key,
    derive_mc_root_key,
    derive_multicast_key_material,
    encrypt_mc_key,
)
from simulator.lorawan.fuota.device_app import FuotaDeviceApplication
from simulator.lorawan.network_server import NetworkServer
from simulator.lorawan.region import (
    BEACON_INTERVAL, EU868_DATA_RATES, MAX_FCNT, RX2_DEFAULT_FREQUENCY, max_frm_payload,
)

logger = logging.getLogger(__name__)


__all__ = [
    "MULTICAST_SETUP_FPORT",
    "PACKAGE_IDENTIFIER",
    "PACKAGE_VERSION",
    "MAX_MULTICAST_GROUPS",
    "TIME_TO_START_UNSYNCHRONIZED",
    "McCID",
    "MulticastSetupCommand",
    "PackageVersionReq",
    "PackageVersionAns",
    "McGroupStatusReq",
    "McGroupStatusEntry",
    "McGroupStatusAns",
    "McGroupSetupReq",
    "McGroupSetupAns",
    "McGroupDeleteReq",
    "McGroupDeleteAns",
    "McClassCSessionReq",
    "McClassCSessionAns",
    "McClassBSessionReq",
    "McClassBSessionAns",
    "MulticastSetupCommandType",
    "parse_downlink_commands",
    "parse_uplink_commands",
    "encode_commands",
    "EU868_MIN_DL_FREQUENCY",
    "EU868_MAX_DL_FREQUENCY",
    "MulticastSessionRequest",
    "MulticastSetupDeviceApplication",
    "ServerMulticastGroup",
    "DeviceSetupState",
    "MulticastSetupServerApplication",
]


#: RECOMMENDED default FPort for the Remote Multicast Setup package (§3).
MULTICAST_SETUP_FPORT = 200

#: ``PackageIdentifier`` of the Remote Multicast Setup package (§3, §4.1).
PACKAGE_IDENTIFIER = 2

#: ``PackageVersion`` implemented here, i.e. TS005 **v2.0.0** (§4.1).
PACKAGE_VERSION = 2

#: Maximum number of simultaneous multicast contexts, ``McGroupID`` 0..3 (§4.3).
#: A device SHALL support at least 1; if it supports *N*, ``McGroupID`` SHALL
#: be in ``[0 : N-1]``.
MAX_MULTICAST_GROUPS = 4

#: ``TimeToStart`` value meaning "more than 2**24-1 seconds away", which tells
#: the Application Server the device clock is out of synchronisation (§4.5).
TIME_TO_START_UNSYNCHRONIZED = 0xFFFFFF


class McCID(IntEnum):
    """Remote Multicast Setup command identifiers (TS005-2.0.0 §3, Table 2)."""

    PACKAGE_VERSION = 0x00
    MC_GROUP_STATUS = 0x01
    MC_GROUP_SETUP = 0x02
    MC_GROUP_DELETE = 0x03
    MC_CLASS_C_SESSION = 0x04
    MC_CLASS_B_SESSION = 0x05


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _check_uint(name: str, value: int, bits: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an int, got {value!r}")
    if not 0 <= value < (1 << bits):
        raise ValueError(f"{name} must fit in {bits} bits (0..{(1 << bits) - 1}), got {value}")
    return value


def _check_group_id(group_id: int) -> int:
    """``McGroupID`` is 2 bits wide and valid in ``[0 : MAX_MULTICAST_GROUPS-1]``."""
    if not isinstance(group_id, int) or isinstance(group_id, bool):
        raise ValueError(f"group_id must be an int, got {group_id!r}")
    if not 0 <= group_id < MAX_MULTICAST_GROUPS:
        raise ValueError(
            f"group_id must be 0..{MAX_MULTICAST_GROUPS - 1} (TS005 §4.3), got {group_id}"
        )
    return group_id


def _encode_dl_frequency(dl_frequency: int) -> bytes:
    """Encode ``DLFreq``: 24-bit little endian, in units of 100 Hz (§4.5, §4.6).

    Same meaning and coding as the ``NewChannelReq`` ``Freq`` field (Class C)
    and the ``BeaconFreqReq`` ``Frequency`` field (Class B), so
    ``actual frequency [Hz] = DLFreq * 100``.  ``DLFreq = 0`` is legal and, for
    Class B, selects the region's default beacon frequency-hopping scheme.
    """
    if not isinstance(dl_frequency, int) or isinstance(dl_frequency, bool):
        raise ValueError(f"dl_frequency must be an int, got {dl_frequency!r}")
    if dl_frequency % 100 != 0:
        raise ValueError(
            f"dl_frequency must be a multiple of 100 Hz (DLFreq is in units of "
            f"100 Hz, TS005 §4.5), got {dl_frequency}"
        )
    raw = dl_frequency // 100
    if not 0 <= raw <= 0xFFFFFF:
        raise ValueError(
            f"dl_frequency must be 0..{0xFFFFFF * 100} Hz (DLFreq is 24 bits), "
            f"got {dl_frequency}"
        )
    return raw.to_bytes(3, "little")


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

@dataclass
class MulticastSetupCommand:
    """Base class for all TS005 Remote Multicast Setup commands.

    ``encode()`` always emits ``CID | payload``; ``encoded_payload_size`` is the
    length of the payload *without* the CID octet.  Fixed-size commands also
    expose it as the class attribute ``PAYLOAD_SIZE``; variable-length answers
    (``McGroupStatusAns``, ``McClassB/CSessionAns``) set ``PAYLOAD_SIZE`` to
    ``None`` and compute the size from their own fields.
    """

    #: Payload size in octets, or ``None`` when the command is variable length.
    PAYLOAD_SIZE: ClassVar[int | None] = None

    cid: McCID

    def encode_payload(self) -> bytes:
        """Encode the command payload (without the CID octet)."""
        return b""

    def encode(self) -> bytes:
        """Encode the full command: CID octet followed by the payload."""
        return bytes([self.cid]) + self.encode_payload()

    @property
    def encoded_payload_size(self) -> int:
        """Size in octets of this command's payload (CID octet excluded)."""
        return len(self.encode_payload())

    @property
    def encoded_size(self) -> int:
        """Size in octets of the full encoded command, CID octet included."""
        return 1 + self.encoded_payload_size


# ---------------------------------------------------------------------------
# PackageVersion (CID 0x00) — TS005-2.0.0 §4.1, Tables 2 and 3
# ---------------------------------------------------------------------------

@dataclass
class PackageVersionReq(MulticastSetupCommand):
    """Server → device request for the package identity (§4.1).

    No payload: the encoded command is the single octet ``0x00``.
    """

    PAYLOAD_SIZE: ClassVar[int | None] = 0

    cid: McCID = McCID.PACKAGE_VERSION

    @staticmethod
    def decode_payload(data: bytes) -> PackageVersionReq:
        if len(data) != 0:
            raise ValueError(f"PackageVersionReq takes no payload, got {len(data)} bytes")
        return PackageVersionReq()


@dataclass
class PackageVersionAns(MulticastSetupCommand):
    """Device → server answer carrying the package identity (§4.1, Table 3).

    Payload: ``PackageIdentifier`` (1) ``| PackageVersion`` (1).
    For this package ``PackageIdentifier = 2`` and ``PackageVersion = 2``.
    """

    PAYLOAD_SIZE: ClassVar[int | None] = 2

    package_identifier: int = PACKAGE_IDENTIFIER
    package_version: int = PACKAGE_VERSION
    cid: McCID = McCID.PACKAGE_VERSION

    def encode_payload(self) -> bytes:
        _check_uint("package_identifier", self.package_identifier, 8)
        _check_uint("package_version", self.package_version, 8)
        return bytes([self.package_identifier, self.package_version])

    @staticmethod
    def decode_payload(data: bytes) -> PackageVersionAns:
        if len(data) != 2:
            raise ValueError(f"PackageVersionAns needs 2 bytes, got {len(data)}")
        return PackageVersionAns(package_identifier=data[0], package_version=data[1])


# ---------------------------------------------------------------------------
# McGroupStatus (CID 0x01) — TS005-2.0.0 §4.2, Tables 4-7
# ---------------------------------------------------------------------------

@dataclass
class McGroupStatusReq(MulticastSetupCommand):
    """Server → device request for the status of some multicast groups (§4.2).

    Payload: a single ``CmdMask`` octet (Table 5)::

        bits 7:4 = RFU
        bits 3:0 = ReqGroupMask

    ``ReqGroupMask[n] = 1`` means the *n*-th multicast group SHOULD be included
    in the answer; ``0`` means it SHALL NOT be included.
    """

    PAYLOAD_SIZE: ClassVar[int | None] = 1

    req_group_mask: int = 0
    cid: McCID = McCID.MC_GROUP_STATUS

    def encode_payload(self) -> bytes:
        _check_uint("req_group_mask", self.req_group_mask, 4)
        return bytes([self.req_group_mask & 0x0F])

    @staticmethod
    def decode_payload(data: bytes) -> McGroupStatusReq:
        if len(data) != 1:
            raise ValueError(f"McGroupStatusReq needs 1 byte, got {len(data)}")
        return McGroupStatusReq(req_group_mask=data[0] & 0x0F)


@dataclass
class McGroupStatusEntry:
    """One ``[McGroupID + McAddr]`` record of ``McGroupStatusAns`` (§4.2).

    5 octets: 1 octet ``McGroupID`` followed by the 4-octet little-endian
    ``McAddr``.  The upper 6 bits of the ``McGroupID`` octet are unspecified;
    they are transmitted as 0 and ignored on reception.
    """

    group_id: int = 0
    mc_addr: int = 0

    def encode(self) -> bytes:
        _check_group_id(self.group_id)
        _check_uint("mc_addr", self.mc_addr, 32)
        return bytes([self.group_id & 0x03]) + self.mc_addr.to_bytes(4, "little")

    @staticmethod
    def decode(data: bytes) -> McGroupStatusEntry:
        if len(data) != 5:
            raise ValueError(f"McGroupStatusEntry needs 5 bytes, got {len(data)}")
        return McGroupStatusEntry(
            group_id=data[0] & 0x03,
            mc_addr=int.from_bytes(data[1:5], "little"),
        )


@dataclass
class McGroupStatusAns(MulticastSetupCommand):
    """Device → server multicast group report (§4.2, Tables 6 and 7).

    **Variable length**: ``1 + 5 * NbItems`` octets, where
    ``NbItems = popcount(AnsGroupMask)``.

    ``Status`` octet (Table 7)::

        bit  7   = RFU
        bits 6:4 = NbTotalGroups   (groups currently defined, valid range 0..4)
        bits 3:0 = AnsGroupMask    (groups actually listed below)

    If the full requested list does not fit in the available payload, the
    device SHALL discard groups starting with the highest ``McGroupID`` until
    the answer fits, so ``AnsGroupMask`` may differ from ``ReqGroupMask``.
    """

    PAYLOAD_SIZE: ClassVar[int | None] = None

    nb_total_groups: int = 0
    ans_group_mask: int = 0
    groups: list[McGroupStatusEntry] = field(default_factory=list)
    cid: McCID = McCID.MC_GROUP_STATUS

    def encode_payload(self) -> bytes:
        if not 0 <= self.nb_total_groups <= MAX_MULTICAST_GROUPS:
            raise ValueError(
                f"nb_total_groups must be 0..{MAX_MULTICAST_GROUPS} (TS005 §4.2), "
                f"got {self.nb_total_groups}"
            )
        _check_uint("ans_group_mask", self.ans_group_mask, 4)
        expected = bin(self.ans_group_mask & 0x0F).count("1")
        if len(self.groups) != expected:
            raise ValueError(
                f"McGroupStatusAns carries {len(self.groups)} records but "
                f"ans_group_mask=0x{self.ans_group_mask:X} announces {expected}"
            )
        status = ((self.nb_total_groups & 0x07) << 4) | (self.ans_group_mask & 0x0F)
        buf = bytearray([status])
        for entry in self.groups:
            buf.extend(entry.encode())
        return bytes(buf)

    @staticmethod
    def payload_size_from_status(status: int) -> int:
        """Total payload size implied by a ``Status`` octet: ``1 + 5*NbItems``."""
        return 1 + 5 * bin(status & 0x0F).count("1")

    @staticmethod
    def decode_payload(data: bytes) -> McGroupStatusAns:
        if len(data) < 1:
            raise ValueError("McGroupStatusAns needs at least 1 byte, got 0")
        status = data[0]
        expected = McGroupStatusAns.payload_size_from_status(status)
        if len(data) != expected:
            raise ValueError(
                f"McGroupStatusAns with status 0x{status:02X} needs {expected} bytes, "
                f"got {len(data)}"
            )
        groups = [
            McGroupStatusEntry.decode(data[1 + 5 * i:6 + 5 * i])
            for i in range((len(data) - 1) // 5)
        ]
        return McGroupStatusAns(
            nb_total_groups=(status >> 4) & 0x07,
            ans_group_mask=status & 0x0F,
            groups=groups,
        )


# ---------------------------------------------------------------------------
# McGroupSetup (CID 0x02) — TS005-2.0.0 §4.3, Tables 8-11
# ---------------------------------------------------------------------------

@dataclass
class McGroupSetupReq(MulticastSetupCommand):
    """Server → device creation/modification of a multicast group (§4.3, Table 8).

    Payload, **29 octets**::

        offset 0  size 1  : McGroupIDHeader (bits 7:2 RFU, bits 1:0 McGroupID)
        offset 1  size 4  : McAddr          (little endian)
        offset 5  size 16 : McKey_encrypted
        offset 21 size 4  : minMcFCnt       (little endian)
        offset 25 size 4  : maxMcFCnt       (little endian)

    ``McAddr`` has the same format as a LoRaWAN ``DevAddr``.  The device accepts
    a multicast downlink on this address only while
    ``minMcFCnt <= McFCnt <= maxMcFCnt`` (32-bit comparison).
    ``McKey_encrypted`` is ``aes128_decrypt(McKEKey, McKey)``; the device
    recovers ``McKey = aes128_encrypt(McKEKey, McKey_encrypted)``.
    """

    PAYLOAD_SIZE: ClassVar[int | None] = 29

    group_id: int = 0
    mc_addr: int = 0
    mc_key_encrypted: bytes = b"\x00" * 16
    min_fcnt: int = 0
    max_fcnt: int = 0
    cid: McCID = McCID.MC_GROUP_SETUP

    def encode_payload(self) -> bytes:
        _check_group_id(self.group_id)
        _check_uint("mc_addr", self.mc_addr, 32)
        if len(self.mc_key_encrypted) != 16:
            raise ValueError(
                f"mc_key_encrypted must be 16 bytes, got {len(self.mc_key_encrypted)}"
            )
        _check_uint("min_fcnt", self.min_fcnt, 32)
        _check_uint("max_fcnt", self.max_fcnt, 32)
        return (
            bytes([self.group_id & 0x03])
            + self.mc_addr.to_bytes(4, "little")
            + bytes(self.mc_key_encrypted)
            + self.min_fcnt.to_bytes(4, "little")
            + self.max_fcnt.to_bytes(4, "little")
        )

    @staticmethod
    def decode_payload(data: bytes) -> McGroupSetupReq:
        if len(data) != 29:
            raise ValueError(f"McGroupSetupReq needs 29 bytes, got {len(data)}")
        return McGroupSetupReq(
            group_id=data[0] & 0x03,
            mc_addr=int.from_bytes(data[1:5], "little"),
            mc_key_encrypted=bytes(data[5:21]),
            min_fcnt=int.from_bytes(data[21:25], "little"),
            max_fcnt=int.from_bytes(data[25:29], "little"),
        )


@dataclass
class McGroupSetupAns(MulticastSetupCommand):
    """Device → server acknowledgement of ``McGroupSetupReq`` (§4.3, Table 11).

    Payload: a single ``McGroupIDHeader`` octet::

        bits 7:3 = RFU
        bit  2   = IDerror
        bits 1:0 = McGroupID

    ``IDerror = 1`` means the device does not support the multicast context
    indexed by that ``McGroupID`` (e.g. a single-group device asked to create
    ``McGroupID = 1``).
    """

    PAYLOAD_SIZE: ClassVar[int | None] = 1

    group_id: int = 0
    id_error: bool = False
    cid: McCID = McCID.MC_GROUP_SETUP

    def encode_payload(self) -> bytes:
        # An ID error is precisely the case where the group id is unsupported,
        # so the 2-bit field is only range-checked, never the 0..3 group range.
        _check_uint("group_id", self.group_id, 2)
        return bytes([(int(self.id_error) << 2) | (self.group_id & 0x03)])

    @staticmethod
    def decode_payload(data: bytes) -> McGroupSetupAns:
        if len(data) != 1:
            raise ValueError(f"McGroupSetupAns needs 1 byte, got {len(data)}")
        return McGroupSetupAns(group_id=data[0] & 0x03, id_error=bool(data[0] & 0x04))


# ---------------------------------------------------------------------------
# McGroupDelete (CID 0x03) — TS005-2.0.0 §4.4, Tables 12-15
# ---------------------------------------------------------------------------

@dataclass
class McGroupDeleteReq(MulticastSetupCommand):
    """Server → device deletion of a multicast group (§4.4).

    Payload: a single octet, ``bits 7:2 = RFU``, ``bits 1:0 = McGroupID``.
    """

    PAYLOAD_SIZE: ClassVar[int | None] = 1

    group_id: int = 0
    cid: McCID = McCID.MC_GROUP_DELETE

    def encode_payload(self) -> bytes:
        _check_group_id(self.group_id)
        return bytes([self.group_id & 0x03])

    @staticmethod
    def decode_payload(data: bytes) -> McGroupDeleteReq:
        if len(data) != 1:
            raise ValueError(f"McGroupDeleteReq needs 1 byte, got {len(data)}")
        return McGroupDeleteReq(group_id=data[0] & 0x03)


@dataclass
class McGroupDeleteAns(MulticastSetupCommand):
    """Device → server acknowledgement of ``McGroupDeleteReq`` (§4.4).

    Payload: a single octet::

        bits 7:3 = RFU
        bit  2   = McGroupUndefined
        bits 1:0 = McGroupID

    ``McGroupUndefined = 1`` means no group with that ``McGroupID`` was defined
    in the device, so nothing was deleted.
    """

    PAYLOAD_SIZE: ClassVar[int | None] = 1

    group_id: int = 0
    mc_group_undefined: bool = False
    cid: McCID = McCID.MC_GROUP_DELETE

    def encode_payload(self) -> bytes:
        _check_uint("group_id", self.group_id, 2)
        return bytes([(int(self.mc_group_undefined) << 2) | (self.group_id & 0x03)])

    @staticmethod
    def decode_payload(data: bytes) -> McGroupDeleteAns:
        if len(data) != 1:
            raise ValueError(f"McGroupDeleteAns needs 1 byte, got {len(data)}")
        return McGroupDeleteAns(
            group_id=data[0] & 0x03,
            mc_group_undefined=bool(data[0] & 0x04),
        )


# ---------------------------------------------------------------------------
# Session answers (shared layout) — TS005-2.0.0 §4.5/§4.6, Tables 20 and 25
# ---------------------------------------------------------------------------

@dataclass
class _SessionAns(MulticastSetupCommand):
    """Common implementation of ``McClassC/BSessionAns`` (Tables 19/20, 24/25).

    ``Status&McGroupID`` octet::

        bits 7:6 = Reserved for future errors (SHALL be sent as 0, treated as
                   errors when set — note this is the opposite of the usual
                   "silently ignore RFU" rule)
        bit  5   = StartMissed      (SessionTime is in the past)
        bit  4   = McGroupUndefined (group never created, or deleted)
        bit  3   = FreqError        (DLFreq not usable by the device)
        bit  2   = DRError          (data rate not defined)
        bits 1:0 = McGroupID

    ``TimeToStart`` (3 octets, little endian) is present **only if bits 2
    through 7 are all 0**, i.e. only when the session was accepted, making the
    answer either 1 or 4 octets long.  It counts seconds from this uplink to
    the start of the multicast session, letting the server (which timestamps
    uplinks) verify the device's clock synchronisation.  Intervals longer than
    ``2**24 - 1`` seconds SHALL be reported as
    :data:`TIME_TO_START_UNSYNCHRONIZED` (``0xFFFFFF``), which caps scheduling
    at roughly 194 days.
    """

    PAYLOAD_SIZE: ClassVar[int | None] = None

    group_id: int = 0
    mc_group_undefined: bool = False
    freq_error: bool = False
    dr_error: bool = False
    start_missed: bool = False
    time_to_start: int | None = None
    reserved_errors: int = 0
    cid: McCID = McCID.MC_CLASS_C_SESSION

    @property
    def status_byte(self) -> int:
        """The encoded ``Status&McGroupID`` octet."""
        _check_uint("group_id", self.group_id, 2)
        _check_uint("reserved_errors", self.reserved_errors, 2)
        return (
            ((self.reserved_errors & 0x03) << 6)
            | (int(self.start_missed) << 5)
            | (int(self.mc_group_undefined) << 4)
            | (int(self.freq_error) << 3)
            | (int(self.dr_error) << 2)
            | (self.group_id & 0x03)
        )

    @property
    def has_error(self) -> bool:
        """True when any of status bits 2..7 is set, i.e. the request failed."""
        return (self.status_byte & 0xFC) != 0

    def encode_payload(self) -> bytes:
        status = self.status_byte
        if self.has_error:
            if self.time_to_start is not None:
                raise ValueError(
                    "time_to_start SHALL be omitted when any error bit (status "
                    "bits 2..7) is set (TS005 §4.5)"
                )
            return bytes([status])
        if self.time_to_start is None:
            raise ValueError(
                "time_to_start is mandatory when no error bit is set (TS005 §4.5)"
            )
        _check_uint("time_to_start", self.time_to_start, 24)
        return bytes([status]) + self.time_to_start.to_bytes(3, "little")

    @staticmethod
    def payload_size_from_status(status: int) -> int:
        """Payload size implied by a status octet: 1 on error, else 4."""
        return 1 if (status & 0xFC) else 4

    @classmethod
    def _decode(cls, data: bytes, name: str) -> "_SessionAns":
        if len(data) < 1:
            raise ValueError(f"{name} needs at least 1 byte, got 0")
        status = data[0]
        expected = _SessionAns.payload_size_from_status(status)
        if len(data) != expected:
            raise ValueError(
                f"{name} with status 0x{status:02X} needs {expected} bytes, got {len(data)}"
            )
        time_to_start = int.from_bytes(data[1:4], "little") if expected == 4 else None
        return cls(
            group_id=status & 0x03,
            dr_error=bool(status & 0x04),
            freq_error=bool(status & 0x08),
            mc_group_undefined=bool(status & 0x10),
            start_missed=bool(status & 0x20),
            reserved_errors=(status >> 6) & 0x03,
            time_to_start=time_to_start,
        )


# ---------------------------------------------------------------------------
# McClassCSession (CID 0x04) — TS005-2.0.0 §4.5, Tables 17-20
# ---------------------------------------------------------------------------

@dataclass
class McClassCSessionReq(MulticastSetupCommand):
    """Server → device scheduling of a multicast Class C session (§4.5, Table 17).

    Payload, **10 octets**::

        offset 0 size 1 : McGroupIDHeader (bits 7:2 RFU, bits 1:0 McGroupID)
        offset 1 size 4 : SessionTime     (little endian)
        offset 5 size 1 : SessionTimeOut  (bits 7:4 RFU, bits 3:0 TimeOut)
        offset 6 size 3 : DLFreq          (little endian, units of 100 Hz)
        offset 9 size 1 : DR

    ``SessionTime`` is the start of the Class C window in seconds since the GPS
    epoch (00:00:00, Sunday 6 January 1980) modulo ``2**32`` — the same format
    as the ``Time`` field of the Class B beacon.

    ``TimeOut`` is the *maximum* session length, ``2**TimeOut`` **seconds**
    (1 s .. 32768 s); the application MAY revert to Class A earlier.
    """

    PAYLOAD_SIZE: ClassVar[int | None] = 10

    group_id: int = 0
    session_time: int = 0
    session_timeout: int = 0
    dl_frequency: int = 0
    data_rate: int = 0
    cid: McCID = McCID.MC_CLASS_C_SESSION

    @property
    def timeout_seconds(self) -> int:
        """Maximum session duration in seconds: ``2**TimeOut`` (§4.5)."""
        return 1 << self.session_timeout

    def encode_payload(self) -> bytes:
        _check_group_id(self.group_id)
        _check_uint("session_time", self.session_time, 32)
        _check_uint("session_timeout", self.session_timeout, 4)
        _check_uint("data_rate", self.data_rate, 8)
        return (
            bytes([self.group_id & 0x03])
            + self.session_time.to_bytes(4, "little")
            + bytes([self.session_timeout & 0x0F])
            + _encode_dl_frequency(self.dl_frequency)
            + bytes([self.data_rate & 0xFF])
        )

    @staticmethod
    def decode_payload(data: bytes) -> McClassCSessionReq:
        if len(data) != 10:
            raise ValueError(f"McClassCSessionReq needs 10 bytes, got {len(data)}")
        return McClassCSessionReq(
            group_id=data[0] & 0x03,
            session_time=int.from_bytes(data[1:5], "little"),
            session_timeout=data[5] & 0x0F,
            dl_frequency=int.from_bytes(data[6:9], "little") * 100,
            data_rate=data[9],
        )


@dataclass
class McClassCSessionAns(_SessionAns):
    """Device → server acknowledgement of ``McClassCSessionReq`` (§4.5, Table 19).

    1 or 4 octets; see :class:`_SessionAns` for the status layout and the
    conditional ``TimeToStart`` field.
    """

    cid: McCID = McCID.MC_CLASS_C_SESSION

    @staticmethod
    def decode_payload(data: bytes) -> McClassCSessionAns:
        decoded = McClassCSessionAns._decode(data, "McClassCSessionAns")
        assert isinstance(decoded, McClassCSessionAns)
        return decoded


# ---------------------------------------------------------------------------
# McClassBSession (CID 0x05) — TS005-2.0.0 §4.6, Tables 21-25
# ---------------------------------------------------------------------------

@dataclass
class McClassBSessionReq(MulticastSetupCommand):
    """Server → device scheduling of a multicast Class B session (§4.6, Table 21).

    Payload, **10 octets**::

        offset 0 size 1 : McGroupIDHeader    (bits 7:2 RFU, bits 1:0 McGroupID)
        offset 1 size 4 : SessionTime        (little endian, multiple of 128)
        offset 5 size 1 : TimeOutPeriodicity (bit 7 RFU, bits 6:4 Periodicity,
                                              bits 3:0 TimeOut)
        offset 6 size 3 : DLFreq             (little endian, units of 100 Hz)
        offset 9 size 1 : DR

    Differences from the Class C request (§4.6):

    - ``SessionTime`` SHALL be an integer multiple of 128 (one beacon period).
      The spec does not say what a device does otherwise; this encoder does not
      enforce it, so a simulated server can deliberately send a bad value.
    - ``TimeOut`` is expressed in **BeaconPeriods of 128 s**, so the maximum
      duration is ``128 * 2**TimeOut`` seconds (``TimeOut = 8`` is ≈9.1 hours).
    - ``Periodicity`` uses the ``PingSlotInfoReq`` encoding of TS001.
    - ``DLFreq = 0`` selects the region's default Class B frequency-hopping
      beacon scheme.
    """

    PAYLOAD_SIZE: ClassVar[int | None] = 10

    group_id: int = 0
    session_time: int = 0
    periodicity: int = 0
    session_timeout: int = 0
    dl_frequency: int = 0
    data_rate: int = 0
    cid: McCID = McCID.MC_CLASS_B_SESSION

    @property
    def timeout_seconds(self) -> int:
        """Maximum session duration in seconds: ``128 * 2**TimeOut`` (§4.6)."""
        return 128 << self.session_timeout

    @property
    def ping_nb(self) -> int:
        """Ping slots opened per 128 s beacon period: ``2**(7 - Periodicity)``.

        ``PingSlotInfoReq`` encoding (TS001 Class B): ``Periodicity = 0`` gives
        128 slots per beacon period (one per second), ``Periodicity = 7`` gives
        a single slot per beacon period.
        """
        return 128 >> self.periodicity

    @property
    def ping_period_seconds(self) -> int:
        """Seconds between two ping slots: ``2**Periodicity`` (128 / ``ping_nb``)."""
        return 1 << self.periodicity

    @property
    def ping_period_slots(self) -> int:
        """Ping period in 30 ms slot units: ``4096 / ping_nb = 2**(5 + Periodicity)``."""
        return 4096 // self.ping_nb

    def encode_payload(self) -> bytes:
        _check_group_id(self.group_id)
        _check_uint("session_time", self.session_time, 32)
        _check_uint("periodicity", self.periodicity, 3)
        _check_uint("session_timeout", self.session_timeout, 4)
        _check_uint("data_rate", self.data_rate, 8)
        timeout_periodicity = ((self.periodicity & 0x07) << 4) | (self.session_timeout & 0x0F)
        return (
            bytes([self.group_id & 0x03])
            + self.session_time.to_bytes(4, "little")
            + bytes([timeout_periodicity])
            + _encode_dl_frequency(self.dl_frequency)
            + bytes([self.data_rate & 0xFF])
        )

    @staticmethod
    def decode_payload(data: bytes) -> McClassBSessionReq:
        if len(data) != 10:
            raise ValueError(f"McClassBSessionReq needs 10 bytes, got {len(data)}")
        return McClassBSessionReq(
            group_id=data[0] & 0x03,
            session_time=int.from_bytes(data[1:5], "little"),
            periodicity=(data[5] >> 4) & 0x07,
            session_timeout=data[5] & 0x0F,
            dl_frequency=int.from_bytes(data[6:9], "little") * 100,
            data_rate=data[9],
        )


@dataclass
class McClassBSessionAns(_SessionAns):
    """Device → server acknowledgement of ``McClassBSessionReq`` (§4.6, Table 24).

    Structurally and semantically identical to :class:`McClassCSessionAns`.
    """

    cid: McCID = McCID.MC_CLASS_B_SESSION

    @staticmethod
    def decode_payload(data: bytes) -> McClassBSessionAns:
        decoded = McClassBSessionAns._decode(data, "McClassBSessionAns")
        assert isinstance(decoded, McClassBSessionAns)
        return decoded


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

MulticastSetupCommandType = (
    PackageVersionReq | PackageVersionAns |
    McGroupStatusReq | McGroupStatusAns |
    McGroupSetupReq | McGroupSetupAns |
    McGroupDeleteReq | McGroupDeleteAns |
    McClassCSessionReq | McClassCSessionAns |
    McClassBSessionReq | McClassBSessionAns
)


#: Fixed downlink (server → device) payload sizes, in octets (§3, Table 2).
_DOWNLINK_PAYLOAD_SIZES: dict[McCID, int] = {
    McCID.PACKAGE_VERSION: 0,
    McCID.MC_GROUP_STATUS: 1,
    McCID.MC_GROUP_SETUP: 29,
    McCID.MC_GROUP_DELETE: 1,
    McCID.MC_CLASS_C_SESSION: 10,
    McCID.MC_CLASS_B_SESSION: 10,
}

#: Fixed uplink (device → server) payload sizes.  ``McGroupStatusAns`` and the
#: two session answers are variable length and therefore absent from this map.
_UPLINK_FIXED_PAYLOAD_SIZES: dict[McCID, int] = {
    McCID.PACKAGE_VERSION: 2,
    McCID.MC_GROUP_SETUP: 1,
    McCID.MC_GROUP_DELETE: 1,
}


def parse_downlink_commands(data: bytes) -> list[MulticastSetupCommandType]:
    """Parse a server → device FRMPayload received on the package FPort (§3).

    Several commands may be concatenated as ``CID | payload | …``; they are
    returned in wire order.  Parsing stops (with a warning) on an unknown CID
    or a truncated payload, since the remaining octets can no longer be framed.
    """
    commands: list[MulticastSetupCommandType] = []
    pos = 0

    while pos < len(data):
        cid_byte = data[pos]
        try:
            cid = McCID(cid_byte)
        except ValueError:
            logger.warning(
                f"TS005 downlink: unknown command id 0x{cid_byte:02X} at offset {pos}, "
                f"dropping the remaining {len(data) - pos} byte(s)"
            )
            break

        size = _DOWNLINK_PAYLOAD_SIZES[cid]
        payload = data[pos + 1:pos + 1 + size]
        if len(payload) < size:
            logger.warning(
                f"TS005 downlink: truncated {cid.name} at offset {pos} "
                f"({len(payload)}/{size} payload bytes), dropping the rest"
            )
            break

        match cid:
            case McCID.PACKAGE_VERSION:
                commands.append(PackageVersionReq())
            case McCID.MC_GROUP_STATUS:
                commands.append(McGroupStatusReq.decode_payload(payload))
            case McCID.MC_GROUP_SETUP:
                commands.append(McGroupSetupReq.decode_payload(payload))
            case McCID.MC_GROUP_DELETE:
                commands.append(McGroupDeleteReq.decode_payload(payload))
            case McCID.MC_CLASS_C_SESSION:
                commands.append(McClassCSessionReq.decode_payload(payload))
            case McCID.MC_CLASS_B_SESSION:
                commands.append(McClassBSessionReq.decode_payload(payload))

        pos += 1 + size

    return commands


def parse_uplink_commands(data: bytes) -> list[MulticastSetupCommandType]:
    """Parse a device → server FRMPayload received on the package FPort (§3).

    Three answers are variable length and their size is derived from the first
    payload octet:

    - ``McGroupStatusAns``: ``1 + 5 * popcount(AnsGroupMask)`` (§4.2);
    - ``McClassCSessionAns`` / ``McClassBSessionAns``: 4 octets when status bits
      2..7 are all 0, otherwise 1 octet (§4.5, §4.6).

    Parsing stops (with a warning) on an unknown CID or a truncated payload.
    """
    commands: list[MulticastSetupCommandType] = []
    pos = 0

    while pos < len(data):
        cid_byte = data[pos]
        try:
            cid = McCID(cid_byte)
        except ValueError:
            logger.warning(
                f"TS005 uplink: unknown command id 0x{cid_byte:02X} at offset {pos}, "
                f"dropping the remaining {len(data) - pos} byte(s)"
            )
            break

        rest = data[pos + 1:]
        size = _UPLINK_FIXED_PAYLOAD_SIZES.get(cid)
        if size is None:
            # Variable length: the status octet determines the payload size.
            if len(rest) < 1:
                logger.warning(
                    f"TS005 uplink: truncated {cid.name} at offset {pos} "
                    f"(no status byte), dropping the rest"
                )
                break
            if cid is McCID.MC_GROUP_STATUS:
                size = McGroupStatusAns.payload_size_from_status(rest[0])
            else:
                size = _SessionAns.payload_size_from_status(rest[0])

        payload = rest[:size]
        if len(payload) < size:
            logger.warning(
                f"TS005 uplink: truncated {cid.name} at offset {pos} "
                f"({len(payload)}/{size} payload bytes), dropping the rest"
            )
            break

        match cid:
            case McCID.PACKAGE_VERSION:
                commands.append(PackageVersionAns.decode_payload(payload))
            case McCID.MC_GROUP_STATUS:
                commands.append(McGroupStatusAns.decode_payload(payload))
            case McCID.MC_GROUP_SETUP:
                commands.append(McGroupSetupAns.decode_payload(payload))
            case McCID.MC_GROUP_DELETE:
                commands.append(McGroupDeleteAns.decode_payload(payload))
            case McCID.MC_CLASS_C_SESSION:
                commands.append(McClassCSessionAns.decode_payload(payload))
            case McCID.MC_CLASS_B_SESSION:
                commands.append(McClassBSessionAns.decode_payload(payload))

        pos += 1 + size

    return commands


def encode_commands(commands: list[MulticastSetupCommand]) -> bytes:
    """Concatenate TS005 commands into a single FRMPayload (§3).

    Commands are executed by the receiver first to last, so the caller controls
    ordering.  No payload-size check against the region's ``MaxAppPl`` is done
    here; that belongs to the sender.
    """
    buf = bytearray()
    for cmd in commands:
        buf.extend(cmd.encode())
    return bytes(buf)


# ===========================================================================
# Application classes — TS005-2.0.0 §3 "Remote Multicast Setup" package
# ===========================================================================
#
# Everything above this banner is pure wire framing. What follows is the
# stateful half of the package: the device-side state machine that answers the
# commands, and the network-server-side orchestrator that issues them.
#
# Simulator conventions used here:
#
# - ``sim.current_time()`` **is** the GPS time base. The simulation starts at
#   GPS second 0, so a TS005 ``SessionTime`` maps straight onto a simulation
#   timestamp and no epoch conversion is ever done. Both classes take an
#   injectable ``time_provider`` so unit tests can pin "now" without a run.
# - TS005 commands are always unicast (§3), so answers go out with no random
#   spreading delay — unlike TS004, where a whole multicast group would answer
#   at once.
# ---------------------------------------------------------------------------


#: Lowest downlink frequency the EU868 band plan allows, in hertz. Used to
#: validate ``DLFreq`` and raise ``FreqError`` (TS005 §4.5).
EU868_MIN_DL_FREQUENCY = 863_000_000

#: Highest downlink frequency the EU868 band plan allows, in hertz.
EU868_MAX_DL_FREQUENCY = 870_000_000


def _dl_frequency_supported(dl_frequency: int) -> bool:
    """Whether a ``DLFreq`` value is usable by an EU868 device (TS005 §4.5/§4.6).

    ``DLFreq = 0`` is always accepted: for Class C it means "keep the group's
    own channel" and for Class B it selects the region's default beacon
    frequency-hopping scheme (§4.6).
    """
    if dl_frequency == 0:
        return True
    return EU868_MIN_DL_FREQUENCY <= dl_frequency <= EU868_MAX_DL_FREQUENCY


@dataclass
class MulticastSessionRequest:
    """One ``McClassB/CSessionReq`` as seen and answered by a device.

    Recorded in :attr:`MulticastSetupDeviceApplication.session_history` so tests
    and metric collectors can inspect what the device was asked to do and what
    it replied, without reaching into the device itself.

    :ivar mode: ``OperatingMode.CLASS_C`` or ``OperatingMode.CLASS_B``.
    :ivar mc_addr: The group's ``McAddr``, or None when the group was undefined.
    :ivar session_time: The ``SessionTime`` actually used, i.e. after the Class B
        128 s alignment of §4.6 has been applied.
    :ivar requested_session_time: The ``SessionTime`` exactly as received.
    :ivar started: True when the device actually scheduled the session.
    """

    mode: OperatingMode
    group_id: int
    mc_addr: int | None
    session_time: int
    requested_session_time: int
    timeout_seconds: int
    data_rate: int
    frequency: int | None
    ping_periodicity: int | None
    received_at: int
    answer: _SessionAns
    started: bool = False


class MulticastSetupDeviceApplication(FuotaDeviceApplication):
    """Device-side TS005 Remote Multicast Setup package (FPort 200).

    Parses every command of a downlink payload in order, executes it, and sends
    **one** concatenated uplink carrying all the answers (§3: "several commands
    may be concatenated in a single payload; they are executed first to last").
    Because TS005 traffic is unicast the answer goes out with no delay.

    With a :class:`~simulator.lorawan.device.LoRaWanDevice` attached the device
    is the single source of truth for the multicast contexts: groups are created
    with ``join_multicast_group`` and sessions with ``start_class_c_session`` /
    ``start_class_b_session``. Without one the package keeps its own dictionary
    of contexts so the state machine can be unit tested without a simulation.

    Reference: LoRaWAN Remote Multicast Setup TS005-2.0.0 §3 to §4.6.
    """

    def __init__(
        self,
        device: LoRaWanDevice | None = None,
        *,
        mc_ke_key: bytes | None = None,
        gen_app_key: bytes | None = None,
        lorawan_1_1: bool = False,
        rng: random.Random | None = None,
        time_provider: Callable[[], int] | None = None,
        max_uplink_payload: int | None = None,
    ) -> None:
        """
        :param device: The device this package runs on, or None for codec-level use.
        :param mc_ke_key: ``McKEKey`` (16 bytes). When omitted it is derived from
            *gen_app_key* through ``McRootKey`` (TS005 §4.3.1/§4.3.2).
        :param gen_app_key: ``GenAppKey`` (LoRaWAN 1.0.x) or ``AppKey``
            (LoRaWAN 1.1+), used only when *mc_ke_key* is not given.
        :param lorawan_1_1: Select the LoRaWAN 1.1 ``McRootKey`` derivation
            (``0x20`` prefix over ``AppKey``) instead of the 1.0.x one
            (``0x00`` prefix over ``GenAppKey``).
        :param time_provider: Returns the device's current GPS second. Defaults
            to ``int(sim.current_time())``.
        :param max_uplink_payload: FRMPayload budget used when deciding how many
            ``McGroupStatusAns`` records fit (§4.2). Defaults to the DR5 limit.
        """
        super().__init__(device, rng)

        if mc_ke_key is None:
            if gen_app_key is None:
                raise ValueError(
                    "MulticastSetupDeviceApplication needs either mc_ke_key or "
                    "gen_app_key to recover McKey (TS005 §4.3)"
                )
            mc_ke_key = derive_mc_ke_key(
                mc_root_key=derive_mc_root_key(key=gen_app_key, lorawan_1_1=lorawan_1_1)
            )
        if len(mc_ke_key) != 16:
            raise ValueError(f"mc_ke_key must be 16 bytes, got {len(mc_ke_key)}")

        self.mc_ke_key = mc_ke_key
        self.lorawan_1_1 = lorawan_1_1
        self._time_provider = (
            time_provider if time_provider is not None else lambda: int(sim.current_time())
        )
        self.max_uplink_payload = (
            max_uplink_payload if max_uplink_payload is not None else max_frm_payload(5)
        )

        #: Multicast contexts held when no device is attached, keyed by ``McGroupID``.
        self._groups: dict[int, MulticastGroup] = {}
        #: Every session request received, oldest first (tests and metrics).
        self.session_history: list[MulticastSessionRequest] = []
        #: Number of ``PackageVersionReq`` commands answered.
        self.package_version_requests = 0

    def port(self) -> int:
        return MULTICAST_SETUP_FPORT

    # ---- Multicast contexts ----

    @property
    def max_groups(self) -> int:
        """Number of multicast contexts this device supports (``McGroupID`` range)."""
        if self.device is not None:
            return min(self.device.max_multicast_groups, MAX_MULTICAST_GROUPS)
        return MAX_MULTICAST_GROUPS

    @property
    def groups(self) -> dict[int, MulticastGroup]:
        """The multicast contexts currently defined, keyed by ``McGroupID`` (§4.2)."""
        if self.device is not None:
            return {g.group_id: g for g in self.device.multicast_groups.values()}
        return dict(self._groups)

    def get_group(self, group_id: int) -> MulticastGroup | None:
        """The multicast context stored under an ``McGroupID``, or None."""
        if self.device is not None:
            return self.device.get_multicast_group_by_id(group_id)
        return self._groups.get(group_id)

    # ---- Downlink handling ----

    async def on_downlink(self, payload: bytes) -> None:
        """Execute every command in a TS005 downlink and answer them in one uplink (§3)."""
        commands = parse_downlink_commands(payload)
        if not commands:
            return

        answers: list[MulticastSetupCommand] = []
        used = 0
        for command in commands:
            answer = await self._handle_command(command, budget=self.max_uplink_payload - used)
            if answer is None:
                continue
            answers.append(answer)
            used += answer.encoded_size

        if not answers:
            return

        encoded = encode_commands(answers)
        logger.info(
            f"{sim.current_time():.2f}s  MC-SETUP  answering "
            f"{len(commands)} command(s) with {len(answers)} answer(s), {len(encoded)} bytes"
        )
        # TS005 commands are unicast (§3), so there is no need to spread the answers of a
        # whole group in time the way TS004's BlockAckDelay does.
        await self.queue_uplink(encoded)

    async def _handle_command(
        self, command: MulticastSetupCommandType, *, budget: int,
    ) -> MulticastSetupCommand | None:
        match command:
            case PackageVersionReq():
                self.package_version_requests += 1
                return PackageVersionAns(
                    package_identifier=PACKAGE_IDENTIFIER, package_version=PACKAGE_VERSION,
                )
            case McGroupStatusReq():
                return self._handle_group_status(command, budget=budget)
            case McGroupSetupReq():
                return self._handle_group_setup(command)
            case McGroupDeleteReq():
                return self._handle_group_delete(command)
            case McClassCSessionReq():
                return await self._handle_class_c_session(command)
            case McClassBSessionReq():
                return await self._handle_class_b_session(command)
            case _:
                logger.warning(
                    f"{sim.current_time():.2f}s  MC-SETUP  ignoring unexpected "
                    f"{type(command).__name__} on the device side"
                )
                return None

    def _handle_group_status(self, req: McGroupStatusReq, *, budget: int) -> McGroupStatusAns:
        """Report the requested multicast contexts (TS005 §4.2).

        Records are emitted in ascending ``McGroupID``; when they do not all fit
        in the remaining payload budget the device drops them "starting with the
        highest ``McGroupID``" and clears their bits in ``AnsGroupMask``.
        """
        groups = self.groups
        selected = sorted(
            group_id for group_id in groups if req.req_group_mask & (1 << group_id)
        )

        # 1 CID octet + 1 Status octet, then 5 octets per record.
        room = max(0, (budget - 2) // 5)
        if len(selected) > room:
            dropped = selected[room:]
            selected = selected[:room]
            logger.debug(
                f"{sim.current_time():.2f}s  MC-SETUP  McGroupStatusAns does not fit, "
                f"dropping McGroupID(s) {dropped}"
            )

        mask = 0
        entries: list[McGroupStatusEntry] = []
        for group_id in selected:
            mask |= 1 << group_id
            entries.append(
                McGroupStatusEntry(group_id=group_id, mc_addr=groups[group_id].group_addr)
            )

        return McGroupStatusAns(
            nb_total_groups=len(groups), ans_group_mask=mask, groups=entries,
        )

    def _handle_group_setup(self, req: McGroupSetupReq) -> McGroupSetupAns:
        """Create or replace a multicast context (TS005 §4.3).

        ``McKey`` is recovered with ``aes128_encrypt(McKEKey, McKey_encrypted)``
        (§4.3.3 — the inversion is deliberate so devices only need AES encrypt),
        then ``McAppSKey``/``McNwkSKey`` are derived from it and ``McAddr``.
        """
        if req.group_id >= self.max_groups:
            logger.warning(
                f"{sim.current_time():.2f}s  MC-SETUP  McGroupSetupReq for unsupported "
                f"McGroupID={req.group_id} (device supports {self.max_groups})"
            )
            return McGroupSetupAns(group_id=req.group_id, id_error=True)

        mc_key = decrypt_mc_key(
            mc_ke_key=self.mc_ke_key, mc_key_encrypted=req.mc_key_encrypted,
        )
        material = derive_multicast_key_material(mc_key=mc_key, mc_addr=req.mc_addr)

        try:
            group = MulticastGroup(
                group_addr=req.mc_addr,
                nwk_s_key=material.mc_nwk_s_key,
                app_s_key=material.mc_app_s_key,
                group_id=req.group_id,
                min_fcnt=req.min_fcnt,
                max_fcnt=req.max_fcnt,
            )
        except AssertionError:
            # A counter window with minMcFCnt > maxMcFCnt is unusable. TS005 has no
            # dedicated status bit for it, so it is reported as an ID error.
            logger.warning(
                f"{sim.current_time():.2f}s  MC-SETUP  McGroupSetupReq for McGroupID="
                f"{req.group_id} has an invalid counter window "
                f"[{req.min_fcnt}, {req.max_fcnt}]"
            )
            return McGroupSetupAns(group_id=req.group_id, id_error=True)

        if self.device is not None:
            joined = self.device.join_multicast_group(group)
        else:
            # Re-using an McGroupID or an McAddr replaces the previous context, which is
            # exactly what LoRaWanDevice.join_multicast_group does.
            for stored_id, stored in list(self._groups.items()):
                if stored.group_addr == group.group_addr and stored_id != req.group_id:
                    del self._groups[stored_id]
            self._groups[req.group_id] = group
            joined = True

        if not joined:
            logger.warning(
                f"{sim.current_time():.2f}s  MC-SETUP  device refused multicast group "
                f"0x{req.mc_addr:08X} (McGroupID={req.group_id})"
            )
            return McGroupSetupAns(group_id=req.group_id, id_error=True)

        logger.info(
            f"{sim.current_time():.2f}s  MC-SETUP  multicast group 0x{req.mc_addr:08X} "
            f"set up as McGroupID={req.group_id}, "
            f"McFCnt window [{req.min_fcnt}, {req.max_fcnt}]"
        )
        return McGroupSetupAns(group_id=req.group_id, id_error=False)

    def _handle_group_delete(self, req: McGroupDeleteReq) -> McGroupDeleteAns:
        """Delete a multicast context and any session scheduled on it (TS005 §4.4)."""
        group = self.get_group(req.group_id)
        if group is None:
            logger.warning(
                f"{sim.current_time():.2f}s  MC-SETUP  McGroupDeleteReq for undefined "
                f"McGroupID={req.group_id}"
            )
            return McGroupDeleteAns(group_id=req.group_id, mc_group_undefined=True)

        if self.device is not None:
            self.device.leave_multicast_group(group.group_addr)
        else:
            self._groups.pop(req.group_id, None)

        logger.info(
            f"{sim.current_time():.2f}s  MC-SETUP  multicast group 0x{group.group_addr:08X} "
            f"(McGroupID={req.group_id}) deleted"
        )
        return McGroupDeleteAns(group_id=req.group_id, mc_group_undefined=False)

    # ---- Sessions ----

    def _validate_session(
        self, group_id: int, data_rate: int, dl_frequency: int,
    ) -> tuple[MulticastGroup | None, bool, bool, bool]:
        """Check a session request's group, data rate and frequency.

        :returns: ``(group, mc_group_undefined, dr_error, freq_error)``.
        """
        group = self.get_group(group_id)
        return (
            group,
            group is None,
            data_rate not in EU868_DATA_RATES,
            not _dl_frequency_supported(dl_frequency),
        )

    def _session_frequency(self, group: MulticastGroup | None, dl_frequency: int) -> int | None:
        """Resolve ``DLFreq = 0`` to the group's channel, or the region's RX2 default."""
        if dl_frequency != 0:
            return dl_frequency
        if group is not None and group.frequency is not None:
            return group.frequency
        return RX2_DEFAULT_FREQUENCY

    async def _handle_class_c_session(self, req: McClassCSessionReq) -> McClassCSessionAns:
        """Schedule a multicast Class C session (TS005 §4.5)."""
        group, undefined, dr_error, freq_error = self._validate_session(
            req.group_id, req.data_rate, req.dl_frequency,
        )
        now = self._time_provider()
        start_missed = req.session_time < now

        answer = McClassCSessionAns(
            group_id=req.group_id,
            mc_group_undefined=undefined,
            dr_error=dr_error,
            freq_error=freq_error,
            start_missed=start_missed,
        )
        frequency = self._session_frequency(group, req.dl_frequency)
        started = await self._apply_session(
            answer=answer,
            group=group,
            mode=OperatingMode.CLASS_C,
            session_time=req.session_time,
            timeout_seconds=req.timeout_seconds,
            data_rate=req.data_rate,
            frequency=frequency,
            ping_periodicity=None,
            now=now,
        )
        self.session_history.append(MulticastSessionRequest(
            mode=OperatingMode.CLASS_C,
            group_id=req.group_id,
            mc_addr=group.group_addr if group is not None else None,
            session_time=req.session_time,
            requested_session_time=req.session_time,
            timeout_seconds=req.timeout_seconds,
            data_rate=req.data_rate,
            frequency=frequency,
            ping_periodicity=None,
            received_at=now,
            answer=answer,
            started=started,
        ))
        return answer

    async def _handle_class_b_session(self, req: McClassBSessionReq) -> McClassBSessionAns:
        """Schedule a multicast Class B session (TS005 §4.6).

        ``SessionTime`` SHALL be an integer multiple of 128 s (one beacon
        period). The specification does not say what a device does with a
        misaligned value; this implementation **rounds down to the preceding
        beacon boundary** and logs a warning, so a misaligned request still
        lines up with a beacon instead of being silently dropped. Rounding down
        can push the start into the past, in which case ``StartMissed`` is
        raised exactly as it would be for any other past session.
        """
        group, undefined, dr_error, freq_error = self._validate_session(
            req.group_id, req.data_rate, req.dl_frequency,
        )
        now = self._time_provider()

        session_time = req.session_time
        if session_time % BEACON_INTERVAL != 0:
            aligned = session_time - (session_time % BEACON_INTERVAL)
            logger.warning(
                f"{sim.current_time():.2f}s  MC-SETUP  McClassBSessionReq SessionTime "
                f"{session_time} is not a multiple of {BEACON_INTERVAL} s (TS005 §4.6), "
                f"rounding down to {aligned}"
            )
            session_time = aligned

        start_missed = session_time < now
        answer = McClassBSessionAns(
            group_id=req.group_id,
            mc_group_undefined=undefined,
            dr_error=dr_error,
            freq_error=freq_error,
            start_missed=start_missed,
        )
        frequency = self._session_frequency(group, req.dl_frequency)
        started = await self._apply_session(
            answer=answer,
            group=group,
            mode=OperatingMode.CLASS_B,
            session_time=session_time,
            timeout_seconds=req.timeout_seconds,
            data_rate=req.data_rate,
            frequency=frequency,
            ping_periodicity=req.periodicity,
            now=now,
        )
        self.session_history.append(MulticastSessionRequest(
            mode=OperatingMode.CLASS_B,
            group_id=req.group_id,
            mc_addr=group.group_addr if group is not None else None,
            session_time=session_time,
            requested_session_time=req.session_time,
            timeout_seconds=req.timeout_seconds,
            data_rate=req.data_rate,
            frequency=frequency,
            ping_periodicity=req.periodicity,
            received_at=now,
            answer=answer,
            started=started,
        ))
        return answer

    async def _apply_session(
        self,
        *,
        answer: _SessionAns,
        group: MulticastGroup | None,
        mode: OperatingMode,
        session_time: int,
        timeout_seconds: int,
        data_rate: int,
        frequency: int | None,
        ping_periodicity: int | None,
        now: int,
    ) -> bool:
        """Fill in ``TimeToStart`` and hand an accepted session to the device.

        ``TimeToStart`` is only present when status bits 2..7 are all clear
        (§4.5); it counts the seconds from this answer's uplink to the start of
        the session and saturates at :data:`TIME_TO_START_UNSYNCHRONIZED`
        (``0xFFFFFF``), which tells the Application Server the device's clock is
        out of synchronisation.

        :returns: True when the session was actually scheduled on the device.
        """
        if answer.has_error:
            logger.warning(
                f"{sim.current_time():.2f}s  MC-SETUP  Class {mode.value} session for "
                f"McGroupID={answer.group_id} rejected "
                f"(status 0x{answer.status_byte:02X})"
            )
            return False

        assert group is not None
        answer.time_to_start = min(session_time - now, TIME_TO_START_UNSYNCHRONIZED)

        if self.device is None or not sim.is_running():
            # Codec-level use: the answer is complete, but there is no device (or no
            # running simulation) to hang a background task off.
            return False

        if mode is OperatingMode.CLASS_C:
            await self.device.start_class_c_session(
                group.group_addr,
                start_time=float(session_time),
                timeout_seconds=float(timeout_seconds),
                data_rate=data_rate,
                frequency=frequency,
            )
        else:
            await self.device.start_class_b_session(
                group.group_addr,
                start_time=float(session_time),
                timeout_seconds=float(timeout_seconds),
                data_rate=data_rate,
                frequency=frequency,
                ping_periodicity=ping_periodicity if ping_periodicity is not None else 4,
            )

        logger.info(
            f"{sim.current_time():.2f}s  MC-SETUP  Class {mode.value} session accepted on "
            f"0x{group.group_addr:08X} (McGroupID={answer.group_id}): start at "
            f"{session_time}s, {timeout_seconds}s long, DR{data_rate}, "
            f"TimeToStart={answer.time_to_start}s"
        )
        return True


# ---------------------------------------------------------------------------
# Network-server side
# ---------------------------------------------------------------------------

@dataclass
class ServerMulticastGroup:
    """A multicast group as the network-server package knows it (TS005 §4.3).

    :ivar members: Devices that were sent an ``McGroupSetupReq`` for this group.
    :ivar session_requested: Devices that were sent a session request for it.
    """

    group_id: int
    mc_addr: int
    mc_key: bytes
    mc_app_s_key: bytes
    mc_nwk_s_key: bytes
    min_fcnt: int = 0
    max_fcnt: int = MAX_FCNT
    data_rate: int | None = None
    frequency: int | None = None
    members: set[int] = field(default_factory=set)
    session_requested: set[int] = field(default_factory=set)


@dataclass
class DeviceSetupState:
    """What the server has learned about one device's multicast configuration.

    :ivar groups: ``McGroupID`` -> ``McAddr`` for every group the device
        acknowledged without an ``IDerror``.
    :ivar setup_errors: ``McGroupID``s the device reported ``IDerror`` for.
    :ivar delete_errors: ``McGroupID``s the device reported ``McGroupUndefined``
        for in answer to an ``McGroupDeleteReq``.
    :ivar time_to_start: ``McGroupID`` -> the ``TimeToStart`` of the most recent
        accepted session answer, in seconds.
    :ivar clock_offset: ``McGroupID`` -> the difference, in seconds, between the
        device's ``TimeToStart`` and the interval the server itself expected.
        Non-zero means the device's clock has drifted (§4.5).
    """

    dev_addr: int
    package_identifier: int | None = None
    package_version: int | None = None
    groups: dict[int, int] = field(default_factory=dict)
    setup_errors: set[int] = field(default_factory=set)
    delete_errors: set[int] = field(default_factory=set)
    last_status: McGroupStatusAns | None = None
    class_c_answers: dict[int, McClassCSessionAns] = field(default_factory=dict)
    class_b_answers: dict[int, McClassBSessionAns] = field(default_factory=dict)
    session_answers: list[_SessionAns] = field(default_factory=list)
    time_to_start: dict[int, int] = field(default_factory=dict)
    clock_offset: dict[int, int] = field(default_factory=dict)

    def last_session_answer(self, group_id: int) -> _SessionAns | None:
        """The most recent Class B or Class C answer for a group, whichever came last."""
        for answer in reversed(self.session_answers):
            if answer.group_id == group_id:
                return answer
        return None


class MulticastSetupServerApplication(Application):
    """Network-server side TS005 Remote Multicast Setup package (FPort 200).

    Drives the setup phase of a FUOTA campaign: it creates the group on the
    :class:`~simulator.lorawan.network_server.NetworkServer`, encrypts ``McKey``
    once per device with that device's ``McKEKey``, and queues the commands as
    pending downlinks. Like the TS003 clock-sync server package it never calls
    ``queue_downlink``: the network server polls :meth:`get_downlink` and the
    answers arrive through :meth:`on_uplink`, which keeps the package decoupled
    from the gateway and from the radio layer entirely.

    Several commands are concatenated into one FRMPayload whenever they fit in
    the region's maximum payload for the server's default data rate (§3).

    Reference: LoRaWAN Remote Multicast Setup TS005-2.0.0 §3 to §4.6.
    """

    def __init__(
        self,
        network_server: NetworkServer,
        *,
        key_provider: Callable[[int], bytes] | Mapping[int, bytes],
        lorawan_1_1: bool = False,
        time_provider: Callable[[], int] | None = None,
        max_downlink_payload: int | None = None,
        on_answer: Callable[[int, MulticastSetupCommandType], object] | None = None,
    ) -> None:
        """
        :param network_server: The network server the groups are created on.
        :param key_provider: Maps a ``DevAddr`` to that device's ``McKEKey``,
            either as a callable or as a plain mapping.
        :param lorawan_1_1: Recorded for completeness; the key derivation itself
            happens in whatever produced the ``McKEKey`` values.
        :param time_provider: Returns the current GPS second. Defaults to
            ``int(sim.current_time())``.
        :param max_downlink_payload: FRMPayload budget per downlink. Defaults to
            the region limit for the network server's default data rate.
        :param on_answer: Optional hook invoked as ``on_answer(dev_addr, cmd)``
            for every answer parsed, for orchestrators that drive the next step
            of a FUOTA campaign. May be a coroutine function.
        """
        self.network_server = network_server
        self.lorawan_1_1 = lorawan_1_1
        self._key_provider = key_provider
        self._time_provider = (
            time_provider if time_provider is not None else lambda: int(sim.current_time())
        )
        self.max_downlink_payload = (
            max_downlink_payload
            if max_downlink_payload is not None
            else max_frm_payload(network_server._default_data_rate)
        )
        self.on_answer = on_answer

        self._pending: dict[int, deque[MulticastSetupCommand]] = defaultdict(deque)
        #: Groups created through :meth:`setup_group`, keyed by ``McGroupID``.
        self.groups: dict[int, ServerMulticastGroup] = {}
        #: Per-device view of the multicast configuration, keyed by ``DevAddr``.
        self.device_state: dict[int, DeviceSetupState] = {}
        #: Number of answers parsed, for metrics.
        self.answers_received = 0
        #: Last session request issued per ``McGroupID``, used to check device clocks.
        self._last_session_request_sent: dict[
            int, McClassCSessionReq | McClassBSessionReq
        ] = {}

    def port(self) -> int:
        return MULTICAST_SETUP_FPORT

    # ---- Helpers ----

    def mc_ke_key(self, dev_addr: int) -> bytes:
        """The ``McKEKey`` of a device, from the configured key provider."""
        if callable(self._key_provider):
            key = self._key_provider(dev_addr)
        else:
            key = self._key_provider[dev_addr]
        if len(key) != 16:
            raise ValueError(
                f"McKEKey for 0x{dev_addr:08X} must be 16 bytes, got {len(key)}"
            )
        return key

    def state(self, dev_addr: int) -> DeviceSetupState:
        """The (lazily created) state record of a device."""
        state = self.device_state.get(dev_addr)
        if state is None:
            state = DeviceSetupState(dev_addr=dev_addr)
            self.device_state[dev_addr] = state
        return state

    def _queue(self, dev_addrs: Iterable[int], command: MulticastSetupCommand) -> None:
        for dev_addr in dev_addrs:
            self._pending[dev_addr].append(command)
            self.state(dev_addr)

    # ---- Campaign API ----

    def setup_group(
        self,
        dev_addrs: Sequence[int],
        *,
        group_id: int,
        mc_addr: int,
        mc_key: bytes,
        min_fcnt: int = 0,
        max_fcnt: int = MAX_FCNT,
        data_rate: int | None = None,
        frequency: int | None = None,
    ) -> ServerMulticastGroup:
        """Create a multicast group and queue an ``McGroupSetupReq`` per device (§4.3).

        The group's session keys are derived from *mc_key* and *mc_addr* and the
        group is registered on the network server, so multicast downlinks can be
        scheduled for it straight away. Each device receives its own copy of the
        command because ``McKey_encrypted`` is wrapped with that device's
        ``McKEKey``.

        :returns: The :class:`ServerMulticastGroup` record (also stored in
            :attr:`groups`).
        """
        _check_group_id(group_id)
        if len(mc_key) != 16:
            raise ValueError(f"mc_key must be 16 bytes, got {len(mc_key)}")

        material = derive_multicast_key_material(mc_key=mc_key, mc_addr=mc_addr)
        self.network_server.create_multicast_group(
            mc_addr,
            material.mc_nwk_s_key,
            material.mc_app_s_key,
            group_id=group_id,
            min_fcnt=min_fcnt,
            max_fcnt=max_fcnt,
            data_rate=data_rate,
            frequency=frequency,
        )

        group = ServerMulticastGroup(
            group_id=group_id,
            mc_addr=mc_addr,
            mc_key=mc_key,
            mc_app_s_key=material.mc_app_s_key,
            mc_nwk_s_key=material.mc_nwk_s_key,
            min_fcnt=min_fcnt,
            max_fcnt=max_fcnt,
            data_rate=data_rate,
            frequency=frequency,
            members=set(dev_addrs),
        )
        self.groups[group_id] = group

        for dev_addr in dev_addrs:
            self._pending[dev_addr].append(McGroupSetupReq(
                group_id=group_id,
                mc_addr=mc_addr,
                mc_key_encrypted=encrypt_mc_key(
                    mc_ke_key=self.mc_ke_key(dev_addr), mc_key=mc_key,
                ),
                min_fcnt=min_fcnt,
                max_fcnt=max_fcnt,
            ))
            self.state(dev_addr)

        logger.info(
            f"{sim.current_time():.2f}s  MC-SETUP-NS  group 0x{mc_addr:08X} "
            f"(McGroupID={group_id}) created for {len(group.members)} device(s), "
            f"McFCnt window [{min_fcnt}, {max_fcnt}]"
        )
        return group

    def request_status(self, dev_addrs: Sequence[int], mask: int = 0x0F) -> None:
        """Queue an ``McGroupStatusReq`` with the given ``ReqGroupMask`` (§4.2)."""
        self._queue(dev_addrs, McGroupStatusReq(req_group_mask=mask))
        logger.info(
            f"{sim.current_time():.2f}s  MC-SETUP-NS  McGroupStatusReq "
            f"(mask 0x{mask & 0x0F:X}) queued for {len(dev_addrs)} device(s)"
        )

    def request_package_version(self, dev_addrs: Sequence[int]) -> None:
        """Queue a ``PackageVersionReq`` (§4.1)."""
        self._queue(dev_addrs, PackageVersionReq())

    def delete_group(self, dev_addrs: Sequence[int], group_id: int) -> None:
        """Queue an ``McGroupDeleteReq`` and forget the group's membership (§4.4)."""
        _check_group_id(group_id)
        self._queue(dev_addrs, McGroupDeleteReq(group_id=group_id))
        group = self.groups.get(group_id)
        if group is not None:
            group.members.difference_update(dev_addrs)
            group.session_requested.difference_update(dev_addrs)
        logger.info(
            f"{sim.current_time():.2f}s  MC-SETUP-NS  McGroupDeleteReq for McGroupID="
            f"{group_id} queued for {len(dev_addrs)} device(s)"
        )

    def start_class_c_session(
        self,
        dev_addrs: Sequence[int],
        *,
        group_id: int,
        session_time: int,
        session_timeout: int,
        dl_frequency: int = 0,
        data_rate: int | None = None,
    ) -> McClassCSessionReq:
        """Queue an ``McClassCSessionReq`` for a group (§4.5).

        :param session_time: Start of the session in GPS seconds, i.e. a
            simulation timestamp.
        :param session_timeout: The 4-bit ``TimeOut`` exponent; the session lasts
            at most ``2**session_timeout`` **seconds**.
        :param dl_frequency: ``DLFreq`` in hertz; 0 leaves the choice to the device.
        :param data_rate: Downlink data rate index; defaults to the group's own
            rate, then to the network server's default.
        """
        group = self._require_group(group_id)
        request = McClassCSessionReq(
            group_id=group_id,
            session_time=session_time,
            session_timeout=session_timeout,
            dl_frequency=dl_frequency if dl_frequency else (group.frequency or 0),
            data_rate=self._resolve_data_rate(group, data_rate),
        )
        self._queue(dev_addrs, request)
        self._last_session_request_sent[group_id] = request
        group.session_requested.update(dev_addrs)
        logger.info(
            f"{sim.current_time():.2f}s  MC-SETUP-NS  Class C session on "
            f"0x{group.mc_addr:08X} (McGroupID={group_id}) at {session_time}s for "
            f"{request.timeout_seconds}s queued for {len(dev_addrs)} device(s)"
        )
        return request

    def start_class_b_session(
        self,
        dev_addrs: Sequence[int],
        *,
        group_id: int,
        session_time: int,
        session_timeout: int,
        periodicity: int = 4,
        dl_frequency: int = 0,
        data_rate: int | None = None,
    ) -> McClassBSessionReq:
        """Queue an ``McClassBSessionReq`` for a group (§4.6).

        :param session_time: Start of the session in GPS seconds. TS005 §4.6
            requires a multiple of 128 (one beacon period); a misaligned value is
            passed through unchanged so a simulation can exercise the device's
            handling of it.
        :param session_timeout: The 4-bit ``TimeOut`` exponent; the session lasts
            at most ``128 * 2**session_timeout`` seconds.
        :param periodicity: ``Periodicity`` in the ``PingSlotInfoReq`` encoding,
            so the group opens ``128 >> periodicity`` ping slots per beacon period.
        """
        group = self._require_group(group_id)
        if session_time % BEACON_INTERVAL != 0:
            logger.warning(
                f"{sim.current_time():.2f}s  MC-SETUP-NS  Class B SessionTime "
                f"{session_time} is not a multiple of {BEACON_INTERVAL} s (TS005 §4.6)"
            )
        request = McClassBSessionReq(
            group_id=group_id,
            session_time=session_time,
            periodicity=periodicity,
            session_timeout=session_timeout,
            dl_frequency=dl_frequency if dl_frequency else (group.frequency or 0),
            data_rate=self._resolve_data_rate(group, data_rate),
        )
        self._queue(dev_addrs, request)
        self._last_session_request_sent[group_id] = request
        group.session_requested.update(dev_addrs)
        logger.info(
            f"{sim.current_time():.2f}s  MC-SETUP-NS  Class B session on "
            f"0x{group.mc_addr:08X} (McGroupID={group_id}) at {session_time}s for "
            f"{request.timeout_seconds}s, {request.ping_nb} ping slot(s) per beacon "
            f"period, queued for {len(dev_addrs)} device(s)"
        )
        return request

    def _require_group(self, group_id: int) -> ServerMulticastGroup:
        group = self.groups.get(group_id)
        if group is None:
            raise KeyError(
                f"No multicast group set up under McGroupID={group_id}; call "
                f"setup_group() first"
            )
        return group

    def _resolve_data_rate(self, group: ServerMulticastGroup, data_rate: int | None) -> int:
        if data_rate is not None:
            return data_rate
        if group.data_rate is not None:
            return group.data_rate
        return self.network_server._default_data_rate

    # ---- Application plumbing ----

    async def get_downlink(self, dev_addr: int) -> bytes | None:
        """Hand the network server the next FRMPayload for a device.

        Consecutive queued commands are concatenated while they fit in
        :attr:`max_downlink_payload` (§3). A single command that cannot fit at
        all is dropped with a warning rather than blocking the queue forever.
        """
        queue = self._pending.get(dev_addr)
        if not queue:
            return None

        buf = bytearray()
        while queue and len(buf) + queue[0].encoded_size <= self.max_downlink_payload:
            buf.extend(queue.popleft().encode())

        if not buf:
            dropped = queue.popleft()
            logger.warning(
                f"{sim.current_time():.2f}s  MC-SETUP-NS  dropping "
                f"{type(dropped).__name__} for 0x{dev_addr:08X}: {dropped.encoded_size} "
                f"bytes exceeds the {self.max_downlink_payload} byte FRMPayload limit"
            )
            return None

        logger.debug(
            f"{sim.current_time():.2f}s  MC-SETUP-NS  downlink for 0x{dev_addr:08X}: "
            f"{len(buf)} bytes, {len(queue)} command(s) still queued"
        )
        return bytes(buf)

    async def on_uplink(self, dev_addr: int, payload: bytes) -> None:
        """Record the answers a device sent back (§4.1 to §4.6)."""
        commands = parse_uplink_commands(payload)
        state = self.state(dev_addr)

        for command in commands:
            self.answers_received += 1
            self._record_answer(state, command)
            if self.on_answer is not None:
                result = self.on_answer(dev_addr, command)
                if inspect.isawaitable(result):
                    await result

    def _record_answer(
        self, state: DeviceSetupState, command: MulticastSetupCommandType,
    ) -> None:
        dev_addr = state.dev_addr
        match command:
            case PackageVersionAns():
                state.package_identifier = command.package_identifier
                state.package_version = command.package_version
            case McGroupStatusAns():
                state.last_status = command
                for entry in command.groups:
                    state.groups[entry.group_id] = entry.mc_addr
            case McGroupSetupAns():
                if command.id_error:
                    state.setup_errors.add(command.group_id)
                    state.groups.pop(command.group_id, None)
                    logger.warning(
                        f"{sim.current_time():.2f}s  MC-SETUP-NS  0x{dev_addr:08X} "
                        f"rejected McGroupID={command.group_id} (IDerror)"
                    )
                else:
                    state.setup_errors.discard(command.group_id)
                    group = self.groups.get(command.group_id)
                    state.groups[command.group_id] = (
                        group.mc_addr if group is not None else 0
                    )
                    logger.info(
                        f"{sim.current_time():.2f}s  MC-SETUP-NS  0x{dev_addr:08X} "
                        f"joined McGroupID={command.group_id}"
                    )
            case McGroupDeleteAns():
                if command.mc_group_undefined:
                    state.delete_errors.add(command.group_id)
                else:
                    state.delete_errors.discard(command.group_id)
                    state.groups.pop(command.group_id, None)
            case McClassCSessionAns() | McClassBSessionAns():
                self._record_session_answer(state, command)
            case _:
                logger.warning(
                    f"{sim.current_time():.2f}s  MC-SETUP-NS  ignoring unexpected "
                    f"{type(command).__name__} from 0x{dev_addr:08X}"
                )

    def _record_session_answer(self, state: DeviceSetupState, answer: _SessionAns) -> None:
        state.session_answers.append(answer)
        if isinstance(answer, McClassCSessionAns):
            state.class_c_answers[answer.group_id] = answer
        else:
            assert isinstance(answer, McClassBSessionAns)
            state.class_b_answers[answer.group_id] = answer

        if answer.has_error:
            logger.warning(
                f"{sim.current_time():.2f}s  MC-SETUP-NS  0x{state.dev_addr:08X} refused "
                f"the session on McGroupID={answer.group_id} "
                f"(status 0x{answer.status_byte:02X})"
            )
            state.time_to_start.pop(answer.group_id, None)
            return

        assert answer.time_to_start is not None
        state.time_to_start[answer.group_id] = answer.time_to_start

        # The server timestamps the uplink, so comparing its own view of the interval with
        # the device's TimeToStart exposes a drifting device clock (§4.5).
        request = self._last_session_request(answer.group_id)
        if request is not None:
            expected = request.session_time - self._time_provider()
            state.clock_offset[answer.group_id] = answer.time_to_start - expected

        logger.info(
            f"{sim.current_time():.2f}s  MC-SETUP-NS  0x{state.dev_addr:08X} accepted the "
            f"session on McGroupID={answer.group_id}, TimeToStart="
            f"{answer.time_to_start}s"
        )

    def _last_session_request(
        self, group_id: int,
    ) -> McClassCSessionReq | McClassBSessionReq | None:
        """The most recent session request this package queued for a group."""
        return self._last_session_request_sent.get(group_id)

    # ---- Progress helpers for orchestrators ----

    def all_devices_acked_group(self, group_id: int) -> bool:
        """Whether every device the group was set up for acknowledged it (§4.3)."""
        group = self.groups.get(group_id)
        if group is None or not group.members:
            return False
        return all(
            group_id in self.state(dev_addr).groups for dev_addr in group.members
        )

    def devices_in_session(self, group_id: int) -> set[int]:
        """Devices that accepted the latest session request for a group (§4.5/§4.6).

        A device is counted once it has answered without any error bit set, which
        is exactly the condition under which it will switch class at
        ``SessionTime``.
        """
        group = self.groups.get(group_id)
        if group is None:
            return set()
        in_session: set[int] = set()
        for dev_addr in group.session_requested:
            answer = self.state(dev_addr).last_session_answer(group_id)
            if answer is not None and not answer.has_error:
                in_session.add(dev_addr)
        return in_session

    def pending_devices(self, dev_addrs: Iterable[int] | None = None) -> set[int]:
        """Devices that still have TS005 commands waiting to go out.

        :param dev_addrs: Restrict the answer to these devices; by default every
            device the package has ever queued something for is considered.
        """
        candidates = self._pending.keys() if dev_addrs is None else dev_addrs
        return {dev_addr for dev_addr in candidates if self._pending.get(dev_addr)}

    def pending_commands(self, dev_addr: int) -> list[MulticastSetupCommand]:
        """A read-only snapshot of a device's queued commands, in send order."""
        return list(self._pending.get(dev_addr, ()))
