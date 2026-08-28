
from typing import Dict

from simulator.lora.packet_metadata import PacketMetadata
from simulator.lora.radio_config import LoraConfig


class RxChain:
    """
        A single receive chain (demodulator) of a radio.

        Simple end device radios like the SX126x only have one of these, they can listen to
        exactly one channel with one set of modulation parameters at a time. Gateway concentrators
        like the SX1301/SX1302 have several chains running in parallel, allowing them to demodulate
        packets on multiple channels and/or spreading factors simultaneously.

        Every chain keeps its own bookkeeping of the packets that are currently in the air within
        its part of the spectrum, since whether a packet collides or not depends on what the chain
        listening to it is tuned to.
    """

    chain_id: int
    config: LoraConfig
    enabled: bool
    packets_in_transit: Dict[int, PacketMetadata]

    def __init__(self, chain_id: int, config: LoraConfig|None = None, enabled: bool = True):
        self.chain_id = chain_id
        self.config = config if config is not None else LoraConfig()
        self.enabled = enabled
        self.packets_in_transit = {}

    def __repr__(self):
        return (
            f"RxChain(id={self.chain_id}, freq={self.config.frequency/1e6:.3f}MHz, "
            f"sf={self.config.spreading_factor}, bw={self.config.bandwidth}, "
            f"enabled={self.enabled})"
        )
