from abc import ABC, abstractmethod
import asyncio, logging
from typing import Optional

from simulator.lora.enums.code_rate import CodeRate
from simulator.lora.enums.spreading_factor import SpreadingFactor
from simulator.lora.enums.bandwidth import Bandwidth
from simulator.lora.enums.radio_state import RadioState
from simulator.lora.packet import LoraPacket
from simulator.environment import simulation_env as sim
from simulator.queue import Queue

# Global radio counter for unique radio IDs
_radio_id_counter = 0

class LoraRadio(ABC):
    class PacketMetadata:
        """ Radio specific metadata about a packet. """
        def __init__(
            self, 
            packet: LoraPacket, 
            rssi: float, 
            snr: float, 
            collision: bool, 
            missed_start: bool
        ):
            self.packet = packet
            self.rssi = rssi
            self.snr = snr
            self.collision = collision
            self.missed_start = missed_start
            self.missed_end = False
            self.interrupted = False

        def __repr__(self):
            status = []
            if self.collision:
                status.append("collision")
            if self.missed_start:
                status.append("missed_start")
            if self.interrupted:
                status.append("interrupted")
            if self.missed_end:
                status.append("missed_end")
            status_str = ", ".join(status) if status else "successful"
            return f"PacketMetadata(status={status_str})"
        
        def received_successfully(self) -> bool:
            return not (self.collision or self.missed_start or self.missed_end or self.interrupted)


    __RECEIVE_PROCESS_DELAY = 0.0001

    __radio_state: RadioState
    __packets_in_transit: dict[int, PacketMetadata]
    __rx_queue: Queue
    
    __rx_bandwidth: Bandwidth = Bandwidth.KHz125
    __rx_spreading_factor: SpreadingFactor = SpreadingFactor.SF7
    __rx_code_rate: CodeRate = CodeRate.CR4_5
    __rx_preamble_len: int = 8
    __rx_payload_len: int = 64
    __rx_symbols: int = 0
    __rx_fixed_payload_len: bool = False
    __rx_crc_enabled: bool = True
    __rx_iq_inverted: bool = False
    __rx_continuous: bool = True

    __tx_power: int = 14  # in dBm
    __tx_bandwidth: Bandwidth = Bandwidth.KHz125
    __tx_spreading_factor: SpreadingFactor = SpreadingFactor.SF7
    __tx_code_rate: CodeRate = CodeRate.CR4_5
    __tx_preamble_len: int = 8
    __tx_fixed_len: bool = False
    __tx_crc_enable: bool = True
    __tx_freq_hop_enable: bool = False
    __tx_freq_hop_period: int = 0
    __tx_iq_inverted: bool = False
    __tx_timeout: int = 3_000  # in milliseconds

    _radio_id: int

    position: tuple[float, float]
    logger: logging.Logger

    # [time, event_type, radio, packet_id, details]
    events: list[list[float, str, int, int|None, str|None]] = []
    
    def __init__(self, position: tuple[float, float] = (0.0, 0.0)):
        self.__packets_in_transit = {}
        self.__rx_queue = Queue()
        self.position = position
        self.__radio_state = RadioState.OFF

        global _radio_id_counter
        _radio_id_counter += 1
        self._radio_id = _radio_id_counter

        self.logger = logging.getLogger(f"LoraRadio-{self._radio_id}")

        # Prevents circular import
        from simulator.lora.phy_layer import LoraPhyLayer
        phy = LoraPhyLayer()
        phy.subscribe(self)


    def get_state(self) -> RadioState:
        return self.__radio_state
    

    def _set_state(self, state: RadioState) -> None:
        self.logger.debug(f"radio={self._radio_id} set_state(state={state})")
        self.__radio_state = state
        self.events.append([
            sim.current_time(), "state_change", self._radio_id, None, state
        ])


    def receive(self, continuous: bool = True) -> None:
        """
            Puts the radio into receive mode without blocking or waiting for packets. The other
            blocking receive methods already put the radio into receive mode so this method is only
            needed to put the radio into receive mode after transmitting data.
        """
        self.logger.debug(f"radio={self._radio_id} receive(continuous={continuous})")
        if not continuous:
            raise NotImplementedError("Non-continuous receive mode is not yet implemented.")
        self._set_state(RadioState.RX)
        self.__rx_continuous = continuous


    async def receive_data_nowait(self) -> Optional[LoraPacket]:
        """
            Tries to receive data from the modem if it is available, immediately returns None if no
            data is available in the receive queue.
        """
        self.logger.debug(f"receive_data()")
        if self.__radio_state == RadioState.OFF:
            self._set_state(RadioState.RX)
        try:
            packet = await self.__rx_queue.get_timeout(0)
            self.logger.debug(f"receive_data() got packet: {packet}")
            return packet
        except asyncio.QueueEmpty:
            self.logger.debug(f"receive_data() got no packet")
            return None


    async def receive_data_wait(self) -> LoraPacket:
        """
            Blocking wait that does not return until some data is received (correctly) by the radio.
        """
        self.logger.debug(f"receive_data_wait()")
        
        if self.__radio_state == RadioState.OFF:
            self._set_state(RadioState.RX)
        
        packet = await self.__rx_queue.get()
        await sim.sleep(self.__RECEIVE_PROCESS_DELAY)
        return packet


    async def receive_data_within(self, timeout: float) -> LoraPacket:
        """
            Waits for the given amount of time until some data is received over the radio and
            throws a TimeoutError if no data is received within that time.
        """
        self.logger.debug(f"receive_data_within(timeout={timeout})")

        if self.__radio_state == RadioState.OFF:
            self.logger.debug(f"radio={self._radio_id} state = RX")
            self._set_state(RadioState.RX)

        await sim.sleep(self.__RECEIVE_PROCESS_DELAY)
        return await self.__rx_queue.get_timeout(timeout)


    async def transmit_data(self, data: bytes) -> None:
        raise NotImplementedError("Non-blocking transmit_data is not yet implemented.")


    async def transmit_data_blocking(self, data: bytes) -> None:
        if self.__radio_state == RadioState.RX:
            for meta in self.__packets_in_transit.values():
                meta.interrupted = True

        self._set_state(RadioState.TX)

        packet = LoraPacket(
            payload=data,
            code_rate=self.__tx_code_rate,
            tx_power=self.__tx_power,
            spreading_factor=self.__tx_spreading_factor,
            bandwidth=self.__tx_bandwidth,
            crc_enabled=True,
            preamble_len=8,
            fixed_len=False,
        )

        # Prevents circular import
        from simulator.lora.phy_layer import LoraPhyLayer

        phy_layer = LoraPhyLayer()
        await phy_layer.transmit_packet_blocking(self, packet)

        self._set_state(RadioState.OFF)


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
        
        self.__rx_bandwidth = bandwidth
        self.__rx_spreading_factor = spreading_factor
        self.__rx_code_rate = code_rate
        self.__rx_preable_len = preamble_len
        self.__rx_payload_len = max_payload_len
        self.__rx_symbols = symbols
        self.__rx_fixed_payload_len = fixed_payload_len
        self.__rx_crc_enabled = crc_enabled
        self.__rx_iq_inverted = iq_inverted
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
        self.__tx_bandwidth = bandwidth
        self.__tx_spreading_factor = spreading_factor
        self.__tx_code_rate = code_rate
        self.__tx_preamble_len = preamble_len
        self.__tx_fixed_len = fixed_len
        self.__tx_crc_enable = crc_enable
        self.__tx_freq_hop_enable = freq_hop_enable
        self.__tx_freq_hop_period = freq_hop_period
        self.__tx_iq_inverted = iq_inverted
        self.__tx_timeout = timeout


    def _receive_start(self, packet: LoraPacket, rssi: float):
        """
            Called when the radio begins receiving a packet. This is a callback function used by the 
            PHY layer to notify the radio of incoming packets, end users should never be calling
            this directly.
        """
        self.logger.debug(f"radio={self._radio_id} _receive_start({packet}, rssi={rssi})")

        # Is the radio turned on and able to receive the packets?
        missed_start = not self.__can_receive(packet)

        # Does this packet overlap with any other packets currently being sent?
        possible_collisions = self.__find_overlap(packet)

        # Overlapping packets do not have to collide if they use different enough parameters
        found_collision = False
        for meta in possible_collisions:
            if self.__packets_collide(meta.packet, packet):
                meta.packet.collision = True
                found_collision = True

        snr = self.__estimate_snr()

        # Add this packet to the list of packets in transit
        self.__packets_in_transit[packet.id] = LoraRadio.PacketMetadata(   
            packet=packet,
            rssi=rssi,
            snr=snr,
            collision=found_collision,
            missed_start=missed_start
        )


    async def _receive_end(self, packet: LoraPacket):
        """
            Called when the radio finishes receiving a packet. The 
        """
        self.logger.debug(f"radio={self._radio_id} _receive_end({packet})")
        
        assert packet.id in self.__packets_in_transit, f"radio={self._radio_id} BUG: Packet {packet.id} not found in transit?"

        # Remove packet from in transit list
        metadata = self.__packets_in_transit.pop(packet.id, None)
        if not self.__can_receive(packet):
            metadata.missed_end = True

        # Did we successfully receive the whole packet?
        if metadata.received_successfully():
            self.logger.debug(f"putting packet into rx queue: {packet}")
            await self.__rx_queue.put(packet)

            # Turn off the radio if it was not in continuous receive mode
            if self.__radio_state == RadioState.RX and not self.__rx_continuous:
                self._set_state(RadioState.OFF)

        else:
            self.logger.debug(f"radio={self._radio_id} packet lost due to {metadata}")


    def __can_receive(self, packet: LoraPacket) -> bool:
        """
            Determines whether the radio can receive the given packet based on its current
            configuration.
        """
        # Is the radio turned on?
        if self.__radio_state != RadioState.RX: 
            return False
        
        if packet.code_rate != self.__rx_code_rate:
            self.logger.info(
                f"radio={self._radio_id} cannot receive packet {packet.id} due to code rate \
                    mismatch: {packet.code_rate} != {self.__rx_code_rate}"
            )
            return False
        
        if packet.spreading_factor != self.__rx_spreading_factor:
            self.logger.info(
                f"radio={self._radio_id} cannot receive packet {packet.id} due to SF \
                    mismatch: {packet.spreading_factor} != {self.__rx_spreading_factor}"
            )
            return False
        
        if packet.bandwidth != self.__rx_bandwidth:
            self.logger.info(
                f"radio={self._radio_id} cannot receive packet {packet.id} due to BW \
                    mismatch: {packet.bandwidth} != {self.__rx_bandwidth}"
            )
            return False

        if packet.crc_enabled != self.__rx_crc_enabled:
            self.logger.info(
                f"radio={self._radio_id} cannot receive packet {packet.id} due to CRC \
                    enabled mismatch: {packet.crc_enabled} != {self.__rx_crc_enabled}"
            )
            return False
        
        if packet.fixed_len != self.__rx_fixed_payload_len:
            self.logger.info(
                f"radio={self._radio_id} cannot receive packet {packet.id} due to fixed \
                    length mode mismatch: {packet.fixed_len} != {self.__rx_fixed_payload_len}"
            )
            return False
        
        if packet.preamble_len != self.__rx_preable_len:
            self.logger.info(
                f"radio={self._radio_id} cannot receive packet {packet.id} due to \
                    preamble length mismatch: {packet.preamble_len} != {self.__rx_preable_len}"
            )
            return False
        
        if packet.iq_inverted != self.__rx_iq_inverted:
            self.logger.info(
                f"radio={self._radio_id} cannot receive packet {packet.id} due to IQ \
                    inversion mismatch: {packet.iq_inverted} != {self.__rx_iq_inverted}"
            )
            return False
        
        # In implicit mode, we need to check more things since the packet does not have a header to
        # indicate its parameters.
        if self.__rx_fixed_payload_len:
            
            if packet.payload_len != self.__rx_payload_len:
                self.logger.info(
                    f"radio={self._radio_id} cannot receive packet {packet.id} due to \
                        payload length mismatch: {packet.payload_len} != {self.__rx_payload_len}"
                )
                return False
            
            if packet.code_rate != self.__rx_code_rate:
                self.logger.info(
                    f"radio={self._radio_id} cannot receive packet {packet.id} due to \
                        code rate mismatch: {packet.code_rate} != {self.__rx_code_rate}"
                )
                return False
        
        return True
    

    def __find_overlap(self, packet: LoraPacket) -> list[PacketMetadata]:
        """
            Finds all packets in transit whose send time overlaps with the given packet.
        """
        collisions = []
        for meta in self.__packets_in_transit.values():
            if self.__packets_collide(meta.packet, packet):
                collisions.append(meta)
        return collisions
    

    def __packets_collide(self, p1: LoraPacket, p2: LoraPacket) -> bool:
        """
            Determines whether two packets collide based on their parameters.
        """
        # Simple placeholder implementation: packets collide if they have the same SF and BW
        if p1.spreading_factor == p2.spreading_factor and p1.bandwidth == p2.bandwidth:
            return True
        return False


    def __estimate_snr(self) -> float:
        """
            Estimate the SNR of a packet based on the positions of the transmitter and receiver.
        """
        # Placeholder implementation
        return 10.0
