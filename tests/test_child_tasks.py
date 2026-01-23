import pytest
import pytest_asyncio
import asyncio
from simulator.environment import SimulationEnvironment


class TestChildTasks:
    """Tests for child task functionality based on the child_tasks example."""

    def test_child_tasks_execution_order(self):
        """
        Test that child tasks execute at the correct simulation times.
        Based on examples/child_tasks.py
        """
        sim = SimulationEnvironment()
        events: list[tuple[float, str]] = []

        async def dummy_child_task(counter_id: int):
            await sim.sleep(0.5)
            events.append((sim.current_time(), f"child_{counter_id}"))

        async def counter_task(counter_id: int, count_to: int, delay: int):
            for i in range(count_to):
                await sim.sleep(delay)
                events.append((sim.current_time(), f"counter_{counter_id}_{i}"))
                await sim.start_child_task(dummy_child_task(counter_id))

        # Create multiple counter tasks with different parameters
        sim.create_task(counter_task(counter_id=1, count_to=5, delay=1))
        sim.create_task(counter_task(counter_id=2, count_to=5, delay=2))

        # Run the simulation for sufficient time
        sim.run(simulation_length=15)

        # Expected events based on the logic:
        # Counter 1 (delay=1): prints at t=1,2,3,4,5 -> children finish at t=1.5,2.5,3.5,4.5,5.5
        # Counter 2 (delay=2): prints at t=2,4,6,8,10 -> children finish at t=2.5,4.5,6.5,8.5,10.5
        #
        # Note: Events at the same timestamp may occur in any order due to async scheduling,
        # so we group expected events by timestamp and check that all events in each group occur.
        expected_events_by_time: dict[float, set[str]] = {
            1.0: {"counter_1_0"},
            1.5: {"child_1"},
            2.0: {"counter_1_1", "counter_2_0"},
            2.5: {"child_1", "child_2"},
            3.0: {"counter_1_2"},
            3.5: {"child_1"},
            4.0: {"counter_1_3", "counter_2_1"},
            4.5: {"child_1", "child_2"},
            5.0: {"counter_1_4"},
            5.5: {"child_1"},
            6.0: {"counter_2_2"},
            6.5: {"child_2"},
            8.0: {"counter_2_3"},
            8.5: {"child_2"},
            10.0: {"counter_2_4"},
            10.5: {"child_2"},
        }

        # Group actual events by timestamp
        actual_events_by_time: dict[float, list[str]] = {}
        for time, event in events:
            # Round to avoid floating point issues
            time = round(time, 1)
            if time not in actual_events_by_time:
                actual_events_by_time[time] = []
            actual_events_by_time[time].append(event)

        # Check that all expected timestamps are present
        assert set(actual_events_by_time.keys()) == set(expected_events_by_time.keys()), \
            f"Timestamps mismatch.\nExpected: {sorted(expected_events_by_time.keys())}\nActual: {sorted(actual_events_by_time.keys())}"

        # Check that each timestamp has the expected events
        for time, expected_events in expected_events_by_time.items():
            actual = actual_events_by_time.get(time, [])
            assert set(actual) == expected_events, \
                f"At t={time}: expected {expected_events}, got {set(actual)}"
