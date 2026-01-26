#!/usr/bin/env python3
from enum import IntEnum
import sys, time, asyncio, logging, random
from typing import Tuple
import threading
from cffi import FFI

sys.path.append('.')

from simulator.lora.radio import LoraRadio
from simulator.lora.radio_power_profile import RadioPowerProfile, Stm32wl55PowerProfile
from simulator.environment import simulation_env as sim

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

class HAL_StatusTypeDef(IntEnum):
    HAL_OK = 0
    HAL_ERROR = 1
    HAL_BUSY = 2
    HAL_TIMEOUT = 3

class STM32Node(FFI):
    """
    A simulated node running native STM32 code via CFFI.
    """
    radio: LoraRadio
    _boot_delay: float
    __exports: dict
    logger: logging.Logger

    def __init__(
            self, 
            source_file: str, 
            radio_power_profile: RadioPowerProfile = Stm32wl55PowerProfile(),
            position: Tuple[float, float] = (0.0, 0.0),
            ffi_backend = None,
            boot_delay: float = 0.0,
        ):
        """
        Docstring for __init__
        
        :param source_file: C/C++ source file to compile and run for this node
        :param radio_power_profile: Power profile for the radio
        :param position: X,Y position for the radio in meters, used to estimate path loss
        :param ffi_backend: FFI backend to use, default is None
        :param boot_delay: Delay in simulation time seconds to wait before starting the node
        """
        FFI.__init__(self, backend=ffi_backend)
        self.__exports = {}
        self._boot_delay = boot_delay
        self.radio = LoraRadio(
            position=position,
            power_profile=radio_power_profile
        )
        self.logger = logging.getLogger(f'STM32Node@{source_file}')
        self.source_file = source_file

        sim.create_task(self.__run())

        @self.export('int(void*)')
        def HAL_SUBGHZ_Init(_ptr) -> int:
            self.logger.debug('HAL_SUBGHZ_Init()')
            return HAL_StatusTypeDef.HAL_OK.value

        @self.export('int(void)')
        def HAL_GetTick() -> int:
            self.logger.debug('HAL_GetTick()')
            return round((sim.current_time() - self._boot_delay) * 1000)

        @self.export('void(double)')
        def sim_sleep_start(duration: float):
            async def sleep_task():
                await sim.sleep(duration)
                self.lib.sim_sleep_end()
            # Note that the sleep task does NOT run in the simulator tasks since we do not want to
            # create a new sim lock.
            self.loop.create_task(sleep_task())
        

    async def __run(self):
        with open(self.source_file, 'r') as f:
            source_code = f.read()
        
        # Declare main function so it can be called from Python
        self.cdef('void run_sensor(void);')
        
        # Declare the sleep callback functions defined in C
        self.cdef('void sim_sleep_end(void);')
        
        self.lib = self.verify(
            source_code,
            extra_compile_args=['-std=c11', '-O2'],
        )

        # 
        self.loop = asyncio.get_event_loop()
        self.thread = asyncio.create_task(asyncio.to_thread(self.lib.run_sensor))

        await self.thread


    def export(self, signature):
        """ Returns a decorator that exports a Python function to C with the given signature. """
        func_type = self._typeof(signature, consider_function_as_funcptr=True)
        def decorator(func):
            name = func.__name__
            assert name not in self.__exports, f"duplicate export {name}"
            callback_var = self.getctype(func_type, name)
            self.cdef('extern %s;' % callback_var)
            self.__exports[name] = (func_type, func)
        return decorator

    def verify(self, source='', **kwargs):
        extras = []
        pyexports = sorted(self.__exports.items())
        for name, export in pyexports:
            callback_var = self.getctype(export[0], name)
            extras.append("%s;" % callback_var)
        extras.append(source)
        source = '\n'.join(extras)
        lib = FFI.verify(self, source, **kwargs)
        for name, export in pyexports:
            cb = self.callback(export[0], export[1])
            export_with_cb = (export[0], export[1], cb)
            self.__exports[name] = export_with_cb
            setattr(lib, name, cb)
        return lib
        


if __name__ == '__main__':
    node1 = STM32Node('./examples/native-stm32/main.c')
    
    node1.logger.setLevel(logging.DEBUG)
    node1.logger.addHandler(logging.StreamHandler(sys.stdout))

    sim.logger.setLevel(logging.DEBUG)
    sim.logger.addHandler(logging.StreamHandler(sys.stdout))

    # node2 = STM32Node('./main.c')
    # node2.load_source('./examples/native-stm32/main.c
    
    sim.run(30)

    # node1.lib.main()