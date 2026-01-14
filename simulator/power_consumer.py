from typing import Optional
from pandas import DataFrame
from simulator.environment import simulation_env as sim

class PowerConsumer:
    """
        A class representing a power-consuming device in a simulation environment, for example a
        sensor or hardware peripheral like a radio module.
    """
    __last_update_time: float
    __current_power: float
    __total_energy_consumed: float
    __events: dict

    events: Optional[DataFrame]

    def __init__(self, current_power: float = 0.0, energy_consumed: float = 0.0):
        """
            Initialize a PowerConsumer instance.
            
            By default the device consumes no power and has consumed no energy, but if you can
            optionally specify initial values for these parameters if a device starts the 
            simulation powered on (and thus consuming power) or has consumed energy prior to the
            start of the simulation.

            :param current_power: The initial power consumption in watts.
            :param energy_consumed: The initial total energy consumed in joules.
        """
        self.__last_update_time = 0.0
        self.__current_power = current_power
        self.__total_energy_consumed = energy_consumed
        self.__events = {
            "time": [],
            "power": [],
            "energy": []
        }
        sim.create_task(self.__on_sim_end(), )


    async def __on_sim_end(self):
        """
            Called when the simulations ends to perform necessary cleanup and finalize the total 
            energy consumed.
        """
        await sim.wait_for_sim_end()
        now = sim.current_time()
        elapsed = now - self.__last_update_time
        assert elapsed >= 0.0, "BUG: Simulation time went backwards!"
        self.__total_energy_consumed += self.__current_power * elapsed
        self.__last_update_time = now
        self.events = DataFrame(self.__events)
        del self.__events


    def set_power_consumption(self, power: float) -> float:
        """
            Set the power consumption (in watts) at this moment in simulation time. This 
            consumption level will be held until the next call to this method.

            IMPORTANT: You can provide a negative value for power if you want to simulate a device that
            is generating power (for example a solar panel). However, be aware that this may lead to
            situations where the total energy consumed becomes negative, which may not make sense
            in your simulation context.

            For convenience, this method returns the amount of energy that has been consumed so far.
        """
        now = sim.current_time()
        elapsed = now - self.__last_update_time
        
        assert elapsed >= 0.0, "BUG: Simulation time went backwards!"
        self.__total_energy_consumed += self.__current_power * elapsed
        self.__last_update_time = now

        self.__current_power = power

        self.__events["time"].append(now)
        self.__events["power"].append(self.__current_power)
        self.__events["energy"].append(self.__total_energy_consumed)

        return self.__total_energy_consumed
    

    def get_total_energy_consumed(self) -> float:
        """
            Get the total energy consumed (in joules) by this device up to the current moment
            in simulation time.
        """
        now = sim.current_time()
        elapsed = now - self.__last_update_time
        assert elapsed >= 0.0, "BUG: Simulation time went backwards!"
        
        self.__total_energy_consumed += self.__current_power * elapsed
        self.__last_update_time = now

        return self.__total_energy_consumed
