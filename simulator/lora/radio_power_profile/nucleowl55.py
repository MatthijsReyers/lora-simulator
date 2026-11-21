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

        Measured on one board, see measurements/nucleo_wl55jc1_power_profile_5v/ for the firmware,
        the capture (run-1) and the script that extracts these values.
    """
    BOARD_SLEEP_POWER = 0.027658  # whole board, radio asleep and CPU in Stop 1
    MCU_RUN_POWER = 0.004737  # on top of that while the CPU runs (4 MHz MSI, busy waiting)

    # The radio's own sleep draw is ~140 nA (datasheet), part of BOARD_SLEEP_POWER.
    _SLEEP_POWER_USAGE = 0.0
    _STANDBY_POWER_USAGE = 0.003348  # standby on the RC oscillator
    _RX_POWER_USAGE = {  # by bandwidth [kHz]
        125: 0.030706,
        250: 0.032230,
        500: 0.032961,
    }
    _RX_BOOSTED_POWER_USAGE = 0.002469  # extra with boosted receive gain (measured at 125 kHz)
    _TX_POWER_USAGE = {
        # Set up as the ST SubGHz_Phy middleware does for this board, by TX power [dBm].
        'middleware': {
            0: 0.063445,
            1: 0.067400,
            2: 0.073672,
            3: 0.079856,
            4: 0.085974,
            5: 0.091760,
            6: 0.099679,
            7: 0.109236,
            8: 0.118635,
            9: 0.127733,
            10: 0.140348,
            11: 0.151705,
            12: 0.163715,
            13: 0.173719,
            14: 0.184082,
            15: 0.195119,
            # >= 16 dBm switches from the low-power PA to the high-power PA, hence the jump.
            16: 0.565062,
            17: 0.583310,
            18: 0.601696,
            19: 0.618855,
            20: 0.637159,
            21: 0.656589,
            22: 0.678110,
        },
        # Low-power PA with the settings that are optimal for 14 dBm (SetPaConfig 0x04, 0x00).
        'lp_14dbm': {
            0: 0.058981,
            1: 0.063942,
            2: 0.068972,
            3: 0.074048,
            4: 0.078954,
            5: 0.085637,
            6: 0.093883,
            7: 0.101960,
            8: 0.109692,
            9: 0.119986,
            10: 0.129140,
            11: 0.138356,
            12: 0.145740,
            13: 0.152692,
            14: 0.158547,
        },
    }
    _TX_STARTUP_TIME = {  # by TX power [dBm], seconds
        'middleware': {  # 40 us ramp
            0: 84e-6, 1: 78e-6, 2: 73e-6, 3: 69e-6, 4: 67e-6, 5: 65e-6, 6: 64e-6, 7: 61e-6,
            8: 61e-6, 9: 60e-6, 10: 59e-6, 11: 59e-6, 12: 59e-6, 13: 59e-6, 14: 60e-6, 15: 61e-6,
            16: 65e-6, 17: 65e-6, 18: 66e-6, 19: 67e-6, 20: 68e-6, 21: 69e-6, 22: 69e-6,
        },
        'lp_14dbm': {  # 200 us ramp
            0: 430e-6, 1: 374e-6, 2: 326e-6, 3: 308e-6, 4: 299e-6, 5: 287e-6, 6: 275e-6, 7: 269e-6,
            8: 263e-6, 9: 257e-6, 10: 254e-6, 11: 256e-6, 12: 259e-6, 13: 261e-6, 14: 267e-6,
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
