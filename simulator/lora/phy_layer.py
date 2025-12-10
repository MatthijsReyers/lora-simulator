import logging
from typing import Tuple
from simulator.environment import simulation_env as sim
from simulator.lora.packet import LoraPacket
from simulator.lora.radio import LoraRadio

class LoraPhyLayer():
    """
        Singleton class for LoRa physical layer simulation. This class handles the spreading of
        packets over the simulated environment and manages interference and collisions.
    """
    __instance = None
    __subscribers: list[LoraRadio] = []
    
    logger = logging.getLogger("LoraPhyLayer")

    def __new__(cls, *args, **kwargs):
        if not cls.__instance:
            cls.__instance = super(LoraPhyLayer, cls).__new__(cls)
        return cls.__instance

    def __init__(self):
        if not hasattr(self, 'initialized'):
            self.initialized = True


    def subscribe(self, radio: 'LoraRadio'):
        """
            Subscribe a LoRa radio to the physical layer for transmission and reception of packets.
        """
        self.__subscribers.append(radio)


    def transmit_packet(
        self, 
        sender: LoraRadio,
        packet: LoraPacket
    ) -> float:
        """
            Simulate the transmission of a LoRa packet in the environment.
            This is a non-blocking version that schedules the transmission and returns immediately.
        """
        raise NotImplementedError("Non-blocking transmit_data is not implemented yet.")


    async def transmit_packet_blocking(
        self, 
        sender: LoraRadio,
        packet: LoraPacket
    ) -> float:
        """
            Simulate the transmission of a LoRa packet in the environment.

            Returns the airtime of the transmission.
        """
        self.logger.debug(f"transmit_packet_blocking(sender={id(sender) % 1000})")
        
        airtime = LoraPhyLayer.estimate_airtime(packet)
        self.logger.debug(f"airtime estimated: {airtime:.3f} s")

        # Notify radios about the start of the transmission
        self.logger.debug(f"subscribers count: {len(self.__subscribers)}")
        for radio in self.__subscribers:
            if radio == sender: continue
            rssi = self.__estimate_rssi(sender.position, radio.position)
            radio._receive_start(packet, rssi)

        # Advanced simulation time by the airtime of the packet
        await sim.sleep(airtime)

        # Notify radios about the end of the transmission
        for radio in self.__subscribers:
            if radio == sender: continue
            await radio._receive_end(packet)

        return airtime


    @classmethod
    def __estimate_rssi(cls, pos_tx: Tuple[float, float], pos_rx: Tuple[float, float]) -> float:
        """ 
            Estimate the Received Signal Strength Indicator (RSSI) at the receiver based on the
            positions of the transmitter and receiver.
        """
        return 0
    

    @staticmethod
    def estimate_airtime(packet: LoraPacket) -> float:
        """ 
            In seconds; estimate the airtime of a LoRa packet based on its parameters.
        """
        return 0.05


