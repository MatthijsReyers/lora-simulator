#!/usr/bin/env python3
import sys
sys.path.append('.') # To allow importing the simulator package while running from root folder.
from simulator.environment import simulation_env as sim

async def dummy_child_task(counter_id: int):
    await sim.sleep(0.5)
    print(f"{sim.current_time():.2f} - Child Task of counter {counter_id} says hi!")

async def counter_task(counter_id: int, count_to: int, delay: int):
    for i in range(count_to):
        await sim.sleep(delay)
        print(f"{sim.current_time():.2f} - Counter {counter_id}: {i}")
        await sim.start_child_task(dummy_child_task(counter_id))

if __name__ == "__main__":

    # Create multiple counter tasks with different parameters
    sim.create_task(counter_task(counter_id=1, count_to=5, delay=1))
    sim.create_task(counter_task(counter_id=2, count_to=5, delay=2))
    
    # Run the simulation for a sufficient length of time to allow all counters to complete
    sim.run(simulation_length=150000)
