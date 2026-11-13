"""
    What a radio did between two moments, and what that costs with a given power profile.

    The cost does not depend on the power profile the radio was simulated with: it is worked out
    afterwards from the time spent in every state, so that devices of different protocols can be
    compared with one and the same energy model.
"""
import math

from simulator.lora.enums.radio_state import RadioState
from simulator.lora.radio import LoraRadio
from simulator.lora.radio_power_profile import EwsSensorPowerProfile, RadioPowerProfile


def state_durations(radio: LoraRadio, start: float, end: float) -> dict[RadioState, float]:
    """ Seconds the radio spent in every state between `start` and `end`. """
    seconds = {state: 0.0 for state in RadioState}
    (since, state) = (0.0, RadioState.OFF)
    for (moment, next_state) in radio.state_log + [(math.inf, RadioState.OFF)]:
        overlap = min(moment, end) - max(since, start)
        if overlap > 0:
            seconds[state] += overlap
        (since, state) = (moment, next_state)
    return seconds


def update_cost(
        radio: LoraRadio,
        start: float,
        end: float,
        updated: bool,
        profile: RadioPowerProfile | None = None,
        mcu_run_power: float = EwsSensorPowerProfile.MCU_RUN_POWER,
        sleep_power: float = EwsSensorPowerProfile.SLEEP_POWER,
        rx_seconds: float | None = None,
    ) -> dict[str, float | bool]:
    """
        The cost of a firmware update for one device, in the columns every FUOTA scenario
        reports for the protocol comparison (`papers/fuota-comparison`).

        :param start: When the update started for this device: the first frame of the protocol
            it sent or could have received.
        :param end: When the device held the complete image, or when it gave up.
        :param updated: Whether it did get the image.
        :param profile: Radio power levels, the EWS sensor's by default.
        :param mcu_run_power: Drawn by the rest of the board whenever the radio is not off.
        :param sleep_power: Drawn by the whole board while the radio is off.
        :param rx_seconds: For a scenario whose devices are simulated with the receiver always
            on although the protocol only needs it for part of that time: the receiver time to
            charge. The rest of the simulated receive time counts as sleep.
    """
    profile = profile or EwsSensorPowerProfile()
    seconds = state_durations(radio, start, end)
    if rx_seconds is not None:
        seconds[RadioState.OFF] += seconds[RadioState.RX] - rx_seconds
        seconds[RadioState.RX] = rx_seconds
    awake = seconds[RadioState.TX] + seconds[RadioState.RX] + seconds[RadioState.STANDBY]
    rx_power = sum(profile.rx_power(chain.config) for chain in radio.rx_chains if chain.enabled)

    cost: dict[str, float | bool] = {
        "cmp_updated": updated,
        "cmp_time_s": end - start,
        "cmp_tx_s": seconds[RadioState.TX],
        "cmp_rx_s": seconds[RadioState.RX],
        "cmp_standby_s": seconds[RadioState.STANDBY],
        "cmp_sleep_s": seconds[RadioState.OFF],
        "cmp_tx_j": seconds[RadioState.TX] * profile.tx_power(radio.tx_power, radio.tx_config),
        "cmp_rx_j": seconds[RadioState.RX] * rx_power,
        "cmp_standby_j": seconds[RadioState.STANDBY] * profile.standby_power(),
        "cmp_mcu_j": awake * mcu_run_power,
        "cmp_sleep_j": seconds[RadioState.OFF] * sleep_power,
    }
    cost["cmp_total_j"] = sum(float(cost[key]) for key in cost if key.endswith("_j"))
    return cost
