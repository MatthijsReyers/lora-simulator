"""Tests for LoRaWAN frame encoding/decoding."""

from simulator.lorawan.enums.frame_types import MType, Major
from simulator.lorawan.frame import (
    MHDR, FCtrl, FHDR, MACPayload, PHYPayload,
    JoinRequestPayload, JoinAcceptPayload,
)


class TestMHDR:
    def test_encode_decode_roundtrip(self):
        for mtype in MType:
            mhdr = MHDR(mtype=mtype)
            byte = mhdr.encode()
            decoded = MHDR.decode(byte)
            assert decoded.mtype == mtype
            assert decoded.major == Major.LORAWAN_R1

    def test_known_encoding(self):
        # Unconfirmed Data Up: MType=010, RFU=000, Major=00 => 0b01000000 = 0x40
        mhdr = MHDR(mtype=MType.UNCONFIRMED_DATA_UP)
        assert mhdr.encode() == 0x40

    def test_join_request_encoding(self):
        mhdr = MHDR(mtype=MType.JOIN_REQUEST)
        assert mhdr.encode() == 0x00


class TestFCtrl:
    def test_uplink_roundtrip(self):
        fctrl = FCtrl(adr=True, adr_ack_req=True, ack=False, class_b=False, f_opts_len=5)
        byte = fctrl.encode_uplink()
        decoded = FCtrl.decode_uplink(byte)
        assert decoded.adr is True
        assert decoded.adr_ack_req is True
        assert decoded.ack is False
        assert decoded.class_b is False
        assert decoded.f_opts_len == 5

    def test_downlink_roundtrip(self):
        fctrl = FCtrl(adr=False, ack=True, class_b=True, f_opts_len=3)
        byte = fctrl.encode_downlink()
        decoded = FCtrl.decode_downlink(byte)
        assert decoded.adr is False
        assert decoded.ack is True
        assert decoded.class_b is True  # FPending
        assert decoded.f_opts_len == 3


class TestFHDR:
    def test_encode_decode_uplink(self):
        fhdr = FHDR(dev_addr=0x01020304, fctrl=FCtrl(adr=True), fcnt=42, fopts=b"\x02\x03")
        encoded = fhdr.encode(uplink=True)
        decoded, consumed = FHDR.decode(encoded, uplink=True)
        assert decoded.dev_addr == 0x01020304
        assert decoded.fcnt == 42
        assert decoded.fopts == b"\x02\x03"
        assert decoded.fctrl.adr is True
        assert consumed == len(encoded)

    def test_minimal_fhdr(self):
        fhdr = FHDR(dev_addr=0x00000000, fctrl=FCtrl(), fcnt=0)
        encoded = fhdr.encode(uplink=True)
        assert len(encoded) == 7  # 4 + 1 + 2 + 0 FOpts


class TestMACPayload:
    def test_encode_decode_with_payload(self):
        fhdr = FHDR(dev_addr=0xAABBCCDD, fctrl=FCtrl(), fcnt=1)
        mac = MACPayload(fhdr=fhdr, fport=1, frm_payload=b"hello")
        encoded = mac.encode(uplink=True)
        decoded = MACPayload.decode(encoded, uplink=True)
        assert decoded.fhdr.dev_addr == 0xAABBCCDD
        assert decoded.fhdr.fcnt == 1
        assert decoded.fport == 1
        assert decoded.frm_payload == b"hello"

    def test_encode_decode_no_payload(self):
        fhdr = FHDR(dev_addr=0x11223344, fctrl=FCtrl(), fcnt=0)
        mac = MACPayload(fhdr=fhdr)
        encoded = mac.encode(uplink=True)
        decoded = MACPayload.decode(encoded, uplink=True)
        assert decoded.fport is None
        assert decoded.frm_payload == b""

    def test_fport_zero(self):
        """FPort 0 means FRMPayload contains MAC commands encrypted with NwkSKey."""
        fhdr = FHDR(dev_addr=0x00000001, fctrl=FCtrl(), fcnt=5)
        mac = MACPayload(fhdr=fhdr, fport=0, frm_payload=b"\x02\x03")
        encoded = mac.encode(uplink=True)
        decoded = MACPayload.decode(encoded, uplink=True)
        assert decoded.fport == 0
        assert decoded.frm_payload == b"\x02\x03"


class TestPHYPayload:
    def test_data_frame_roundtrip(self):
        fhdr = FHDR(dev_addr=0xDEADBEEF, fctrl=FCtrl(), fcnt=100)
        mac = MACPayload(fhdr=fhdr, fport=10, frm_payload=b"\x01\x02\x03")
        phy = PHYPayload(
            mhdr=MHDR(mtype=MType.UNCONFIRMED_DATA_UP),
            mac_payload=mac,
            mic=b"\xAA\xBB\xCC\xDD",
        )
        encoded = phy.encode()
        decoded = PHYPayload.decode_data(encoded)
        assert decoded.mhdr.mtype == MType.UNCONFIRMED_DATA_UP
        assert decoded.mac_payload is not None
        assert decoded.mac_payload.fhdr.dev_addr == 0xDEADBEEF
        assert decoded.mac_payload.fhdr.fcnt == 100
        assert decoded.mac_payload.fport == 10
        assert decoded.mac_payload.frm_payload == b"\x01\x02\x03"
        assert decoded.mic == b"\xAA\xBB\xCC\xDD"


class TestJoinRequest:
    def test_encode_decode_roundtrip(self):
        app_eui = bytes(range(8))
        dev_eui = bytes(range(8, 16))
        jr = JoinRequestPayload(app_eui=app_eui, dev_eui=dev_eui, dev_nonce=0x1234)
        encoded = jr.encode()
        assert len(encoded) == 18
        decoded = JoinRequestPayload.decode(encoded)
        assert decoded.app_eui == app_eui
        assert decoded.dev_eui == dev_eui
        assert decoded.dev_nonce == 0x1234

    def test_phy_payload_roundtrip(self):
        app_eui = b"\x01\x02\x03\x04\x05\x06\x07\x08"
        dev_eui = b"\x11\x22\x33\x44\x55\x66\x77\x88"
        jr = JoinRequestPayload(app_eui=app_eui, dev_eui=dev_eui, dev_nonce=99)
        phy = PHYPayload(
            mhdr=MHDR(mtype=MType.JOIN_REQUEST),
            join_request=jr,
            mic=b"\x00\x11\x22\x33",
        )
        encoded = phy.encode()
        assert len(encoded) == 23  # 1 + 18 + 4
        decoded = PHYPayload.decode_join_request(encoded)
        assert decoded.join_request is not None
        assert decoded.join_request.app_eui == app_eui
        assert decoded.join_request.dev_nonce == 99
        assert decoded.mic == b"\x00\x11\x22\x33"


class TestJoinAccept:
    def test_encode_decode_roundtrip(self):
        ja = JoinAcceptPayload(
            app_nonce=0x112233, net_id=0x000013,
            dev_addr=0xAABBCCDD, dl_settings=0x00, rx_delay=1,
        )
        encoded = ja.encode()
        assert len(encoded) == 12
        decoded = JoinAcceptPayload.decode(encoded)
        assert decoded.app_nonce == 0x112233
        assert decoded.net_id == 0x000013
        assert decoded.dev_addr == 0xAABBCCDD
        assert decoded.rx_delay == 1

    def test_dl_settings_properties(self):
        ja = JoinAcceptPayload(
            app_nonce=0, net_id=0, dev_addr=0,
            dl_settings=0x53,  # RX1DROffset=5, RX2DR=3
            rx_delay=0,
        )
        assert ja.rx1_dr_offset == 5
        assert ja.rx2_data_rate == 3

    def test_with_cflist(self):
        cf_list = bytes(range(16))
        ja = JoinAcceptPayload(
            app_nonce=0, net_id=0, dev_addr=0, dl_settings=0,
            rx_delay=0, cf_list=cf_list,
        )
        encoded = ja.encode()
        assert len(encoded) == 28
        decoded = JoinAcceptPayload.decode(encoded)
        assert decoded.cf_list == cf_list
