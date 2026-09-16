from simulator.lora.radio_config import LoraConfig
from simulator.lora.radio_power_profile import RadioPowerProfile
import random

class NucleoWL55PowerProfile(RadioPowerProfile):
    """
        Power profile for the STM32WL55 Nucleo development boards built-in radio. These values are
        based on empirical measurements of the board's 3.3V power consumption (JP1 / VDD_MCU rail)
        performed with two Joulescope JS220s at 500 kHz. See the capture in
        measurements/nucleo_wl55jc1_power_profile/ and measurements/extract_power_profile.py to
        reproduce the extraction. This profile models ONLY the radio part of the MCU.
    """
    _SLEEP_POWER_USAGE = 0.017295191064476967
    _STANDBY_POWER_USAGE = 0.018751682713627815
    _RX_POWER_USAGE = {  # by bandwidth [kHz]
        125: 0.03051689825952053,
        250: 0.03162598796188831,
        500: 0.032342640683054924,
    }
    _TX_POWER_USAGE = {  # by TX power [dBm]
        0: 0.053809987381100655,
        1: 0.05575610138475895,
        2: 0.05886215902864933,
        3: 0.06195263750851154,
        4: 0.06479218043386936,
        5: 0.06735543347895145,
        6: 0.07070539332926273,
        7: 0.07480822689831257,
        8: 0.0787563044577837,
        9: 0.08261861838400364,
        10: 0.08805967308580875,
        11: 0.09345334209501743,
        12: 0.0996472705155611,
        13: 0.10571375675499439,
        14: 0.11355689354240894,
        15: 0.12320944853127003,
        16: 0.32778126187622547,
        17: 0.3430588562041521,
        18: 0.36240887828171253,
        19: 0.38385692425072193,
        20: 0.4086353797465563,
        21: 0.437241168692708,
        22: 0.45126486010849476,
    }
    _TX_STARTUP_TIME = {  # by TX power [dBm], seconds
        0: 0.0012296694393721522,
        1: 0.0012156692433694105,
        2: 0.001217669271369806,
        3: 0.001205669103367446,
        4: 0.001213669215369015,
        5: 0.0012116691873686333,
        6: 0.001199669019366273,
        7: 0.001205669103367446,
        8: 0.001205669103367446,
        9: 0.001199669019366273,
        10: 0.0012096691593682374,
        11: 0.001205669103367446,
        12: 0.0012016690473666688,
        13: 0.0012016690473666688,
        14: 0.001199669019366273,
        15: 0.0011916689073647043,
        16: 6.765317114440506e-05,
        17: 6.765317114440506e-05,
        18: 6.765317114440506e-05,
        19: 6.565314314400938e-05,
        20: 6.765317114440506e-05,
        21: 6.565314314400938e-05,
        22: 6.765317114440506e-05,
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

    def tx_startup_time(self, power: float|int, config: LoraConfig) -> float:
        if type(power) is float:
            power = int(power)
        assert power in self._TX_STARTUP_TIME, "Unsupported TX power level for STM32WL55 profile"
        startup = self._TX_STARTUP_TIME[power]
        if self._randomize:
            # Measured spread differs per PA: ~9.1 µs for the low-power PA, ~1 µs for the
            # high-power PA.
            startup += random.gauss(0.0, 9.1e-6 if power <= 15 else 1.0e-6)
        return startup

    def tx_power(self, power: float|int, config: LoraConfig) -> float:
        if type(power) is float:
            power = int(power)
        assert power in self._TX_POWER_USAGE, "Unsupported TX power level for STM32WL55 profile"
        return self._TX_POWER_USAGE[power]

    def rx_power(self, config: LoraConfig) -> float:
        return self._RX_POWER_USAGE[config.bandwidth.to_khz()]

    def standby_startup_time(self) -> float:
        # 10-90% rise time of the measured sleep -> standby transition.
        return 0.0005

    def disabled_power(self) -> float:
        # The radio is put to sleep when "disabled", it cannot be turned off completely.
        return self._SLEEP_POWER_USAGE

    def sleep_power(self) -> float:
        return self._SLEEP_POWER_USAGE

    def standby_power(self) -> float:
        return self._STANDBY_POWER_USAGE
