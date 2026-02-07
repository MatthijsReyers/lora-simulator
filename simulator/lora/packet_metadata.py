from typing import Optional, Tuple
from simulator.lora.packet import LoraPacket

class PacketMetadata:
    """ Radio specific metadata about a packet. """
    def __init__(
        self, 
        packet: LoraPacket, 
        collision: bool, 
        missed_start: bool,
        demodulate_failure: Optional[bool] = None,
    ):
        self.packet = packet
        self.collision = collision
        self.missed_start = missed_start
        self.demodulate_failure = demodulate_failure
        self.missed_end = None
        self.interrupted = None

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
        if self.demodulate_failure:
            status.append("demodulate_failure")
        status_str = ", ".join(status) if status else "successful"
        return f"PacketMetadata(status={status_str}, packet=Packet(id={self.packet.id}))"
    
    def received_successfully(self) -> bool:
        return not (self.collision or self.missed_start or self.missed_end or self.interrupted)
