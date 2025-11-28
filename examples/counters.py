#!/usr/bin/env python3
import sys, time, asyncio, logging
sys.path.append('.')

from simulator.environment import simulation_env as sim

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

if __name__ == "__main__":
    async def counter_task(counter_id: int, count_to: int, delay: int):
        for i in range(count_to):
            await sim.sleep(delay)
            print(f"Counter {counter_id}: {sim.current_time():.2f}")
    
    # async def network_task():
    #     # Sleep in real time
    #     await sim.wait_within(asyncio.sleep(2), 4)
    #     print(f"Sleeping finished: {sim.current_time()}")
    

    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)

    formatter = logging.Formatter("%(asctime)s;%(levelname)s;%(message)s")

    # add formatter to ch
    ch.setFormatter(formatter)

    # add ch to logger
    logger.addHandler(ch)
    sim.logger.setLevel(logging.INFO)
    sim.logger.addHandler(ch)

    # Create multiple counter tasks with different parameters
    sim.create_task(counter_task(counter_id=1, count_to=5, delay=1))
    sim.create_task(counter_task(counter_id=2, count_to=3, delay=2))
    sim.create_task(counter_task(counter_id=3, count_to=4, delay=1.5))
    # sim.create_task(network_task())
    
    # Run the simulation for a sufficient length of time to allow all counters to complete
    sim.run(simulation_length=150)

