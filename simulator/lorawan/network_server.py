"""
LoRaWAN network server.

Handles uplink processing (MIC verification, decryption, application routing)
and downlink scheduling. In a real deployment, the network server is a cloud
service; here it runs as a simulation component that the gateway forwards frames to.

Reference: LoRaWAN L2 1.0.4 Specification §4, §6.
"""

from __future__ import annotations
import logging
from dataclasses import dataclass
from collections import deque

from simulator.environment import simulation_env as sim
from simulator.lorawan.application import Application
from simulator.lorawan.enums.frame_types import MType
from simulator.lorawan.frame import (
    MHDR, FCtrl, FHDR, MACPayload, PHYPayload,
)
from simulator.lorawan.crypto import compute_data_mic, encrypt_frm_payload
from simulator.lorawan.region import MAX_FCNT

logger = logging.getLogger(__name__)


@dataclass
class DeviceRecord:
    """Network server's record for an activated device."""
    dev_addr: int
    nwk_s_key: bytes
    app_s_key: bytes
    fcnt_up: int = 0 # Expected next uplink frame counter
    fcnt_down: int = 0 # Next downlink frame counter


@dataclass
class PendingDownlink:
    """A downlink frame queued for transmission."""
    dev_addr: int
    fport: int
    payload: bytes 
    confirmed: bool = False


class NetworkServer:
    """Simplified LoRaWAN network server for simulation.

    Responsibilities:
    - Device registration (ABP sessions)
    - Uplink processing: MIC verification, decryption, application routing
    - Downlink scheduling: queued downlinks sent via gateway in RX windows
    """
    _devices: dict[int, DeviceRecord]
    _applications: dict[int, Application]
    _downlink_queue: dict[int, deque[PendingDownlink]]


    def __init__(self) -> None:
        self._devices = {} # dev_addr -> DeviceRecord
        self._applications = {} # fport -> Application
        self._downlink_queue = {} # dev_addr -> queue


    def register_device(self, dev_addr: int, nwk_s_key: bytes, app_s_key: bytes) -> None:
        """Register an ABP-activated device."""
        self._devices[dev_addr] = DeviceRecord(
            dev_addr=dev_addr, nwk_s_key=nwk_s_key, app_s_key=app_s_key,
        )
        self._downlink_queue[dev_addr] = deque()


    def register_application(self, app: Application) -> None:
        """Register an application handler for a specific FPort."""
        port = app.port()
        assert 1 <= port <= 223, f"FPort must be 1-223, got {port}"
        self._applications[port] = app


    def queue_downlink(self, dev_addr: int, fport: int, payload: bytes,
                       confirmed: bool = False) -> None:
        """Queue a downlink for the given device."""
        assert dev_addr in self._devices, f"Unknown device 0x{dev_addr:08X}"
        self._downlink_queue[dev_addr].append(
            PendingDownlink(dev_addr=dev_addr, fport=fport, payload=payload, confirmed=confirmed)
        )


    async def handle_uplink(self, raw: bytes) -> bytes | None:
        """Process an uplink frame received from a gateway.

        Returns an encoded downlink PHYPayload if one is pending, or None.

        Args:
            raw: Raw PHYPayload bytes as received over the air.
        """
        try:
            phy = PHYPayload.decode_data(raw)
        except (AssertionError, Exception) as e:
            logger.warning(f"{sim.current_time():.2f}s  NS  failed to decode uplink: {e}")
            return None

        assert phy.mac_payload is not None
        mac = phy.mac_payload
        dev_addr = mac.fhdr.dev_addr

        # Look up device
        device = self._devices.get(dev_addr)
        if device is None:
            logger.warning(
                f"{sim.current_time():.2f}s  NS  unknown DevAddr=0x{dev_addr:08X}"
            )
            return None

        # Verify MIC
        mhdr_and_payload = raw[:-4]
        expected_mic = compute_data_mic(
            device.nwk_s_key,
            dev_addr=dev_addr,
            fcnt=mac.fhdr.fcnt,
            uplink=True,
            mhdr_and_payload=mhdr_and_payload,
        )
        if expected_mic != phy.mic:
            logger.warning(
                f"{sim.current_time():.2f}s  NS  MIC mismatch for "
                f"DevAddr=0x{dev_addr:08X} FCnt={mac.fhdr.fcnt}"
            )
            return None

        # Frame counter check (basic, 16-bit rollover not handled yet)
        if mac.fhdr.fcnt < device.fcnt_up:
            logger.warning(
                f"{sim.current_time():.2f}s  NS  FCnt too low for "
                f"DevAddr=0x{dev_addr:08X}: got {mac.fhdr.fcnt}, expected >= {device.fcnt_up}"
            )
            return None
        device.fcnt_up = mac.fhdr.fcnt + 1

        # Decrypt and route to application
        if mac.fport is not None and mac.fport > 0 and len(mac.frm_payload) > 0:
            plaintext = encrypt_frm_payload(
                device.app_s_key,
                dev_addr=dev_addr,
                fcnt=mac.fhdr.fcnt,
                uplink=True,
                payload=mac.frm_payload,
            )
            app = self._applications.get(mac.fport)
            if app is not None:
                await app.on_uplink(dev_addr, plaintext)

        logger.debug(
            f"{sim.current_time():.2f}s  NS  uplink OK  DevAddr=0x{dev_addr:08X}  "
            f"FCnt={mac.fhdr.fcnt}  FPort={mac.fport}"
        )

        # Check for pending downlinks (either queued or from applications)
        return await self._build_downlink(device)


    async def _build_downlink(self, device: DeviceRecord) -> bytes | None:
        """Build a downlink frame if one is pending for this device."""
        pending: PendingDownlink | None = None

        # Check explicit queue first
        queue = self._downlink_queue.get(device.dev_addr)
        if queue:
            pending = queue.popleft()

        # Then check applications for pending data
        if pending is None:
            for app in self._applications.values():
                dl_payload = await app.get_downlink(device.dev_addr)
                if dl_payload is not None:
                    pending = PendingDownlink(
                        dev_addr=device.dev_addr,
                        fport=app.port(),
                        payload=dl_payload,
                    )
                    break

        if pending is None:
            return None

        assert device.fcnt_down <= MAX_FCNT, "Downlink frame counter overflow"

        # Encrypt payload
        encrypted = encrypt_frm_payload(
            device.app_s_key,
            dev_addr=device.dev_addr,
            fcnt=device.fcnt_down,
            uplink=False,
            payload=pending.payload,
        )

        # Build frame
        mtype = MType.CONFIRMED_DATA_DN if pending.confirmed else MType.UNCONFIRMED_DATA_DN
        fhdr = FHDR(
            dev_addr=device.dev_addr,
            fctrl=FCtrl(),
            fcnt=device.fcnt_down,
        )
        mac_payload = MACPayload(fhdr=fhdr, fport=pending.fport, frm_payload=encrypted)
        mhdr = MHDR(mtype=mtype)

        mhdr_and_payload = bytes([mhdr.encode()]) + mac_payload.encode(uplink=False)
        mic = compute_data_mic(
            device.nwk_s_key,
            dev_addr=device.dev_addr,
            fcnt=device.fcnt_down,
            uplink=False,
            mhdr_and_payload=mhdr_and_payload,
        )

        phy = PHYPayload(mhdr=mhdr, mac_payload=mac_payload, mic=mic)
        raw = phy.encode()

        logger.debug(
            f"{sim.current_time():.2f}s  NS  downlink built  "
            f"DevAddr=0x{device.dev_addr:08X}  FCnt={device.fcnt_down}  "
            f"FPort={pending.fport}  {len(raw)} bytes"
        )

        device.fcnt_down += 1
        return raw
