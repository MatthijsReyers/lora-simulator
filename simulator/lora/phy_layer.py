import logging
from typing import Callable, Optional

from pandas import DataFrame
from simulator.environment import simulation_env as sim
from simulator.path_loss.log_distance_path_loss import log_distance_path_loss
from simulator.lora.packet import LoraPacket
from simulator.lora.radio import LoraRadio
from copy import deepcopy

class LoraPhyLayer():
    """
        Singleton class for LoRa physical layer simulation. This class handles the spreading of
        packets over the simulated environment and manages interference and collisions.
    """
    __instance = None
    __subscribers: list[LoraRadio] = []

    # A log of all transmitted packets, these are stored as a dictionary of lists because that is
    # much more performant then appending to a DataFrame.
    __packets_log: dict
    
    packets_log: Optional[DataFrame] = None
    
    logger = logging.getLogger("LoraPhyLayer")

    path_loss_estimator: Callable[[float, float], float]
    noise_floor: float


    def __new__(cls, *args, **kwargs):
        if not cls.__instance:
            cls.__instance = super(LoraPhyLayer, cls).__new__(cls)
        return cls.__instance


    def __init__(
            self, 
            path_loss: Callable[[float, float], float] | None = None, 
            noise_floor: float | None = None,    
        ):
        if not hasattr(self, 'initialized'):
            self.initialized = True
            self.enable_capture_effect = True
            self.noise_floor = -120.0
            self.path_loss_estimator = log_distance_path_loss(
                exponent=2.0, sigma=0.5
            )
            sim.create_task(self.__on_sim_end())
            self.__packets_log = {
                "id": [],
                "radio_id": [],
                "start_time": [],
                "airtime": [],
                "location.x": [],
                "location.y": [],
                "tx_power": [],
                "frequency": [],
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
        if path_loss is not None:
            self.path_loss_estimator = path_loss
        if noise_floor is not None:
            self.noise_floor = noise_floor


    async def __on_sim_end(self):
        """
            Called when the simulation ends.
        """
        await sim.wait_for_sim_end()
        # Convert the packets log to a DataFrame for easier analysis, we do this at the end because
        # continuously appending rows to a DataFrame is very inefficient.
        self.packets_log = DataFrame(self.__packets_log)


    def subscribe(self, radio: 'LoraRadio'):
        """
            Subscribe a LoRa radio to the physical layer for transmission and reception of packets.
        """
        from simulator.lora.radio import LoraRadio
        assert isinstance(radio, LoraRadio), "Subscriber must be an instance of LoraRadio"
        assert radio not in self.__subscribers, "Radio is already subscribed"
        self.__subscribers.append(radio)


    async def transmit_packet_blocking(
        self, 
        sender: LoraRadio,
        packet: LoraPacket
    ) -> float:
        """
            Simulate the transmission of a LoRa packet in the environment. This call blocks for the
            duration of the packet's airtime. Run this function call within a new child task in the
            simulator if you wish to simulate non-blocking transmissions.

            Returns the airtime of the transmission.
        """
        self.logger.debug(f"transmit_packet_blocking(sender={id(sender) % 1000})")
        
        assert sender in self.__subscribers, "BUG: Sender radio is not subscribed to the PHY layer"

        # Log packet for later analysis
        self.__log_packet(packet, sender, sim.current_time())

        # Notify radios about the start of the transmission
        for radio in self.__subscribers:
            if radio == sender: continue
            p = deepcopy(packet)
            p.rx_location = radio.position
            radio._on_receive_start(p)

        # Advance simulation time by the preamble airtime
        await sim.sleep(packet.preamble_airtime)

        for radio in self.__subscribers:
            if radio == sender: continue
            await radio._on_receive_preamble(packet.id)

        await sim.sleep(packet.payload_airtime)

        # Notify radios about the end of the transmission
        for radio in self.__subscribers:
            if radio == sender: continue
            await radio._on_receive_end(packet.id)

        return packet.airtime


    def __log_packet(self, packet: LoraPacket, radio: LoraRadio, start_time: float):
        self.__packets_log["id"].append(packet.id)
        self.__packets_log["radio_id"].append(radio._radio_id)
        self.__packets_log["start_time"].append(start_time)
        self.__packets_log["airtime"].append(packet.airtime)
        self.__packets_log["location.x"].append(radio.position[0])
        self.__packets_log["location.y"].append(radio.position[1])
        self.__packets_log["tx_power"].append(packet.tx_power)
        self.__packets_log["frequency"].append(packet.config.frequency)
        self.__packets_log["payload"].append(packet.payload)
        self.__packets_log["code_rate"].append(packet.config.code_rate)
        self.__packets_log["spreading_factor"].append(packet.config.spreading_factor)
        self.__packets_log["bandwidth"].append(packet.config.bandwidth)
        self.__packets_log["crc_enabled"].append(packet.config.crc_enabled)
        self.__packets_log["preamble_len"].append(packet.config.preamble_len)
        self.__packets_log["fixed_len"].append(packet.config.fixed_payload_len)
        self.__packets_log["iq_inverted"].append(packet.config.iq_inverted)
        self.__packets_log["symbols"].append(packet.config.symbols)
