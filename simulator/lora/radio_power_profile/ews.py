from simulator.lora.radio_config import LoraConfig
from simulator.lora.radio_power_profile import RadioPowerProfile


class EwsSensorPowerProfile(RadioPowerProfile):
    """
        The energy model of the EWS sensor firmware (its `libs/energy`), datasheet estimates at
        3.6 V. This profile is the radio part; the MCU and sleep levels are constants for whoever
        models the rest of the board, the radio's own standby level is therefore zero.
    """
    VDD = 3.6
    RX_POWER = 17.3e-3
    PA_CURRENT = {10: 15e-3, 14: 45e-3}  # by output power in dBm
    MCU_RUN_POWER = 18e-3  # whenever the sensor is awake
    SLEEP_POWER = 5e-6  # the whole board asleep
    SESSION_ENERGY = 2 * 376e-6  # opening plus closing the radio once

    def standby_startup_time(self) -> float:
        return 100e-6

    def standby_power(self) -> float:
        return 0.0

    def tx_startup_time(self, power: float | int, config: LoraConfig) -> float:
        return 0.0

    def tx_power(self, power: float | int, config: LoraConfig) -> float:
        # Linear between the two known points and flat outside of them.
        share = min(max((power - 10) / 4, 0.0), 1.0)
        current = self.PA_CURRENT[10] + share * (self.PA_CURRENT[14] - self.PA_CURRENT[10])
        return current * self.VDD

    def rx_power(self, config: LoraConfig) -> float:
        return self.RX_POWER
