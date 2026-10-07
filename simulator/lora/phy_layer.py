import logging
from typing import Callable, Mapping, Optional

from pandas import DataFrame
from simulator.environment import simulation_env as sim
from simulator.path_loss.log_distance_path_loss import log_distance_path_loss
from simulator.lora.enums.spreading_factor import SpreadingFactor
from simulator.lora.packet import LoraPacket
from simulator.lora.radio import LoraRadio
from simulator.lora.radio_config import LoraConfig
from copy import deepcopy

# Ways to specify receiver sensitivities, see `LoraPhyLayer.set_sensitivity`.
SensitivityTable = Mapping[SpreadingFactor | int, float]
SensitivityModel = Callable[[LoraConfig], float]

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

    # When set, decides the sensitivity of a receive chain instead of the noise floor plus the
    # per spreading factor SNR requirement. See `set_sensitivity`.
    sensitivity_override: Optional[SensitivityModel]


    def __new__(cls, *args, **kwargs):
        if not cls.__instance:
            cls.__instance = super(LoraPhyLayer, cls).__new__(cls)
        return cls.__instance


    def __init__(
            self, 
            path_loss: Callable[[float, float], float] | None = None, 
            noise_floor: float | None = None,    
            sensitivity: SensitivityTable | SensitivityModel | None = None,
        ):
        """
            Every argument that is left empty keeps its current value, so configuring the
            singleton from several places does not undo earlier settings.

            :param path_loss: Path loss estimator `(distance, frequency) -> dB`.
            :param noise_floor: In dBm, the noise floor the SNR of every packet is computed
                against. Without a sensitivity override this also sets the receiver sensitivity,
                which is the noise floor plus the SNR requirement of the spreading factor.
            :param sensitivity: Receiver sensitivities to use instead of the noise floor based
                ones, see `set_sensitivity`.
        """
        if not hasattr(self, 'initialized'):
            self.initialized = True
            self.enable_capture_effect = True
            self.noise_floor = -120.0
            self.sensitivity_override = None
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
        if sensitivity is not None:
            self.set_sensitivity(sensitivity)


    def set_sensitivity(self, sensitivity: SensitivityTable | SensitivityModel | None) -> None:
        """
            Overrides the receiver sensitivities. By default a packet can be demodulated when its
            SNR is above the requirement of its spreading factor, i.e. the sensitivity is the noise
            floor plus `SpreadingFactor.minimum_snr()`, which steps 2.5dB per spreading factor.
            Datasheets and papers often quote sensitivities that do not follow that pattern (for
            example -123, -126, -129, -132, -134.5 and -137dBm for SF7 to SF12), this lets you use
            those values directly.

            :param sensitivity: Either a mapping from spreading factor to sensitivity in dBm, a
                callable that returns the sensitivity in dBm for a receive chain's `LoraConfig`
                (for sensitivities that also depend on the bandwidth, say), or None to go back to
                the noise floor based sensitivities.
        """
        if sensitivity is None or callable(sensitivity):
            self.sensitivity_override = sensitivity
            return
        table = {SpreadingFactor(sf): float(dbm) for (sf, dbm) in sensitivity.items()}
        assert table, "Sensitivity table must not be empty"

        def from_table(config: LoraConfig) -> float:
            try:
                return table[config.spreading_factor]
            except KeyError:
                raise KeyError(
                    f"No receiver sensitivity configured for {config.spreading_factor}"
                ) from None
        self.sensitivity_override = from_table


    def sensitivity(self, config: LoraConfig) -> float:
        """
            The receiver sensitivity in dBm of a receive chain with the given configuration: a
            packet whose received power is below this value is lost in the noise.
        """
        if self.sensitivity_override is not None:
            return self.sensitivity_override(config)
        return self.noise_floor + config.spreading_factor.minimum_snr()


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
