from __future__ import annotations
import asyncio
import logging
import random
from dataclasses import dataclass, field

from simulator.environment import simulation_env as sim
from simulator.lora.client_radio import LoraClientRadio
from simulator.lora.enums.radio_state import RadioState
from simulator.lora.packet import LoraPacket
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
    EU868_DATA_RATES, RECEIVE_DELAY1, RECEIVE_DELAY2, RX_WINDOW_DURATION,
    RX_WINDOW_GUARD, MAX_FCNT,
    JOIN_ACCEPT_DELAY1, JOIN_ACCEPT_DELAY2,
    BEACON_INTERVAL, BEACON_RESERVED, BEACON_GUARD,
    PING_SLOT_LEN, CLASS_B_DEFAULT_PING_NB, MAX_BEACON_LESS_PERIOD,
)


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
    """LoRaWAN multicast group session (downlink only).

    A multicast group uses a shared DevAddr and session keys. All devices in the
    group can decrypt downlinks addressed to this DevAddr. Multicast groups do not
    send uplinks — they are used for FUOTA and other broadcast scenarios.
    """
    group_addr: int
    nwk_s_key: bytes
    app_s_key: bytes
    fcnt_down: int = 0


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

    def __init__(
        self,
        session: DeviceSession | None = None,
        data_rate: int = 5,
        tx_power: int = 14,
        operating_mode: OperatingMode = OperatingMode.CLASS_A,
        otaa_credentials: OTAACredentials | None = None,
        ping_nb: int = CLASS_B_DEFAULT_PING_NB,
    ):
        assert session is not None or otaa_credentials is not None, (
            "Provide either a session (ABP) or otaa_credentials (OTAA)"
        )
        self.radio = LoraClientRadio()
        self.session = session
        self.data_rate = data_rate
        self.tx_power = tx_power
        self.operating_mode = operating_mode
        self.rx1_delay = RECEIVE_DELAY1
        self.battery_level: int = 255  # 0=external, 1-254=level, 255=unknown
        self._applications: dict[int, Application] = {}
        self._pending_mac_answers: list[MACCommand] = []
        self._otaa_credentials = otaa_credentials
        self._dev_nonce: int = 0
        self._multicast_groups: dict[int, MulticastGroup] = {}  # group_addr -> group
        self._class_c_running = False
        self._class_c_stop = asyncio.Event()
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

    def join_multicast_group(self, group: MulticastGroup) -> None:
        """Join a multicast group to receive group downlinks."""
        self._multicast_groups[group.group_addr] = group
        logger.debug(
            f"{sim.current_time():.2f}s  DEVICE  joined multicast group "
            f"0x{group.group_addr:08X}"
        )

    def leave_multicast_group(self, group_addr: int) -> None:
        """Leave a multicast group."""
        self._multicast_groups.pop(group_addr, None)

    async def switch_mode(self, mode: OperatingMode) -> None:
        """Switch operating mode at runtime.

        Handles starting/stopping the background tasks associated with
        Class B (beacon + ping slots) and Class C (continuous RX).
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
            self._stop_class_c_rx()

        # Start new-mode background tasks
        if mode == OperatingMode.CLASS_B and old_mode != OperatingMode.CLASS_B:
            await self._start_class_b()
        if mode == OperatingMode.CLASS_C and old_mode != OperatingMode.CLASS_C:
            await self._start_class_c_rx()

    def _configure_radio(self) -> None:
        """Configure the radio for the current data rate and operating mode."""
        dr = EU868_DATA_RATES[self.data_rate]
        self.radio.set_rx_config(
            spreading_factor=dr.spreading_factor.value,
            bandwidth=dr.bandwidth.to_khz(),
            # Only Class C devices keep their receiver running between windows. Class A and
            # Class B open short single shot windows instead, so their radio has to drop back
            # out of RX on its own once a packet arrives or a transmission finishes.
            rx_continuous=self.operating_mode == OperatingMode.CLASS_C,
        )
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
        result = await self._rx_window(tx_end + JOIN_ACCEPT_DELAY1, RX_WINDOW_DURATION)
        if result is not None:
            join_result = process_join_accept(
                result.payload, self._otaa_credentials, dev_nonce,
            )
            if join_result is not None:
                self._activate_from_join(join_result)
                return True

        # RX2 window at JOIN_ACCEPT_DELAY2
        result = await self._rx_window(tx_end + JOIN_ACCEPT_DELAY2, RX_WINDOW_DURATION)
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
        fopts = self._drain_mac_answers()
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

        # Transmit
        await self.radio.transmit_data_blocking(raw)
        self.session.fcnt_up += 1

        # Handle receive windows based on operating mode
        return await self._handle_rx_windows()

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

            :param open_at: Simulation timestamp at which the radio should be listening.
            :param duration: In seconds, how long to keep the window open.
            :returns: The received packet, or None when the window closed empty.
        """
        # Wake up the radio slightly before the window opens, to account for startup time and
        # clock drift.
        wake_at = open_at - RX_WINDOW_GUARD - self.radio.power_profile.standby_startup_time()
        if wake_at > sim.current_time():
            await sim.sleep_until(wake_at)
        await self.radio.standby()

        try:
            try:
                result = await self.radio.receive_data_within(duration + RX_WINDOW_GUARD)
            except TimeoutError:
                # The window only bounds how long the device waits for a preamble to show up.
                # Once it has locked onto one it keeps the receiver on until the frame is over,
                # so a downlink that starts just before the window closes still gets received.
                if not self.radio.carrier_sense_instant():
                    return None
                await self.radio.wait_for_channel_idle()
                result = await self.radio.receive_data_nowait()
                if result is None:
                    return None
            assert isinstance(result, LoraPacket)
            return result
        finally:
            if self.radio.get_state() != RadioState.TX:
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
        # go all the way back to sleep until RX1 opens.
        await self.radio.off()

        result = await self._rx_window(tx_end + RECEIVE_DELAY1, RX_WINDOW_DURATION)
        if result is not None:
            await self._process_downlink(result.payload)
            return True

        result = await self._rx_window(tx_end + RECEIVE_DELAY2, RX_WINDOW_DURATION)
        if result is not None:
            await self._process_downlink(result.payload)
            return True

        return False

    async def _class_c_rx_windows(self) -> bool:
        """Class C: RX1 window after uplink, then resume continuous RX2.

        The background Class C RX task handles unsolicited downlinks between
        uplinks. This method only handles the RX1 window after a TX.
        """
        # RX1 window (same as Class A, except the receiver never powers down)
        await sim.sleep(RECEIVE_DELAY1)
        try:
            result = await self.radio.receive_data_within(RX_WINDOW_DURATION)
            assert isinstance(result, LoraPacket)
            await self._process_downlink(result.payload)
            return True
        except TimeoutError:
            pass

        # Resume continuous RX2 — the background _class_c_rx_loop picks up from here
        await self.radio.receive(continuous=True)
        return False

    async def _start_class_c_rx(self) -> None:
        """Start the Class C continuous RX background task."""
        if self._class_c_running:
            return
        self._class_c_stop.clear()
        self._class_c_running = True
        await sim.start_child_task(self._class_c_rx_loop())
        self._class_c_task = True  # type: ignore[assignment]

    def _stop_class_c_rx(self) -> None:
        """Stop the Class C continuous RX background task."""
        if not self._class_c_running:
            return
        self._class_c_stop.set()
        self._class_c_running = False

    async def _class_c_rx_loop(self) -> None:
        """Background task: continuously listen for downlinks (Class C).

        Runs until the simulation ends or the device switches away from Class C.
        Processes any received downlink immediately (both unicast and multicast).
        """
        await self.radio.receive(continuous=True)
        while sim.is_running() and not self._class_c_stop.is_set():
            try:
                result = await self.radio.receive_data_within(1.0)
                assert isinstance(result, LoraPacket)
                await self._process_downlink(result.payload)
            except TimeoutError:
                pass

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
            if mac.fhdr.fcnt >= mc_group.fcnt_down:
                mc_group.fcnt_down = mac.fhdr.fcnt + 1
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
            # If we have timing info, sleep until just before the expected
            # beacon so we don't time out too early after ping slots.
            if self._beacon_time is not None:
                expected = float(
                    self._beacon_time
                    + (missed_beacons + 1) * BEACON_INTERVAL
                )
                wake_at = expected - 1.0
                if wake_at > sim.current_time():
                    await sim.sleep_until(wake_at)

            try:
                # A beacon window is a single shot window like any other: `receive(continuous=
                # True)` would latch the radio into Class C style continuous RX and leave it
                # there for the rest of the simulation.
                result = await self._rx_window(
                    sim.current_time(), BEACON_RESERVED + 2.0
                )
                if result is None:
                    raise TimeoutError
                beacon_time = decode_beacon(result.payload)
                if beacon_time is None:
                    continue  # Not a beacon frame; keep listening

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

    async def _class_b_ping_slots(self, beacon_time: int) -> None:
        """Open RX at each scheduled ping slot within a beacon period."""
        assert self.session is not None

        slot_times = compute_ping_slot_times(
            beacon_time=beacon_time,
            dev_addr=self.session.dev_addr,
            ping_nb=self.ping_nb,
        )

        guard_time = beacon_time + BEACON_INTERVAL - BEACON_GUARD

        for slot_time in slot_times:
            if self._class_b_stop.is_set():
                break
            if slot_time >= guard_time:
                break
            if slot_time <= sim.current_time():
                continue  # Already past this slot

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
