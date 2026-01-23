import pytest
import pytest_asyncio
import asyncio
from simulator.environment import SimulationEnvironment


class TestSimulationEnvironment:
    """Tests for the SimulationEnvironment class."""

    def test_initial_state(self):
        """Test that a new simulation environment has correct initial state."""
        env = SimulationEnvironment()
        assert env.current_time() == 0.0
        assert not env.is_running()
        assert not env.is_finished()

    def test_custom_tick_size(self):
        """Test that custom tick size is applied correctly."""
        env = SimulationEnvironment(tick_size=0.001)
        assert env.current_time() == 0.0

    def test_create_task_before_run(self):
        """Test that tasks can be added before simulation starts."""
        env = SimulationEnvironment()
        
        async def dummy_task():
            await env.sleep(0.000001)
        
        # Should not raise
        env.create_task(dummy_task())

    def test_next_tick(self):
        """Test that next_tick returns correct value."""
        tick_size = 0.000001
        env = SimulationEnvironment(tick_size=tick_size)
        assert env.next_tick() == tick_size
