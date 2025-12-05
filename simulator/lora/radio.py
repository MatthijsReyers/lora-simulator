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


class LoraRadio(ABC):
    class PacketMetadata:
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

    __RECEIVE_PROCESS_DELAY = 0.0001

    __radio_state: RadioState = RadioState.OFF
    __packets_in_transit: dict[int, PacketMetadata] = {}
    __rx_queue = Queue()
    
    __rx_bandwidth: Bandwidth = Bandwidth.KHz125
    __rx_spreading_factor: SpreadingFactor = SpreadingFactor.SF7
    __rx_code_rate: CodeRate = CodeRate.CR4_5
    __rx_preable_len: int = 8
    __rx_symbol_timeout: int = 5
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

    position: tuple[float, float]
    logger: logging.Logger
    
    def __init__(self, position: tuple[float, float] = (0.0, 0.0)):
        self.position = position
        self.logger = logging.getLogger(f"LoraRadio-{id(self) % 1000}")

        # Prevents circular import
        from simulator.lora.phy_layer import LoraPhyLayer
        phy = LoraPhyLayer()
        phy.subscribe(self)

    def get_state(self) -> RadioState:
        return self.__radio_state

    def receive(self, continuous: bool = True) -> None:
        """
            Puts the radio into receive mode without blocking or waiting for packets. The other
            blocking receive methods already put the radio into receive mode so this method is only
            needed to put the radio into receive mode after transmitting data.
        """
        self.logger.debug(f"radio={id(self) % 1000} receive(continuous={continuous})")
        if not continuous:
            raise NotImplementedError("Non-continuous receive mode is not yet implemented.")
        self.__radio_state = RadioState.RX
        self.logger.debug(f"radio={id(self) % 1000} state = RX")
        self.__rx_continuous = continuous


    async def receive_data_nowait(self) -> Optional[LoraPacket]:
        """
            Tries to receive data from the modem if it is available, immediately returns None if no
            data is available in the receive queue.
        """
        self.logger.debug(f"receive_data()")
        if self.__radio_state == RadioState.OFF:
            self.logger.debug(f"radio={id(self) % 1000} state = RX")
            self.__radio_state = RadioState.RX
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
            self.logger.debug(f"radio={id(self) % 1000} state = RX")
            self.__radio_state = RadioState.RX
        
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
            self.logger.debug(f"radio={id(self) % 1000} state = RX")
            self.__radio_state = RadioState.RX

        await sim.sleep(self.__RECEIVE_PROCESS_DELAY)
        return await self.__rx_queue.get_timeout(timeout)


    async def transmit_data(self, data: bytes) -> None:
        raise NotImplementedError("transmit_data is not yet implemented.")


    async def transmit_data_blocking(self, data: bytes) -> None:
        if self.__radio_state == RadioState.RX:
            for meta in self.__packets_in_transit.values():
                meta.interrupted = True

        self.logger.debug(f"radio={id(self) % 1000} state = TX")
        self.__radio_state = RadioState.TX

        packet = LoraPacket(
            payload=data,
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


    def set_rx_config(
        self,
        bandwidth: Bandwidth = Bandwidth.KHz125,
        spreading_factor: SpreadingFactor = SpreadingFactor.SF7,
        code_rate: CodeRate = CodeRate.CR4_5,
        preamble_len: int = 8,
        symbol_timeout: int = 5,
        payload_len: int = 64,
        symbols: int = 0,
        fixed_payload_len: bool = False,
        crc_enabled: bool = True,
        iq_inverted: bool = False,
        rx_continuous: bool = True,
    ):
        self.__rx_bandwidth = bandwidth
        self.__rx_spreading_factor = spreading_factor
        self.__rx_code_rate = code_rate
        self.__rx_preable_len = preamble_len
        self.__rx_symbol_timeout = symbol_timeout
        self.__rx_payload_len = payload_len
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
        bandwidth: Bandwidth = Bandwidth.KHz125,
        spreading_factor: SpreadingFactor = SpreadingFactor.SF7,
        code_rate: CodeRate = CodeRate.CR4_5,
        preamble_len: int = 8,
        fixed_len: bool = False,
        crc_enable: bool = True,
        freq_hop_enable: bool = False,
        freq_hop_period: int = 0,
        iq_inverted: bool = False,
        timeout: int = 3_000,
    ):
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
        self.logger.debug(f"radio={id(self) % 1000} _receive_start({packet}, rssi={rssi})")

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
        self.__packets_in_transit[id(packet)] = LoraRadio.PacketMetadata(   
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
        self.logger.debug(f"radio={id(self) % 1000} _receive_end({packet})")

        assert id(packet) in self.__packets_in_transit, "BUG: Packet not found in transit?"

        # Remove packet from in transit list
        metadata = self.__packets_in_transit.pop(id(packet), None)

        if not self.__can_receive(packet):
            metadata.missed_end = True

        # Did we successfully receive the whole packet?
        if not metadata.collision and not metadata.missed_start and not metadata.interrupted:
            self.logger.debug(f"putting packet into rx queue: {packet}")
            await self.__rx_queue.put(packet)

            # Turn off the radio if it was not in continuous receive mode
            if self.__radio_state == RadioState.RX and not self.__rx_continuous:
                self.__radio_state = RadioState.OFF

        else:
            self.logger.debug(f"radio={id(self) % 1000} packet lost due to {metadata}")


    def __can_receive(self, packet: LoraPacket) -> bool:
        """
            Determines whether the radio can receive the given packet based on its current
            configuration.
        """
        # Is the radio turned on?
        if self.__radio_state != RadioState.RX: 
            return False
        
        # TODO: Implement additional checks based on radio configuration (e.g., SF, BW, frequency)
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
    