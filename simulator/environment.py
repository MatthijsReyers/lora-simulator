from collections.abc import Coroutine
from simulator.wakeup_queue import WakeUpQueue
import asyncio
import logging

class SimulationEnvironment:
    """ 
        Simulation environment that manages the simulation time and coordinates the nodes. 

        The simulation environment advances the simulation time in discrete ticks (defined by
        self.__TICK_SIZE) and allows nodes to sleep and wait for a specific simulation time.
        
        Note that as long as any task is not within a `sleep`, `wait`, or `wait_real` call the
        simulation time does not advance. This means you can perform any amount of computation
        within a simulation tick, even real network calls of unknown time duration, without 
        affecting the simulation time.
    """

    __current_tick: int
    
    __timer_lock: asyncio.Lock
    __timer_locks: int

    __wakeup_events: WakeUpQueue
    __tasks: list

    __TICK_SIZE: float

    logger = logging.getLogger('simulator')

    def __init__(self, tick_size: float = 0.0001):
        self.__current_tick = 0
        self.__wakeup_events = WakeUpQueue()
        self.__timer_lock = asyncio.Lock()
        self.__timer_locks = 0
        self.__tasks = []
        self.__TICK_SIZE = tick_size
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
        self.logger.debug(f'{self.current_time():.2f} __wait_for_timer_unlock(), {self.__timer_locks}')
        while True:
            await self.__timer_lock.acquire()
            if self.__timer_locks == 0:
                # This return path does not release the lock on purpose, the caller is responsible 
                # for unlocking the timer lock.
                return 
            self.__timer_lock.release()
            await asyncio.sleep(0)


    async def __run_simulation(self, simulation_length: float):
        ticks = round(simulation_length / self.__TICK_SIZE)
        while self.__current_tick < ticks:
            await self.__wait_for_timer_unlock()

            next_tick = await self.__wakeup_events.peek_tick()
            
            if next_tick is None:
                # No tasks to wake up, we're done.
                self.__current_tick = ticks
                break

            else:
                # Wake up any tasks that are scheduled to wake up at the current time
                tick, events = await self.__wakeup_events.next_tick()

                self.__current_tick = tick 

                for event in events:
                    event.set()
                    # Prevent the simulation timer from advancing while the task is running
                    self.__timer_locks += 1

            self.__timer_lock.release()


    async def __task_finished(self):
        """ 
            Notifies the simulation environment that a task has finished executing. This allows
            the simulation timer to advance again if there are no other active tasks.
        """
        self.logger.debug(f'{self.current_time():.2f} __task_finished()')
        await self.__dec_timer_lock()


    def run(self, simulation_length: int, loop: asyncio.AbstractEventLoop = None):
        """
            Runs the simulation for the given length in seconds. 
        """
        assert self.__current_tick == 0, "Simulation can only be run once."
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
            `wait_real`.
        """
        if self.__current_tick > 0:
            raise RuntimeError("Cannot add tasks after the simulation has started.")
        
        # We can increment the timer lock here without acquiring the lock because this method can
        # only be called before the async runtime is setup and the simulation has started.
        self.__timer_locks += 1

        async def wrapped_task():
            # Increment the timer lock since there is now one more active task which might block
            # the simulation time from advancing.
            await self.wait_for_sim_start()
            t = asyncio.create_task(task)
            await t
            await self.__task_finished()

        self.__tasks.append(wrapped_task)


    def current_time(self) -> int:
        """ Returns the current simulation time in seconds. """
        return self.__current_tick * self.__TICK_SIZE


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
        if self.__current_tick == 0:
            await self.sleep(self.__TICK_SIZE)


    async def sleep(self, duration: float):
        """ 
            Wait for the simulation time to advance for the given duration.
        """
        self.logger.debug(f'{self.current_time():.2f} sleep({duration})')
        
        # Compute timestamp at which we need to wake up the task.
        wakeup_time = self.__current_tick + round(duration / self.__TICK_SIZE)
        
        # Register an event to be set when the timer hits the wakeup time
        event = asyncio.Event()
        await self.__wakeup_events.add(wakeup_time, event)

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
            limited amount of ticks you should use `wait_real` instead.

            IMPORTANT: There is almost NEVER a reason to use this method, technically it would
            allow you to run the entire simulation while waiting for a single real network call to
            complete. You should almost always use `wait_real` instead to model the future taking
            some amount of time within the simulation.
        """

        # Reduce the timer lock counter so the simulation timer can advance
        await self.__dec_timer_lock()

        # Wait for the future to complete
        await future

        # Re-acquire the timer lock to prevent the simulation timer from advancing while the task
        # is running...
        await self.__inc_timer_lock()


    async def wait_real(self, future, duration: int):
        """ 
            Waits for the given future to complete while allowing the simulation time to advance
            only for the given duration (in seconds). 
            
            Note that this function does not throw a timeout error! The future can take any 
            arbitrary amount of real time to complete, but the simulation time will only be allowed
            to advance for the given duration.
            
            This method can be used to simulate real network calls or other blocking operations
            taking some amount of time within the simulation.
        """
        self.logger.debug(f'{self.current_time():.2f} wait_real({duration})')
        # Run both the sleep and the future wait concurrently
        await self.sleep(duration)
        await future

simulation_env = SimulationEnvironment()

