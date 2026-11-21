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

        The values are the mean of four boards. Standby and receiving repeat within 0.1 mW on a
        board and differ under 3 % between boards. Transmitting does not repeat that well: the
        same board drew up to 14 % more in a second run, so take the TX values as good to about
        10 %. Two more boards were measured and left out: they draw 32 and 141 mW extra whenever
        the CPU runs or the radio is awake.
        See measurements/nucleo_wl55jc1_power_profile_5v/ for the firmware, the captures and the
        scripts that extract these values.
    """
    BOARD_SLEEP_POWER = 0.027908  # whole board, radio asleep and CPU in Stop 1
    MCU_RUN_POWER = 0.004744  # on top of that while the CPU runs (4 MHz MSI, busy waiting)

    # The radio's own sleep draw is ~140 nA (datasheet), part of BOARD_SLEEP_POWER.
    _SLEEP_POWER_USAGE = 0.0
    _STANDBY_POWER_USAGE = 0.003377  # standby on the RC oscillator
    _RX_POWER_USAGE = {  # by bandwidth [kHz]
        125: 0.030304,
        250: 0.031807,
        500: 0.032416,
    }
    _RX_BOOSTED_POWER_USAGE = 0.002295  # extra with boosted receive gain (measured at 125 kHz)
    _TX_POWER_USAGE = {
        # Set up as the ST SubGHz_Phy middleware does for this board, by TX power [dBm].
        'middleware': {
            0: 0.066676,
            1: 0.070556,
            2: 0.076684,
            3: 0.082774,
            4: 0.088831,
            5: 0.094451,
            6: 0.102174,
            7: 0.111605,
            8: 0.120869,
            9: 0.129657,
            10: 0.141591,
            11: 0.152255,
            12: 0.163103,
            13: 0.172060,
            14: 0.180530,
            15: 0.186741,
            # >= 16 dBm switches from the low-power PA to the high-power PA, hence the jump.
            16: 0.568325,
            17: 0.584430,
            18: 0.601027,
            19: 0.617172,
            20: 0.636787,
            21: 0.662813,
            22: 0.677432,
        },
        # Low-power PA with the settings that are optimal for 14 dBm (SetPaConfig 0x04, 0x00).
        'lp_14dbm': {
            0: 0.064575,
            1: 0.069844,
            2: 0.075105,
            3: 0.080425,
            4: 0.085375,
            5: 0.092170,
            6: 0.100397,
            7: 0.108211,
            8: 0.115505,
            9: 0.124949,
            10: 0.133226,
            11: 0.141231,
            12: 0.147396,
            13: 0.153203,
            14: 0.158446,
        },
    }
    _TX_STARTUP_TIME = {  # by TX power [dBm], seconds
        'middleware': {  # 40 us ramp
            0: 81e-6, 1: 77e-6, 2: 73e-6, 3: 69e-6, 4: 67e-6, 5: 66e-6, 6: 64e-6, 7: 63e-6,
            8: 62e-6, 9: 62e-6, 10: 61e-6, 11: 60e-6, 12: 60e-6, 13: 61e-6, 14: 62e-6, 15: 63e-6,
            16: 66e-6, 17: 67e-6, 18: 68e-6, 19: 68e-6, 20: 69e-6, 21: 69e-6, 22: 70e-6,
        },
        'lp_14dbm': {  # 200 us ramp
            0: 380e-6, 1: 340e-6, 2: 315e-6, 3: 301e-6, 4: 295e-6, 5: 285e-6, 6: 276e-6, 7: 272e-6,
            8: 268e-6, 9: 265e-6, 10: 264e-6, 11: 266e-6, 12: 269e-6, 13: 272e-6, 14: 277e-6,
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
