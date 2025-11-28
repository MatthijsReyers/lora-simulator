
import asyncio


class WakeUpQueue():
    __data_lock: asyncio.Lock
    __data: list[tuple[int, list[asyncio.Event]]]

    def __init__(self):
        self.__data = []
        self.__data_lock = asyncio.Lock()
    
    async def add(self, wakeup_tick: int, event: asyncio.Event):
        """ 
            Adds a wakeup event for the given tick.
        """
        assert wakeup_tick >= 0, "Wakeup tick must be non-negative"

        # Acquire the data lock
        await self.__data_lock.acquire()

        # Find the position to insert the new wakeup event
        pos = 0
        while pos < len(self.__data) and self.__data[pos][0] < wakeup_tick:
            pos += 1
        
        if pos < len(self.__data) and self.__data[pos][0] == wakeup_tick:
            # Tick already exists, append the event to the list
            self.__data[pos][1].append(event)
        else:
            # Insert a new entry
            self.__data.insert(pos, (wakeup_tick, [event]))

        # Release the data lock
        self.__data_lock.release()


    async def peek_tick(self) -> int | None:
        """ 
            Returns the tick of the next wakeup event without removing it.
            Returns None if the queue is empty.
        """
        await self.__data_lock.acquire()
        try:
            if len(self.__data) == 0:
                return None
            return self.__data[0][0]
        finally:
            self.__data_lock.release()


    async def next_tick(self) -> None | tuple[int, list[asyncio.Event]]:
        """ 
            Returns the next tick and the events at that tick.
            Returns None if the queue is empty.
        """
        await self.__data_lock.acquire()
        try:
            if len(self.__data) == 0:
                return None
            return self.__data.pop(0)
        finally:
            self.__data_lock.release()