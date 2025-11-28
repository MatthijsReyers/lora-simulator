from abc import ABC, abstractmethod
from typing import Optional
from enums import Bandwidth, SpreadingFactor, CodeRate

from simulator.lora_packet import LoraPacket

class Node(ABC):
    
    @abstractmethod
    async def process(self):
        """ This is the actual business logic of the node. """

    async def receive_data(self) -> Optional[LoraPacket]:
        """
            Tries to receive data from the modem if it is available, immediately returns None if no
            data is available in the receive queue.
        """

    async def receive_data_wait(self) -> Optional[LoraPacket]:
        """
            Blocking wait that does not return until some data is received (correctly) by the radio.
        """

    async def receive_data_within(self, timeout: float) -> Optional[LoraPacket]:
        """
            Waits for the given amount of time until some data is received over the radio and
            returns None if no data was received.
        """

    async def send_data(self, packet: LoraPacket) -> None:
        pass

    async def set_rx_config(
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
        pass

    async def set_tx_config(
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
        pass