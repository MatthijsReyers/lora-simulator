from __future__ import annotations
import logging
from dataclasses import dataclass

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
from simulator.lorawan.region import (
    EU868_DATA_RATES, RECEIVE_DELAY1, RECEIVE_DELAY2, MAX_FCNT,
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

        Currently supports ABP (Activation by Personalization). OTAA will be added in
        a later phase.

        Reference: LoRaWAN L2 1.0.4 Specification chapter 3 & 4.
    """

    def __init__(
        self,
        session: DeviceSession,
        data_rate: int = 5,
        tx_power: int = 14,
        operating_mode: OperatingMode = OperatingMode.CLASS_A,
    ):
        self.radio = LoraRadio()
        self.session = session
        self.data_rate = data_rate
        self.tx_power = tx_power
        self.operating_mode = operating_mode
        self._applications: dict[int, Application] = {}
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
        fhdr = FHDR(
            dev_addr=self.session.dev_addr,
            fctrl=FCtrl(),
            fcnt=self.session.fcnt_up,
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

        # Decrypt and route to application
        if mac.fport is not None and mac.fport > 0 and len(mac.frm_payload) > 0:
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
