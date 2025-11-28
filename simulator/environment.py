
import asyncio
from collections.abc import Coroutine
import logging
from venv import logger

SIMULATION_TICK = 0.001  # 1 ms

class SimulationEnvironment:
    """ 
        Simulation environment that manages the simulation time and coordinates the nodes. 

        The simulation environment advances the simulation time in discrete ticks (defined by
        SIMULATION_TICK) and allows nodes to sleep and wait for a specific simulation time.
        
        Note that as long as any task is not within a `sleep`, `wait`, or `wait_until` call the
        simulation time does not advance. This means you can perform any amount of computation
        within a simulation tick, even real network calls of unknown time duration, without 
        affecting the simulation time.
    """

    __current_time: int
    
    __timer_lock: asyncio.Lock
    __timer_locks: int

    __wakeup_events: dict[int, list[asyncio.Event]]
    __tasks: list

    logger = logging.getLogger('simulator')

    def __init__(self):
        self.__current_time = 0
        self.__wakeup_events = {}
        self.__timer_lock = asyncio.Lock()
        self.__timer_locks = 0
        self.__tasks = []
        self.logger.setLevel(logging.WARN)


    async def __inc_timer_lock(self):
        self.logger.debug(f'{self.current_time():.2f} __inc_timer_lock(), {self.__timer_locks + 1}')
        await self.__timer_lock.acquire()
        self.__timer_locks += 1
        self.__timer_lock.release()


    async def __dec_timer_lock(self):
        self.logger.debug(f'{self.current_time():.2f} __dec_timer_lock(), {self.__timer_locks - 1}')
        await self.__timer_lock.acquire()
        self.__timer_locks -= 1
        assert self.__timer_locks >= 0, "Timer locks cannot be negative"
        self.__timer_lock.release()


    async def __wait_for_timer_unlock(self):
        self.logger.debug(f'{self.current_time():.2f} __wait_for_timer_unlock(), {self.__timer_locks - 1}')
        while True:
            await self.__timer_lock.acquire()
            if self.__timer_locks == 0:
                # This return path does not release the lock on purpose, the caller is responsible 
                # for unlocking the timer lock.
                return 
            self.__timer_lock.release()
            await asyncio.sleep(0)


    async def __run_simulation(self, simulation_length: float):
        while self.current_time() < simulation_length:
            await self.__wait_for_timer_unlock()
            # Wake up any tasks that are scheduled to wake up at the current time
            if self.__current_time in self.__wakeup_events:
                for event in self.__wakeup_events[self.__current_time]:
                    event.set()
                    # Prevent the simulation timer from advancing while the task is running
                    self.__timer_locks += 1
                del self.__wakeup_events[self.__current_time]
            # Advance the simulation time by one tick
            self.__current_time += 1
            self.__timer_lock.release()


    async def __task_finished(self):
        """ 
            Notifies the simulation environment that a task has finished executing. This allows
            the simulation timer to advance again if there are no other active tasks.
        """
        await self.__dec_timer_lock()


    def run(self, simulation_length: int, loop: asyncio.AbstractEventLoop = None):
        """
            Runs the simulation for the given length in seconds. 
        """
        if not loop:
            loop = asyncio.get_event_loop()
        loop.run_until_complete(asyncio.wait([
            *[loop.create_task(task()) for task in self.__tasks],
            loop.create_task(self.__run_simulation(simulation_length))
        ]))
        loop.close()


    def create_task(self, task: 'Coroutine'):
        """ 
            Adds a new async task to the simulation environment to run during the simulation. This must be done before the
            simulation starts.

            Note that by calling this method you create a task that automatically blocks the
            simulation time from advancing until the task itself calls `sleep`, `wait`, or 
            `wait_within`.
        """
        if self.__current_time > 0:
            raise RuntimeError("Cannot add tasks after the simulation has started.")
        
        # We can increment the timer lock here without acquiring the lock because this method can
        # only be called before the async runtime is setup and the simulation has started.
        self.__timer_locks += 1

        async def wrapped_task():
            # Increment the timer lock since there is now one more active task which might block
            # the simulation time from advancing.
            await self.wait_for_sim_start()
            await asyncio.create_task(task)
            await self.__task_finished()

        self.__tasks.append(wrapped_task)


    def current_time(self) -> int:
        """ Returns the current simulation time in seconds. """
        return self.__current_time * SIMULATION_TICK


    async def wait_for_sim_start(self):
        """ 
            Waits until the simulation actually starts (i.e., the simulation timer starts 
            advancing). Note that we automatically prepend this to all tasks added to the
            simulation so that they don't start executing before the simulation actually starts.

            You should only ever need to call this method if you are creating tasks manually
            outside of the simulation environment that still need to be synchronized with the
            start of the simulation.
            Although if you are doing that you should really reconsider your design because I
            cannot think of any valid use case for it??
        """
        # Sleeping for 1 tick at the start of the simulation essentially just detects the moment
        # the simulation time starts advancing.
        if self.__current_time == 0:
            await self.sleep(SIMULATION_TICK)


    async def sleep(self, duration: float):
        """ 
            Wait for the simulation time to advance for the given duration.
        """
        self.logger.debug(f'{self.current_time():.2f} sleep({duration})')
        
        # Compute timestamp at which we need to wake up the task.
        wakeup_time = self.__current_time + round(duration / SIMULATION_TICK)
        
        # Register an event to be set when the timer hits the wakeup time
        if wakeup_time not in self.__wakeup_events:
            self.__wakeup_events[wakeup_time] = []
        event = asyncio.Event()
        self.__wakeup_events[wakeup_time].append(event)

        # Reduce the timer lock counter so the simulation timer can advance
        await self.__dec_timer_lock()

        # Wait for the timer to hit the wakeup time
        await event.wait()

        # # Re-acquire the timer lock to prevent the simulation timer from advancing while the task
        # # is running...
        # await self.__inc_timer_lock()

        return event
    

    async def wait(self, future):
        """ 
            Waits for the given future to complete while allowing the simulation timer to advance
            in the mean time. Note that if you only want to allow the simulation to advance for a
            limited amount of ticks you should use `wait_within` instead.

            IMPORTANT: There is almost NEVER a reason to use this method, technically it would
            allow you to run the entire simulation while waiting for a single real network call to
            complete. You should almost always use `wait_within` instead to model a network call 
            taking some amount of time within the simulation.
        """

        # Reduce the timer lock counter so the simulation timer can advance
        await self.__dec_timer_lock()

        # Wait for the future to complete
        await future

        # Re-acquire the timer lock to prevent the simulation timer from advancing while the task
        # is running...
        await self.__inc_timer_lock()


    async def wait_within(self, future, time_limit: int):
        """ 
            Waits for the given future to complete while allowing the simulation time to advance
            only for the given time limit (in seconds). 
            
            Note that this function does not throw a timeout error! The future can take any 
            arbitrary amount of real time to complete, but the simulation time will only be allowed
            to advance for the given time limit.

            This method can be used to simulate real network calls or other blocking operations
            taking some amount of time within the simulation.
        """
        self.logger.debug(f'{self.current_time():.2f} wait_within({time_limit})')
        # Run both the sleep and the future wait concurrently
        await self.sleep(time_limit),
        await future

simulation_env = SimulationEnvironment()

