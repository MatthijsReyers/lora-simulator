from typing import Literal
import random

from simulator.lora.radio_config import LoraConfig
from simulator.lora.radio_power_profile import RadioPowerProfile


class NucleoWL55PowerProfile(RadioPowerProfile):
    """
        Power profile for the radio of a Nucleo-WL55JC1 development board, measured at the 5 V
        supply of the board with a Joulescope JS220 at 1 MHz. The board's 3.3 V linear regulator
        sits between that supply and the chip, so every value includes what the regulator loses.
        Stm32WL55PowerProfile has the same radio measured behind the regulator, which is about a
        third less.

        Every value is what the board draws in that radio state minus what it draws with the radio
        asleep, the radio is on its switching regulator (SMPS). The board itself is NOT in here:
        BOARD_SLEEP_POWER is drawn all the time and MCU_RUN_POWER comes on top of it while the CPU
        is running, add a separate PowerConsumer for those!

        The values are the mean of three boards. They agree within 3 % when receiving and within
        5 % on the high-power PA, but one of them differs by up to 25 % on the low-power PA.
        See measurements/nucleo_wl55jc1_power_profile_5v/ for the firmware, the captures and the
        scripts that extract these values.
    """
    BOARD_SLEEP_POWER = 0.027850  # whole board, radio asleep and CPU in Stop 1
    MCU_RUN_POWER = 0.004752  # on top of that while the CPU runs (4 MHz MSI, busy waiting)

    # The radio's own sleep draw is ~140 nA (datasheet), part of BOARD_SLEEP_POWER.
    _SLEEP_POWER_USAGE = 0.0
    _STANDBY_POWER_USAGE = 0.003392  # standby on the RC oscillator
    _RX_POWER_USAGE = {  # by bandwidth [kHz]
        125: 0.030316,
        250: 0.031827,
        500: 0.032397,
    }
    _RX_BOOSTED_POWER_USAGE = 0.002330  # extra with boosted receive gain (measured at 125 kHz)
    _TX_POWER_USAGE = {
        # Set up as the ST SubGHz_Phy middleware does for this board, by TX power [dBm].
        'middleware': {
            0: 0.064338,
            1: 0.067956,
            2: 0.073592,
            3: 0.079158,
            4: 0.084704,
            5: 0.089937,
            6: 0.097101,
            7: 0.105850,
            8: 0.114561,
            9: 0.122967,
            10: 0.134609,
            11: 0.145374,
            12: 0.156747,
            13: 0.166389,
            14: 0.176450,
            15: 0.185815,
            # >= 16 dBm switches from the low-power PA to the high-power PA, hence the jump.
            16: 0.560102,
            17: 0.577376,
            18: 0.595411,
            19: 0.612791,
            20: 0.634259,
            21: 0.663707,
            22: 0.677584,
        },
        # Low-power PA with the settings that are optimal for 14 dBm (SetPaConfig 0x04, 0x00).
        'lp_14dbm': {
            0: 0.064210,
            1: 0.069456,
            2: 0.074654,
            3: 0.079875,
            4: 0.084835,
            5: 0.091594,
            6: 0.099792,
            7: 0.107664,
            8: 0.115043,
            9: 0.124613,
            10: 0.133068,
            11: 0.141137,
            12: 0.147331,
            13: 0.153203,
            14: 0.158597,
        },
    }
    _TX_STARTUP_TIME = {  # by TX power [dBm], seconds
        'middleware': {  # 40 us ramp
            0: 83e-6, 1: 79e-6, 2: 74e-6, 3: 71e-6, 4: 69e-6, 5: 67e-6, 6: 65e-6, 7: 64e-6,
            8: 63e-6, 9: 62e-6, 10: 61e-6, 11: 61e-6, 12: 60e-6, 13: 60e-6, 14: 61e-6, 15: 62e-6,
            16: 66e-6, 17: 66e-6, 18: 67e-6, 19: 68e-6, 20: 69e-6, 21: 69e-6, 22: 70e-6,
        },
        'lp_14dbm': {  # 200 us ramp
            0: 387e-6, 1: 345e-6, 2: 316e-6, 3: 302e-6, 4: 296e-6, 5: 285e-6, 6: 276e-6, 7: 271e-6,
            8: 268e-6, 9: 264e-6, 10: 263e-6, 11: 265e-6, 12: 268e-6, 13: 271e-6, 14: 276e-6,
        },
    }

    def __init__(self,
            randomize: bool = False,
            tx_setup: Literal['middleware', 'lp_14dbm'] = 'middleware',
            rx_boosted: bool = False,
        ):
        """
            Initialize a Nucleo-WL55JC1 based PowerProfile.

            :param randomize: If true, random variations will be added to the TX startup time to
                simulate real-world variations. Default is false to keep the simulation
                deterministic.
            :param tx_setup: How the firmware sets up the power amplifier. 'middleware' is what
                the ST SubGHz_Phy middleware does on this board: the low-power PA tuned for
                15 dBm up to 15 dBm and the high-power PA above that, with a 40 us ramp.
                'lp_14dbm' is the low-power PA tuned for 14 dBm with a 200 us ramp (up to
                14 dBm), which draws less for the same output power setting.
            :param rx_boosted: If true, the radio receives with boosted gain.
        """
        super().__init__()
        assert tx_setup in self._TX_POWER_USAGE, "TX setup must be 'middleware' or 'lp_14dbm'"
        self._randomize = randomize
        self._tx_setup = tx_setup
        self._rx_boosted = rx_boosted

    def tx_startup_time(self, power: float|int, config: LoraConfig) -> float:
        if type(power) is float:
            power = int(power)
        table = self._TX_STARTUP_TIME[self._tx_setup]
        assert power in table, "Unsupported TX power level for Nucleo-WL55 profile"
        startup = table[power]
        if self._randomize:
            # The measured spread between pulses is about 1 µs.
            startup += random.gauss(0.0, 1.0e-6)
        return startup

    def tx_power(self, power: float|int, config: LoraConfig) -> float:
        if type(power) is float:
            power = int(power)
        table = self._TX_POWER_USAGE[self._tx_setup]
        assert power in table, "Unsupported TX power level for Nucleo-WL55 profile"
        return table[power]

    def rx_power(self, config: LoraConfig) -> float:
        boost = self._RX_BOOSTED_POWER_USAGE if self._rx_boosted else 0.0
        return self._RX_POWER_USAGE[config.bandwidth.to_khz()] + boost

    def standby_startup_time(self) -> float:
        # 10-90% rise time of the measured sleep -> standby transition.
        return 50e-6

    def disabled_power(self) -> float:
        # The radio is put to sleep when "disabled", it cannot be turned off completely.
        return self._SLEEP_POWER_USAGE

    def sleep_power(self) -> float:
        return self._SLEEP_POWER_USAGE

    def standby_power(self) -> float:
        return self._STANDBY_POWER_USAGE
