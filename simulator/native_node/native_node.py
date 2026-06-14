#!/usr/bin/env python3
import sys, asyncio, logging
from types import ModuleType
from typing import List
from cffi import FFI
from os import path

sys.path.append('.')

from simulator.environment import simulation_env as sim

class NativeNode(FFI):
    """
        A node radio running C code via CFFI, while exposing callbacks for simulator interaction.

        This node class is intended for cross-compiling native sensor code to run in the simulator.
        Generally you will not need to use this class directly, but rather use a subclass that
        implements the necessary HAL callbacks for a specific MCU.

        We implement the RadioNode subclass that has a LoRa radio and exposes the radio callbacks
        to the C. And a STM32Node subclass that implements some STM32 HAL functions with simulator
        functionality.
    """
    _boot_delay: float
    __exports: dict
    logger: logging.Logger
    source_files: List[str]

    def __init__(
            self, 
            source_file: str, 
            extra_source_files: List[str] | None = None,
            include_dirs: List[str] | None = None,
            ffi_backend: ModuleType|None = None,
            boot_delay: float = 0.0,
        ):
        """
        Basic node running native C code via CFFI, with generic C callbacks for the simulated 
        radio. You should inherit and extend this class to implement the HAL for a specific MCU if
        you want to test more hardware specific C code.
        
        :param source_file: C/C++ source file to compile and run for this node
        :param extra_source_files: Additional C/C++ source files to include in compilation
        :param include_dirs: Additional include directories for header file resolution
        :param ffi_backend: FFI backend to use, default is None
        :param boot_delay: Delay in simulation time seconds to wait before starting the node
        """
        FFI.__init__(self, backend=ffi_backend)
        self.__exports = {}
        self._boot_delay = boot_delay
        self.logger = logging.getLogger(f'Node{id(self) % 1000}@{source_file}')
        self.setup_callbacks()
        self.source_files =[
            path.dirname(path.realpath(__file__))+'/native_node.c'
        ]
        if extra_source_files:
            self.source_files.extend(extra_source_files)
        if source_file:
            self.source_files.append(source_file)

        source_code = ''
        for sf in self.source_files:
            with open(sf, 'r') as f:
                source_code += f.read() + '\n'

        source_code += f"\n#define SIM_NODE_INSTANCE {id(self)}\n"

        is_cpp = source_file and source_file.endswith(('.cpp', '.cc', '.cxx'))
        compile_args = ['-std=c++17' if is_cpp else '-std=c11', '-O2']
        verify_kwargs: dict = {'extra_compile_args': compile_args}
        if is_cpp:
            verify_kwargs['source_extension'] = '.cpp'
        if include_dirs:
            verify_kwargs['include_dirs'] = include_dirs

        self.lib = self.verify(
            source_code,
            **verify_kwargs,
        )
        sim.create_task(self.__run())


    def setup_callbacks(self):
        """
        Method to set up additional C callbacks specific to the MCU being simulated.
        
        Override this in subclasses, while calling super().setup_callbacks() to still set up the
        base callbacks from the parent class.
        """
        # Definitions for the C functions we will call from Python.
        self.cdef('void run_sensor(void);')
        self.cdef('void sim_sleep_end(void);')

        # Set up the Python functions that we want to be able to call from C.
        @self.export('double(void)')
        def sim_current_time() -> float:
            return sim.current_time() - self._boot_delay

        @self.export('void(double)')
        def sim_sleep_start(duration: float):
            async def sleep_task():
                await sim.sleep(duration)
                self.lib.sim_sleep_end()
            # Note that the sleep task does NOT run in the simulator tasks since we do not want to
            # create a new sim lock.
            self.loop.create_task(sleep_task())


    async def __run(self):
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

