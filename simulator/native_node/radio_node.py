#!/usr/bin/env python3
from abc import abstractmethod
import sys, asyncio, logging
from typing import Tuple
from cffi import FFI

sys.path.append('.')

from simulator.lora.radio import LoraRadio
from simulator.lora.radio_power_profile import RadioPowerProfile, Stm32wl55PowerProfile
from simulator.environment import simulation_env as sim
from simulator.native_node.native_node import NativeNode

class RadioNode(NativeNode):
    """
        A network node with a radio running native C code via CFFI.
    """
    radio: LoraRadio

    def __init__(
            self, 
            source_file: str, 
            extra_source_files: list[str] | None = None,
            include_dirs: list[str] | None = None,
            radio_power_profile: RadioPowerProfile = Stm32wl55PowerProfile(),
            position: Tuple[float, float] = (0.0, 0.0),
            ffi_backend = None,
            boot_delay: float = 0.0,
        ):
        """
        Basic node running native C code via CFFI, with generic C callbacks for the simulated 
        radio. You should inherit and extend this class to implement the HAL for a specific MCU if
        you want to test more hardware specific C code.
        
        :param source_file: C/C++ source file to compile and run for this node
        :param extra_source_files: Additional C/C++ source files to include in compilation
        :param include_dirs: Additional include directories for header file resolution
        :param radio_power_profile: Power profile for the radio
        :param position: X,Y position for the radio in meters, used to estimate path loss
        :param ffi_backend: FFI backend to use, default is None
        :param boot_delay: Delay in simulation time seconds to wait before starting the node
        """
        super().__init__(
            source_file=source_file,
            extra_source_files=extra_source_files,
            include_dirs=include_dirs,
            ffi_backend=ffi_backend,
            boot_delay=boot_delay,
        )
        self.radio = LoraRadio(
            position=position,
            power_profile=radio_power_profile
        )

    def setup_callbacks(self):
        """
        Method to set up additional C callbacks specific to the MCU being simulated.
        
        Override this in subclasses, while calling super().setup_callbacks() to still set up the
        base callbacks from the parent class.
        """
        super().setup_callbacks()

        @self.export('double(void)')
        def sim_radio_set_rx_config():
            self.radio.set_rx_config()
