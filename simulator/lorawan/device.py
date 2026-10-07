from __future__ import annotations
import asyncio
import logging
import random
from dataclasses import dataclass, field

from simulator.environment import simulation_env as sim
from simulator.exceptions import SimulatorException
from simulator.lora.client_radio import LoraClientRadio
from simulator.lora.enums.radio_state import RadioState
from simulator.lora.packet import LoraPacket
from simulator.lora.packet_metadata import PacketMetadata
from simulator.lorawan.application import Application
from simulator.lorawan.enums.frame_types import MType
from simulator.lorawan.enums.operating_mode import OperatingMode
from simulator.lorawan.frame import (
    MHDR, FCtrl, FHDR, MACPayload, PHYPayload,
)
from simulator.lorawan.crypto import compute_data_mic, encrypt_frm_payload
from simulator.lorawan.join import (
    OTAACredentials, JoinResult, build_join_request, process_join_accept,
)
from simulator.lorawan.mac_commands import (
    MACCommand, MACCommandType, encode_mac_commands, parse_downlink_commands,
    LinkADRReq, LinkADRAns, DutyCycleReq, DutyCycleAns,
    RXParamSetupReq, RXParamSetupAns, DevStatusReq, DevStatusAns,
    RXTimingSetupReq, RXTimingSetupAns, LinkCheckAns,
    NewChannelReq, NewChannelAns,
)
from simulator.lorawan.beacon import decode_beacon, compute_ping_slot_times
from simulator.lorawan.region import (
    EU868_DATA_RATES, RECEIVE_DELAY1, RECEIVE_DELAY2, RX_WINDOW_GUARD, MAX_FCNT,
    DOWNLINK_IQ_INVERTED, max_frm_payload, rx_window_duration,
    JOIN_ACCEPT_DELAY1, JOIN_ACCEPT_DELAY2,
    BEACON_INTERVAL, BEACON_RESERVED, BEACON_GUARD,
    BEACON_LATE_TOLERANCE,
    PING_SLOT_LEN, CLASS_B_DEFAULT_PING_NB, MAX_BEACON_LESS_PERIOD,
)


# TS005 §2.1: a device supports at most four multicast contexts, indexed by McGroupID 0..3.
MAX_MULTICAST_GROUPS = 4


logger = logging.getLogger(__name__)


@dataclass
class DeviceSession:
    """LoRaWAN session state for an activated device."""
    dev_addr: int
    nwk_s_key: bytes
    app_s_key: bytes
    fcnt_up: int = 0
    fcnt_down: int = 0


@dataclass
class MulticastGroup:
    """LoRaWAN multicast group context (downlink only).

    A multicast group uses a shared DevAddr (``McAddr``) and shared session keys
    (``McNwkSKey`` / ``McAppSKey``). All devices in the group can decrypt downlinks
    addressed to this address. Multicast groups never send uplinks — they are used for
    FUOTA and other broadcast scenarios.

    Reference: LoRaWAN Remote Multicast Setup TS005-2.0.0 §2.1 (multicast group context)
    and §2.5 (``McGroupSetupReq``).

    :ivar group_addr: ``McAddr``, the 4-octet multicast network address.
    :ivar group_id: ``McGroupID``, the device-local index of this context (0–3).
    :ivar min_fcnt: ``minMcFCnt``, the lowest multicast frame counter the server will use.
    :ivar max_fcnt: ``maxMcFCnt``, the group's lifetime expressed as a counter ceiling.
    :ivar fcnt_down: The next multicast frame counter the device will accept. A frame is
        taken only when ``min_fcnt <= fcnt <= max_fcnt`` *and* ``fcnt >= fcnt_down``, so a
        device that missed the first K frames still accepts frame K+1 while a replay of an
        already-accepted counter is dropped.
    :ivar data_rate: Downlink data rate index to use while a session on this group runs, or
        None to keep the device's own RX configuration.
    :ivar frequency: Downlink frequency in hertz to use while a session runs, or None.
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

    def __post_init__(self) -> None:
        assert 0 <= self.group_id < MAX_MULTICAST_GROUPS, (
            f"McGroupID must be 0-{MAX_MULTICAST_GROUPS - 1}, got {self.group_id}"
        )
        assert 0 <= self.min_fcnt <= self.max_fcnt <= MAX_FCNT, (
            f"Invalid multicast counter window [{self.min_fcnt}, {self.max_fcnt}]"
        )
        # A group whose window starts above zero has not accepted anything below minMcFCnt.
        self.fcnt_down = max(self.fcnt_down, self.min_fcnt)

    def accepts_fcnt(self, fcnt: int) -> bool:
        """Whether a multicast downlink with this frame counter may be accepted.

        Reference: TS005-2.0.0 §2.5 — the device accepts a frame only while
        ``minMcFCnt <= McFCnt <= maxMcFCnt``, and never twice.
        """
        if not (self.min_fcnt <= fcnt <= self.max_fcnt):
            return False
        return fcnt >= self.fcnt_down

    def record_fcnt(self, fcnt: int) -> None:
        """Remember an accepted frame counter so the same frame is not taken twice."""
        self.fcnt_down = fcnt + 1


@dataclass
class MulticastSession:
    """A scheduled or running Class B/C multicast session on the device.

    Reference: TS005-2.0.0 §2.7 (``McClassCSessionReq``) and §2.8 (``McClassBSessionReq``).

    Times are simulation timestamps. ``sim.current_time()`` is the device's GPS time base:
    the simulator starts at GPS second 0, so a TS005 ``SessionTime`` maps straight onto a
    simulation timestamp without conversion.
    """
    group_addr: int
    mode: OperatingMode
    start_time: float
    end_time: float
    data_rate: int
    frequency: int | None = None
    ping_nb: int | None = None
    running: bool = False
    #: Set when a newer session for the same group took over; the superseded session then
    #: leaves the radio alone instead of reverting it.
    superseded: bool = False

    @property
    def timeout_seconds(self) -> float:
        return self.end_time - self.start_time


class LoRaWanDevice:
    """
        LoRaWAN end-device implementation.

        A single device class that supports Class A, B, and C operating modes via the
        OperatingMode enum. All devices start as Class A (baseline). The operating mode
        can be switched at runtime (e.g., Class A -> Class C for FUOTA, then back).

        Supports both ABP (Activation by Personalization) and OTAA (Over-The-Air
        Activation). For OTAA, pass ``otaa_credentials`` with no ``session``; the device
        must call ``join()`` before sending data.

        Reference: LoRaWAN L2 1.0.4 Specification chapter 3 & 4.
    """

    #: Longest the Class C receive loop waits for a frame before looking at its stop flag
    #: again. Reception is *not* gated by this interval: the receiver stays in continuous RX
    #: and the radio queues whatever it demodulates, so a frame that arrives while the loop is
    #: between two iterations is still picked up on the next one.
    CLASS_C_POLL_INTERVAL = 1.0

    #: How long the Class C loop stays off the air after finding the radio transmitting. Only
    #: reached when something other than :meth:`send_uplink` drives the transmitter.
    CLASS_C_TX_BACKOFF = 0.01

    #: Longest single wait inside a receive window, in seconds. A window that is parked on
    #: the receive queue cannot notice the radio being taken away from it: an uplink puts
    #: the radio into TX and the Class A windows that follow power it down, after which the
    #: window would sit "listening" with the receiver off until its deadline. Waking this
    #: often to re-arm the receiver bounds how long a long window (a beacon search of up to
    #: a whole beacon period) stays deaf after the device's own uplink. RX1/RX2 windows are
    #: far shorter than this and are not affected.
    RX_WINDOW_REARM_INTERVAL = 1.0

    def __init__(
        self,
        session: DeviceSession | None = None,
        data_rate: int = 5,
        tx_power: int = 14,
        operating_mode: OperatingMode = OperatingMode.CLASS_A,
        otaa_credentials: OTAACredentials | None = None,
        ping_nb: int = CLASS_B_DEFAULT_PING_NB,
        max_multicast_groups: int = MAX_MULTICAST_GROUPS,
    ):
        assert session is not None or otaa_credentials is not None, (
            "Provide either a session (ABP) or otaa_credentials (OTAA)"
        )
        self.radio = LoraClientRadio()
        self.session = session
        self.data_rate = data_rate
        # Downlink RX parameters, kept separate from the uplink data rate so a multicast
        # session can retune the receiver without disturbing uplinks or RX1 (TS005 §2.7).
        # None means "follow the uplink data rate / leave the radio on its current channel".
        self.rx_data_rate: int | None = None
        self.rx_frequency: int | None = None
        self.tx_power = tx_power
        self.operating_mode = operating_mode
        self.rx1_delay = RECEIVE_DELAY1
        self.battery_level: int = 255  # 0=external, 1-254=level, 255=unknown
        self._applications: dict[int, Application] = {}
        self._pending_mac_answers: list[MACCommand] = []
        self._otaa_credentials = otaa_credentials
        self._dev_nonce: int = 0
        self._multicast_groups: dict[int, MulticastGroup] = {}  # group_addr -> group
        self.max_multicast_groups = max_multicast_groups
        self._multicast_sessions: dict[int, MulticastSession] = {}  # group_addr -> session
        self._session_cancels: dict[int, asyncio.Event] = {}  # group_addr -> cancel flag
        self._class_c_running = False
        self._class_c_stop = asyncio.Event()
        # Non-zero while something else (an uplink and its RX1 window) owns the radio; the
        # continuous receive loop idles until it drops back to zero.
        self._class_c_suspends = 0
        self._class_c_waiters: list[asyncio.Event] = []
        # Class B state
        self._class_b_running = False
        self._class_b_stop = asyncio.Event()
        self._beacon_time: int | None = None
        self._beacon_locked = False
        self.ping_nb: int = ping_nb
        self._configure_radio()

    def register_application(self, app: Application) -> None:
        """Register an application handler for a specific FPort."""
        port = app.port()
        assert 1 <= port <= 223, f"FPort must be 1-223, got {port}"
        self._applications[port] = app

    def join_multicast_group(self, group: MulticastGroup) -> bool:
        """Join a multicast group to receive group downlinks.

        A device holds at most ``max_multicast_groups`` contexts (TS005 §2.1, four by
        default). Joining a group whose ``group_id`` (``McGroupID``) is already in use
        *replaces* that context, mirroring a repeated ``McGroupSetupReq`` for the same
        index; so does re-joining the same ``group_addr``. A join that would need a fifth
        distinct ``McGroupID`` is refused.

        :returns: True when the group was stored, False when no context was free. (The
            return value is new; callers that ignore it behave exactly as before.)
        """
        existing = self._multicast_groups.get(group.group_addr)
        if existing is None:
            # Re-using an McGroupID replaces whatever context was stored under it.
            by_id = self.get_multicast_group_by_id(group.group_id)
            if by_id is not None:
                del self._multicast_groups[by_id.group_addr]
            elif len(self._multicast_groups) >= self.max_multicast_groups:
                logger.warning(
                    f"{sim.current_time():.2f}s  DEVICE  cannot join multicast group "
                    f"0x{group.group_addr:08X}: all {self.max_multicast_groups} contexts in use"
                )
                return False

        self._multicast_groups[group.group_addr] = group
        logger.debug(
            f"{sim.current_time():.2f}s  DEVICE  joined multicast group "
            f"0x{group.group_addr:08X} (McGroupID={group.group_id})"
        )
        return True

    def leave_multicast_group(self, group_addr: int) -> None:
        """Leave a multicast group and cancel any session scheduled on it."""
        self.cancel_session(group_addr)
        self._multicast_groups.pop(group_addr, None)

    def get_multicast_group(self, group_addr: int) -> MulticastGroup | None:
        """Look a multicast context up by its ``McAddr``."""
        return self._multicast_groups.get(group_addr)

    def get_multicast_group_by_id(self, group_id: int) -> MulticastGroup | None:
        """Look a multicast context up by its device-local ``McGroupID`` (TS005 §2.1)."""
        for group in self._multicast_groups.values():
            if group.group_id == group_id:
                return group
        return None

    @property
    def multicast_groups(self) -> dict[int, MulticastGroup]:
        """All multicast contexts currently held, keyed by ``McAddr``."""
        return dict(self._multicast_groups)

    # ---- Downlink RX configuration ----

    def set_rx_config(self, data_rate: int | None, frequency: int | None = None) -> None:
        """Override the downlink RX parameters used outside Class A RX1/RX2.

        Class C continuous RX and Class B ping slots listen with these parameters; uplinks
        and the Class A RX1 window keep using the device's own ``data_rate``, as required by
        TS005 §2.7 ("except during Class A RX1 and RX2 slots").

        :param data_rate: EU868 data rate index, or None to fall back to the uplink rate.
        :param frequency: Carrier frequency in hertz, or None to leave the radio tuned where
            it is.
        """
        assert data_rate is None or data_rate in EU868_DATA_RATES, (
            f"Unknown data rate DR{data_rate}"
        )
        self.rx_data_rate = data_rate
        self.rx_frequency = frequency
        self._configure_radio()

    def clear_rx_config(self) -> None:
        """Drop any downlink RX override and go back to the device's own parameters."""
        self.set_rx_config(None, None)

    def _apply_uplink_rx_config(self) -> None:
        """Temporarily tune the receiver to the unicast parameters (RX1/RX2)."""
        dr = EU868_DATA_RATES[self.data_rate]
        self.radio.set_rx_config(
            spreading_factor=dr.spreading_factor.value,
            bandwidth=dr.bandwidth.to_khz(),
            iq_inverted=DOWNLINK_IQ_INVERTED,
            rx_continuous=self.operating_mode == OperatingMode.CLASS_C,
            # RX1/RX2 live on the channel the uplink went out on, which is the one the
            # transmitter is tuned to.
            frequency=self.radio.tx_frequency,
        )

    async def switch_mode(self, mode: OperatingMode) -> None:
        """Switch operating mode at runtime.

        Handles starting/stopping the background tasks associated with
        Class B (beacon + ping slots) and Class C (continuous RX).

        Leaving Class C also powers the receiver down and drops whatever it had queued, see
        :meth:`_leave_class_c`.
        """
        old_mode = self.operating_mode
        self.operating_mode = mode
        # Whether the receiver stays on between windows depends on the mode.
        self._configure_radio()
        logger.debug(
            f"{sim.current_time():.2f}s  DEVICE  mode {old_mode.value} -> {mode.value}"
        )

        # Stop old-mode background tasks
        if old_mode == OperatingMode.CLASS_B and mode != OperatingMode.CLASS_B:
            self._stop_class_b()
        if old_mode == OperatingMode.CLASS_C and mode != OperatingMode.CLASS_C:
            await self._leave_class_c()

        # Start new-mode background tasks
        if mode == OperatingMode.CLASS_B and old_mode != OperatingMode.CLASS_B:
            await self._start_class_b()
        if mode == OperatingMode.CLASS_C and old_mode != OperatingMode.CLASS_C:
            await self._start_class_c_rx()

    def _configure_radio(self) -> None:
        """Configure the radio for the current data rate and operating mode.

        The transmitter always follows the device's own ``data_rate``. The receiver follows
        ``rx_data_rate`` / ``rx_frequency`` when those overrides are set (a running multicast
        session), and the uplink data rate otherwise.
        """
        rx_dr = EU868_DATA_RATES[
            self.rx_data_rate if self.rx_data_rate is not None else self.data_rate
        ]
        self.radio.set_rx_config(
            spreading_factor=rx_dr.spreading_factor.value,
            bandwidth=rx_dr.bandwidth.to_khz(),
            iq_inverted=DOWNLINK_IQ_INVERTED,
            # Only Class C devices keep their receiver running between windows. Class A and
            # Class B open short single shot windows instead, so their radio has to drop back
            # out of RX on its own once a packet arrives or a transmission finishes.
            rx_continuous=self.operating_mode == OperatingMode.CLASS_C,
            frequency=self.rx_frequency,
        )
        dr = EU868_DATA_RATES[self.data_rate]
        self.radio.set_tx_config(
            power=self.tx_power,
            spreading_factor=dr.spreading_factor.value,
            bandwidth=dr.bandwidth.to_khz(),
        )

    async def join(self) -> bool:
        """
        Perform OTAA join procedure: send JoinRequest and wait for JoinAccept.

        Uses JOIN_ACCEPT_DELAY1 (5s) and JOIN_ACCEPT_DELAY2 (6s) windows.

        Returns True on successful join, False on timeout or failure.
        """
        assert self._otaa_credentials is not None, "No OTAA credentials configured"

        dev_nonce = self._dev_nonce
        self._dev_nonce += 1

        raw = build_join_request(self._otaa_credentials, dev_nonce)

        logger.debug(
            f"{sim.current_time():.2f}s  DEVICE TX  JoinRequest  "
            f"DevEUI={self._otaa_credentials.dev_eui.hex()}  DevNonce={dev_nonce}"
        )

        await self.radio.transmit_data_blocking(raw)

        tx_end = self.radio.tx_end_time
        assert tx_end is not None, "Join accept windows opened without a JoinRequest"
        await self.radio.off()

        # RX1 window at JOIN_ACCEPT_DELAY1
        result = await self._rx_window(tx_end + JOIN_ACCEPT_DELAY1, self._rx_window_timeout())
        if result is not None:
            join_result = process_join_accept(
                result.payload, self._otaa_credentials, dev_nonce,
            )
            if join_result is not None:
                self._activate_from_join(join_result)
                return True

        # RX2 window at JOIN_ACCEPT_DELAY2
        result = await self._rx_window(tx_end + JOIN_ACCEPT_DELAY2, self._rx_window_timeout())
        if result is not None:
            join_result = process_join_accept(
                result.payload, self._otaa_credentials, dev_nonce,
            )
            if join_result is not None:
                self._activate_from_join(join_result)
                return True

        logger.warning(
            f"{sim.current_time():.2f}s  DEVICE  Join failed (no JoinAccept received)"
        )
        return False

    def _activate_from_join(self, result: JoinResult) -> None:
        """Activate the device with session keys derived from a successful join."""
        self.session = DeviceSession(
            dev_addr=result.dev_addr,
            nwk_s_key=result.nwk_s_key,
            app_s_key=result.app_s_key,
        )
        self.rx1_delay = result.rx_delay
        self._configure_radio()
        logger.debug(
            f"{sim.current_time():.2f}s  DEVICE  Joined! "
            f"DevAddr=0x{result.dev_addr:08X}  RX1Delay={result.rx_delay}s"
        )

    async def send_uplink(self, fport: int, payload: bytes, confirmed: bool = False) -> bool:
        """
            Send an uplink frame and handle RX windows.

            Args:
                fport: Application port (1-223).
                payload: Application payload (plaintext, will be encrypted).
                confirmed: If True, send as Confirmed Data Up (expect ACK).

            Returns:
                True if a downlink was received (or ACK for confirmed), False otherwise.
        """
        assert 1 <= fport <= 223, f"FPort must be 1-223, got {fport}"
        assert self.session is not None, "Device not activated (call join() or provide session)"
        assert self.session.fcnt_up <= MAX_FCNT, "Frame counter overflow"

        # MAC answers ride along in FOpts and eat into the room left for the application
        # payload, so they have to be drained before the size can be checked.
        fopts = self._drain_mac_answers()
        limit = max_frm_payload(self.data_rate, len(fopts))
        if len(payload) > limit:
            raise ValueError(
                f"Payload of {len(payload)} bytes exceeds the {limit} byte FRMPayload limit "
                f"at DR{self.data_rate} with {len(fopts)} FOpts bytes"
            )

        # Encrypt FRMPayload
        encrypted = encrypt_frm_payload(
            self.session.app_s_key,
            dev_addr=self.session.dev_addr,
            fcnt=self.session.fcnt_up,
            uplink=True,
            payload=payload,
        )

        # Build the frame
        mtype = MType.CONFIRMED_DATA_UP if confirmed else MType.UNCONFIRMED_DATA_UP
        fhdr = FHDR(
            dev_addr=self.session.dev_addr,
            fctrl=FCtrl(class_b=self.operating_mode == OperatingMode.CLASS_B),
            fcnt=self.session.fcnt_up,
            fopts=fopts,
        )
        mac_payload = MACPayload(fhdr=fhdr, fport=fport, frm_payload=encrypted)
        mhdr = MHDR(mtype=mtype)

        # Compute MIC
        mhdr_and_payload = bytes([mhdr.encode()]) + mac_payload.encode(uplink=True)
        mic = compute_data_mic(
            self.session.nwk_s_key,
            dev_addr=self.session.dev_addr,
            fcnt=self.session.fcnt_up,
            uplink=True,
            mhdr_and_payload=mhdr_and_payload,
        )

        phy = PHYPayload(mhdr=mhdr, mac_payload=mac_payload, mic=mic)
        raw = phy.encode()

        logger.debug(
            f"{sim.current_time():.2f}s  DEVICE TX  "
            f"DevAddr=0x{self.session.dev_addr:08X}  FCnt={self.session.fcnt_up}  "
            f"FPort={fport}  {len(raw)} bytes"
        )

        # A Class C device may transmit at any moment, but its continuous receiver cannot keep
        # listening while it does — so hand the radio over for the transmission and the RX1
        # window that follows it, and give it back afterwards (L2 1.0.4 §19.3: a Class C
        # device returns to continuous RX2 reception as soon as RX1 closes).
        self._suspend_class_c_rx()
        try:
            # Transmit
            await self.radio.transmit_data_blocking(raw)
            self.session.fcnt_up += 1

            # Handle receive windows based on operating mode
            return await self._handle_rx_windows()
        finally:
            await self._resume_class_c_rx()

    async def _handle_rx_windows(self) -> bool:
        """
            Open receive windows after an uplink transmission.

            Returns True if a downlink was received.
        """
        if self.operating_mode == OperatingMode.CLASS_A:
            return await self._class_a_rx_windows()
        elif self.operating_mode == OperatingMode.CLASS_B:
            return await self._class_a_rx_windows()  # Class B uses same post-uplink windows
        elif self.operating_mode == OperatingMode.CLASS_C:
            return await self._class_c_rx_windows()
        return False

    async def _rx_window(self, open_at: float, duration: float) -> LoraPacket | None:
        """
            Sleep until a receive window, hold it open, and power the radio back down after.

            The radio is only ever in RX for the duration of the window itself. A Class A or
            Class B device has no reason to keep its receiver powered outside of its windows, and
            leaving it on is what used to make these devices draw continuous RX power for the
            whole simulation.

            Frames that were already sitting in the radio's receive queue when the window
            opened are discarded: they were picked up while the device was listening for
            something else (continuous Class C reception, say) and are not what this window is
            waiting for. Handing one of them back would close the window on a frame the caller
            never asked for.

            :param open_at: Simulation timestamp at which the radio should be listening.
            :param duration: In seconds, how long to keep the window open.
            :returns: The received packet, or None when the window closed empty.
        """
        # Wake up the radio slightly before the window opens, to account for startup time and
        # clock drift.
        wake_at = open_at - self._rx_wakeup_guard()
        if wake_at > sim.current_time():
            await sim.sleep_until(wake_at)
            if not sim.is_running():
                # `sleep_until` does not raise at the end of the simulation the way `sleep`
                # does: the environment simply wakes every sleeper one last time at the final
                # tick. The radio (and its power consumer) are torn down by then, so touching
                # them here would raise inside whichever background task owns this window.
                return None
        # A Class C device is already listening; dropping it into standby first would blind it
        # for the radio's whole startup time.
        if self.radio.get_state() != RadioState.RX:
            await self.radio.standby()

        opened_at = sim.current_time()
        deadline = opened_at + duration + RX_WINDOW_GUARD

        try:
            while True:
                remaining = deadline - sim.current_time()
                if remaining <= 0:
                    late = await self._receive_late_frame()
                    if late is None:
                        return None
                    result = late
                else:
                    try:
                        received = await self.radio.receive_data_within(
                            min(remaining, self.RX_WINDOW_REARM_INTERVAL), metadata=True
                        )
                        assert isinstance(received, tuple)
                        result = received
                    except TimeoutError:
                        # Either the window is over (the next pass checks for a frame that
                        # started inside it) or this was just a re-arm point, see
                        # RX_WINDOW_REARM_INTERVAL.
                        continue
                    except RuntimeError:
                        # The device is transmitting: nothing can be received until that is
                        # over, after which the next pass puts the receiver back on.
                        await sim.sleep(self.CLASS_C_TX_BACKOFF)
                        continue
                assert isinstance(result, tuple)
                (packet, meta) = result
                if meta.arrival_time is not None and meta.arrival_time < opened_at:
                    # Stale: queued before this window opened. Keep waiting for a frame that
                    # belongs to the window itself.
                    continue
                return packet
        finally:
            await self._rest_radio()

    def _rx_wakeup_guard(self) -> float:
        """How early the receiver has to be woken for it to be listening on time."""
        return RX_WINDOW_GUARD + self.radio.power_profile.standby_startup_time()

    def _rx_window_timeout(self) -> float:
        """How long an RX1/RX2 (or join accept) window stays open when nothing arrives.

        Sized from the receiver's *current* parameters, which the caller has already set up
        for the window (:meth:`_apply_uplink_rx_config` / :meth:`_configure_radio`): a window
        at DR0 waits the ~200 ms it takes to see six SF12 symbols, one at DR5 is done after
        ~15 ms. See :func:`simulator.lorawan.region.rx_window_duration`.
        """
        chain = next(c for c in self.radio.rx_chains if c.enabled)
        return rx_window_duration(chain.config.spreading_factor, chain.config.bandwidth)

    async def _receive_late_frame(self) -> tuple[LoraPacket, PacketMetadata] | None:
        """Finish receiving a frame whose preamble started inside a window that just closed.

        The window only bounds how long the device waits for a preamble to show up. Once it
        has locked onto one it keeps the receiver on until the frame is over, so a downlink
        that starts just before the window closes still gets received. Returns None when the
        channel was idle at the deadline, or when what was on the air never made it into the
        receive queue (a collision, a frame on other parameters).
        """
        if not self.radio.carrier_sense_instant():
            return None
        await self.radio.wait_for_channel_idle()
        pending = await self.radio.receive_data_nowait(metadata=True)
        if pending is None:
            return None
        assert isinstance(pending, tuple)
        return pending

    async def _rest_radio(self) -> None:
        """Put the radio back into the state the device rests in between receive windows.

        Class A and Class B devices power the receiver down; a Class C device goes back to
        continuous reception, which is where LoRaWAN L2 1.0.4 §19.3 leaves it outside its RX1
        windows. Getting this wrong in either direction is expensive: a Class A device that
        keeps listening both burns RX power for the whole simulation and queues frames meant
        for other devices, while a Class C device that powers down misses the multicast
        traffic its session was opened for.
        """
        if self.radio.get_state() == RadioState.TX:
            return
        if self.operating_mode == OperatingMode.CLASS_C and not self._class_c_stop.is_set():
            await self.radio.receive(continuous=True)
        else:
            await self.radio.off()

    async def _class_a_rx_windows(self) -> bool:
        """
            Class A: two short RX windows after an uplink, radio asleep in between.

            Outside of the two windows the radio goes back to its lowest power state, which is
            what gives a Class A device its characteristic burst shaped power trace.
        """
        # Both windows are timed from the end of the uplink's air time, not from where we are
        # now: the radio also spends time transitioning out of TX before handing control back.
        tx_end = self.radio.tx_end_time
        assert tx_end is not None, "Receive windows opened without a preceding transmission"

        # The transmitter drops into standby when the uplink finishes, from where the radio can
        # go all the way back to sleep until RX1 opens. A device that switched to Class C while
        # this uplink was in the air keeps listening instead.
        await self._rest_radio()

        # TS005 §2.7: a running multicast session must not disturb the Class A windows, those
        # keep listening with the unicast parameters.
        restore = self._rx_override_active()
        if restore:
            self._apply_uplink_rx_config()
        try:
            result = await self._rx_window(tx_end + RECEIVE_DELAY1, self._rx_window_timeout())
            if result is not None:
                await self._process_downlink(result.payload)
                return True

            result = await self._rx_window(tx_end + RECEIVE_DELAY2, self._rx_window_timeout())
            if result is not None:
                await self._process_downlink(result.payload)
                return True

            return False
        finally:
            if restore:
                self._configure_radio()

    async def _class_c_rx_windows(self) -> bool:
        """Class C: RX1 window after uplink, then resume continuous RX2.

        The background Class C RX task handles unsolicited downlinks between
        uplinks. This method only handles the RX1 window after a TX.
        """
        # RX1 window (same as Class A, except the receiver never powers down). A multicast
        # session's parameters are suspended for the window (TS005 §2.7).
        restore = self._rx_override_active()
        if restore:
            self._apply_uplink_rx_config()
        try:
            await sim.sleep(RECEIVE_DELAY1)
            reply: LoraPacket | None
            try:
                received = await self.radio.receive_data_within(self._rx_window_timeout())
                assert isinstance(received, LoraPacket)
                reply = received
            except TimeoutError:
                late = await self._receive_late_frame()
                reply = late[0] if late is not None else None
            if reply is not None:
                await self._process_downlink(reply.payload)
                return True
        finally:
            if restore:
                self._configure_radio()

        # Resume continuous RX2 — the background _class_c_rx_loop picks up from here
        await self.radio.receive(continuous=True)
        return False

    def _rx_override_active(self) -> bool:
        """Whether a downlink RX override (from a multicast session) is in effect."""
        return self.rx_data_rate is not None or self.rx_frequency is not None

    async def _start_class_c_rx(self) -> None:
        """Start the Class C continuous RX background task."""
        if self._class_c_running:
            return
        self._class_c_stop.clear()
        self._class_c_running = True
        await sim.start_child_task(self._class_c_rx_loop())

    def _stop_class_c_rx(self) -> None:
        """Stop the Class C continuous RX background task."""
        if not self._class_c_running:
            return
        self._class_c_stop.set()
        self._class_c_running = False

    async def _leave_class_c(self) -> None:
        """Stop continuous reception, power the receiver down and drop what it collected.

        A radio left in RX after the device has gone back to Class A keeps demodulating and
        queueing frames addressed to other devices, and the device's next RX window would then
        pop one of those stale frames and close before its own reply ever arrived. So the
        receiver is switched off — which is where a Class A device rests anyway — and the queue
        is emptied.
        """
        self._stop_class_c_rx()
        await self._wake_class_c_loop()
        self.radio.flush_rx_queue()
        if self.radio.get_state() != RadioState.TX:
            await self.radio.off()

    def _suspend_class_c_rx(self) -> None:
        """Hand the radio to an uplink's transmission and RX1 window.

        Suspensions nest; the continuous receiver comes back only once every one of them has
        been matched by a :meth:`_resume_class_c_rx`.
        """
        self._class_c_suspends += 1

    async def _resume_class_c_rx(self) -> None:
        """Give the radio back to the Class C receive loop."""
        assert self._class_c_suspends > 0, "Unbalanced Class C receiver suspend"
        self._class_c_suspends -= 1
        if self._class_c_suspends == 0:
            await self._wake_class_c_loop()

    async def _wake_class_c_loop(self) -> None:
        """Wake the Class C receive loop out of its idle wait on the next tick.

        The wake-up goes through the environment rather than setting the event directly: the
        environment is what re-takes the timer lock for a woken task, and a task woken behind
        its back would let simulation time run away underneath it.
        """
        waiters, self._class_c_waiters = self._class_c_waiters, []
        if not sim.is_running():
            return
        for event in waiters:
            await sim.schedule_event_no_await(event, sim.next_tick())

    async def _wait_for_class_c_wake(self) -> None:
        """Idle until the radio is handed back, or until the simulation ends."""
        event = asyncio.Event()
        self._class_c_waiters.append(event)
        await sim.schedule_event_wait(event, sim.last_tick())
        if event in self._class_c_waiters:
            self._class_c_waiters.remove(event)

    async def _class_c_rx_loop(self) -> None:
        """Background task: continuously listen for downlinks (Class C).

        Runs until the simulation ends or the device switches away from Class C. Processes any
        received downlink immediately (both unicast and multicast).

        The receiver stays in continuous RX for the whole time the loop runs, so reception does
        not depend on where the loop happens to be: the radio queues every frame it demodulates
        and the loop takes it from there. ``CLASS_C_POLL_INTERVAL`` therefore only bounds how
        quickly the loop notices that it should stop, never which frames it sees.

        The one thing that does take the radio away is this device transmitting. ``send_uplink``
        announces that with :meth:`_suspend_class_c_rx`, and the loop idles — rather than
        tripping over a radio that refuses to receive while it transmits — until the uplink and
        its RX1 window are done.
        """
        try:
            while sim.is_running() and not self._class_c_stop.is_set():
                if self._class_c_suspends > 0:
                    await self._wait_for_class_c_wake()
                    continue

                if self.radio.get_state() != RadioState.RX:
                    await self.radio.receive(continuous=True)

                try:
                    result = await self.radio.receive_data_within(
                        self.CLASS_C_POLL_INTERVAL
                    )
                except TimeoutError:
                    continue
                except RuntimeError:
                    # Something other than send_uplink put the radio into TX. Stay off it
                    # until the transmission is over instead of spinning on the error.
                    await sim.sleep(self.CLASS_C_TX_BACKOFF)
                    continue

                assert isinstance(result, LoraPacket)
                if self._class_c_stop.is_set() or self._class_c_suspends > 0:
                    # The radio changed hands while this frame was being demodulated, so it
                    # is no longer this loop's to process.
                    continue
                await self._process_downlink(result.payload)
        except SimulatorException:
            # The simulation ended while the loop was waiting for a frame.
            return

    async def _process_downlink(self, raw: bytes) -> None:
        """Decode and process a received downlink frame (unicast or multicast)."""
        try:
            phy = PHYPayload.decode_data(raw)
        except (AssertionError, Exception) as e:
            logger.warning(f"{sim.current_time():.2f}s  DEVICE  failed to decode downlink: {e}")
            return

        assert phy.mac_payload is not None
        mac = phy.mac_payload
        dev_addr = mac.fhdr.dev_addr

        # Resolve keys: unicast session or multicast group
        mc_group = self._multicast_groups.get(dev_addr)
        if mc_group is not None:
            # TS005 §2.5: a multicast frame is only taken while it sits inside the group's
            # [minMcFCnt, maxMcFCnt] window and has not been accepted before.
            if not mc_group.accepts_fcnt(mac.fhdr.fcnt):
                logger.debug(
                    f"{sim.current_time():.2f}s  DEVICE  multicast frame dropped, FCnt="
                    f"{mac.fhdr.fcnt} outside [{mc_group.fcnt_down}, {mc_group.max_fcnt}] "
                    f"for group 0x{dev_addr:08X}"
                )
                return
            nwk_s_key = mc_group.nwk_s_key
            app_s_key = mc_group.app_s_key
            is_multicast = True
        elif self.session is not None and dev_addr == self.session.dev_addr:
            nwk_s_key = self.session.nwk_s_key
            app_s_key = self.session.app_s_key
            is_multicast = False
        else:
            # Not addressed to us
            return

        # Verify MIC
        mhdr_and_payload = raw[:-4]
        expected_mic = compute_data_mic(
            nwk_s_key,
            dev_addr=dev_addr,
            fcnt=mac.fhdr.fcnt,
            uplink=False,
            mhdr_and_payload=mhdr_and_payload,
        )
        if expected_mic != phy.mic:
            logger.warning(
                f"{sim.current_time():.2f}s  DEVICE  MIC mismatch on downlink "
                f"FCnt={mac.fhdr.fcnt}"
            )
            return

        # Update downlink frame counter
        if is_multicast:
            assert mc_group is not None
            mc_group.record_fcnt(mac.fhdr.fcnt)
        else:
            assert self.session is not None
            if mac.fhdr.fcnt >= self.session.fcnt_down:
                self.session.fcnt_down = mac.fhdr.fcnt + 1

        # Multicast frames do not carry MAC commands
        if not is_multicast:
            # Process MAC commands from FOpts
            if mac.fhdr.fopts:
                self._handle_mac_commands(parse_downlink_commands(mac.fhdr.fopts))

            # Process MAC commands from FPort 0 (encrypted with NwkSKey)
            if mac.fport == 0 and len(mac.frm_payload) > 0:
                decrypted = encrypt_frm_payload(
                    nwk_s_key,
                    dev_addr=dev_addr,
                    fcnt=mac.fhdr.fcnt,
                    uplink=False,
                    payload=mac.frm_payload,
                )
                self._handle_mac_commands(parse_downlink_commands(decrypted))
                logger.debug(
                    f"{sim.current_time():.2f}s  DEVICE RX  "
                    f"FCnt={mac.fhdr.fcnt}  FPort={mac.fport}"
                )
                return

        # Decrypt and route to application
        if mac.fport is not None and mac.fport > 0 and len(mac.frm_payload) > 0:
            plaintext = encrypt_frm_payload(
                app_s_key,
                dev_addr=dev_addr,
                fcnt=mac.fhdr.fcnt,
                uplink=False,
                payload=mac.frm_payload,
            )
            app = self._applications.get(mac.fport)
            if app is not None:
                await app.on_downlink(plaintext)
            else:
                logger.debug(
                    f"{sim.current_time():.2f}s  DEVICE  no application for FPort={mac.fport}"
                )

        logger.debug(
            f"{sim.current_time():.2f}s  DEVICE RX  "
            f"{'MC ' if is_multicast else ''}"
            f"FCnt={mac.fhdr.fcnt}  FPort={mac.fport}"
        )

    def _handle_mac_commands(self, commands: list[MACCommandType]) -> None:
        """Process downlink MAC commands and queue appropriate answers."""
        for cmd in commands:
            match cmd:
                case LinkCheckAns():
                    logger.debug(
                        f"{sim.current_time():.2f}s  DEVICE  LinkCheckAns: "
                        f"margin={cmd.margin} dB, gw_cnt={cmd.gw_cnt}"
                    )

                case LinkADRReq():
                    dr_ok = cmd.data_rate in EU868_DATA_RATES or cmd.data_rate == 0x0F
                    power_ok = 0 <= cmd.tx_power <= 14 or cmd.tx_power == 0x0F

                    if dr_ok and cmd.data_rate != 0x0F:
                        self.data_rate = cmd.data_rate
                    if power_ok and cmd.tx_power != 0x0F:
                        self.tx_power = cmd.tx_power
                    if dr_ok or power_ok:
                        self._configure_radio()

                    self._pending_mac_answers.append(LinkADRAns(
                        channel_mask_ack=True,
                        data_rate_ack=dr_ok,
                        power_ack=power_ok,
                    ))
                    logger.debug(
                        f"{sim.current_time():.2f}s  DEVICE  LinkADRReq: "
                        f"DR={cmd.data_rate} TXPow={cmd.tx_power}"
                    )

                case DutyCycleReq():
                    self._pending_mac_answers.append(DutyCycleAns())
                    logger.debug(
                        f"{sim.current_time():.2f}s  DEVICE  DutyCycleReq: "
                        f"max_duty_cycle={cmd.max_duty_cycle}"
                    )

                case RXParamSetupReq():
                    dr_ok = cmd.rx2_data_rate in EU868_DATA_RATES
                    self._pending_mac_answers.append(RXParamSetupAns(
                        rx1_dr_offset_ack=True,
                        rx2_data_rate_ack=dr_ok,
                        channel_ack=True,
                    ))
                    logger.debug(
                        f"{sim.current_time():.2f}s  DEVICE  RXParamSetupReq: "
                        f"RX1DRoffset={cmd.rx1_dr_offset} RX2DR={cmd.rx2_data_rate}"
                    )

                case DevStatusReq():
                    self._pending_mac_answers.append(DevStatusAns(
                        battery=self.battery_level,
                        margin=0,
                    ))
                    logger.debug(f"{sim.current_time():.2f}s  DEVICE  DevStatusReq")

                case NewChannelReq():
                    self._pending_mac_answers.append(NewChannelAns(
                        data_rate_range_ok=True,
                        channel_freq_ok=True,
                    ))
                    logger.debug(
                        f"{sim.current_time():.2f}s  DEVICE  NewChannelReq: "
                        f"ch={cmd.ch_index} freq={cmd.frequency}"
                    )

                case RXTimingSetupReq():
                    self.rx1_delay = cmd.delay
                    self._pending_mac_answers.append(RXTimingSetupAns())
                    logger.debug(
                        f"{sim.current_time():.2f}s  DEVICE  RXTimingSetupReq: "
                        f"delay={cmd.delay}s"
                    )

    def _drain_mac_answers(self) -> bytes:
        """Encode and drain pending MAC command answers for inclusion in FOpts."""
        if not self._pending_mac_answers:
            return b""
        encoded = encode_mac_commands(self._pending_mac_answers)
        self._pending_mac_answers.clear()
        # FOpts max 15 bytes; if too large, caller should use FPort 0 instead
        if len(encoded) > 15:
            logger.warning(
                f"{sim.current_time():.2f}s  DEVICE  MAC answers exceed FOpts limit "
                f"({len(encoded)} bytes), truncating to 15"
            )
            encoded = encoded[:15]
        return encoded

    # ---- Timed multicast sessions (TS005 §2.7 / §2.8) ----

    async def start_class_c_session(
        self,
        group_addr: int,
        *,
        start_time: float,
        timeout_seconds: float,
        data_rate: int,
        frequency: int | None = None,
    ) -> None:
        """Schedule a Class C multicast session on a joined group.

        A background task sleeps until *start_time*, applies the session's RX parameters,
        switches the device to Class C, and reverts to the previous operating mode and RX
        configuration ``timeout_seconds`` later. Scheduling a session for a group that
        already has one replaces (and cancels) the previous one, per the "replace" reading
        of TS005 §2.7.

        Time base: *start_time* is a simulation timestamp. ``sim.current_time()`` is the
        device's GPS clock, so a TS005 ``SessionTime`` is used directly.

        Reference: TS005-2.0.0 §2.7 — session runs from ``SessionTime`` to
        ``SessionTime + 2^TimeOut``.

        :param group_addr: ``McAddr`` of a group the device has joined.
        :param start_time: Absolute simulation time at which the session opens.
        :param timeout_seconds: Maximum session length; the device reverts at
            ``start_time + timeout_seconds``.
        :param data_rate: Downlink data rate index used for the session.
        :param frequency: Downlink frequency in hertz, or None to stay on the current
            channel.
        """
        await self._start_session(MulticastSession(
            group_addr=group_addr,
            mode=OperatingMode.CLASS_C,
            start_time=start_time,
            end_time=start_time + timeout_seconds,
            data_rate=data_rate,
            frequency=frequency,
        ))

    async def start_class_b_session(
        self,
        group_addr: int,
        *,
        start_time: float,
        timeout_seconds: float,
        data_rate: int,
        frequency: int | None = None,
        ping_periodicity: int = 4,
    ) -> None:
        """Schedule a Class B multicast session on a joined group.

        The device switches to Class B (beacon loop) for the duration of the session and
        additionally opens the group's ping slots, computed from the *multicast* address.

        Reference: TS005-2.0.0 §2.8 — ``Periodicity`` uses the ``PingSlotInfoReq`` encoding,
        so the group opens ``128 / 2**ping_periodicity`` slots per beacon period.

        :param ping_periodicity: ``Periodicity`` field, 0–7. 0 means 128 ping slots per
            beacon period, 7 means one.
        """
        assert 0 <= ping_periodicity <= 7, "Periodicity must be 0-7 (TS005 §2.8)"
        await self._start_session(MulticastSession(
            group_addr=group_addr,
            mode=OperatingMode.CLASS_B,
            start_time=start_time,
            end_time=start_time + timeout_seconds,
            data_rate=data_rate,
            frequency=frequency,
            ping_nb=128 >> ping_periodicity,
        ))

    async def _start_session(self, session: MulticastSession) -> None:
        """Register a session, replacing any previous one for the same group, and run it."""
        assert session.group_addr in self._multicast_groups, (
            f"No multicast group 0x{session.group_addr:08X} on this device"
        )
        self.cancel_session(session.group_addr, superseded=True)

        cancel = asyncio.Event()
        self._session_cancels[session.group_addr] = cancel
        self._multicast_sessions[session.group_addr] = session

        logger.debug(
            f"{sim.current_time():.2f}s  DEVICE  Class {session.mode.value} multicast session "
            f"scheduled for 0x{session.group_addr:08X} at {session.start_time:.2f}s "
            f"(timeout {session.timeout_seconds:.2f}s, DR{session.data_rate})"
        )

        await sim.start_child_task(self._run_session(session, cancel))

    def cancel_session(self, group_addr: int, superseded: bool = False) -> bool:
        """Cancel a scheduled or running multicast session.

        The session's task wakes up immediately and, unless it is being replaced by a newer
        session, restores the operating mode and RX configuration it had applied. Reverting
        needs to await the mode switch, so the device leaves the session a tick or so after
        this call returns.

        :param superseded: Internal. True when a newer session for the same group is taking
            over, in which case the cancelled session leaves the radio untouched.
        :returns: True when a session was cancelled, False when there was none.
        """
        cancel = self._session_cancels.pop(group_addr, None)
        session = self._multicast_sessions.pop(group_addr, None)
        if cancel is None and session is None:
            return False
        if session is not None:
            session.superseded = superseded
        if cancel is not None:
            cancel.set()
        logger.debug(
            f"{sim.current_time():.2f}s  DEVICE  multicast session on 0x{group_addr:08X} "
            f"cancelled"
        )
        return True

    def active_session(self, group_addr: int | None = None) -> MulticastSession | None:
        """The scheduled or running session for a group, or any running session.

        :param group_addr: When given, the session registered for that group (whether it has
            started or is still waiting for its start time). When omitted, the first session
            that is currently running.
        """
        if group_addr is not None:
            return self._multicast_sessions.get(group_addr)
        for session in self._multicast_sessions.values():
            if session.running:
                return session
        return None

    def active_sessions(self) -> list[MulticastSession]:
        """Every scheduled or running multicast session, in registration order."""
        return list(self._multicast_sessions.values())

    async def _run_session(self, session: MulticastSession, cancel: asyncio.Event) -> None:
        """Background task driving one multicast session from start to timeout."""
        previous_mode = self.operating_mode
        previous_rx: tuple[int | None, int | None] = (self.rx_data_rate, self.rx_frequency)
        # Switch a receiver start-up time early, so the radio is actually listening *at*
        # SessionTime rather than a fraction of a millisecond after it. A multicast frame
        # scheduled for the very start of the session would otherwise begin while the radio
        # was still coming out of sleep, and the device would never hear its preamble.
        arm_at = session.start_time - self._rx_wakeup_guard()
        try:
            if await self._sleep_until_or_cancel(arm_at, cancel):
                session.running = False
                self._forget_session(session)
                return
            if not sim.is_running():
                self._forget_session(session)
                return

            previous_mode = self.operating_mode
            previous_rx = (self.rx_data_rate, self.rx_frequency)
            session.running = True
            self.set_rx_config(session.data_rate, session.frequency)
            await self.switch_mode(session.mode)

            logger.info(
                f"{sim.current_time():.2f}s  DEVICE  Class {session.mode.value} multicast "
                f"session started on 0x{session.group_addr:08X} until "
                f"{session.end_time:.2f}s"
            )

            cancelled = await self._sleep_until_or_cancel(session.end_time, cancel)
            session.running = False

            # A replacing session owns the radio now, undoing its configuration would be
            # wrong. An explicit cancel, and a plain timeout, both revert the device.
            if not session.superseded:
                if not cancelled:
                    logger.info(
                        f"{sim.current_time():.2f}s  DEVICE  multicast session on "
                        f"0x{session.group_addr:08X} timed out, reverting to Class "
                        f"{previous_mode.value}"
                    )
                self.set_rx_config(*previous_rx)
                await self.switch_mode(previous_mode)
            self._forget_session(session)

        except SimulatorException:
            # The simulation ended underneath us. Restoring radio state is pointless at that
            # point, but the bookkeeping must still run so the device does not look like it
            # is sitting in a session afterwards. Swallowed on purpose: the environment's
            # task wrapper treats any escaping exception as a fatal simulation error.
            session.running = False
            self._forget_session(session)

    async def _sleep_until_or_cancel(self, timestamp: float, cancel: asyncio.Event) -> bool:
        """Sleep until *timestamp*, waking early when *cancel* is set.

        :returns: True when the sleep was cut short by the cancel event.
        """
        if cancel.is_set():
            return True
        if timestamp <= sim.current_time():
            return False

        # Never ask the environment to sleep until the tick it is already on: that event
        # can no longer fire and the task would hang.
        sleeper = asyncio.ensure_future(
            sim.sleep_until(max(timestamp, sim.next_tick()))
        )
        waiter = asyncio.ensure_future(cancel.wait())
        try:
            await asyncio.wait({sleeper, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (sleeper, waiter):
                if not task.done():
                    task.cancel()
            # `sim.sleep_until` re-acquires the timer lock from its own cancellation
            # handler, so give it a chance to run before we carry on.
            await asyncio.sleep(0)
        if sleeper.done() and not sleeper.cancelled():
            # Surface a simulation-end exception to the caller.
            sleeper.result()
        return cancel.is_set()

    def _forget_session(self, session: MulticastSession) -> None:
        """Drop the registration of *session*, unless it was already replaced by a new one."""
        if self._multicast_sessions.get(session.group_addr) is session:
            del self._multicast_sessions[session.group_addr]
            self._session_cancels.pop(session.group_addr, None)

    # ---- Class B: beacon synchronization & ping slots ----

    async def _start_class_b(self) -> None:
        """Start the Class B beacon listening background task."""
        if self._class_b_running:
            return
        self._class_b_stop.clear()
        self._class_b_running = True
        await sim.start_child_task(self._class_b_beacon_loop())

    def _stop_class_b(self) -> None:
        """Stop the Class B background task."""
        if not self._class_b_running:
            return
        self._class_b_stop.set()
        self._class_b_running = False
        self._beacon_locked = False

    async def _class_b_beacon_loop(self) -> None:
        """Background task: listen for beacons and open ping slots (Class B).

        Wraps :meth:`_class_b_beacon_scheduler`, which is always waiting for a beacon or a
        ping slot when the simulation runs out. The environment wakes every sleeper one
        last time at the final tick, so the end of the simulation surfaces as a
        ``SimulatorException``; swallow it rather than letting it look like a failure.
        """
        try:
            await self._class_b_beacon_scheduler()
        except SimulatorException:
            return

    async def _beacon_window(self, open_at: float, duration: float) -> int | None:
        """Listen for a beacon in one window; returns its timestamp, or None if none came.

        The window keeps its deadline no matter what else lands in it: a frame that is not a
        beacon (a downlink to another device, say) is dropped and the receiver stays on for
        whatever is left of the window. Re-opening a fresh window after every such frame,
        which is what this used to do, kept the receiver on far longer than the beacon slot.

        A beacon window is a single shot window like any other: ``receive(continuous=True)``
        would latch the radio into Class C style continuous RX and leave it there for the
        rest of the simulation. Beacons go out with the network's own parameters, never
        with a multicast session's, so the receiver is put back on the unicast settings for
        the window.
        """
        deadline = open_at + duration
        retuned = self._rx_override_active()
        if retuned:
            self._apply_uplink_rx_config()
        try:
            while True:
                result = await self._rx_window(open_at, max(0.0, deadline - open_at))
                if result is None:
                    return None
                beacon_time = decode_beacon(result.payload)
                if beacon_time is not None:
                    return beacon_time
                # Not a beacon: keep listening until the window would have closed anyway.
                open_at = sim.current_time()
                if open_at >= deadline or not sim.is_running():
                    return None
        finally:
            if retuned:
                self._configure_radio()

    async def _class_b_beacon_scheduler(self) -> None:
        """Beacon acquisition and ping slot loop, one iteration per beacon period.

        Each iteration covers one beacon period:
        1. Sleep until the expected beacon time (if known).
        2. Open RX to receive the beacon broadcast.
        3. Decode the beacon timestamp and compute ping slot times.
        4. Open short RX windows at each ping slot.
        5. Repeat for the next beacon.

        If beacons are missed for ``MAX_BEACON_LESS_PERIOD`` seconds, the device
        reverts to Class A (beacon sync lost).
        """
        missed_beacons = 0

        while sim.is_running() and not self._class_b_stop.is_set():
            if self._beacon_time is not None:
                # Tracking: the next beacon is due one period after the last one seen (or
                # after the last one estimated, if some were missed). The receiver opens for
                # the beacon's reserved slot, plus a margin for a beacon the gateway could not
                # start on time because its transmitter was still busy with another frame.
                open_at = float(self._beacon_time + (missed_beacons + 1) * BEACON_INTERVAL)
                duration = BEACON_RESERVED + BEACON_LATE_TOLERANCE
            else:
                # Acquisition: the device knows nothing about the network's timing yet and
                # has to listen for up to a whole beacon period, as a real device does.
                open_at = sim.current_time()
                duration = BEACON_INTERVAL + BEACON_RESERVED

            try:
                beacon_time = await self._beacon_window(open_at, duration)
                if beacon_time is None:
                    raise TimeoutError

                self._beacon_time = beacon_time
                if not self._beacon_locked:
                    self._beacon_locked = True
                    logger.info(
                        f"{sim.current_time():.2f}s  DEVICE  beacon acquired "
                        f"time={beacon_time}"
                    )
                else:
                    logger.debug(
                        f"{sim.current_time():.2f}s  DEVICE  beacon received "
                        f"time={beacon_time}"
                    )
                missed_beacons = 0

                # Open ping slots for this beacon period
                await self._class_b_ping_slots(beacon_time)

            except TimeoutError:
                missed_beacons += 1
                if missed_beacons * BEACON_INTERVAL >= MAX_BEACON_LESS_PERIOD:
                    logger.warning(
                        f"{sim.current_time():.2f}s  DEVICE  beacon sync lost, "
                        f"reverting to Class A"
                    )
                    self.operating_mode = OperatingMode.CLASS_A
                    self._class_b_running = False
                    self._beacon_locked = False
                    return

                # Try ping slots with estimated timing if we had a previous lock
                if self._beacon_time is not None:
                    estimated = self._beacon_time + missed_beacons * BEACON_INTERVAL
                    await self._class_b_ping_slots(int(estimated))

    def collect_ping_slots(
        self, beacon_time: int,
    ) -> list[tuple[float, MulticastSession | None]]:
        """All ping slots the device should open in one beacon period.

        The device's own unicast ping slots (computed from its ``DevAddr``) are merged with
        the slots of every running Class B multicast session (computed from that group's
        ``McAddr``). Slots that fall within one slot length of each other cannot both be
        opened, so only the first survives; where a unicast and a multicast slot coincide the
        multicast one wins, as required by TS005 §2.8.

        Reference: LoRaWAN L2 1.0.4 §12.1 for the slot computation itself.

        :returns: ``(slot_time, session)`` pairs sorted by time, where *session* is None for
            a unicast slot.
        """
        entries: list[tuple[float, MulticastSession | None]] = []

        if self.session is not None:
            for slot_time in compute_ping_slot_times(
                beacon_time=beacon_time,
                dev_addr=self.session.dev_addr,
                ping_nb=self.ping_nb,
            ):
                entries.append((slot_time, None))

        for session in self._multicast_sessions.values():
            if not session.running or session.mode != OperatingMode.CLASS_B:
                continue
            if not session.ping_nb:
                continue
            for slot_time in compute_ping_slot_times(
                beacon_time=beacon_time,
                dev_addr=session.group_addr,
                ping_nb=session.ping_nb,
            ):
                entries.append((slot_time, session))

        # Sorting multicast ahead of unicast at identical times makes the de-duplication
        # below keep the multicast slot.
        entries.sort(key=lambda entry: (entry[0], entry[1] is None))

        merged: list[tuple[float, MulticastSession | None]] = []
        for entry in entries:
            if merged and entry[0] - merged[-1][0] < PING_SLOT_LEN:
                continue
            merged.append(entry)
        return merged

    async def _class_b_ping_slots(self, beacon_time: int) -> None:
        """Open RX at each scheduled ping slot within a beacon period."""
        guard_time = beacon_time + BEACON_INTERVAL - BEACON_GUARD

        for slot_time, session in self.collect_ping_slots(beacon_time):
            if self._class_b_stop.is_set() or not sim.is_running():
                break
            if slot_time >= guard_time:
                break
            if slot_time <= sim.current_time():
                continue  # Already past this slot

            # A unicast slot listens with the unicast parameters even while a multicast
            # session has retuned the receiver, and vice versa (TS005 §2.8).
            retuned = self._tune_for_slot(session)
            try:
                result = await self._rx_window(slot_time, PING_SLOT_LEN)
                if result is None:
                    continue  # Nothing in this slot
                # Ignore beacon frames that leak into ping slot windows
                if decode_beacon(result.payload) is not None:
                    continue
                await self._process_downlink(result.payload)
            except RuntimeError:
                # Radio busy (e.g., concurrent uplink TX)
                pass
            finally:
                if retuned:
                    self._configure_radio()

    def _tune_for_slot(self, session: MulticastSession | None) -> bool:
        """Tune the receiver for one ping slot; returns True if it has to be restored."""
        if session is None:
            if self._rx_override_active():
                self._apply_uplink_rx_config()
                return True
            return False
        if (self.rx_data_rate, self.rx_frequency) == (session.data_rate, session.frequency):
            return False
        dr = EU868_DATA_RATES[session.data_rate]
        self.radio.set_rx_config(
            spreading_factor=dr.spreading_factor.value,
            bandwidth=dr.bandwidth.to_khz(),
            iq_inverted=DOWNLINK_IQ_INVERTED,
            rx_continuous=False,
            frequency=session.frequency,
        )
        return True
