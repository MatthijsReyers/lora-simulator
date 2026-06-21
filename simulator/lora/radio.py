from abc import ABC
import asyncio, logging
from typing import Any, Dict, List, Optional, Tuple
import pandas as pd
import random

from simulator.exceptions import SimulatorException
from simulator.lora.airtime import symbol_airtime
from simulator.lora.enums.code_rate import CodeRate
from simulator.lora.enums.spreading_factor import SpreadingFactor
from simulator.lora.enums.bandwidth import Bandwidth
from simulator.lora.enums.radio_state import RadioState
from simulator.lora.packet import LoraPacket
from simulator.environment import simulation_env as sim
from simulator.lora.packet_metadata import PacketMetadata
from simulator.lora.radio_config import LoraConfig
from simulator.lora.radio_power_profile import RadioPowerProfile, Stm32wl55PowerProfile
from simulator.power_consumer import PowerConsumer
from simulator.queue import Queue

# Global radio counter for unique radio IDs
_radio_id_counter = 0

class AlreadyTransmittingException(SimulatorException):
    """Exception raised when attempting to transmit while already transmitting."""
    def __init__(self):
        super().__init__(
            "Radio is already transmitting."
        )

class LoraRadio(ABC):
    __RECEIVE_PROCESS_DELAY = 0.0001

    __radio_state: RadioState
    __packets_in_transit: dict[int, PacketMetadata]
    __rx_queue: Queue[Tuple[LoraPacket, PacketMetadata]]
    
    __rx_config: LoraConfig
    __rx_continuous: bool = True

    __tx_config: LoraConfig
    __tx_power: int = 14  # in dBm
    # __tx_freq_hop_enable: bool = False
    # __tx_freq_hop_period: int = 0
    # __tx_timeout: int = 3_000  # in milliseconds

    _radio_id: int

    position: tuple[float, float]
    logger: logging.Logger
    power_consumer: PowerConsumer

    __state_log: List[Tuple[float, RadioState]]
    __packets_log: Dict[str, Any]


    def __init__(
            self, 
            position: tuple[float, float] = (0.0, 0.0), 
            power_profile: RadioPowerProfile = Stm32wl55PowerProfile(),
        ):
        self.__packets_in_transit = {}
        self.__rx_queue = Queue()
        self.__idle_waiters: list[asyncio.Event] = []
        self.position = position
        self.__radio_state = RadioState.OFF
        self.__rx_config = LoraConfig()
        self.__tx_config = LoraConfig()

        global _radio_id_counter
        _radio_id_counter += 1
        self._radio_id = _radio_id_counter

        self.logger = logging.getLogger(f"LoraRadio-{self._radio_id}")

        self.power_consumer = PowerConsumer()
        self.power_profile = power_profile

        self.__state_log = []
        self.__packets_log = {
            "id": [],
            "time": [],
            "snr": [],
            "rssi": [],
            "collision": [],
            "missed_start": [],
            "missed_end": [],
            "interrupted": [],
        }

        # Prevents circular import
        from simulator.lora.phy_layer import LoraPhyLayer
        self.phy = LoraPhyLayer()
        self.phy.subscribe(self)

        sim.create_task(self.__on_sim_end())


    @property
    def rx_config(self) -> LoraConfig:
        return self.__rx_config.copy()
    
    @property
    def rx_continuous(self) -> bool:
        return self.__rx_continuous
    
    @property
    def tx_config(self) -> LoraConfig:
        return self.__tx_config.copy()

    @property
    def tx_power(self) -> int:
        return self.__tx_power


    async def __on_sim_end(self):
        await sim.wait_for_sim_end()
        self.packets_log = pd.DataFrame(self.__packets_log)


    def get_state(self) -> RadioState:
        return self.__radio_state


    def _set_state(self, state: RadioState) -> None:
        self.logger.debug(f"radio={self._radio_id} set_state(state={state})")
        self.__radio_state = state
        self.__state_log.append((sim.current_time(), state))
        match state:
            case RadioState.OFF:
                power = self.power_profile.disabled_power()
            case RadioState.RX:
                power = self.power_profile.rx_power(self.__rx_config)
            case RadioState.TX:
                power = self.power_profile.tx_power(self.__tx_power, self.__tx_config)
            case RadioState.STANDBY:
                power = self.power_profile.standby_power()
        self.power_consumer.set_power_consumption(power)


    async def standby(self) -> None:
        """
            Puts the radio into standby mode.
        """
        self.logger.debug(f"radio={self._radio_id} standby()")
        if self.__radio_state != RadioState.STANDBY:
            self._set_state(RadioState.STANDBY)
            await sim.sleep(self.power_profile.standby_startup_time())


    async def off(self) -> None:
        """
            Turns the radio off.
        """
        self.logger.debug(f"radio={self._radio_id} off()")
        if self.__radio_state != RadioState.OFF:
            self._set_state(RadioState.OFF)


    async def receive(self, continuous: bool = True) -> None:
        """
            Puts the radio into receive mode without blocking or waiting for packets. The other
            blocking receive methods already put the radio into receive mode so this method is only
            needed to put the radio into receive mode after transmitting data.
        """
        self.logger.debug(f"radio={self._radio_id} receive(continuous={continuous})")

        if self.__radio_state == RadioState.TX:
            raise RuntimeError("Cannot enter receive mode while radio is transmitting.")

        if self.__radio_state == RadioState.OFF:
            await self.standby()

        self._set_state(RadioState.RX)
        self.__rx_continuous = continuous


    async def carrier_sense(self) -> bool:
        """
            Performs carrier sensing to determine if the channel is currently busy.

            :returns: True if the channel is busy, False otherwise.
        """
        self.logger.debug(f"radio={self._radio_id} carrier_sense()")

        old_state = self.__radio_state

        if self.__radio_state == RadioState.OFF:
            await self.standby()
        if self.__radio_state == RadioState.TX:
            raise RuntimeError("Cannot perform carrier sensing while radio is transmitting.")
        if self.__radio_state != RadioState.RX:
            self._set_state(RadioState.RX)

        sense_time = symbol_airtime(self.__rx_config.spreading_factor, self.__rx_config.bandwidth) * 6
        await sim.sleep(sense_time)

        medium_busy = self.carrier_sense_instant()

        # Go back to whatever state the radio was in before carrier sensing started.
        self._set_state(old_state)

        return medium_busy
    

    def carrier_sense_instant(self) -> bool:
        """
            Unrealistic ideal instantaneous carrier sense that just checks if there are any packets
            currently being received with matching parameters. This is not how real carrier sensing
            works but it can be useful for testing or implementing idealized protocols that assume
            perfect carrier sensing.

            :returns: True if the channel is busy, False otherwise.
        """

        packets = [ 
            p for p in self.__packets_in_transit.values() if (
                p.packet.config.spreading_factor == self.__rx_config.spreading_factor and 
                p.packet.config.bandwidth == self.__rx_config.bandwidth
            )
        ]
        return len(packets) > 0


    async def wait_for_channel_idle(self) -> None:
        """
            Block until the channel is idle (no matching packets in transit).

            This is an event-driven wait — it does NOT poll. When the last
            in-transit packet finishes, all waiters are woken on the same
            simulation tick. This is useful for modelling idealized CSMA
            protocols with zero propagation delay (a ≈ 0).
        """
        while self.carrier_sense_instant():
            event = asyncio.Event()
            self.__idle_waiters.append(event)
            await sim.schedule_event_wait(event, sim.last_tick())
            # Clean up in case we were woken by the simulation-end timeout
            # rather than by the idle notification in _on_receive_end.
            if event in self.__idle_waiters:
                self.__idle_waiters.remove(event)


    async def receive_data_nowait(
            self, metadata: bool = False
        ) -> Optional[LoraPacket | Tuple[LoraPacket, PacketMetadata]]:
        """
            Tries to receive data from the modem if it is available, immediately returns None if no
            data is available in the receive queue. Note that this call does not put the radio in
            receive mode.

            :param metadata: If true, also returns the PacketMetadata along with the LoraPacket.
        """
        self.logger.debug(f"radio={self._radio_id} receive_data()")

        try:
            (packet, meta) = await self.__rx_queue.get_timeout(0)
            await sim.sleep(self.__RECEIVE_PROCESS_DELAY)
            if metadata: 
                return (packet, meta)
            return packet
        except asyncio.QueueEmpty:
            return None


    async def receive_data_wait(
            self, metadata: bool = False
        ) -> LoraPacket | Tuple[LoraPacket, PacketMetadata]:
        """
            Blocking wait that does not return until some data is received (correctly) by the radio.
            Note that if a packet is already in the reception queue it does not put the radio in 
            receive mode.

            :param metadata: If true, also returns the PacketMetadata along with the LoraPacket.
        """
        self.logger.debug(f"radio={self._radio_id} receive_data_wait()")

        # Has a packet already been received?
        try: 
            (packet, meta) = await self.__rx_queue.get_timeout(0)
            if metadata: 
                return (packet, meta)
            return packet
        except asyncio.TimeoutError: pass
        
        if self.__radio_state == RadioState.TX:
            raise RuntimeError("Cannot receive data while radio is transmitting.")

        if self.__radio_state == RadioState.OFF:
            await self.standby()

        if self.__radio_state != RadioState.RX:
            self._set_state(RadioState.RX)
        
        (packet, meta) = await self.__rx_queue.get()
        await sim.sleep(self.__RECEIVE_PROCESS_DELAY)
        if metadata: 
            return (packet, meta)
        return packet


    async def receive_data_within(
            self, timeout: float, metadata: bool = False
        ) -> LoraPacket | Tuple[LoraPacket, PacketMetadata]:
        """
            Waits for the given amount of time until some data is received over the radio and
            throws a TimeoutError if no data is received within that time.

            :param timeout: In seconds, time to wait for a packet to be received.
            :param metadata: If true, also returns the PacketMetadata along with the LoraPacket.
        """
        self.logger.debug(f"receive_data_within(timeout={timeout})")

        # Has a packet already been received?
        try: 
            (packet, meta) = await self.__rx_queue.get_timeout(0)
            if metadata: 
                return (packet, meta)
            return packet
        except asyncio.TimeoutError: pass

        if self.__radio_state == RadioState.TX:
            raise RuntimeError("Cannot receive data while radio is transmitting.")

        if self.__radio_state == RadioState.OFF:
            await self.standby()

        if self.__radio_state != RadioState.RX:
            self._set_state(RadioState.RX)

        await sim.sleep(self.__RECEIVE_PROCESS_DELAY)

        (packet, meta) = await self.__rx_queue.get_timeout(timeout)
        if metadata: 
            return (packet, meta)
        return packet


    async def transmit_data(self, data: bytes) -> asyncio.Event:
        """ 
            Transmit data asynchronously. Returns an asyncio.Event that is set when the
            transmission is complete. Throws AlreadyTransmittingException if the radio is already
            currently transmitting.
        """
        assert isinstance(data, bytes), "Data to transmit must be bytes"

        if self.__radio_state == RadioState.TX:
            raise AlreadyTransmittingException()

        tx_finished_event = asyncio.Event()

        async def transmit_task():
            await self.transmit_data_blocking(data)
            tx_finished_event.set()

        await sim.start_child_task(transmit_task())

        return tx_finished_event


    async def transmit_data_blocking(self, data: bytes):
        """
            Transmit data to other radios, This call does not return until the transmission is
            complete. Throws AlreadyTransmittingException if the radio is already currently
            transmitting (via non-blocking method).
        """
        assert isinstance(data, bytes), "Data to transmit must be bytes"

        if self.__radio_state == RadioState.TX:
            raise AlreadyTransmittingException()

        # TODO: Technically one could keep track of preambles and delay TX until RX is done, but
        # that's not how most radios work so for now we just interrupt any ongoing receptions.
        if self.__radio_state == RadioState.RX:
            for meta in self.__packets_in_transit.values():
                meta.interrupted = True

        if self.__radio_state == RadioState.OFF:
            await self.standby()

        self._set_state(RadioState.TX)

        packet = LoraPacket(
            payload=data,
            tx_power=self.__tx_power,
            tx_location=self.position,
            config=self.__tx_config.copy(),
        )

        # Prevents circular import
        from simulator.lora.phy_layer import LoraPhyLayer

        phy_layer = LoraPhyLayer()
        airtime = await phy_layer.transmit_packet_blocking(self, packet)

        if self.__rx_continuous:
            self._set_state(RadioState.RX)
        else:
            await self.standby()
        
        return airtime


    def set_rx_config(
        self,
        bandwidth: Bandwidth|int = Bandwidth.KHz125,
        spreading_factor: SpreadingFactor|int = SpreadingFactor.SF7,
        code_rate: CodeRate|int = CodeRate.CR4_5,
        preamble_len: int = 8,
        max_payload_len: int = 64,
        symbols: int = 0,
        fixed_payload_len: bool = False,
        crc_enabled: bool = True,
        iq_inverted: bool = False,
        rx_continuous: bool = True,
    ):
        """
            Sets the radio's receive configuration. This method is designed to mimic the RX config
            method of the STM32WLX5 HAL library and uses the same default values.
            
            :param bandwidth: LoRa bandwidth (e.g., 125 kHz, 250 kHz, 500 kHz)
            :param spreading_factor: LoRa spreading factor (e.g., SF7, SF8, SF9)
            :param code_rate: LoRa code rate (e.g., 4/5, 4/6, 4/7, 4/8) used by incoming packets
                note that this is only relevant when using implicit mode (i.e. when you set 
                `fixed_payload_len` to true), otherwise the packet header will indicate the used
                code rate.
            :param preamble_len: Preamble length in number of symbols
            :param max_payload_len: Longest payload length the radio should expect to receive
            :param symbols: Description
            :param fixed_payload_len: Does the radio expect fixed length payloads? I.e. will the 
                received packet have a header indicating their length, or is the length known 
                ahead of time? (Set expected length with `max_payload_len` parameter)
            :param crc_enabled: Should the radio expect incoming packets to have a CRC?
            :param iq_inverted: Description
            :param rx_continuous: Should the radio remain in receive mode until explicitly turned
                off?
        """
        if type(bandwidth) is int:
            bandwidth = Bandwidth.from_khz(bandwidth)
        if type(spreading_factor) is int:
            spreading_factor = SpreadingFactor(spreading_factor)
        if type(code_rate) is int:
            code_rate = CodeRate.from_denominator(code_rate)

        assert isinstance(bandwidth, Bandwidth), "Invalid bandwidth"
        assert isinstance(spreading_factor, SpreadingFactor), "Invalid spreading factor"
        assert isinstance(code_rate, CodeRate), "Invalid code rate"
        assert preamble_len > 0, "Preamble length must be positive"
        assert preamble_len <= 0xFFFF, "Preamble length must fit in u16"
        assert max_payload_len >= 0, "Payload length must be non-negative"
        assert type(crc_enabled) is bool, "CRC enabled must be a boolean"
        assert type(iq_inverted) is bool, "IQ inverted must be a boolean"
        assert type(rx_continuous) is bool, "RX continuous must be a boolean"
        
        self.__rx_config = LoraConfig(
            bandwidth=bandwidth,
            spreading_factor=spreading_factor,
            code_rate=code_rate,
            preamble_len=preamble_len,
            payload_len=max_payload_len,
            symbols=symbols,
            fixed_payload_len=fixed_payload_len,
            crc_enabled=crc_enabled,
            iq_inverted=iq_inverted
        )
        self.__rx_continuous = rx_continuous

        # Changing the radio config messes up the reception of all packets in transit
        for meta in self.__packets_in_transit.values():
            meta.interrupted = True


    def set_tx_config(
        self,
        power: int,
        bandwidth: Bandwidth|int = Bandwidth.KHz125,
        spreading_factor: SpreadingFactor|int = SpreadingFactor.SF7,
        code_rate: CodeRate|int = CodeRate.CR4_5,
        preamble_len: int = 8,
        fixed_len: bool = False,
        crc_enable: bool = True,
        freq_hop_enable: bool = False,
        freq_hop_period: int = 0,
        iq_inverted: bool = False,
        timeout: int = 3_000,
    ):
        if type(bandwidth) is int:
            bandwidth = Bandwidth.from_khz(bandwidth)
        if type(spreading_factor) is int:
            spreading_factor = SpreadingFactor(spreading_factor)
        if type(code_rate) is int:
            code_rate = CodeRate.from_denominator(code_rate)

        assert isinstance(bandwidth, Bandwidth), "Invalid bandwidth"
        assert isinstance(spreading_factor, SpreadingFactor), "Invalid spreading factor"
        assert isinstance(code_rate, CodeRate), "Invalid code rate"
        assert preamble_len > 0, "Preamble length must be positive"
        assert preamble_len <= 0xFFFF, "Preamble length must fit in u16"
        assert type(fixed_len) is bool, "Fixed length must be a boolean"
        assert type(crc_enable) is bool, "CRC enable must be a boolean"
        assert type(freq_hop_enable) is bool, "Frequency hop enable must be a boolean"
        assert freq_hop_period >= 0, "Frequency hop period must be non-negative"
        assert type(iq_inverted) is bool, "IQ inverted must be a boolean"
        assert timeout >= 0, "Timeout must be non-negative"

        if freq_hop_enable:
            raise NotImplementedError("Frequency hopping is not yet implemented.")

        self.__tx_power = power
        self.__tx_config = LoraConfig(
            bandwidth=bandwidth,
            spreading_factor=spreading_factor,
            code_rate=code_rate,
            preamble_len=preamble_len,
            fixed_payload_len=fixed_len,
            crc_enabled=crc_enable,
            iq_inverted=iq_inverted
        )
        self.__tx_freq_hop_enable = freq_hop_enable
        self.__tx_freq_hop_period = freq_hop_period
        self.__tx_timeout = timeout


    def _on_receive_start(self, packet: LoraPacket):
        """
            Called when the radio begins receiving a packet. This is a callback function used by the 
            PHY layer to notify the radio of incoming packets, end users should never be calling
            this directly.
        """
        self.logger.debug(f"{sim.current_time():.4f} radio={self._radio_id} _on_receive_start(packet={packet.id}, rssi={packet.rssi})")

        # Is the radio turned on and able to receive the packets?
        missed_start = not self.__can_receive(packet)

        # Does this packet overlap with any other packets currently being sent?
        possible_collisions = self.__packets_in_transit.values()

        # Is the radio able to distinguish this packet from the noise floor?
        required = self.__rx_config.spreading_factor.minimum_snr()
        demodulate_failure = packet.snr < required

        # Overlapping packets do not have to collide if they use different enough parameters
        found_collision = False
        for meta in possible_collisions:
            (fst, snd) = self.__capture_effect(meta.packet, packet)
            if not fst:
                meta.collision = True
            if not snd:
                found_collision = True

        # Add this packet to the list of packets in transit
        self.__packets_in_transit[packet.id] = PacketMetadata(   
            packet=packet,
            collision=found_collision,
            missed_start=missed_start,
            demodulate_failure=demodulate_failure,
        )


    async def _on_receive_preamble(self, packet_id: int):
        """
            Called when the radio detects the preamble of a packet.
        """
        self.logger.debug(f"{sim.current_time():.4f} radio={self._radio_id} _on_receive_preamble({packet_id})")
        # TODO: cancel RX timeout for non-continuous RX mode after preamble is detected
        assert packet_id in self.__packets_in_transit, f"radio={self._radio_id} BUG: Packet {packet_id} not found in transit?"
        metadata = self.__packets_in_transit[packet_id]
        metadata.received_preamble = (
            not metadata.missed_start and 
            not metadata.demodulate_failure and 
            not metadata.collision and
            not metadata.interrupted
        )


    async def _on_receive_end(self, packet_id: int):
        """
            Called when the radio finishes receiving a whole packet.
        """
        self.logger.debug(f"{sim.current_time():.4f} radio={self._radio_id} _on_receive_end({packet_id})")
        assert packet_id in self.__packets_in_transit, f"radio={self._radio_id} BUG: Packet {packet_id} not found in transit?"

        # Remove packet from in transit list
        metadata: PacketMetadata = self.__packets_in_transit.pop(packet_id)
        metadata.missed_end = (not self.__can_receive(metadata.packet))

        # Log packet metadata for later analysis
        self.__log_packet_metadata(metadata)

        # Did we successfully receive the whole packet?
        if metadata.received_successfully():
            self.logger.debug(f"putting packet into rx queue: {metadata.packet}")
            await self.__rx_queue.put((metadata.packet, metadata))

            # Turn off the radio if it was not in continuous receive mode
            if self.__radio_state == RadioState.RX and not self.__rx_continuous:
                self._set_state(RadioState.OFF)

        else:
            self.logger.debug(f"radio={self._radio_id} packet lost due to {metadata}")

        # Notify any coroutines waiting for the channel to become idle.
        if self.__idle_waiters and not self.carrier_sense_instant():
            for waiter in self.__idle_waiters:
                await sim.schedule_event_no_await(waiter, sim.next_tick())
            self.__idle_waiters.clear()


    def __can_receive(self, packet: LoraPacket) -> bool:
        """
            Determines whether the radio can receive the given packet based on its current
            configuration.
        """
        # Is the radio even in receive mode?
        if self.__radio_state != RadioState.RX: 
            return False
        
        if packet.config.code_rate != self.__rx_config.code_rate:
            self.logger.info(
                f"radio={self._radio_id} cannot receive packet {packet.id} due to code rate \
                    mismatch: {packet.config.code_rate} != {self.__rx_config.code_rate}"
            )
            return False
        
        if packet.config.spreading_factor != self.__rx_config.spreading_factor:
            self.logger.info(
                f"radio={self._radio_id} cannot receive packet {packet.id} due to SF \
                    mismatch: {packet.config.spreading_factor} != {self.__rx_config.spreading_factor}"
            )
            return False
        
        if packet.config.bandwidth != self.__rx_config.bandwidth:
            self.logger.info(
                f"radio={self._radio_id} cannot receive packet {packet.id} due to BW \
                    mismatch: {packet.config.bandwidth} != {self.__rx_config.bandwidth}"
            )
            return False

        if packet.config.crc_enabled != self.__rx_config.crc_enabled:
            self.logger.info(
                f"radio={self._radio_id} cannot receive packet {packet.id} due to CRC \
                    enabled mismatch: {packet.config.crc_enabled} != {self.__rx_config.crc_enabled}"
            )
            return False
        
        if packet.config.fixed_payload_len != self.__rx_config.fixed_payload_len:
            self.logger.info(
                f"radio={self._radio_id} cannot receive packet {packet.id} due to fixed \
                    length mode mismatch: {packet.config.fixed_payload_len} != {self.__rx_config.fixed_payload_len}"
            )
            return False
        
        if packet.config.preamble_len != self.__rx_config.preamble_len:
            self.logger.info(
                f"radio={self._radio_id} cannot receive packet {packet.id} due to \
                    preamble length mismatch: {packet.config.preamble_len} != {self.__rx_config.preamble_len}"
            )
            return False
        
        if packet.config.iq_inverted != self.__rx_config.iq_inverted:
            self.logger.info(
                f"radio={self._radio_id} cannot receive packet {packet.id} due to IQ \
                    inversion mismatch: {packet.config.iq_inverted} != {self.__rx_config.iq_inverted}"
            )
            return False
        
        # In implicit mode, we need to check more things since the packet does not have a header to
        # indicate its parameters.
        if self.__rx_config.fixed_payload_len:
            
            if len(packet.payload) != self.__rx_config.payload_len:
                self.logger.info(
                    f"radio={self._radio_id} cannot receive packet {packet.id} due to \
                        payload length mismatch: {len(packet.payload)} != {self.__rx_config.payload_len}"
                )
                return False
            
            if packet.config.code_rate != self.__rx_config.code_rate:
                self.logger.info(
                    f"radio={self._radio_id} cannot receive packet {packet.id} due to \
                        code rate mismatch: {packet.config.code_rate} != {self.__rx_config.code_rate}"
                )
                return False
        
        return True
    

    def __capture_effect(self, first: LoraPacket, second: LoraPacket) -> Tuple[bool, bool]:
        """
            Determines whether the second packet collides with the first packet based on their 
            TX parameters.

            Note: this method assumes that packets do actually overlap in time.
        """
        # If the packets have different bandwidths they won't interfere with each other
        if first.config.bandwidth != second.config.bandwidth:
            return (False, False)
        
        # Traditional SEMTech collision rules if one packet is this much stronger than the other
        # we can definitely demodulate the stronger packet.
        # https://privatevideos.hubs.vidyard.com/watch/iXBL8d2mjyjubK8DhuGcKq
        power_delta = first.snr - second.snr
        if first.config.spreading_factor == second.config.spreading_factor:
            if power_delta > 6.0:
                return (True, False)
        else:
            snir = first.config.spreading_factor.minimum_snr()
            if power_delta > snir:
                return (True, False)
            
        # Detailed capture effect calculations can be disabled for performance or when not needed,
        # in which case we just assume all overlapping packets are lost.
        if not self.phy.enable_capture_effect:
            return (False, False)

        uninterrupted_t = first.symbol_t * 6

        # See: LoRa Scalability: A Simulation Model Based on Interference Measurements (2017)
        # Measurements show that the probability of the second packet being received successfully
        # relates direct to how much of the preamble of the second packet is received without 
        # interference from the first packet.
        p1_end = first.tx_start + first.airtime
        p2_start = second.tx_start

        interfered_t = p1_end - (p2_start + uninterrupted_t)
        interfered_ratio = interfered_t / uninterrupted_t
        snd = interfered_ratio < random.random()

        # Is the first packet stronger or equal in reception to the second?
        if first.rssi >= second.rssi:

            # At least 6 symbols of the preamble of the first packet must be received without collision
            # in order to still have any chance of demodulating the first packet.
            # See: LoRa Scalability: A Simulation Model Based on Interference Measurements (2017)
            if first.tx_start + uninterrupted_t < second.tx_start:
                fst = random.random() < 0.95  # 95% chance to receive the first packet successfully
                return (fst, snd)
            else:
                return (False, snd)
            
        # Second packet is stronger than the first.
        if snd: return (False, True)

        # The second packet may be received if it interrupts the header and causes the header
        # CRC of the first packet to fail, giving the radio a chance to sync on the second packet 
        # instead of the first.
        if not first.config.fixed_payload_len:

            header_start = first.tx_start + first.preamble_airtime
            header_end = header_start + first.header_airtime
            if second.tx_start < header_end:

                # Same calculation as before now, but we use the end of the header as the "end" of
                # the first packet..
                interfered_t = header_end - (p2_start + uninterrupted_t)
                interfered_ratio = interfered_t / uninterrupted_t
                snd = interfered_ratio < random.random()

                return (False, snd)
            
        return (False, snd)


    def __log_packet_metadata(self, metadata: PacketMetadata):
        """
            Logs the metadata of a received packet for later analysis.
        """
        self.__packets_log["id"].append(metadata.packet.id)
        self.__packets_log["time"].append(sim.current_time())
        self.__packets_log["snr"].append(metadata.packet.snr)
        self.__packets_log["rssi"].append(metadata.packet.rssi)
        self.__packets_log["collision"].append(metadata.collision)
        self.__packets_log["missed_start"].append(metadata.missed_start)
        self.__packets_log["missed_end"].append(metadata.missed_end)
        self.__packets_log["interrupted"].append(metadata.interrupted)
