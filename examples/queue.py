#!/usr/bin/env python3
import sys, time, asyncio, logging
sys.path.append('.')

from simulator.environment import simulation_env as sim
from simulator.queue import Queue

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

if __name__ == "__main__":
    
    queue = Queue()

    async def putter():
        for i in range(2):
            await sim.sleep(10)
            print(f"{sim.current_time():.2f} - Task 1 put queue <- {i}")
            await queue.put(i)

    async def getter():
        while sim.current_time() < 130:
            try:
                item = await queue.get_timeout(3)
                # item = await queue.get_timeout(0.5)
                # item = await queue.get()
                print(f"{sim.current_time():.2f} - Task 2 got queue -> {item}")
            except asyncio.TimeoutError:
                print(f"{sim.current_time():.2f} - Task 2 timed out waiting for item")

    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)

    formatter = logging.Formatter("%(asctime)s;%(name)s;%(levelname)s;%(message)s")

    # add formatter to ch
    ch.setFormatter(formatter)

    # add ch to logger
    logger.addHandler(ch)
    sim.logger.setLevel(logging.INFO)
    sim.logger.addHandler(ch)

    # Create multiple counter tasks with different parameters
    sim.create_task(putter(), name="Putter Task")
    sim.create_task(getter(), name="Getter Task")
    
    # Run the simulation for a sufficient length of time to allow all counters to complete
    sim.run(simulation_length=150000)

