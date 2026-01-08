from simulator.lora.packet import LoraPacket


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
