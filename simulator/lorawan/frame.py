from __future__ import annotations
from dataclasses import dataclass
from simulator.lorawan.enums.frame_types import MType, Major


@dataclass
class MHDR:
    """
        MAC header (1 byte): MType (3 bits) | RFU (3 bits) | Major (2 bits).
    """
    mtype: MType
    major: Major = Major.LORAWAN_R1

    def encode(self) -> int:
        return (self.mtype << 5) | (self.major & 0x03)

    @staticmethod
    def decode(byte: int) -> MHDR:
        mtype = MType((byte >> 5) & 0x07)
        major = Major(byte & 0x03)
        return MHDR(mtype=mtype, major=major)


@dataclass
class FCtrl:
    """
        Frame control byte

        Uplink: ADR | ADRACKReq | ACK | ClassB | FOptsLen
        Downlink: ADR | RFU | ACK | FPending | FOptsLen
    """
    adr: bool = False
    adr_ack_req: bool = False  # uplink only
    ack: bool = False
    class_b: bool = False      # uplink: ClassB flag; downlink: FPending
    f_opts_len: int = 0        # 4 bits (0–15)

    def encode_uplink(self) -> int:
        return (
            (int(self.adr) << 7) |
            (int(self.adr_ack_req) << 6) |
            (int(self.ack) << 5) |
            (int(self.class_b) << 4) |
            (self.f_opts_len & 0x0F)
        )

    def encode_downlink(self) -> int:
        return (
            (int(self.adr) << 7) |
            # bit 6 is RFU (0)
            (int(self.ack) << 5) |
            (int(self.class_b) << 4) |  # FPending in downlink context
            (self.f_opts_len & 0x0F)
        )

    @staticmethod
    def decode_uplink(byte: int) -> FCtrl:
        return FCtrl(
            adr=bool(byte & 0x80),
            adr_ack_req=bool(byte & 0x40),
            ack=bool(byte & 0x20),
            class_b=bool(byte & 0x10),
            f_opts_len=byte & 0x0F,
        )

    @staticmethod
    def decode_downlink(byte: int) -> FCtrl:
        return FCtrl(
            adr=bool(byte & 0x80),
            adr_ack_req=False,
            ack=bool(byte & 0x20),
            class_b=bool(byte & 0x10),  # FPending
            f_opts_len=byte & 0x0F,
        )


@dataclass
class FHDR:
    """
        Frame header: DevAddr (4) | FCtrl (1) | FCnt (2) | FOpts (0..15).
    """
    dev_addr: int 
    fctrl: FCtrl
    fcnt: int
    fopts: bytes = b"" # 0–15 bytes of MAC commands

    def encode(self, uplink: bool) -> bytes:
        buf = bytearray()
        buf.extend(self.dev_addr.to_bytes(4, "little"))
        self.fctrl.f_opts_len = len(self.fopts)
        buf.append(self.fctrl.encode_uplink() if uplink else self.fctrl.encode_downlink())
        buf.extend((self.fcnt & 0xFFFF).to_bytes(2, "little"))
        buf.extend(self.fopts)
        return bytes(buf)

    @staticmethod
    def decode(data: bytes, uplink: bool) -> tuple[FHDR, int]:
        """Decode FHDR from bytes. Returns (FHDR, bytes_consumed)."""
        assert len(data) >= 7, "FHDR requires at least 7 bytes"
        dev_addr = int.from_bytes(data[0:4], "little")
        fctrl = FCtrl.decode_uplink(data[4]) if uplink else FCtrl.decode_downlink(data[4])
        fcnt = int.from_bytes(data[5:7], "little")
        fopts_len = fctrl.f_opts_len
        fopts = data[7:7 + fopts_len]
        assert len(fopts) == fopts_len, f"Expected {fopts_len} FOpts bytes, got {len(fopts)}"
        return FHDR(dev_addr=dev_addr, fctrl=fctrl, fcnt=fcnt, fopts=fopts), 7 + fopts_len


@dataclass
class MACPayload:
    """
        MAC payload: FHDR | FPort (optional) | FRMPayload (optional).
    """
    fhdr: FHDR
    fport: int | None = None # 0–255; None means no FPort/FRMPayload
    frm_payload: bytes = b"" # encrypted application or MAC payload

    def encode(self, uplink: bool) -> bytes:
        buf = bytearray(self.fhdr.encode(uplink))
        if self.fport is not None:
            buf.append(self.fport & 0xFF)
            buf.extend(self.frm_payload)
        return bytes(buf)

    @staticmethod
    def decode(data: bytes, uplink: bool) -> MACPayload:
        fhdr, consumed = FHDR.decode(data, uplink)
        remaining = data[consumed:]
        if len(remaining) == 0:
            return MACPayload(fhdr=fhdr)
        fport = remaining[0]
        frm_payload = remaining[1:]
        return MACPayload(fhdr=fhdr, fport=fport, frm_payload=frm_payload)


@dataclass
class JoinRequestPayload:
    """Join Request payload (§6.2.4): AppEUI (8) | DevEUI (8) | DevNonce (2)."""
    app_eui: bytes # 8 bytes (also called JoinEUI in 1.1)
    dev_eui: bytes # 8 bytes
    dev_nonce: int # 16-bit

    def encode(self) -> bytes:
        assert len(self.app_eui) == 8
        assert len(self.dev_eui) == 8
        buf = bytearray()
        buf.extend(self.app_eui[::-1]) # little-endian (LSB first)
        buf.extend(self.dev_eui[::-1])
        buf.extend(self.dev_nonce.to_bytes(2, "little"))
        return bytes(buf)

    @staticmethod
    def decode(data: bytes) -> JoinRequestPayload:
        assert len(data) == 18, f"JoinRequest payload must be 18 bytes, got {len(data)}"
        app_eui = data[0:8][::-1]
        dev_eui = data[8:16][::-1]
        dev_nonce = int.from_bytes(data[16:18], "little")
        return JoinRequestPayload(app_eui=app_eui, dev_eui=dev_eui, dev_nonce=dev_nonce)


@dataclass
class JoinAcceptPayload:
    """
        Join Accept payload (§6.2.5): AppNonce (3) | NetID (3) | DevAddr (4) | DLSettings (1) | RXDelay (1) | CFList (0|16).

        Note: The Join Accept payload is encrypted in the PHYPayload. This class represents the
        decrypted content.
    """
    app_nonce: int     # 24-bit (also called JoinNonce in 1.1)
    net_id: int        # 24-bit
    dev_addr: int      # 32-bit
    dl_settings: int   # 8-bit: RX1DRoffset (3 bits) | RX2DataRate (4 bits)
    rx_delay: int      # 8-bit: delay in seconds (0 maps to 1)
    cf_list: bytes = b""  # optional 16 bytes (channel frequency list)

    @property
    def rx1_dr_offset(self) -> int:
        return (self.dl_settings >> 4) & 0x07

    @property
    def rx2_data_rate(self) -> int:
        return self.dl_settings & 0x0F

    def encode(self) -> bytes:
        buf = bytearray()
        buf.extend(self.app_nonce.to_bytes(3, "little"))
        buf.extend(self.net_id.to_bytes(3, "little"))
        buf.extend(self.dev_addr.to_bytes(4, "little"))
        buf.append(self.dl_settings & 0xFF)
        buf.append(self.rx_delay & 0xFF)
        if self.cf_list:
            assert len(self.cf_list) == 16
            buf.extend(self.cf_list)
        return bytes(buf)

    @staticmethod
    def decode(data: bytes) -> JoinAcceptPayload:
        assert len(data) in (12, 28), f"JoinAccept payload must be 12 or 28 bytes, got {len(data)}"
        app_nonce = int.from_bytes(data[0:3], "little")
        net_id = int.from_bytes(data[3:6], "little")
        dev_addr = int.from_bytes(data[6:10], "little")
        dl_settings = data[10]
        rx_delay = data[11]
        cf_list = data[12:] if len(data) > 12 else b""
        return JoinAcceptPayload(
            app_nonce=app_nonce, net_id=net_id, dev_addr=dev_addr,
            dl_settings=dl_settings, rx_delay=rx_delay, cf_list=cf_list,
        )


@dataclass
class PHYPayload:
    """
        Complete LoRaWAN physical payload.

        For data frames: MHDR (1) | MACPayload | MIC (4)
        For join request: MHDR (1) | JoinRequestPayload (18) | MIC (4)
        For join accept:  MHDR (1) | encrypted(JoinAcceptPayload | MIC) (12 or 28 + 4)
    """
    mhdr: MHDR
    mac_payload: MACPayload | None = None
    join_request: JoinRequestPayload | None = None
    join_accept: JoinAcceptPayload | None = None
    mic: bytes = b"\x00\x00\x00\x00"

    def encode(self) -> bytes:
        """Encode the PHYPayload to bytes (MIC must be set beforehand)."""
        buf = bytearray()
        buf.append(self.mhdr.encode())

        if self.mhdr.mtype == MType.JOIN_REQUEST:
            assert self.join_request is not None
            buf.extend(self.join_request.encode())
        elif self.mhdr.mtype == MType.JOIN_ACCEPT:
            assert self.join_accept is not None
            buf.extend(self.join_accept.encode())
        else:
            assert self.mac_payload is not None
            buf.extend(self.mac_payload.encode(uplink=self.mhdr.mtype.is_uplink))

        buf.extend(self.mic[:4])
        return bytes(buf)

    @staticmethod
    def decode_data(data: bytes) -> PHYPayload:
        """Decode a data frame PHYPayload (not join request/accept)."""
        assert len(data) >= 12, f"Data PHYPayload too short: {len(data)} bytes"
        mhdr = MHDR.decode(data[0])
        mic = data[-4:]
        payload_bytes = data[1:-4]
        mac_payload = MACPayload.decode(payload_bytes, uplink=mhdr.mtype.is_uplink)
        return PHYPayload(mhdr=mhdr, mac_payload=mac_payload, mic=mic)

    @staticmethod
    def decode_join_request(data: bytes) -> PHYPayload:
        """Decode a Join Request PHYPayload."""
        assert len(data) == 23, f"JoinRequest PHYPayload must be 23 bytes, got {len(data)}"
        mhdr = MHDR.decode(data[0])
        assert mhdr.mtype == MType.JOIN_REQUEST
        join_request = JoinRequestPayload.decode(data[1:19])
        mic = data[19:23]
        return PHYPayload(mhdr=mhdr, join_request=join_request, mic=mic)
