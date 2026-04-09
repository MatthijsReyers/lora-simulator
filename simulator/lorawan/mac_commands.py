"""
LoRaWAN MAC commands (§5).

MAC commands are exchanged between the network server and end-device to manage
network parameters. They can be transported:
  - Piggybacked in the FOpts field of FHDR (max 15 bytes total)
  - In the FRMPayload on FPort 0 (encrypted with NwkSKey, not AppSKey)
Both methods MUST NOT be used simultaneously (§4.3.1).

Each command is identified by a 1-byte CID. Commands sent by the server have
a paired answer sent by the device, sharing the same CID.

Reference: LoRaWAN L2 1.0.4 Specification §5.
"""

from __future__ import annotations
from dataclasses import dataclass
from enum import IntEnum


class CID(IntEnum):
    """MAC command identifiers (§5, Table 4)."""
    LINK_CHECK = 0x02
    LINK_ADR = 0x03
    DUTY_CYCLE = 0x04
    RX_PARAM_SETUP = 0x05
    DEV_STATUS = 0x06
    NEW_CHANNEL = 0x07
    RX_TIMING_SETUP = 0x08


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

@dataclass
class MACCommand:
    """Base class for all MAC commands."""
    cid: CID

    def encode_payload(self) -> bytes:
        """Encode the command payload (without the CID byte)."""
        return b""

    def encode(self) -> bytes:
        """Encode the full command: CID + payload."""
        return bytes([self.cid]) + self.encode_payload()


# ---------------------------------------------------------------------------
# LinkCheck (CID 0x02)
# ---------------------------------------------------------------------------

@dataclass
class LinkCheckReq(MACCommand):
    """
    Sent by device to verify connectivity (§5.1).
    No payload.
    """
    cid: CID = CID.LINK_CHECK


@dataclass
class LinkCheckAns(MACCommand):
    """
    Sent by server in response to LinkCheckReq (§5.1).
    Payload: Margin (1 byte, 0–254 dB) | GwCnt (1 byte).
    """
    margin: int = 0
    gw_cnt: int = 0
    cid: CID = CID.LINK_CHECK

    def encode_payload(self) -> bytes:
        return bytes([self.margin & 0xFF, self.gw_cnt & 0xFF])

    @staticmethod
    def decode_payload(data: bytes) -> LinkCheckAns:
        assert len(data) >= 2, f"LinkCheckAns needs 2 bytes, got {len(data)}"
        return LinkCheckAns(margin=data[0], gw_cnt=data[1])


# ---------------------------------------------------------------------------
# LinkADR (CID 0x03)
# ---------------------------------------------------------------------------

@dataclass
class LinkADRReq(MACCommand):
    """
    Sent by server to set data rate, TX power, and channel mask (§5.2).
    Payload: DataRate_TXPower (1) | ChMask (2) | Redundancy (1) = 4 bytes.
    """
    data_rate: int = 0       # 4 bits (0–15), 0xF = keep current
    tx_power: int = 0        # 4 bits (0–15), 0xF = keep current
    ch_mask: int = 0         # 16-bit channel mask
    ch_mask_cntl: int = 0    # 3 bits
    nb_trans: int = 1        # 4 bits (1–15), number of retransmissions
    cid: CID = CID.LINK_ADR

    def encode_payload(self) -> bytes:
        dr_txpow = ((self.data_rate & 0x0F) << 4) | (self.tx_power & 0x0F)
        redundancy = ((self.ch_mask_cntl & 0x07) << 4) | (self.nb_trans & 0x0F)
        return bytes([dr_txpow]) + self.ch_mask.to_bytes(2, "little") + bytes([redundancy])

    @staticmethod
    def decode_payload(data: bytes) -> LinkADRReq:
        assert len(data) >= 4, f"LinkADRReq needs 4 bytes, got {len(data)}"
        data_rate = (data[0] >> 4) & 0x0F
        tx_power = data[0] & 0x0F
        ch_mask = int.from_bytes(data[1:3], "little")
        ch_mask_cntl = (data[3] >> 4) & 0x07
        nb_trans = data[3] & 0x0F
        return LinkADRReq(
            data_rate=data_rate, tx_power=tx_power,
            ch_mask=ch_mask, ch_mask_cntl=ch_mask_cntl, nb_trans=nb_trans,
        )


@dataclass
class LinkADRAns(MACCommand):
    """
    Sent by device to acknowledge LinkADRReq (§5.2).
    Payload: Status (1 byte): bit2=ChannelMaskACK, bit1=DataRateACK, bit0=PowerACK.
    """
    channel_mask_ack: bool = True
    data_rate_ack: bool = True
    power_ack: bool = True
    cid: CID = CID.LINK_ADR

    def encode_payload(self) -> bytes:
        status = (
            (int(self.channel_mask_ack) << 2) |
            (int(self.data_rate_ack) << 1) |
            int(self.power_ack)
        )
        return bytes([status])

    @staticmethod
    def decode_payload(data: bytes) -> LinkADRAns:
        assert len(data) >= 1, f"LinkADRAns needs 1 byte, got {len(data)}"
        return LinkADRAns(
            channel_mask_ack=bool(data[0] & 0x04),
            data_rate_ack=bool(data[0] & 0x02),
            power_ack=bool(data[0] & 0x01),
        )


# ---------------------------------------------------------------------------
# DutyCycle (CID 0x04)
# ---------------------------------------------------------------------------

@dataclass
class DutyCycleReq(MACCommand):
    """
    Sent by server to set max aggregated duty cycle (§5.3).
    Payload: DutyCyclePL (1 byte): MaxDCycle[3:0].
    Aggregated duty cycle = 1 / 2^MaxDCycle (0 = no limit).
    """
    max_duty_cycle: int = 0  # 4 bits
    cid: CID = CID.DUTY_CYCLE

    def encode_payload(self) -> bytes:
        return bytes([self.max_duty_cycle & 0x0F])

    @staticmethod
    def decode_payload(data: bytes) -> DutyCycleReq:
        assert len(data) >= 1, f"DutyCycleReq needs 1 byte, got {len(data)}"
        return DutyCycleReq(max_duty_cycle=data[0] & 0x0F)


@dataclass
class DutyCycleAns(MACCommand):
    """
    Sent by device to acknowledge DutyCycleReq (§5.3).
    No payload.
    """
    cid: CID = CID.DUTY_CYCLE


# ---------------------------------------------------------------------------
# RXParamSetup (CID 0x05)
# ---------------------------------------------------------------------------

@dataclass
class RXParamSetupReq(MACCommand):
    """
    Sent by server to configure RX2 window parameters (§5.4).
    Payload: DLSettings (1 byte): RX1DRoffset[6:4] | RX2DataRate[3:0].
    Note: Frequency field (3 bytes) is also defined in the spec but we omit
    it here since we use single-frequency operation.
    """
    rx1_dr_offset: int = 0   # 3 bits
    rx2_data_rate: int = 0   # 4 bits
    cid: CID = CID.RX_PARAM_SETUP

    def encode_payload(self) -> bytes:
        dl_settings = ((self.rx1_dr_offset & 0x07) << 4) | (self.rx2_data_rate & 0x0F)
        return bytes([dl_settings])

    @staticmethod
    def decode_payload(data: bytes) -> RXParamSetupReq:
        assert len(data) >= 1, f"RXParamSetupReq needs 1 byte, got {len(data)}"
        return RXParamSetupReq(
            rx1_dr_offset=(data[0] >> 4) & 0x07,
            rx2_data_rate=data[0] & 0x0F,
        )


@dataclass
class RXParamSetupAns(MACCommand):
    """
    Sent by device to acknowledge RXParamSetupReq (§5.4).
    Payload: Status (1 byte): bit2=RX1DRoffsetACK, bit1=RX2DataRateACK, bit0=ChannelACK.
    """
    rx1_dr_offset_ack: bool = True
    rx2_data_rate_ack: bool = True
    channel_ack: bool = True
    cid: CID = CID.RX_PARAM_SETUP

    def encode_payload(self) -> bytes:
        status = (
            (int(self.rx1_dr_offset_ack) << 2) |
            (int(self.rx2_data_rate_ack) << 1) |
            int(self.channel_ack)
        )
        return bytes([status])

    @staticmethod
    def decode_payload(data: bytes) -> RXParamSetupAns:
        assert len(data) >= 1, f"RXParamSetupAns needs 1 byte, got {len(data)}"
        return RXParamSetupAns(
            rx1_dr_offset_ack=bool(data[0] & 0x04),
            rx2_data_rate_ack=bool(data[0] & 0x02),
            channel_ack=bool(data[0] & 0x01),
        )


# ---------------------------------------------------------------------------
# DevStatus (CID 0x06)
# ---------------------------------------------------------------------------

@dataclass
class DevStatusReq(MACCommand):
    """
    Sent by server to request device status (§5.5).
    No payload.
    """
    cid: CID = CID.DEV_STATUS


@dataclass
class DevStatusAns(MACCommand):
    """
    Sent by device in response to DevStatusReq (§5.5).
    Payload: Battery (1 byte) | Margin (1 byte, signed 6-bit: -32..31).

    Battery: 0 = external power, 1–254 = level, 255 = unknown.
    Margin: demodulation SNR margin in dB for last received DevStatusReq.
    """
    battery: int = 255       # 0=external, 1-254=level, 255=unknown
    margin: int = 0          # signed 6-bit (-32 to 31)
    cid: CID = CID.DEV_STATUS

    def encode_payload(self) -> bytes:
        # Margin is a signed 6-bit value stored in bits [5:0]
        margin_byte = self.margin & 0x3F
        return bytes([self.battery & 0xFF, margin_byte])

    @staticmethod
    def decode_payload(data: bytes) -> DevStatusAns:
        assert len(data) >= 2, f"DevStatusAns needs 2 bytes, got {len(data)}"
        battery = data[0]
        raw_margin = data[1] & 0x3F
        # Sign-extend 6-bit value
        margin = raw_margin if raw_margin < 32 else raw_margin - 64
        return DevStatusAns(battery=battery, margin=margin)


# ---------------------------------------------------------------------------
# NewChannel (CID 0x07)
# ---------------------------------------------------------------------------

@dataclass
class NewChannelReq(MACCommand):
    """
    Sent by server to create or modify a channel (§5.6).
    Payload: ChIndex (1) | Freq (3) | DrRange (1) = 5 bytes.
    """
    ch_index: int = 0        # channel index
    frequency: int = 0       # frequency in Hz (stored as freq/100 in 24 bits)
    min_dr: int = 0          # 4 bits
    max_dr: int = 0          # 4 bits
    cid: CID = CID.NEW_CHANNEL

    def encode_payload(self) -> bytes:
        freq_raw = self.frequency // 100
        dr_range = ((self.max_dr & 0x0F) << 4) | (self.min_dr & 0x0F)
        return (
            bytes([self.ch_index & 0xFF]) +
            freq_raw.to_bytes(3, "little") +
            bytes([dr_range])
        )

    @staticmethod
    def decode_payload(data: bytes) -> NewChannelReq:
        assert len(data) >= 5, f"NewChannelReq needs 5 bytes, got {len(data)}"
        ch_index = data[0]
        frequency = int.from_bytes(data[1:4], "little") * 100
        max_dr = (data[4] >> 4) & 0x0F
        min_dr = data[4] & 0x0F
        return NewChannelReq(
            ch_index=ch_index, frequency=frequency,
            min_dr=min_dr, max_dr=max_dr,
        )


@dataclass
class NewChannelAns(MACCommand):
    """
    Sent by device to acknowledge NewChannelReq (§5.6).
    Payload: Status (1 byte): bit1=DataRateRangeOK, bit0=ChannelFreqOK.
    """
    data_rate_range_ok: bool = True
    channel_freq_ok: bool = True
    cid: CID = CID.NEW_CHANNEL

    def encode_payload(self) -> bytes:
        status = (int(self.data_rate_range_ok) << 1) | int(self.channel_freq_ok)
        return bytes([status])

    @staticmethod
    def decode_payload(data: bytes) -> NewChannelAns:
        assert len(data) >= 1, f"NewChannelAns needs 1 byte, got {len(data)}"
        return NewChannelAns(
            data_rate_range_ok=bool(data[0] & 0x02),
            channel_freq_ok=bool(data[0] & 0x01),
        )


# ---------------------------------------------------------------------------
# RXTimingSetup (CID 0x08)
# ---------------------------------------------------------------------------

@dataclass
class RXTimingSetupReq(MACCommand):
    """
    Sent by server to set the delay between TX and RX1 (§5.7).
    Payload: Settings (1 byte): Del[3:0]. A value of 0 maps to 1 second.
    """
    delay: int = 1  # 1–15 seconds (0 in wire format maps to 1)
    cid: CID = CID.RX_TIMING_SETUP

    def encode_payload(self) -> bytes:
        # Wire value 0 maps to 1 second per spec
        wire_val = 0 if self.delay <= 1 else (self.delay & 0x0F)
        return bytes([wire_val])

    @staticmethod
    def decode_payload(data: bytes) -> RXTimingSetupReq:
        assert len(data) >= 1, f"RXTimingSetupReq needs 1 byte, got {len(data)}"
        wire_val = data[0] & 0x0F
        delay = 1 if wire_val == 0 else wire_val
        return RXTimingSetupReq(delay=delay)


@dataclass
class RXTimingSetupAns(MACCommand):
    """
    Sent by device to acknowledge RXTimingSetupReq (§5.7).
    No payload.
    """
    cid: CID = CID.RX_TIMING_SETUP


# ---------------------------------------------------------------------------
# Payload sizes (for parsing)
# ---------------------------------------------------------------------------

# Payload sizes for server→device (downlink) commands
_DOWNLINK_PAYLOAD_SIZES: dict[CID, int] = {
    CID.LINK_CHECK: 2,      # LinkCheckAns
    CID.LINK_ADR: 4,        # LinkADRReq
    CID.DUTY_CYCLE: 1,      # DutyCycleReq
    CID.RX_PARAM_SETUP: 1,  # RXParamSetupReq
    CID.DEV_STATUS: 0,      # DevStatusReq
    CID.NEW_CHANNEL: 5,     # NewChannelReq
    CID.RX_TIMING_SETUP: 1, # RXTimingSetupReq
}

# Payload sizes for device→server (uplink) commands
_UPLINK_PAYLOAD_SIZES: dict[CID, int] = {
    CID.LINK_CHECK: 0,      # LinkCheckReq
    CID.LINK_ADR: 1,        # LinkADRAns
    CID.DUTY_CYCLE: 0,      # DutyCycleAns
    CID.RX_PARAM_SETUP: 1,  # RXParamSetupAns
    CID.DEV_STATUS: 2,      # DevStatusAns
    CID.NEW_CHANNEL: 1,     # NewChannelAns
    CID.RX_TIMING_SETUP: 0, # RXTimingSetupAns
}


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

# Type alias for convenience
MACCommandType = (
    LinkCheckReq | LinkCheckAns |
    LinkADRReq | LinkADRAns |
    DutyCycleReq | DutyCycleAns |
    RXParamSetupReq | RXParamSetupAns |
    DevStatusReq | DevStatusAns |
    NewChannelReq | NewChannelAns |
    RXTimingSetupReq | RXTimingSetupAns
)


def parse_downlink_commands(data: bytes) -> list[MACCommandType]:
    """
    Parse MAC commands received by the device (from the server).

    These are found in:
      - FHDR.FOpts of downlink frames
      - FRMPayload of downlink frames with FPort = 0 (decrypted with NwkSKey)
    """
    commands: list[MACCommandType] = []
    pos = 0

    while pos < len(data):
        cid_byte = data[pos]
        try:
            cid = CID(cid_byte)
        except ValueError:
            break  # Unknown CID, stop parsing

        payload_size = _DOWNLINK_PAYLOAD_SIZES.get(cid)
        if payload_size is None:
            break

        pos += 1
        payload = data[pos:pos + payload_size]
        if len(payload) < payload_size:
            break

        match cid:
            case CID.LINK_CHECK:
                commands.append(LinkCheckAns.decode_payload(payload))
            case CID.LINK_ADR:
                commands.append(LinkADRReq.decode_payload(payload))
            case CID.DUTY_CYCLE:
                commands.append(DutyCycleReq.decode_payload(payload))
            case CID.RX_PARAM_SETUP:
                commands.append(RXParamSetupReq.decode_payload(payload))
            case CID.DEV_STATUS:
                commands.append(DevStatusReq())
            case CID.NEW_CHANNEL:
                commands.append(NewChannelReq.decode_payload(payload))
            case CID.RX_TIMING_SETUP:
                commands.append(RXTimingSetupReq.decode_payload(payload))

        pos += payload_size

    return commands


def parse_uplink_commands(data: bytes) -> list[MACCommandType]:
    """
    Parse MAC commands sent by the device (to the server).

    These are found in:
      - FHDR.FOpts of uplink frames
      - FRMPayload of uplink frames with FPort = 0 (decrypted with NwkSKey)
    """
    commands: list[MACCommandType] = []
    pos = 0

    while pos < len(data):
        cid_byte = data[pos]
        try:
            cid = CID(cid_byte)
        except ValueError:
            break

        payload_size = _UPLINK_PAYLOAD_SIZES.get(cid)
        if payload_size is None:
            break

        pos += 1
        payload = data[pos:pos + payload_size]
        if len(payload) < payload_size:
            break

        match cid:
            case CID.LINK_CHECK:
                commands.append(LinkCheckReq())
            case CID.LINK_ADR:
                commands.append(LinkADRAns.decode_payload(payload))
            case CID.DUTY_CYCLE:
                commands.append(DutyCycleAns())
            case CID.RX_PARAM_SETUP:
                commands.append(RXParamSetupAns.decode_payload(payload))
            case CID.DEV_STATUS:
                commands.append(DevStatusAns.decode_payload(payload))
            case CID.NEW_CHANNEL:
                commands.append(NewChannelAns.decode_payload(payload))
            case CID.RX_TIMING_SETUP:
                commands.append(RXTimingSetupAns())

        pos += payload_size

    return commands


def encode_mac_commands(commands: list[MACCommand]) -> bytes:
    """Encode a list of MAC commands into a byte stream."""
    buf = bytearray()
    for cmd in commands:
        buf.extend(cmd.encode())
    return bytes(buf)
