"""
LoRaWAN network server.

Handles uplink processing (MIC verification, decryption, application routing)
and downlink scheduling. In a real deployment, the network server is a cloud
service; here it runs as a simulation component that the gateway forwards frames to.

Supports both ABP device registration and OTAA join handling.

Reference: LoRaWAN L2 1.0.4 Specification §4, §6.
"""

from __future__ import annotations
import logging
import random
from dataclasses import dataclass
from collections import deque

from simulator.environment import simulation_env as sim
from simulator.lorawan.application import Application
from simulator.lorawan.enums.frame_types import MType
from simulator.lorawan.frame import (
    MHDR, FCtrl, FHDR, MACPayload, PHYPayload,
)
from simulator.lorawan.crypto import compute_data_mic, encrypt_frm_payload
from simulator.lorawan.join import (
    OTAADeviceRecord, process_join_request,
)
from simulator.lorawan.mac_commands import (
    MACCommand, MACCommandType, encode_mac_commands, parse_uplink_commands,
    LinkCheckReq, LinkCheckAns, LinkADRReq, LinkADRAns,
    DevStatusAns, RXParamSetupAns, RXTimingSetupAns,
    DutyCycleAns, NewChannelAns,
)
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


@dataclass
class MulticastGroupRecord:
    """Network server's record for a multicast group."""
    group_addr: int
    nwk_s_key: bytes
    app_s_key: bytes
    fcnt_down: int = 0


class NetworkServer:
    """Simplified LoRaWAN network server for simulation.

    Responsibilities:
    - Device registration (ABP sessions and OTAA credentials)
    - OTAA join handling: JoinRequest verification, JoinAccept generation
    - Uplink processing: MIC verification, decryption, application routing
    - Downlink scheduling: queued downlinks sent via gateway in RX windows
    """
    _devices: dict[int, DeviceRecord]
    _applications: dict[int, Application]
    _downlink_queue: dict[int, deque[PendingDownlink]]
    _pending_mac_commands: dict[int, list[MACCommand]]
    _multicast_groups: dict[int, MulticastGroupRecord]


    def __init__(self, net_id: int = 0x000001) -> None:
        self._devices = {} # dev_addr -> DeviceRecord
        self._applications = {} # fport -> Application
        self._downlink_queue = {} # dev_addr -> queue
        self._pending_mac_commands = {} # dev_addr -> list of MAC commands
        self._otaa_devices: dict[bytes, OTAADeviceRecord] = {}  # dev_eui -> record
        self._multicast_groups = {}  # group_addr -> MulticastGroupRecord
        self._net_id = net_id
        self._app_nonce_counter = 0
        self._dev_addr_counter = 0x01000001  # Starting DevAddr for OTAA devices


    def register_device(self, dev_addr: int, nwk_s_key: bytes, app_s_key: bytes) -> None:
        """Register an ABP-activated device."""
        self._devices[dev_addr] = DeviceRecord(
            dev_addr=dev_addr, nwk_s_key=nwk_s_key, app_s_key=app_s_key,
        )
        self._downlink_queue[dev_addr] = deque()
        self._pending_mac_commands[dev_addr] = []


    def register_otaa_device(
        self, app_eui: bytes, dev_eui: bytes, app_key: bytes,
    ) -> None:
        """Register an OTAA-capable device (pre-provisioned root keys)."""
        self._otaa_devices[dev_eui] = OTAADeviceRecord(
            app_eui=app_eui,
            dev_eui=dev_eui,
            app_key=app_key,
        )


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


    def queue_mac_command(self, dev_addr: int, command: MACCommand) -> None:
        """Queue a MAC command to be sent to the device in the next downlink."""
        assert dev_addr in self._devices, f"Unknown device 0x{dev_addr:08X}"
        self._pending_mac_commands[dev_addr].append(command)


    def create_multicast_group(
        self, group_addr: int, nwk_s_key: bytes, app_s_key: bytes,
    ) -> MulticastGroupRecord:
        """Create a multicast group for downlink-only broadcast.

        Returns the MulticastGroupRecord (also stored internally).
        """
        record = MulticastGroupRecord(
            group_addr=group_addr, nwk_s_key=nwk_s_key, app_s_key=app_s_key,
        )
        self._multicast_groups[group_addr] = record
        return record

    def build_multicast_downlink(
        self, group_addr: int, fport: int, payload: bytes,
    ) -> bytes:
        """Build an encrypted multicast downlink frame.

        The caller (typically via the gateway) is responsible for transmitting
        the returned bytes over the radio.
        """
        group = self._multicast_groups[group_addr]
        assert group.fcnt_down <= MAX_FCNT, "Multicast frame counter overflow"

        encrypted = encrypt_frm_payload(
            group.app_s_key,
            dev_addr=group.group_addr,
            fcnt=group.fcnt_down,
            uplink=False,
            payload=payload,
        )

        fhdr = FHDR(
            dev_addr=group.group_addr,
            fctrl=FCtrl(),
            fcnt=group.fcnt_down,
        )
        mac_payload = MACPayload(fhdr=fhdr, fport=fport, frm_payload=encrypted)
        mhdr = MHDR(mtype=MType.UNCONFIRMED_DATA_DN)

        mhdr_and_payload = bytes([mhdr.encode()]) + mac_payload.encode(uplink=False)
        mic = compute_data_mic(
            group.nwk_s_key,
            dev_addr=group.group_addr,
            fcnt=group.fcnt_down,
            uplink=False,
            mhdr_and_payload=mhdr_and_payload,
        )

        phy = PHYPayload(mhdr=mhdr, mac_payload=mac_payload, mic=mic)
        raw = phy.encode()

        logger.debug(
            f"{sim.current_time():.2f}s  NS  multicast downlink  "
            f"GroupAddr=0x{group.group_addr:08X}  FCnt={group.fcnt_down}  "
            f"FPort={fport}  {len(raw)} bytes"
        )

        group.fcnt_down += 1
        return raw


    async def handle_uplink(self, raw: bytes) -> bytes | None:
        """Process an uplink frame received from a gateway.

        Returns an encoded downlink PHYPayload if one is pending, or None.
        Handles both JoinRequest and data uplinks.

        Args:
            raw: Raw PHYPayload bytes as received over the air.
        """
        if len(raw) < 1:
            return None

        # Check MType to distinguish JoinRequest from data frames
        mhdr = MHDR.decode(raw[0])
        if mhdr.mtype == MType.JOIN_REQUEST:
            return self._handle_join_request(raw)

        return await self._handle_data_uplink(raw)


    def _handle_join_request(self, raw: bytes) -> bytes | None:
        """Process a JoinRequest and generate a JoinAccept if valid."""
        app_nonce = self._app_nonce_counter
        self._app_nonce_counter += 1

        dev_addr = self._dev_addr_counter
        self._dev_addr_counter += 1

        result = process_join_request(
            raw=raw,
            device_db=self._otaa_devices,
            net_id=self._net_id,
            assign_dev_addr=dev_addr,
            app_nonce=app_nonce,
        )

        if result is None:
            return None

        join_accept_raw, otaa_record = result

        # Derive the same session keys the device will derive so we can
        # communicate with it after the join.
        from simulator.lorawan.crypto import derive_session_keys
        from simulator.lorawan.frame import JoinRequestPayload

        # Re-decode the JoinRequest to get dev_nonce
        phy = PHYPayload.decode_join_request(raw)
        assert phy.join_request is not None
        dev_nonce = phy.join_request.dev_nonce

        nwk_s_key, app_s_key = derive_session_keys(
            otaa_record.app_key, app_nonce, self._net_id, dev_nonce,
        )

        # Register the device with its new session
        self.register_device(dev_addr, nwk_s_key, app_s_key)

        logger.debug(
            f"{sim.current_time():.2f}s  NS  JoinAccept sent  "
            f"DevEUI={phy.join_request.dev_eui.hex()}  "
            f"DevAddr=0x{dev_addr:08X}"
        )

        return join_accept_raw


    async def _handle_data_uplink(self, raw: bytes) -> bytes | None:
        """Process a data uplink frame."""
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

        # Process MAC command answers from FOpts
        if mac.fhdr.fopts:
            self._handle_mac_answers(device, parse_uplink_commands(mac.fhdr.fopts))

        # Process MAC commands from FPort 0 (encrypted with NwkSKey)
        if mac.fport == 0 and len(mac.frm_payload) > 0:
            decrypted = encrypt_frm_payload(
                device.nwk_s_key,
                dev_addr=dev_addr,
                fcnt=mac.fhdr.fcnt,
                uplink=True,
                payload=mac.frm_payload,
            )
            self._handle_mac_answers(device, parse_uplink_commands(decrypted))

        # Decrypt and route to application
        elif mac.fport is not None and mac.fport > 0 and len(mac.frm_payload) > 0:
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
            # No application data, but check if we have MAC commands to send
            mac_cmds = self._pending_mac_commands.get(device.dev_addr, [])
            if not mac_cmds:
                return None
            # Send a frame with only MAC commands in FOpts (no FPort/FRMPayload)
            return self._build_mac_only_downlink(device)

        assert device.fcnt_down <= MAX_FCNT, "Downlink frame counter overflow"

        # Collect pending MAC commands for FOpts
        fopts = self._drain_mac_commands(device.dev_addr)

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
            fopts=fopts,
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


    def _build_mac_only_downlink(self, device: DeviceRecord) -> bytes | None:
        """Build a downlink frame containing only MAC commands in FOpts."""
        assert device.fcnt_down <= MAX_FCNT, "Downlink frame counter overflow"

        fopts = self._drain_mac_commands(device.dev_addr)
        if not fopts:
            return None

        fhdr = FHDR(
            dev_addr=device.dev_addr,
            fctrl=FCtrl(),
            fcnt=device.fcnt_down,
            fopts=fopts,
        )
        mac_payload = MACPayload(fhdr=fhdr)
        mhdr = MHDR(mtype=MType.UNCONFIRMED_DATA_DN)

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
            f"{sim.current_time():.2f}s  NS  MAC-only downlink  "
            f"DevAddr=0x{device.dev_addr:08X}  FCnt={device.fcnt_down}  "
            f"FOpts={len(fopts)} bytes"
        )

        device.fcnt_down += 1
        return raw


    def _handle_mac_answers(self, device: DeviceRecord, commands: list[MACCommandType]) -> None:
        """Process MAC command answers received from a device."""
        for cmd in commands:
            match cmd:
                case LinkCheckReq():
                    # Device is requesting a link check — respond with LinkCheckAns
                    self._pending_mac_commands.setdefault(device.dev_addr, []).append(
                        LinkCheckAns(margin=10, gw_cnt=1)
                    )
                    logger.debug(
                        f"{sim.current_time():.2f}s  NS  LinkCheckReq from "
                        f"DevAddr=0x{device.dev_addr:08X}"
                    )

                case LinkADRAns():
                    logger.debug(
                        f"{sim.current_time():.2f}s  NS  LinkADRAns from "
                        f"DevAddr=0x{device.dev_addr:08X}: "
                        f"ch_mask={cmd.channel_mask_ack} dr={cmd.data_rate_ack} "
                        f"power={cmd.power_ack}"
                    )

                case DutyCycleAns():
                    logger.debug(
                        f"{sim.current_time():.2f}s  NS  DutyCycleAns from "
                        f"DevAddr=0x{device.dev_addr:08X}"
                    )

                case RXParamSetupAns():
                    logger.debug(
                        f"{sim.current_time():.2f}s  NS  RXParamSetupAns from "
                        f"DevAddr=0x{device.dev_addr:08X}: "
                        f"offset={cmd.rx1_dr_offset_ack} dr={cmd.rx2_data_rate_ack} "
                        f"ch={cmd.channel_ack}"
                    )

                case DevStatusAns():
                    logger.debug(
                        f"{sim.current_time():.2f}s  NS  DevStatusAns from "
                        f"DevAddr=0x{device.dev_addr:08X}: "
                        f"battery={cmd.battery} margin={cmd.margin} dB"
                    )

                case NewChannelAns():
                    logger.debug(
                        f"{sim.current_time():.2f}s  NS  NewChannelAns from "
                        f"DevAddr=0x{device.dev_addr:08X}: "
                        f"dr_range={cmd.data_rate_range_ok} freq={cmd.channel_freq_ok}"
                    )

                case RXTimingSetupAns():
                    logger.debug(
                        f"{sim.current_time():.2f}s  NS  RXTimingSetupAns from "
                        f"DevAddr=0x{device.dev_addr:08X}"
                    )


    def _drain_mac_commands(self, dev_addr: int) -> bytes:
        """Encode and drain pending MAC commands for a device (for FOpts)."""
        commands = self._pending_mac_commands.get(dev_addr, [])
        if not commands:
            return b""
        encoded = encode_mac_commands(commands)
        commands.clear()
        if len(encoded) > 15:
            logger.warning(
                f"{sim.current_time():.2f}s  NS  MAC commands exceed FOpts limit "
                f"({len(encoded)} bytes), truncating to 15"
            )
            encoded = encoded[:15]
        return encoded
