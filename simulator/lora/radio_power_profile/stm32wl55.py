from typing import Literal
import numpy as np

from simulator.lora.radio_config import LoraConfig
from simulator.lora.radio_power_profile import RadioPowerProfile


class Stm32WL55PowerProfile(RadioPowerProfile):
    """
        Power profile for the STM32WL55 sub-GHz radio based on the typical values from the ST
        datasheet (DS13293 Rev 2, section 5.3.3 "Sub-GHz radio characteristics"). All currents
        are datasheet typicals at VDD = 3.3 V, 25 degrees C, 868 MHz, and only cover the radio
        subsystem itself: regulator/board overhead and the MCU are not included. For a profile
        based on empirical whole-board measurements of the Nucleo-WL55JC1 development board see
        NucleoWL55PowerProfile.

        The radio has two power amplifiers: a low-power PA (up to +15 dBm) and a high-power PA
        (up to +22 dBm). This profile selects the low-power PA for output powers up to and
        including +15 dBm and the high-power PA above that, matching the ST reference firmware.
        TX currents between the datasheet's specified output powers are linearly interpolated
        (Table 28, "optimized" PA match, 868-915 MHz, VDDRF = 3.3 V); below +10 dBm the
        datasheet provides no figures so the +10 dBm value is used, which overestimates the
        consumption of very low TX powers.
    """
    _VDD = 3.3  # All datasheet typicals are specified at VDD = 3.3 V.

    # Table 27: Sub-GHz radio power consumption [A].
    _SLEEP_CURRENT = 140e-9  # Sleep with warm start, configuration retained.
    _STANDBY_RC_CURRENT = 0.7e-3  # Standby mode, RC 13 MHz on, HSE32 off.
    _STANDBY_HSE32_CURRENT = {'smps': 1.05e-3, 'ldo': 0.99e-3}
    _RX_CURRENT = {  # by (regulator, rx_boosted), LoRa 125 kHz.
        ('smps', False): 4.82e-3,
        ('smps', True): 5.46e-3,
        ('ldo', False): 8.90e-3,
        ('ldo', True): 10.22e-3,
    }

    # Table 28: TX consumption [A] by output power [dBm], 868-915 MHz, VDDRF = 3.3 V.
    _LP_PA_TX_CURRENT = {  # Low-power PA (optimized for 14 dBm; +15 dBm from optimal settings).
        10: 17.5e-3,
        14: 23.5e-3,
        15: 25.5e-3,
    }
    _HP_PA_TX_CURRENT = {  # High-power PA (optimized for 22 dBm).
        14: 92.0e-3,
        17: 98.0e-3,
        20: 107.5e-3,
        22: 120.0e-3,
    }

    # Tables 29, 31 and 32: wakeup/startup times [s].
    _SYNTH_WAKEUP_TIME = 40e-6  # TS_FS: synthesizer wakeup from standby, HSE32 mode.
    _TX_WAKEUP_TIME = 36e-6  # TS_TX: TX wakeup with synthesizer enabled, excluding PA ramping.
    _REGULATOR_STARTUP_TIME = {'smps': 70e-6, 'ldo': 60e-6}  # TSSMPS / TSLDO.
    _OSC_WAKEUP_TIME = 170e-6  # TS_OSC: crystal oscillator wakeup from standby RC.

    def __init__(self,
            regulator: Literal['smps', 'ldo'] = 'smps',
            rx_boosted: bool = False,
            pa_ramp_time: float = 40e-6,
        ):
        """
            Initialize a datasheet-based STM32WL55 radio power profile.

            :param regulator: Which internal regulator powers the radio: the buck converter
                ('smps', default) or the linear regulator ('ldo'). The LDO roughly doubles the
                RX current at 3.3 V.
            :param rx_boosted: If true, use the RX boosted gain currents (better sensitivity at
                the cost of ~0.6-1.3 mA extra RX current).
            :param pa_ramp_time: The configured PA ramping time in seconds. Programmable from
                10 us to 3.4 ms on the STM32WL55; defaults to 40 us as used by the ST LoRaWAN
                middleware.
        """
        super().__init__()
        assert regulator in ('smps', 'ldo'), "Regulator must be 'smps' or 'ldo'"
        assert 10e-6 <= pa_ramp_time <= 3400e-6, "PA ramp time must be between 10 us and 3.4 ms"
        self._regulator = regulator
        self._rx_boosted = rx_boosted
        self._pa_ramp_time = pa_ramp_time

    def disabled_power(self) -> float:
        # The radio cannot be turned off completely, at best it is put in sleep mode.
        return self.sleep_power()

    def sleep_power(self) -> float:
        return self._SLEEP_CURRENT * self._VDD

    def standby_power(self) -> float:
        # Standby with the RC 13 MHz oscillator, the default mode the radio wakes up into.
        return self._STANDBY_RC_CURRENT * self._VDD

    def standby_startup_time(self) -> float:
        return self._REGULATOR_STARTUP_TIME[self._regulator]

    def tx_startup_time(self, power: float|int, config: LoraConfig) -> float:
        return self._SYNTH_WAKEUP_TIME + self._TX_WAKEUP_TIME + self._pa_ramp_time

    def tx_power(self, power: float|int, config: LoraConfig) -> float:
        assert power <= 22, "The STM32WL55 cannot transmit above +22 dBm"
        # The low-power PA covers up to +15 dBm, the high-power PA is used above that.
        table = self._LP_PA_TX_CURRENT if power <= 15 else self._HP_PA_TX_CURRENT
        levels = sorted(table.keys())
        currents = [table[level] for level in levels]
        return float(np.interp(power, levels, currents)) * self._VDD

    def rx_power(self, config: LoraConfig) -> float:
        # The datasheet only specifies RX consumption for LoRa at 125 kHz bandwidth; the same
        # value is used for all configurations.
        return self._RX_CURRENT[(self._regulator, self._rx_boosted)] * self._VDD
