from __future__ import annotations
import logging
import random
from dataclasses import dataclass, field

from simulator.environment import simulation_env as sim
from simulator.lora.radio import LoraRadio
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
from simulator.lorawan.region import (
    EU868_DATA_RATES, RECEIVE_DELAY1, RECEIVE_DELAY2, MAX_FCNT,
    JOIN_ACCEPT_DELAY1, JOIN_ACCEPT_DELAY2,
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
    ):
        assert session is not None or otaa_credentials is not None, (
            "Provide either a session (ABP) or otaa_credentials (OTAA)"
        )
        self.radio = LoraRadio()
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
        self._configure_radio()

    def register_application(self, app: Application) -> None:
        """Register an application handler for a specific FPort."""
        port = app.port()
        assert 1 <= port <= 223, f"FPort must be 1-223, got {port}"
        self._applications[port] = app

    def _configure_radio(self) -> None:
        """Configure the radio for the current data rate."""
        dr = EU868_DATA_RATES[self.data_rate]
        self.radio.set_rx_config(
            spreading_factor=dr.spreading_factor.value,
            bandwidth=dr.bandwidth.to_khz(),
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

        # RX1 window at JOIN_ACCEPT_DELAY1
        await sim.sleep(JOIN_ACCEPT_DELAY1)
        try:
            result = await self.radio.receive_data_within(0.5)
            assert isinstance(result, LoraPacket)
            join_result = process_join_accept(
                result.payload, self._otaa_credentials, dev_nonce,
            )
            if join_result is not None:
                self._activate_from_join(join_result)
                return True
        except TimeoutError:
            pass

        # RX2 window at JOIN_ACCEPT_DELAY2
        await sim.sleep(JOIN_ACCEPT_DELAY2 - JOIN_ACCEPT_DELAY1 - 0.5)
        try:
            result = await self.radio.receive_data_within(0.5)
            assert isinstance(result, LoraPacket)
            join_result = process_join_accept(
                result.payload, self._otaa_credentials, dev_nonce,
            )
            if join_result is not None:
                self._activate_from_join(join_result)
                return True
        except TimeoutError:
            pass

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
            fctrl=FCtrl(),
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
        elif self.operating_mode == OperatingMode.CLASS_C:
            return await self._class_c_rx_windows()
        return False

    async def _class_a_rx_windows(self) -> bool:
        """Class A: two short RX windows after uplink."""
        # RX1
        await sim.sleep(RECEIVE_DELAY1)
        try:
            result = await self.radio.receive_data_within(0.5)
            assert isinstance(result, LoraPacket)
            await self._process_downlink(result.payload)
            return True
        except TimeoutError:
            pass

        # RX2
        await sim.sleep(RECEIVE_DELAY2 - RECEIVE_DELAY1 - 0.5)
        try:
            result = await self.radio.receive_data_within(0.5)
            assert isinstance(result, LoraPacket)
            await self._process_downlink(result.payload)
            return True
        except TimeoutError:
            pass

        return False

    async def _class_c_rx_windows(self) -> bool:
        """
            Class C: RX1 window, then continuous RX2 until next uplink.

            For now, just do RX1 + a brief RX2 check (continuous RX2 is managed
            externally by keeping the radio in RX mode between uplinks).
        """
        # RX1 window (same as Class A)
        await sim.sleep(RECEIVE_DELAY1)
        try:
            result = await self.radio.receive_data_within(0.5)
            assert isinstance(result, LoraPacket)
            await self._process_downlink(result.payload)
            return True
        except TimeoutError:
            pass

        # For Class C, the device returns to continuous RX2 after RX1 closes.
        # The caller is responsible for keeping the radio in RX mode between uplinks.
        await self.radio.receive(continuous=True)
        return False

    async def _process_downlink(self, raw: bytes) -> None:
        """Decode and process a received downlink frame."""
        assert self.session is not None
        try:
            phy = PHYPayload.decode_data(raw)
        except (AssertionError, Exception) as e:
            logger.warning(f"{sim.current_time():.2f}s  DEVICE  failed to decode downlink: {e}")
            return

        assert phy.mac_payload is not None
        mac = phy.mac_payload

        # Verify MIC
        mhdr_and_payload = raw[:-4]
        expected_mic = compute_data_mic(
            self.session.nwk_s_key,
            dev_addr=mac.fhdr.dev_addr,
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
        if mac.fhdr.fcnt >= self.session.fcnt_down:
            self.session.fcnt_down = mac.fhdr.fcnt + 1

        # Process MAC commands from FOpts
        if mac.fhdr.fopts:
            self._handle_mac_commands(parse_downlink_commands(mac.fhdr.fopts))

        # Process MAC commands from FPort 0 (encrypted with NwkSKey)
        if mac.fport == 0 and len(mac.frm_payload) > 0:
            decrypted = encrypt_frm_payload(
                self.session.nwk_s_key,
                dev_addr=mac.fhdr.dev_addr,
                fcnt=mac.fhdr.fcnt,
                uplink=False,
                payload=mac.frm_payload,
            )
            self._handle_mac_commands(parse_downlink_commands(decrypted))

        # Decrypt and route to application
        elif mac.fport is not None and mac.fport > 0 and len(mac.frm_payload) > 0:
            plaintext = encrypt_frm_payload(
                self.session.app_s_key,
                dev_addr=mac.fhdr.dev_addr,
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
