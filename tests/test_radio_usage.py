import pytest

from simulator.environment import simulation_env as sim
from simulator.lora.client_radio import LoraClientRadio
from simulator.lora.enums.radio_state import RadioState
from simulator.lora.radio_power_profile import EwsSensorPowerProfile
from simulator.lora.radio_usage import state_durations, update_cost


def test_update_cost_counts_only_the_window():
    radio = LoraClientRadio()
    radio.set_tx_config(power=10)

    async def device():
        await sim.sleep_until(10.0)
        await radio.receive()
        await sim.sleep_until(12.0)
        await radio.off()
        await sim.sleep_until(20.0)
        await radio.receive()
        await sim.sleep_until(21.0)
        await radio.off()

    sim.create_task(device())
    sim.run(simulation_length=30)

    startup = radio.power_profile.standby_startup_time()
    seconds = state_durations(radio, 0.0, 30.0)
    assert seconds[RadioState.RX] == pytest.approx(3.0 - 2 * startup)
    assert seconds[RadioState.STANDBY] == pytest.approx(2 * startup)
    assert seconds[RadioState.OFF] == pytest.approx(27.0)

    # Only the second second of the first window and the first half of the sleep after it.
    cost = update_cost(radio, start=11.0, end=16.0, updated=True)
    profile = EwsSensorPowerProfile()
    assert cost["cmp_rx_s"] == pytest.approx(1.0)
    assert cost["cmp_sleep_s"] == pytest.approx(4.0)
    assert cost["cmp_rx_j"] == pytest.approx(profile.RX_POWER)
    assert cost["cmp_mcu_j"] == pytest.approx(profile.MCU_RUN_POWER)
    assert cost["cmp_sleep_j"] == pytest.approx(4 * profile.SLEEP_POWER)
    assert cost["cmp_total_j"] == pytest.approx(
        profile.RX_POWER + profile.MCU_RUN_POWER + 4 * profile.SLEEP_POWER
    )

    # Charging less receiver time than was simulated turns the difference into sleep.
    cost = update_cost(radio, start=11.0, end=16.0, updated=True, rx_seconds=0.25)
    assert cost["cmp_rx_s"] == pytest.approx(0.25)
    assert cost["cmp_sleep_s"] == pytest.approx(4.75)
    assert cost["cmp_mcu_j"] == pytest.approx(0.25 * profile.MCU_RUN_POWER)
