"""The end of a simulation must not break the tasks that are still running.

``SimulationEnvironment.sleep_until`` does not raise when the simulation runs out — the
environment simply wakes every sleeper one last time at the final tick. A device that is
still in Class B or Class C therefore resumes a ping slot or a receive loop *after* the
simulation is over, and used to drive the radio (and with it
:class:`~simulator.power_consumer.PowerConsumer`) one more time, which raised

    AttributeError: 'PowerConsumer' object has no attribute '_PowerConsumer__events'

out of an unrelated background task. These are full ``sim.run()`` tests on purpose: the
behaviour only exists in the shutdown path of the real environment.
"""

from __future__ import annotations

import logging

import pytest

from simulator.environment import simulation_env as sim
from simulator.lora.phy_layer import LoraPhyLayer
from simulator.lorawan.device import DeviceSession, LoRaWanDevice
from simulator.lorawan.enums.operating_mode import OperatingMode
from simulator.lorawan.gateway import LoRaWanGateway
from simulator.lorawan.network_server import NetworkServer

DEV_ADDR = 0x26019001
NWK_S_KEY = bytes.fromhex("2B7E151628AED2A6ABF7158809CF4F3C")
APP_S_KEY = bytes.fromhex("3C4F9C098815F7ABA6D2AE281615E72B")


def _device(index: int = 0) -> LoRaWanDevice:
    return LoRaWanDevice(
        session=DeviceSession(
            dev_addr=DEV_ADDR + index, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY
        )
    )


class TestSimulationEndWithActiveModes:
    """A background task that dies at shutdown is only *logged* by the environment.

    ``SimulationEnvironment`` wraps every task and turns an escaping exception into an
    ``ERROR`` log record (and a ``sys.exit(1)`` the event loop swallows), so these tests
    assert on the log rather than expecting ``sim.run`` to raise.
    """

    def _run(
        self, mode: OperatingMode, *, class_b_gateway: bool, caplog: pytest.LogCaptureFixture
    ) -> LoRaWanDevice:
        caplog.set_level(logging.ERROR, logger="simulator")
        LoraPhyLayer()
        ns = NetworkServer()
        ns.register_device(DEV_ADDR, NWK_S_KEY, APP_S_KEY)
        LoRaWanGateway(network_server=ns, class_b_enabled=class_b_gateway)

        device = _device()

        async def behaviour() -> None:
            await sim.sleep(1.0)
            await device.switch_mode(mode)

        sim.create_task(behaviour())
        # Long enough for the device to acquire a beacon and open ping slots, so the
        # simulation ends while one of those windows is pending.
        sim.run(simulation_length=1290)
        assert [record.message for record in caplog.records] == []
        return device

    def test_a_device_left_in_class_b_does_not_raise(self, caplog):
        device = self._run(
            OperatingMode.CLASS_B, class_b_gateway=True, caplog=caplog
        )
        assert device.operating_mode is OperatingMode.CLASS_B
        # The power trace is still readable afterwards; it used to be deleted.
        assert device.radio.power_consumer.get_total_energy_consumed() >= 0.0
        assert device.radio.power_consumer.events is not None

    def test_a_device_left_in_class_c_does_not_raise(self, caplog):
        device = self._run(
            OperatingMode.CLASS_C, class_b_gateway=False, caplog=caplog
        )
        assert device.operating_mode is OperatingMode.CLASS_C
        assert device.radio.power_consumer.get_total_energy_consumed() > 0.0

    def test_the_power_consumer_tolerates_a_state_change_after_the_run(self, caplog):
        device = self._run(
            OperatingMode.CLASS_C, class_b_gateway=False, caplog=caplog
        )
        consumer = device.radio.power_consumer
        before = consumer.get_total_energy_consumed()
        # Exactly what a late ping slot does through radio._set_state().
        consumer.set_power_consumption(0.0)
        assert consumer.get_total_energy_consumed() == pytest.approx(before)
