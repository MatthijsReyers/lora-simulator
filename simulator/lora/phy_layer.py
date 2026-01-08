import logging
from typing import Optional, Tuple

from pandas import DataFrame
from simulator.environment import simulation_env as sim
from simulator.lora.packet import LoraPacket
from simulator.lora.radio import LoraRadio
from simulator.lora.utils import packet_airtime

class LoraPhyLayer():
    """
        Singleton class for LoRa physical layer simulation. This class handles the spreading of
        packets over the simulated environment and manages interference and collisions.
    """
    __instance = None
    __subscribers: list[LoraRadio] = []

    # A log of all transmitted packets, these are stored as a dictionary because that is the most
    # performant way to construct a DataFrame later on.
    __packets_log = {
        "id": [],
        "radio_id": [],
        "start_time": [],
        "duration": [],
        "location.x": [],
        "location.y": [],
        "tx_power": [],
        "code_rate": [],
        "spreading_factor": [],
        "bandwidth": [],
        "crc_enabled": [],
        "preamble_len": [],
        "fixed_len": [],
        "iq_inverted": [],
        "symbols": [],
        "payload": [],
    }
    
    packets_log: Optional[DataFrame] = None
    
    logger = logging.getLogger("LoraPhyLayer")


    def __new__(cls, *args, **kwargs):
        if not cls.__instance:
            cls.__instance = super(LoraPhyLayer, cls).__new__(cls)
        return cls.__instance


    def __init__(self):
        if not hasattr(self, 'initialized'):
            self.initialized = True
            sim.create_task(self.__on_simulation_end())


    async def __on_simulation_end(self):
        """
            Called when the simulation ends.
        """
        await sim.sleep(sim.last_tick())
        self.packets_log = DataFrame(self.__packets_log)
        self.__packets_log = {}


    def subscribe(self, radio: 'LoraRadio'):
        """
            Subscribe a LoRa radio to the physical layer for transmission and reception of packets.
        """
        from simulator.lora.radio import LoraRadio
        assert isinstance(radio, LoraRadio), "Subscriber must be an instance of LoraRadio"
        assert radio not in self.__subscribers, "Radio is already subscribed"
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
        
        assert sender in self.__subscribers, "Sender radio is not subscribed to the PHY layer"

        airtime = packet_airtime(packet)
        self.logger.debug(f"airtime estimated: {airtime:.3f} s")

        # Log packet for later analysis
        self.__log_packet(packet, sender, sim.current_time(), airtime)

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


    def __log_packet(self, packet: LoraPacket, radio: LoraRadio, start_time: float, duration: float):
        self.__packets_log["id"].append(packet.id)
        self.__packets_log["radio_id"].append(id(radio))
        self.__packets_log["start_time"].append(start_time)
        self.__packets_log["duration"].append(duration)
        self.__packets_log["location.x"].append(radio.position[0])
        self.__packets_log["location.y"].append(radio.position[1])
        self.__packets_log["tx_power"].append(packet.tx_power)
        self.__packets_log["code_rate"].append(packet.code_rate)
        self.__packets_log["spreading_factor"].append(packet.spreading_factor)
        self.__packets_log["bandwidth"].append(packet.bandwidth)
        self.__packets_log["crc_enabled"].append(packet.crc_enabled)
        self.__packets_log["preamble_len"].append(packet.preamble_len)
        self.__packets_log["fixed_len"].append(packet.fixed_len)
        self.__packets_log["iq_inverted"].append(packet.iq_inverted)
        self.__packets_log["symbols"].append(packet.symbols)
        self.__packets_log["payload"].append(packet.payload)