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

        The values are the mean of four boards. They agree within 3 % when receiving and within
        5 % on the high-power PA, but one of them differs by up to 25 % on the low-power PA. Two
        more boards were measured and left out: they draw 32 and 141 mW extra whenever the CPU
        runs or the radio is awake.
        See measurements/nucleo_wl55jc1_power_profile_5v/ for the firmware, the captures and the
        scripts that extract these values.
    """
    BOARD_SLEEP_POWER = 0.027879  # whole board, radio asleep and CPU in Stop 1
    MCU_RUN_POWER = 0.004740  # on top of that while the CPU runs (4 MHz MSI, busy waiting)

    # The radio's own sleep draw is ~140 nA (datasheet), part of BOARD_SLEEP_POWER.
    _SLEEP_POWER_USAGE = 0.0
    _STANDBY_POWER_USAGE = 0.003372  # standby on the RC oscillator
    _RX_POWER_USAGE = {  # by bandwidth [kHz]
        125: 0.030304,
        250: 0.031810,
        500: 0.032417,
    }
    _RX_BOOSTED_POWER_USAGE = 0.002296  # extra with boosted receive gain (measured at 125 kHz)
    _TX_POWER_USAGE = {
        # Set up as the ST SubGHz_Phy middleware does for this board, by TX power [dBm].
        'middleware': {
            0: 0.065084,
            1: 0.068834,
            2: 0.074709,
            3: 0.080546,
            4: 0.086315,
            5: 0.091674,
            6: 0.099078,
            7: 0.108148,
            8: 0.117101,
            9: 0.125646,
            10: 0.137512,
            11: 0.148314,
            12: 0.159521,
            13: 0.168957,
            14: 0.178893,
            15: 0.186395,
            # >= 16 dBm switches from the low-power PA to the high-power PA, hence the jump.
            16: 0.562241,
            17: 0.579398,
            18: 0.597124,
            19: 0.614192,
            20: 0.634606,
            21: 0.661398,
            22: 0.676537,
        },
        # Low-power PA with the settings that are optimal for 14 dBm (SetPaConfig 0x04, 0x00).
        'lp_14dbm': {
            0: 0.063217,
            1: 0.068360,
            2: 0.073497,
            3: 0.078740,
            4: 0.083595,
            5: 0.090287,
            6: 0.098395,
            7: 0.106194,
            8: 0.113519,
            9: 0.123075,
            10: 0.131530,
            11: 0.139799,
            12: 0.146269,
            13: 0.152458,
            14: 0.158030,
        },
    }
    _TX_STARTUP_TIME = {  # by TX power [dBm], seconds
        'middleware': {  # 40 us ramp
            0: 83e-6, 1: 78e-6, 2: 74e-6, 3: 70e-6, 4: 68e-6, 5: 66e-6, 6: 65e-6, 7: 64e-6,
            8: 63e-6, 9: 62e-6, 10: 61e-6, 11: 60e-6, 12: 60e-6, 13: 60e-6, 14: 61e-6, 15: 62e-6,
            16: 65e-6, 17: 66e-6, 18: 67e-6, 19: 68e-6, 20: 68e-6, 21: 69e-6, 22: 70e-6,
        },
        'lp_14dbm': {  # 200 us ramp
            0: 395e-6, 1: 353e-6, 2: 320e-6, 3: 304e-6, 4: 298e-6, 5: 287e-6, 6: 277e-6, 7: 272e-6,
            8: 268e-6, 9: 264e-6, 10: 263e-6, 11: 264e-6, 12: 267e-6, 13: 270e-6, 14: 275e-6,
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
