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
from simulator.lorawan.beacon import compute_ping_slot_times
from simulator.lorawan.region import (
    MAX_FCNT, BEACON_RESERVED, PING_SLOT_LEN, max_frm_payload,
)

logger = logging.getLogger(__name__)


@dataclass
class DeviceRecord:
    """Network server's record for an activated device."""
    dev_addr: int
    nwk_s_key: bytes
    app_s_key: bytes
    fcnt_up: int = 0 # Expected next uplink frame counter
    fcnt_down: int = 0 # Next downlink frame counter
    class_b_enabled: bool = False
    class_b_ping_nb: int = 16


@dataclass
class PendingDownlink:
    """A downlink frame queued for transmission."""
    dev_addr: int
    fport: int
    payload: bytes 
    confirmed: bool = False


@dataclass
class MulticastGroupRecord:
    """Network server's record for a multicast group.

    Mirrors the device-side ``MulticastGroup`` context of TS005-2.0.0 §2.1.

    :ivar group_id: ``McGroupID``, the device-local index the group is set up under (0–3).
    :ivar min_fcnt: ``minMcFCnt``, the first multicast counter the server will send.
    :ivar max_fcnt: ``maxMcFCnt``, the counter ceiling that ends the group's lifetime.
    :ivar data_rate: Downlink data rate index for sessions on this group, or None for the
        gateway's default.
    :ivar frequency: Downlink frequency in hertz for sessions on this group, or None.
    :ivar class_b_enabled: Whether ping-slot downlinks are scheduled for this group.
    :ivar class_b_ping_nb: Ping slots per beacon period for the group's Class B session.
    """
    group_addr: int
    nwk_s_key: bytes
    app_s_key: bytes
    fcnt_down: int = 0
    group_id: int = 0
    min_fcnt: int = 0
    max_fcnt: int = MAX_FCNT
    data_rate: int | None = None
    frequency: int | None = None
    class_b_enabled: bool = False
    class_b_ping_nb: int = 16

    def __post_init__(self) -> None:
        assert 0 <= self.group_id <= 3, f"McGroupID must be 0-3, got {self.group_id}"
        assert 0 <= self.min_fcnt <= self.max_fcnt <= MAX_FCNT, (
            f"Invalid multicast counter window [{self.min_fcnt}, {self.max_fcnt}]"
        )
        self.fcnt_down = max(self.fcnt_down, self.min_fcnt)


@dataclass
class ScheduledMulticastDownlink:
    """A multicast payload waiting for its transmission slot.

    The frame itself is only built when the gateway is about to transmit it, so the
    multicast frame counter advances in actual send order.

    :ivar at_time: Simulation time the frame should go out at; None means "as soon as the
        gateway can".
    :ivar sequence: Monotonic tie-breaker that keeps same-time downlinks in FIFO order.
    """
    group_addr: int
    fport: int
    payload: bytes
    at_time: float | None = None
    sequence: int = 0

    def sort_key(self) -> tuple[float, int]:
        return (self.at_time if self.at_time is not None else float("-inf"), self.sequence)


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


    def __init__(self, net_id: int = 0x000001, default_data_rate: int = 5) -> None:
        #: Data rate assumed for payload-size checks when a group pins no rate of its own.
        self._default_data_rate = default_data_rate
        self._multicast_queue: list[ScheduledMulticastDownlink] = []
        self._multicast_sequence = 0
        #: Round-robin cursor into `_applications`, per device (see `_build_downlink`).
        self._app_cursor: dict[int, int] = {}
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
        self,
        group_addr: int,
        nwk_s_key: bytes,
        app_s_key: bytes,
        *,
        group_id: int = 0,
        min_fcnt: int = 0,
        max_fcnt: int = MAX_FCNT,
        data_rate: int | None = None,
        frequency: int | None = None,
    ) -> MulticastGroupRecord:
        """Create a multicast group for downlink-only broadcast.

        Reference: TS005-2.0.0 §2.5 (``McGroupSetupReq``) for the ``McGroupID`` /
        ``minMcFCnt`` / ``maxMcFCnt`` parameters.

        :returns: The ``MulticastGroupRecord`` (also stored internally).
        """
        record = MulticastGroupRecord(
            group_addr=group_addr, nwk_s_key=nwk_s_key, app_s_key=app_s_key,
            group_id=group_id, min_fcnt=min_fcnt, max_fcnt=max_fcnt,
            data_rate=data_rate, frequency=frequency,
        )
        self._multicast_groups[group_addr] = record
        return record

    def get_multicast_group(self, group_addr: int) -> MulticastGroupRecord | None:
        """Look a multicast group record up by its ``McAddr``."""
        return self._multicast_groups.get(group_addr)

    def build_multicast_downlink(
        self, group_addr: int, fport: int, payload: bytes,
    ) -> bytes:
        """Build an encrypted multicast downlink frame.

        The caller (typically via the gateway) is responsible for transmitting
        the returned bytes over the radio.
        """
        group = self._multicast_groups[group_addr]
        assert group.fcnt_down <= MAX_FCNT, "Multicast frame counter overflow"
        assert group.fcnt_down <= group.max_fcnt, (
            f"Multicast group 0x{group_addr:08X} reached maxMcFCnt {group.max_fcnt}"
        )

        # Multicast frames carry no FOpts, so the whole FRMPayload budget is the application's.
        limit = max_frm_payload(
            group.data_rate if group.data_rate is not None else self._default_data_rate
        )
        if len(payload) > limit:
            raise ValueError(
                f"Multicast payload of {len(payload)} bytes exceeds the {limit} byte "
                f"FRMPayload limit at DR"
                f"{group.data_rate if group.data_rate is not None else self._default_data_rate}"
            )

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

    # ---- Multicast downlink scheduling ----

    def schedule_multicast_downlink(
        self, group_addr: int, fport: int, payload: bytes,
        at_time: float | None = None,
    ) -> ScheduledMulticastDownlink:
        """Queue a multicast payload for the gateway to transmit.

        The queue is kept in time order; entries scheduled for the same instant (or with no
        time at all) go out first-in-first-out. The frame is built at transmission time, so
        the group's ``McFCnt`` follows the order the frames actually leave the gateway.

        :param at_time: Absolute simulation time to transmit at. None means "as soon as the
            gateway's radio is free".
        :returns: The queued entry, useful as a handle in tests.
        """
        assert group_addr in self._multicast_groups, (
            f"Unknown multicast group 0x{group_addr:08X}"
        )
        entry = ScheduledMulticastDownlink(
            group_addr=group_addr, fport=fport, payload=payload, at_time=at_time,
            sequence=self._multicast_sequence,
        )
        self._multicast_sequence += 1
        self._multicast_queue.append(entry)
        self._multicast_queue.sort(key=ScheduledMulticastDownlink.sort_key)

        logger.debug(
            f"{sim.current_time():.2f}s  NS  multicast downlink queued  "
            f"GroupAddr=0x{group_addr:08X}  FPort={fport}  "
            f"at={'ASAP' if at_time is None else f'{at_time:.2f}s'}"
        )
        return entry

    def next_multicast_downlink_time(self) -> float | None:
        """Time the next queued multicast downlink is due, or None when the queue is empty.

        An entry with no time of its own is due immediately and reports ``-inf``'s practical
        equivalent: the current simulation time.
        """
        for entry in self._multicast_queue:
            group = self._multicast_groups.get(entry.group_addr)
            if group is not None and group.class_b_enabled:
                continue  # Handed to the Class B ping slot scheduler instead.
            return entry.at_time if entry.at_time is not None else sim.current_time()
        return None

    def pop_due_multicast_downlinks(self, now: float) -> list[ScheduledMulticastDownlink]:
        """Remove and return every queued multicast downlink due at or before *now*.

        Frames of a group that runs a Class B multicast session are **not** returned: their
        transmission time is a ping slot, so they belong to
        ``get_class_b_downlink_schedule`` and are left in the queue for it. Without this the
        gateway's free-running multicast scheduler would put them on the air immediately and
        the devices, listening only in their slots, would never hear them.
        """
        due: list[ScheduledMulticastDownlink] = []
        remaining: list[ScheduledMulticastDownlink] = []
        for entry in self._multicast_queue:
            group = self._multicast_groups.get(entry.group_addr)
            class_b = group is not None and group.class_b_enabled
            if not class_b and (entry.at_time is None or entry.at_time <= now):
                due.append(entry)
            else:
                remaining.append(entry)
        self._multicast_queue = remaining
        return due

    def pending_multicast_downlinks(self) -> list[ScheduledMulticastDownlink]:
        """The queue as it stands, in transmission order (read-only snapshot)."""
        return list(self._multicast_queue)

    # ---- Class B ----

    def enable_multicast_class_b(self, group_addr: int, ping_nb: int = 16) -> None:
        """Schedule a multicast group's downlinks into Class B ping slots.

        The slots are computed from the group's ``McAddr``, which is what the devices in a
        Class B multicast session listen on (TS005-2.0.0 §2.8).

        :param ping_nb: Ping slots per beacon period, ``128 >> Periodicity``.
        """
        group = self._multicast_groups[group_addr]
        group.class_b_enabled = True
        group.class_b_ping_nb = ping_nb

    def disable_multicast_class_b(self, group_addr: int) -> None:
        """Stop scheduling a multicast group's downlinks into ping slots."""
        self._multicast_groups[group_addr].class_b_enabled = False


    def enable_class_b(self, dev_addr: int, ping_nb: int = 16) -> None:
        """Mark a device as Class B so the gateway can schedule ping slot downlinks."""
        device = self._devices[dev_addr]
        device.class_b_enabled = True
        device.class_b_ping_nb = ping_nb

    def disable_class_b(self, dev_addr: int) -> None:
        """Mark a device as no longer in Class B mode."""
        device = self._devices[dev_addr]
        device.class_b_enabled = False

    async def get_class_b_downlink_schedule(
        self, beacon_time: int,
    ) -> list[tuple[int, bytes, float]]:
        """Compute pending Class B downlinks with their ping slot times.

        Called by the gateway after broadcasting a beacon. For each Class B device with a
        pending downlink the method builds the encrypted frame and takes that device's first
        ping slot. Multicast groups that were enabled with ``enable_multicast_class_b`` are
        then served from the multicast queue: one queued payload per ping slot of the group,
        with the slots computed from the group's ``McAddr`` (TS005-2.0.0 §2.8).

        Multicast frames are built here, in slot order, so ``McFCnt`` matches the order the
        gateway will put them on the air.

        Returns:
            List of ``(addr, raw_downlink_bytes, slot_time)`` tuples sorted by ascending
            *slot_time*; *addr* is a ``DevAddr`` for unicast entries and an ``McAddr`` for
            multicast ones.
        """
        schedule: list[tuple[int, bytes, float]] = []

        for dev_addr, device in self._devices.items():
            if not device.class_b_enabled:
                continue

            raw = await self._build_downlink(device)
            if raw is None:
                continue

            slot_times = compute_ping_slot_times(
                beacon_time=beacon_time,
                dev_addr=dev_addr,
                ping_nb=device.class_b_ping_nb,
            )

            if slot_times:
                schedule.append((dev_addr, raw, slot_times[0]))

        schedule.extend(self._class_b_multicast_schedule(beacon_time))

        schedule.sort(key=lambda x: x[2])
        return schedule

    def _class_b_multicast_schedule(
        self, beacon_time: int,
    ) -> list[tuple[int, bytes, float]]:
        """Assign queued multicast payloads to their group's ping slots in this period."""
        schedule: list[tuple[int, bytes, float]] = []

        for group_addr, group in self._multicast_groups.items():
            if not group.class_b_enabled:
                continue

            slot_times = compute_ping_slot_times(
                beacon_time=beacon_time,
                dev_addr=group_addr,
                ping_nb=group.class_b_ping_nb,
            )
            if not slot_times:
                continue

            for slot_time in slot_times:
                entry = self._take_multicast_for(group_addr, slot_time)
                if entry is None:
                    break
                try:
                    raw = self.build_multicast_downlink(
                        group_addr, fport=entry.fport, payload=entry.payload,
                    )
                except ValueError as exc:
                    # Scheduling runs inside the gateway's beacon task; an oversized frame
                    # is dropped and logged rather than killing the simulation.
                    logger.error(
                        f"{sim.current_time():.2f}s  NS  dropping Class B multicast frame "
                        f"for 0x{group_addr:08X}: {exc}"
                    )
                    continue
                schedule.append((group_addr, raw, slot_time))

        return schedule

    def _take_multicast_for(
        self, group_addr: int, slot_time: float,
    ) -> ScheduledMulticastDownlink | None:
        """Pull the first queued entry for a group that is due by *slot_time*."""
        for index, entry in enumerate(self._multicast_queue):
            if entry.group_addr != group_addr:
                continue
            if entry.at_time is not None and entry.at_time > slot_time:
                continue
            return self._multicast_queue.pop(index)
        return None


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


    async def has_pending_downlink(self, dev_addr: int) -> bool:
        """Whether anything is waiting to go out to a device.

        Covers the explicit queue, pending MAC commands and any registered application that
        reports a downlink for this device. The probe is **non-destructive**: applications
        are asked through :meth:`~simulator.lorawan.application.Application.has_downlink`,
        which inspects a queue rather than popping from it. An application that generates its
        downlink on demand and does not override ``has_downlink`` simply reports False here
        and still gets its turn when the frame is actually built.
        """
        if self._downlink_queue.get(dev_addr):
            return True
        if self._pending_mac_commands.get(dev_addr):
            return True
        for app in self._applications.values():
            if await app.has_downlink(dev_addr):
                return True
        return False

    def _applications_round_robin(self, dev_addr: int) -> list[Application]:
        """Registered applications, starting just after the one served last for a device.

        Rotating the start point means several packages with answers pending all get served
        across successive uplinks instead of the lowest FPort starving the others.
        """
        apps = [self._applications[port] for port in sorted(self._applications)]
        if not apps:
            return apps
        start = self._app_cursor.get(dev_addr, 0) % len(apps)
        return apps[start:] + apps[:start]

    async def _build_downlink(self, device: DeviceRecord) -> bytes | None:
        """Build a downlink frame if one is pending for this device."""
        pending: PendingDownlink | None = None

        # Check explicit queue first
        queue = self._downlink_queue.get(device.dev_addr)
        if queue:
            pending = queue.popleft()

        # Then check applications for pending data, round-robin so no package starves.
        if pending is None:
            apps = self._applications_round_robin(device.dev_addr)
            ordered_ports = sorted(self._applications)
            for app in apps:
                dl_payload = await app.get_downlink(device.dev_addr)
                if dl_payload is not None:
                    pending = PendingDownlink(
                        dev_addr=device.dev_addr,
                        fport=app.port(),
                        payload=dl_payload,
                    )
                    # Next uplink starts looking at the application after this one.
                    self._app_cursor[device.dev_addr] = (
                        ordered_ports.index(app.port()) + 1
                    ) % len(ordered_ports)
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

        limit = max_frm_payload(self._default_data_rate, len(fopts))
        if len(pending.payload) > limit:
            # The downlink path runs inside the gateway's receive task, where an exception
            # tears the whole simulation down. An oversized payload is an application bug,
            # not a simulator failure, so it is logged and dropped like any other frame the
            # stack cannot put on the air. The explicit builder APIs
            # (:meth:`build_multicast_downlink`, ``LoRaWanDevice.send_uplink``) still raise,
            # because there the caller is the one choosing the payload size.
            logger.error(
                f"{sim.current_time():.2f}s  NS  dropping downlink of "
                f"{len(pending.payload)} bytes for 0x{device.dev_addr:08X}: it exceeds the "
                f"{limit} byte FRMPayload limit at DR{self._default_data_rate} with "
                f"{len(fopts)} FOpts bytes"
            )
            return None

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
