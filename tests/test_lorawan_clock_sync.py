import pytest

from simulator.lorawan.applications.clock_sync import (
    CLOCK_SYNC_FPORT,
    ClockSyncApplication,
    ClockSyncServerApplication,
    decode_app_time_ans,
    decode_app_time_req,
    encode_app_time_ans,
    encode_app_time_req,
)
from simulator.lorawan.crypto import compute_data_mic, encrypt_frm_payload
from simulator.lorawan.enums.frame_types import MType
from simulator.lorawan.frame import FCtrl, FHDR, MACPayload, MHDR, PHYPayload
from simulator.lorawan.network_server import NetworkServer


DEV_ADDR = 0x26011234
NWK_S_KEY = bytes.fromhex("2B7E151628AED2A6ABF7158809CF4F3C")
APP_S_KEY = bytes.fromhex("3C4F9C098815F7ABA6D2AE281615E72B")


def _build_uplink(dev_addr: int, fcnt: int, fport: int, payload: bytes) -> bytes:
    encrypted = encrypt_frm_payload(
        APP_S_KEY, dev_addr=dev_addr, fcnt=fcnt, uplink=True, payload=payload,
    )
    fhdr = FHDR(dev_addr=dev_addr, fctrl=FCtrl(), fcnt=fcnt)
    mac_payload = MACPayload(fhdr=fhdr, fport=fport, frm_payload=encrypted)
    mhdr = MHDR(mtype=MType.UNCONFIRMED_DATA_UP)
    mhdr_and_payload = bytes([mhdr.encode()]) + mac_payload.encode(uplink=True)
    mic = compute_data_mic(
        NWK_S_KEY,
        dev_addr=dev_addr,
        fcnt=fcnt,
        uplink=True,
        mhdr_and_payload=mhdr_and_payload,
    )
    return PHYPayload(mhdr=mhdr, mac_payload=mac_payload, mic=mic).encode()


class TestClockSyncCodec:
    def test_app_time_req_roundtrip(self):
        payload = encode_app_time_req(device_time=123456, token=7)
        assert decode_app_time_req(payload) == (123456, 7)

    def test_app_time_ans_roundtrip(self):
        payload = encode_app_time_ans(server_time=999, token=42)
        assert decode_app_time_ans(payload) == (999, 42)

    def test_decode_rejects_wrong_length(self):
        assert decode_app_time_req(b"\x01") is None
        assert decode_app_time_ans(b"\x02") is None


class TestClockSyncApplications:
    @pytest.mark.asyncio
    async def test_server_app_queues_response(self):
        ns = NetworkServer()
        ns.register_device(DEV_ADDR, NWK_S_KEY, APP_S_KEY)

        app = ClockSyncServerApplication(time_provider=lambda: 5000)
        ns.register_application(app)
        req = encode_app_time_req(device_time=4900, token=9)

        await app.on_uplink(DEV_ADDR, req)

        # Server app uses get_downlink instead of queue_downlink
        dl = await app.get_downlink(DEV_ADDR)
        assert dl is not None
        assert decode_app_time_ans(dl) == (5000, 9)

    @pytest.mark.asyncio
    async def test_device_app_tracks_offset(self):
        app = ClockSyncApplication()

        req = app.build_time_request(device_time=100)
        decoded_req = decode_app_time_req(req)
        assert decoded_req is not None
        _, token = decoded_req
        ans = encode_app_time_ans(server_time=108, token=token)

        await app.on_downlink(ans)

        assert app.last_device_time == 100
        assert app.last_server_time == 108
        assert app.offset_seconds == 8

    @pytest.mark.asyncio
    async def test_end_to_end_over_network_server(self):
        ns = NetworkServer()
        ns.register_device(DEV_ADDR, NWK_S_KEY, APP_S_KEY)

        server_app = ClockSyncServerApplication(time_provider=lambda: 2000)
        ns.register_application(server_app)

        device_app = ClockSyncApplication()
        req_plain = device_app.build_time_request(device_time=1990)

        uplink = _build_uplink(
            dev_addr=DEV_ADDR,
            fcnt=0,
            fport=CLOCK_SYNC_FPORT,
            payload=req_plain,
        )

        downlink_raw = await ns.handle_uplink(uplink)
        assert downlink_raw is not None

        phy = PHYPayload.decode_data(downlink_raw)
        assert phy.mac_payload is not None
        assert phy.mac_payload.fport == CLOCK_SYNC_FPORT

        down_plain = encrypt_frm_payload(
            APP_S_KEY,
            dev_addr=DEV_ADDR,
            fcnt=0,
            uplink=False,
            payload=phy.mac_payload.frm_payload,
        )

        await device_app.on_downlink(down_plain)
        assert device_app.offset_seconds == 10
        assert server_app.sync_count == 1
