from typing import Optional
from simulator.environment import simulation_env as sim
import asyncio, logging

class Queue():
    """
        A Queue that works well within the simulator environment.
    """

    logger = logging.getLogger(__name__)

    __data: list
    __get_events: list[asyncio.Event]

    def __init__(self):
        self.__data = []
        self.__get_events = []


    def __len__(self):
        return len(self.__data)


    async def put(self, item):
        """
            Put an item into the queue, note that this will advance the simulation by one tick.
        """
        self.logger.debug(f"{sim.current_time():.2f} - put({item})")
        self.__data.append(item)
        
        if len(self.__get_events) == 0:
            return # No one is waiting for an item
        
        # Schedule the first waiting task for an item to be woken up in the next tick
        event = self.__get_events.pop(0)
        self.logger.debug(f"{sim.current_time():.2f} - put({item}) waking event {id(event) % 1000}")

        timestamp = sim.next_tick()
        await sim.schedule_event_no_await(event, timestamp)

        # Wait one tick to simulate time required for moving data.
        await sim.advance_tick()


    async def get(self):
        """
            Get an item from the queue, allowing the simulator to advance time while we wait for
            the queue to have an item available.
        """
        self.logger.debug(f"{sim.current_time():.2f} - get()")
        return await self.__get(sim.last_tick() - 1)


    async def get_timeout(self, timeout: float):
        """
            Get an item from the queue, allowing the simulator to advance time while we wait for
            the queue to have an item available.

            Raises asyncio.TimeoutError if no item is available within the specified timeout.
        """
        self.logger.debug(f"{sim.current_time():.2f} - get_timeout({timeout})")
        return await self.__get(sim.current_time() + timeout)


    async def __get(self, timeout_at=float):
        # If there is data available and no one is waiting, return immediately
        if len(self.__data) > 0 and len(self.__get_events) == 0:
            return self.__data.pop(0)
        
        if timeout_at is None:
            timeout_at = sim.last_tick() - 1

        event = asyncio.Event()

        self.logger.debug(f"{sim.current_time():.2f} - waiting for event {id(event) % 1000}")

        # Add our event to the list of waiting getters, if data is put before the timeout someone
        # will schedule this event to be set and we will wake up normally. 
        self.__get_events.append(event)

        # Schedule a timeout event to wake us up if the timeout expires first
        await sim.schedule_event_wait(event, timeout_at)

        # Did the await finish because data was put, or because we timed out?
        if len(self.__data) == 0:
            self.__get_events.remove(event)
            raise asyncio.TimeoutError()

        return self.__data.pop(0)
    
