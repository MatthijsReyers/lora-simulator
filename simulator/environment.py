from collections.abc import Coroutine
from functools import wraps
from typing import Any, List, Optional
from simulator.exceptions import SimulatorException
from simulator.wakeup_queue import WakeUpQueue
import asyncio
import logging


def requires_running_simulation(method):
    """
    (Internal use on SimulationEnvironment class only)

    Decorator that enforces the method is only called when the simulation is running.
    Raises a SimulatorException if called after the simulation has finished.
    """
    @wraps(method)
    async def wrapper(self, *args, **kwargs):
        if not self.is_running() and self._SimulationEnvironment__current_tick > 0:
            raise SimulatorException(f"Cannot call {method.__name__}, simulation is not running.")
        return await method(self, *args, **kwargs)
    return wrapper


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

    # Current simulation tick
    __current_tick: int

    # Total length of the simulation in ticks
    __simulation_length: Optional[int]

    # Every task running in the simulation environment increments this lock counter by 1. When the
    # counter is greater than 0 the simulation time does not advance. This ensures that as long as
    # there are active tasks the simulation time is blocked from advancing.
    __timer_lock: asyncio.Lock
    __timer_locks: int

    __wakeup_events: WakeUpQueue
    __tasks: List[asyncio.Task[Any]]

    # Length of a single simulation tick in seconds
    __tick_size: float

    logger = logging.getLogger('simulator')


    def __init__(self, tick_size: float = 0.000001):
        try:
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            self.__loop = loop
        except RuntimeError:
            self.__loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.__loop)
        self.__current_tick = 0
        self.__simulation_length = None
        self.__wakeup_events = WakeUpQueue()
        self.__timer_lock = asyncio.Lock()
        self.__timer_locks = 0
        self.__tasks = []
        self.__tick_size = tick_size
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


    async def __run_simulation(self):
        while self.__current_tick < self.__simulation_length:
            await self.__wait_for_timer_unlock()

            next_tick = await self.__wakeup_events.peek_tick()
            
            if next_tick is None:
                # No tasks to wake up, we're done.
                self.__current_tick = self.__simulation_length
                break

            else:
                # Wake up any tasks that are scheduled to wake up at the current time
                tick, events = await self.__wakeup_events.next_tick()

                self.__current_tick = tick 

                for event in events:
                    event.set()
                    # Prevent the simulation timer from advancing while the task is running
                    self.logger.debug(f'{self.current_time():.2f} __run_simulation: <event {id(event) % 1000}>.set(), {self.__timer_locks} + 1')
                    
                    self.__timer_locks += 1

            self.__timer_lock.release()

        self.logger.info(f"Simulation reached the specified length of {self.__simulation_length * self.__tick_size}s")
        await asyncio.sleep(0.1)
        for task in self.__tasks:
            if not task.done():
                # self.logger.warning(f"Cancelling unfinished task {task}")
                task.cancel()


    async def __task_finished(self):
        """ 
            Notifies the simulation environment that a task has finished executing. This allows
            the simulation timer to advance again if there are no other active tasks.
        """
        self.logger.debug(f'{self.current_time():.2f} __task_finished()')
        await self.__dec_timer_lock()


    def run(self, simulation_length: int):
        """
            Runs the simulation for the given length in seconds. 
        """
        assert self.__current_tick == 0, "Simulation can only be run once."

        self.__simulation_length = round(simulation_length / self.__tick_size)

        loop = asyncio.get_event_loop()
        loop.run_until_complete(asyncio.wait({  # type: ignore[arg-type]
            *self.__tasks,
            loop.create_task(self.__run_simulation())
        }))
        loop.close()


    def create_task(self, task: Coroutine[Any, Any, Any], name: str|None = None):
        """ 
            Adds a new async task to the simulation environment to run once the simulation starts,
            this can only be called before the simulation starts. If you want to create a task 
            from within another task while the simulation is running use `start_child_task()` 
            instead.

            Note that by calling this method you create a task that automatically blocks the
            simulation time from advancing until the task itself calls `sleep`, `wait`, 
            `wait_real`, or finishes.
        """
        if self.__current_tick > 0:
            raise SimulatorException(
                "Cannot add a new root task after the simulation has started, if you want to \
                start a new task from a currently running task use start_child_task() instead."
            )
        
        # We can increment the timer lock here without acquiring the lock because this method can
        # only be called before the async runtime is setup and the simulation has started.
        self.__timer_locks += 1

        async def wrapped_task():
            await self.wait_for_sim_start()
            try:
                await asyncio.create_task(task)
            except Exception as e:
                self.logger.error(f"Task raised an exception: {e}")
                import traceback, sys
                traceback.print_exc()
                sys.exit(1)
            await self.__task_finished()

        loop = asyncio.get_event_loop()
        t = loop.create_task(wrapped_task(), name=name)
        self.__tasks.append(t)


    async def start_child_task(self, task: Coroutine[Any, Any, Any], name: str|None = None):
        """ 
            Add a new task to the simulation environment while the simulation is running, this can
            and should only be called from within another task that is already running in the 
            simulation.

            Note that by calling this method you create a task that automatically blocks the
            simulation time from advancing until the task itself calls `sleep`, `wait`, 
            `wait_real`, or finishes.
        """
        task_start_event = asyncio.Event()

        async def wrapped_task():
            self.logger.debug(f'{self.current_time():.2f} start_child_task, {self.__timer_locks}')

            # Increment the timer lock since there is now one more active task which might block
            # the simulation time from advancing.
            await self.__timer_lock.acquire()
            self.logger.debug(f'{self.current_time():.2f} start_child_task locked')
            self.__timer_locks += 1
            self.__timer_lock.release()
            task_start_event.set()
            try:
                await asyncio.create_task(task)
            except Exception as e:
                self.logger.error(f"Task raised an exception: {e}")
                import traceback, sys
                traceback.print_exc()
                sys.exit(1)
            await self.__task_finished()

        loop = asyncio.get_event_loop()
        t = loop.create_task(wrapped_task(), name=name)
        self.__tasks.append(t)

        # Wait for the task to increment the timer lock so the simulation time does not 
        # accidentally advance before the new child task starts executing.
        self.logger.debug(f'{self.current_time():.2f} start_child_task, waiting for task start')
        await task_start_event.wait()
        self.logger.debug(f'{self.current_time():.2f} start_child_task, finished')


    def is_running(self) -> bool:
        """ Returns whether the simulation is currently running. """
        return (self.__current_tick > 0) and \
            (type(self.__simulation_length) == int) and \
            (self.__simulation_length > 0) and \
            (self.__current_tick < self.__simulation_length - 1)


    def is_finished(self) -> bool:
        """ Returns whether the simulation has finished. """
        if self.__simulation_length is None:
            return False
        return self.__current_tick >= self.__simulation_length - 1


    def current_time(self) -> float:
        """ Returns the current simulation time in seconds. """
        return self.__current_tick * self.__tick_size


    def next_tick(self) -> float:
        """ Returns the simulation timestamp of the next tick in seconds. """
        return (self.__current_tick + 1) * self.__tick_size


    def last_tick(self) -> float:
        """ Returns the simulation timestamp at which the simulation will end in seconds. """
        return (type(self.__simulation_length) == int) and \
            (self.__simulation_length - 1) * self.__tick_size


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
            await self.sleep(self.__tick_size)


    async def wait_for_sim_end(self):
        """ 
            Waits until the simulation is over, some classes use this to perform cleanup or final 
            processing at the end of the simulation.
        """
        duration_ticks = self.__simulation_length - self.__current_tick
        await self.sleep(duration_ticks * self.__tick_size)


    @requires_running_simulation
    async def sleep(self, duration: float):
        """ 
            Wait for the simulation time to advance for the given duration.
        """
        self.logger.debug(f'{self.current_time():.2f} sleep({duration})')

        # Compute timestamp at which we need to wake up the task.
        wakeup_time = self.__current_tick + round(duration / self.__tick_size)
        
        # Register an event to be set when the timer hits the wakeup time
        event = asyncio.Event()

        self.logger.debug(f'{self.current_time():.2f} sleep event {id(event) % 1000} scheduled for tick {wakeup_time}')

        await self.__wakeup_events.add(wakeup_time, event)

        # Reduce the timer lock counter so the simulation timer can advance
        await self.__dec_timer_lock()

        try:
            # Wait for the timer to hit the wakeup time
            await event.wait()
        except asyncio.CancelledError as e:
            # self.logger.warning(f'{self.current_time():.2f} sleep({duration}) cancelled')

            # If the sleep is cancelled we need to remove the wakeup event from the wakeup queue
            removed_instances = await self.__wakeup_events.remove(event)
            if removed_instances > 0:
                await self.__inc_timer_lock()
            else:
                # Due to race conditions in the async-scheduler it may be possible that the event
                # was already processed and removed from the wakeup queue? In that case we do not
                # need to re-acquire the timer lock since that already happened when the event was
                # set.
                self.logger.warning(f'{self.current_time():.2f} BUG: sleep({duration}) cancelled" \
                                    " but event already processed')
            raise e
    

    @requires_running_simulation
    async def sleep_until(self, timestamp: float):
        """ 
            Wait for the simulation time to advance until the given time is reached.
        """
        self.logger.debug(f'{self.current_time():.2f} sleep_until({timestamp})')

        # Compute timestamp at which we need to wake up the task.
        wakeup_time = round(timestamp / self.__tick_size)

        assert wakeup_time > self.__current_tick, "Cannot sleep until a time in the past"
        
        # Register an event to be set when the timer hits the wakeup time
        event = asyncio.Event()

        await self.__wakeup_events.add(wakeup_time, event)

        # Reduce the timer lock counter so the simulation timer can advance
        await self.__dec_timer_lock()

        try:
            # Wait for the timer to hit the wakeup time
            await event.wait()
        except asyncio.CancelledError as e:
            # self.logger.warning(f'{self.current_time():.2f} sleep_until({timestamp}) cancelled')

            # If the sleep is cancelled we need to remove the wakeup event from the wakeup queue
            removed_instances = await self.__wakeup_events.remove(event)
            if removed_instances > 0:
                await self.__inc_timer_lock()
            else:
                # Due to race conditions in the async-scheduler it may be possible that the event
                # was already processed and removed from the wakeup queue? In that case we do not
                # need to re-acquire the timer lock since that already happened when the event was
                # set.
                self.logger.warning(f'{self.current_time():.2f} BUG: sleep_until({timestamp}) cancelled" \
                                    " but event already processed')
            raise e
    

    @requires_running_simulation
    async def schedule_event_no_await(self, event: asyncio.Event, timestamp: float):
        """
            Schedule an event to be set at the given simulation timestamp, without awaiting the
            given event.
        """
        self.logger.debug(f'{self.current_time():.2f} schedule_event_no_await({id(event) % 1000}, {timestamp})')

        wakeup_time = round(timestamp / self.__tick_size)

        # Use the next tick if the event is scheduled for the current tick
        if wakeup_time == self.__current_tick:
            wakeup_time += 1

        if wakeup_time <= self.__current_tick:
            raise SimulatorException("Cannot schedule event in the past")

        await self.__wakeup_events.add(wakeup_time, event)


    @requires_running_simulation
    async def schedule_event_wait(self, event: asyncio.Event, timestamp: float):
        """
            Schedule an event to be set at the given simulation timestamp and also immediately
            wait for the event to be set.

            (This is equivalent to calling `schedule_event_no_await` followed by 
            `wait_for_scheduled_event`.)
        """
        self.logger.debug(f'{self.current_time():.2f} schedule_event_wait({timestamp})')
        await self.schedule_event_no_await(event, timestamp)
        return await self.wait_for_scheduled_event(event)
    

    @requires_running_simulation
    async def wait_for_scheduled_event(self, event: asyncio.Event):
        """
            Waits for a previously scheduled event to be set, please note that calling this method
            on an event that was not scheduled using `schedule_event` will break everything.
        """
        self.logger.debug(f'{self.current_time():.2f} wait_for_scheduled_event()')

        # Reduce the timer lock counter so the simulation timer can advance
        await self.__dec_timer_lock()

        result = await event.wait()

        await self.__wakeup_events.remove(event)

        return result


    @requires_running_simulation
    async def advance_tick(self):
        """ Advances the simulation by a single tick. """
        await self.sleep(self.__tick_size)


    @requires_running_simulation
    async def wait_with_duration(self, future, duration: int):
        """ 
            Waits for the given future to complete while allowing the simulation time to advance
            only for the given duration (in seconds). Returns the result of the future.
            
            Note that this function does not throw a timeout error! The future can take any 
            arbitrary amount of real time to complete, but the simulation time will only be allowed
            to advance for the given duration.
            
            This method can be used to simulate real network calls or other blocking operations
            taking some amount of time within the simulation.
        """
        self.logger.debug(f'{self.current_time():.2f} wait_with_duration({duration})')

        if not asyncio.isfuture(future) and not asyncio.iscoroutine(future):
            raise TypeError(
                "'future' must be a Future or Coroutine, did you accidentally already await the " \
                "future and ended up passing the result to wait_with_duration instead?"
            )
        result = await asyncio.gather(
            self.sleep(duration),
            future
        )
        return result[1]


simulation_env = SimulationEnvironment()

