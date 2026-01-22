#!/usr/bin/env python3
import sys
sys.path.append('.') # To allow importing the simulator package while running from root folder.
from simulator.environment import simulation_env as sim

async def counter_task(counter_id: int, count_to: int, delay: int):
    for i in range(count_to):
        await sim.sleep(delay)
        print(f"{sim.current_time():.2f} - Counter {counter_id}: {i}")

if __name__ == "__main__":

    # Create multiple counter tasks with different parameters
    sim.create_task(counter_task(counter_id=1, count_to=5, delay=1))
    sim.create_task(counter_task(counter_id=2, count_to=3, delay=2))
    sim.create_task(counter_task(counter_id=3, count_to=4, delay=1.5))
    
    # Run the simulation for a sufficient length of time to allow all counters to complete
    sim.run(simulation_length=150000)
