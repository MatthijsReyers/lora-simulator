from __future__ import annotations

import logging
import struct
from typing import Callable

from simulator.environment import simulation_env as sim
from simulator.lorawan.application import Application


logger = logging.getLogger(__name__)

# LoRa Alliance TS003 / AppTime package uses FPort 202.
CLOCK_SYNC_FPORT = 202
APP_TIME_REQ_CID = 0x01
APP_TIME_ANS_CID = 0x02


def encode_app_time_req(device_time: int, token: int) -> bytes:
    """Encode a minimal AppTimeReq payload.

    Format (little-endian):
    - CID (1 byte): 0x01
    - DeviceTime (4 bytes, unsigned seconds)
    - Token (1 byte): request identifier echoed by the answer
    """
    assert 0 <= device_time <= 0xFFFFFFFF, "device_time must fit uint32"
    assert 0 <= token <= 0xFF, "token must fit uint8"
    return struct.pack("<BIB", APP_TIME_REQ_CID, device_time, token)


def decode_app_time_req(payload: bytes) -> tuple[int, int] | None:
    """Decode an AppTimeReq payload into (device_time, token)."""
    if len(payload) != 6:
        return None
    cid, device_time, token = struct.unpack("<BIB", payload)
    if cid != APP_TIME_REQ_CID:
        return None
    return device_time, token


def encode_app_time_ans(server_time: int, token: int) -> bytes:
    """Encode a minimal AppTimeAns payload.

    Format (little-endian):
    - CID (1 byte): 0x02
    - ServerTime (4 bytes, unsigned seconds)
    - Token (1 byte): copied from AppTimeReq
    """
    assert 0 <= server_time <= 0xFFFFFFFF, "server_time must fit uint32"
    assert 0 <= token <= 0xFF, "token must fit uint8"
    return struct.pack("<BIB", APP_TIME_ANS_CID, server_time, token)


def decode_app_time_ans(payload: bytes) -> tuple[int, int] | None:
    """Decode an AppTimeAns payload into (server_time, token)."""
    if len(payload) != 6:
        return None
    cid, server_time, token = struct.unpack("<BIB", payload)
    if cid != APP_TIME_ANS_CID:
        return None
    return server_time, token


class ClockSyncApplication(Application):
    """Device-side clock synchronization application (FPort 202).

    Call ``build_time_request()`` to create an uplink payload, then send it on
    FPort 202.  Register this app on the device so that incoming AppTimeAns
    downlinks are routed to ``on_downlink``.
    """

    def __init__(self) -> None:
        self.last_server_time: int | None = None
        self.last_device_time: int | None = None
        self.offset_seconds: int | None = None
        self._last_token: int = 0
        self._pending_token: int | None = None
        self._pending_device_time: int | None = None

    def port(self) -> int:
        return CLOCK_SYNC_FPORT

    async def on_uplink(self, dev_addr: int, payload: bytes) -> None:
        pass

    def build_time_request(self, device_time: int) -> bytes:
        """Build an AppTimeReq and track request state for correlation."""
        self._last_token = (self._last_token + 1) & 0xFF
        self._pending_token = self._last_token
        self._pending_device_time = device_time
        return encode_app_time_req(device_time, self._last_token)

    async def on_downlink(self, payload: bytes) -> None:
        decoded = decode_app_time_ans(payload)
        if decoded is None:
            return

        server_time, token = decoded
        if self._pending_token is None or token != self._pending_token:
            logger.debug(
                f"{sim.current_time():.2f}s  CLOCK SYNC  ignored stale/unknown token={token}"
            )
            return

        assert self._pending_device_time is not None
        self.last_server_time = server_time
        self.last_device_time = self._pending_device_time
        self.offset_seconds = server_time - self._pending_device_time

        logger.info(
            f"{sim.current_time():.2f}s  CLOCK SYNC  synced: "
            f"device={self.last_device_time}s server={self.last_server_time}s "
            f"offset={self.offset_seconds:+d}s"
        )

        self._pending_token = None
        self._pending_device_time = None


class ClockSyncServerApplication(Application):
    """Server-side clock synchronization application (FPort 202).

    Processes AppTimeReq uplinks and provides AppTimeAns responses via the
    ``get_downlink`` mechanism (no direct NetworkServer dependency needed).
    """

    def __init__(
        self,
        time_provider: Callable[[], int] | None = None,
    ) -> None:
        self._time_provider = time_provider if time_provider is not None else lambda: int(sim.current_time())
        self._pending_responses: dict[int, bytes] = {}  # dev_addr -> response
        self.sync_count = 0

    def port(self) -> int:
        return CLOCK_SYNC_FPORT

    async def on_uplink(self, dev_addr: int, payload: bytes) -> None:
        decoded = decode_app_time_req(payload)
        if decoded is None:
            return

        device_time, token = decoded
        server_time = self._time_provider()
        self._pending_responses[dev_addr] = encode_app_time_ans(server_time, token)
        self.sync_count += 1

        logger.info(
            f"{sim.current_time():.2f}s  CLOCK SYNC NS  AppTimeAns ready for "
            f"0x{dev_addr:08X}: device={device_time}s server={server_time}s token={token}"
        )

    async def get_downlink(self, dev_addr: int) -> bytes | None:
        return self._pending_responses.pop(dev_addr, None)
