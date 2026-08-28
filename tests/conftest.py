"""
    Shared test fixtures.

    The simulator relies on two module-level singletons: `simulation_env` (which closes its
    asyncio event loop when `run()` finishes) and `LoraPhyLayer` (which keeps a class-level list
    of every radio ever subscribed). Without a reset between tests, any test that runs a
    simulation or creates a radio leaks state into the tests that follow — most visibly as
    "RuntimeError: Event loop is closed" when a later test constructs a radio or device.
"""
import asyncio
import pytest

import simulator.environment
from simulator.environment import SimulationEnvironment
from simulator.lora.phy_layer import LoraPhyLayer


@pytest.fixture(autouse=True)
def reset_simulator_singletons():
    """Give every test a fresh simulation environment and PHY layer."""

    # Re-initialize the global simulation environment in place. We cannot simply replace the
    # object because most modules bind it at import time via
    # `from simulator.environment import simulation_env as sim`.
    env = simulator.environment.simulation_env
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    SimulationEnvironment.__init__(env)

    # Drop the PHY layer singleton and its class-level subscriber list so radios from previous
    # tests no longer participate in this test's simulation.
    LoraPhyLayer._LoraPhyLayer__instance = None  # type: ignore[attr-defined]
    LoraPhyLayer._LoraPhyLayer__subscribers.clear()  # type: ignore[attr-defined]

    yield
