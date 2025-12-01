#!/usr/bin/env python3
import sys, time, asyncio, logging, random
sys.path.append('.')

from simulator.environment import simulation_env as sim

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

async def make_http_request():
    # Note we use asyncio.sleep here instead of sim.sleep to simulate real a real blocking network
    # call that takes time outside of the simulation environment.
    print(f"Starting HTTP request")
    delay = random.uniform(0.5, 2.0)
    print(f'Simulated network delay: {delay:.2f} seconds')
    await asyncio.sleep(delay)
    print(f"HTTP request finished")

if __name__ == "__main__":
    async def counter_task(counter_id: int, count_to: int, delay: int):
        for i in range(count_to):
            await sim.sleep(delay)
            print(f"{sim.current_time():.2f} - Counter {counter_id}: {i}")
        print('Counter done...')

    async def network_task():
        await sim.sleep(1.5)

        # Make an http request that takes some unknown amount of real time, and advance
        # the simulation by a static amount to simulate a consistent waiting time.
        simulated_duration = 3
        print(f"Starting network task at simulation time: {sim.current_time():.2f}")
        await sim.wait_real(
            make_http_request(), 
            simulated_duration
        )
        print(f"Network task completed at simulation time: {sim.current_time():.2f}")

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
    sim.create_task(counter_task(counter_id=1, count_to=6, delay=1))
    sim.create_task(counter_task(counter_id=2, count_to=6, delay=2))
    sim.create_task(network_task())
    
    # Run the simulation for a sufficient length of time to allow all counters to complete
    sim.run(simulation_length=150000)

