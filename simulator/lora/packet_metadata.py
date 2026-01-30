from typing import Tuple
from simulator.lora.packet import LoraPacket
from simulator.lora.path_loss import log_distance_path_loss
from simulator.lora.distance import distance

class PacketMetadata:
    """ Radio specific metadata about a packet. """
    def __init__(
        self, 
        packet: LoraPacket, 
        rssi: float, 
        snr: float, 
        collision: bool, 
        missed_start: bool,
        rx_location: Tuple[float, float]
    ):
        self.packet = packet
        self.rssi = rssi
        self.snr = snr
        self.collision = collision
        self.missed_start = missed_start
        self.missed_end = False
        self.interrupted = False
        self.rx_location = rx_location

        # Lazy properties, these are not computed until someone tries to access them because we
        # generally don't need them unless packets overlap and we do proper collision detection
        self.__rx_power = None

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
    
    @property
    def rx_power(self):
        if self.__rx_power is None:
            # Prevent circular import
            from simulator.lora.phy_layer import LoraPhyLayer
            phy = LoraPhyLayer()
            d = distance(self.packet.tx_location, self.rx_location)
            freq = self.packet.config.bandwidth.to_hz()
            path_loss = log_distance_path_loss(
                d, freq, phy._path_loss_exponent, phy._path_loss_sigma
            )
            self.__rx_power = self.packet.tx_power - path_loss
        return self.__rx_power

    def received_successfully(self) -> bool:
        return not (self.collision or self.missed_start or self.missed_end or self.interrupted)
