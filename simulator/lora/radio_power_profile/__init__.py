from simulator.lora.radio_config import LoraConfig
from abc import ABC, abstractmethod

class RadioPowerProfile(ABC):
    """
        Abstract base class for defining radio power profiles in a LoRa simulation. Different
        radios can have different power consumption characteristics for various operations which
        can be modeled by implementing this interface.

        Alternatively, you can extend on of the provided profiles if your radio is similar to an
        existing one.
    """
    def disabled_power(self) -> float:
        """
            The power consumption in watts when the radio is disabled/turned off. This is the only
            non-abstract method in this class and set to 0 by default.

            :return: The power consumption in watts.
        """
        return 0.0

    @abstractmethod
    def standby_startup_time(self) -> float:
        """
            Get the time in seconds it takes for the radio to power up and be ready to enter
            standby mode.

            :return: The standby startup time in seconds.
        """
        pass

    @abstractmethod
    def standby_power(self) -> float:
        """
            Get the power consumption in watts when the radio is in standby mode.

            :return: The power consumption in watts.
        """
        pass

    @abstractmethod
    def tx_startup_time(self, power: float|int, config: LoraConfig) -> float:
        """
            Get the time in seconds it takes for the radio to power up and be ready to transmit.
            
            Practically this value is measured as the difference between how long we expect the
            radio to be running according to the `estimate_air_time` function and how long the
            radio is actually consuming full TX power.

            This delay accounts for the time taken to power up the radio hardware or for power to
            stabilize. In our experience this time can vary ever so slightly based on the bandwidth
            used, thought this is so minimal you could reasonably ignore it if you wanted to and
            just provide a constant value instead.

            :return: The transmit startup time in seconds.
        """
        pass

    @abstractmethod
    def tx_power(self, power: float|int, config: LoraConfig) -> float:
        """
            Get the power consumption in watts when transmitting at the specified power level in
            dBm.

            :param power_dbm: The transmit power level in dBm.
            :param config: The radio configuration being used for transmission.
            :return: The power consumption in watts.
        """
        pass

    @abstractmethod
    def rx_power(self, config: LoraConfig) -> float:
        """
            Get the power consumption in watts when receiving at the specified radio configuration.

            :param config: The radio configuration being used for receiving.
            :return: The power consumption in watts.
        """
        pass


# Imported at the bottom so the submodules can import RadioPowerProfile from this package.
from simulator.lora.radio_power_profile.stm32wl55 import Stm32WL55PowerProfile
from simulator.lora.radio_power_profile.nucleowl55 import NucleoWL55PowerProfile

