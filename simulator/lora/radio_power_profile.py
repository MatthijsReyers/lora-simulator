from abc import ABC, abstractmethod
from typing import Dict
import numpy as np
import random, math

from simulator.lora.enums.bandwidth import Bandwidth
from simulator.lora.radio_config import LoraConfig

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
    def tx_startup_time(self, power: int, config: LoraConfig) -> float:
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
    def tx_power(self, power: int, config: LoraConfig) -> float:
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


class Stm32wl55PowerProfile(RadioPowerProfile):
    """
        Power profile for the STM32WL55 Nucleo development boards built-in radio. These values are
        based on imperial measurements of the board's 3.3V power consumption in various modes 
        performed with a Joulescope J220.
    """
    _RX_POWER_USAGE = 0.07213182002305984
    _TX_POWER_USAGE = {
        0: 0.0260469950735569,
        1: 0.028031006455421448,
        2: 0.030816618353128433,
        3: 0.033483803272247314,
        4: 0.035859428346157074,
        5: 0.03782268241047859,
        6: 0.04040294140577316,
        7: 0.043502047657966614,
        8: 0.04664868861436844,
        9: 0.04982096701860428,
        10: 0.05377183109521866,
        11: 0.05812602490186691,
        12: 0.06326960772275925,
        13: 0.06789539009332657,
        14: 0.07422305643558502,
        15: 0.08138120174407959,
        16: 0.27954405546188354,
        17: 0.2844230532646179,
        18: 0.29135674238204956,
        19: 0.29900339245796204,
        20: 0.31182795763015747,
        21: 0.33040058612823486,
        22: 0.3504241108894348,
    }

    def __init__(self, randomize: bool = False):
        """
            Initialize a STM32WL55 Nucleo based PowerProfile.

            :param randomize: If true, random variations will be added to the power consumption
                values to simulate real-world variations between devices. Default is false to keep
                the simulation deterministic.
        """
        super().__init__()
        self._randomize = randomize

    def tx_startup_time(self, power: int, config: LoraConfig) -> float:
        startup_ms_std = 0.001367
        startup_mean_ms = 0.024141 + 0.055071 * (1 - math.exp(-power / 7.65599))
        if self._randomize:
            startup_mean_ms += random.gauss(0.0, startup_ms_std)
        return startup_mean_ms / 1000

    def tx_power(self, power: int, config: LoraConfig) -> float:
        assert power in self._TX_POWER_USAGE, "Unsupported TX power level for STM32WL55 profile"
        return self._TX_POWER_USAGE[power]

    def rx_power(self, config: LoraConfig) -> float:
        return self._RX_POWER_USAGE

    def standby_startup_time(self):
        # IDK? TODO: Measure this properly
        return 0.000001

    def standby_power(self) -> float:
        # IDK? TODO: Measure this properly
        return 0.000001

