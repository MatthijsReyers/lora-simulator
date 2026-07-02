from typing import Tuple, override
from os import path
from simulator.lora.radio_power_profile import RadioPowerProfile, Stm32wl55PowerProfile
from simulator.native_node.radio_node import RadioNode
from simulator.native_node.stm32wl.hal_enums import HAL_StatusTypeDef
import glob

class STM32Node(RadioNode):
    """
    A simulated node running native STM32 code via CFFI.
    """
    _STM32_HAL_SHIM_DIR = path.join(path.dirname(path.realpath(__file__)), 'stm32wl/hal_headers')
    _STM32_HAL_STUBS = path.join(path.dirname(path.realpath(__file__)), 'stm32wl/hal_stubs')
    _STM32_HAL_SOURCES = [ f for f in glob.glob(f'{_STM32_HAL_STUBS}/*.c') ]
    

    _STM32_RADIO_SHIM_DIR = path.join(path.dirname(path.realpath(__file__)), 'stm32wl/radio_driver_headers')
    _STM32_RADIO_STUBS = path.join(path.dirname(path.realpath(__file__)), 'stm32wl/radio_driver_stubs')
    _STM32_RADIO_SOURCES = [ f for f in glob.glob(f'{_STM32_RADIO_STUBS}/*.c') ]


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
        hal_files = self._STM32_HAL_SOURCES
        hal_files.extend(self._STM32_HAL_SOURCES)
        if extra_source_files:
            hal_files.extend(extra_source_files)
        hal_includes = [self._STM32_HAL_SHIM_DIR, self._STM32_RADIO_SHIM_DIR]
        if include_dirs:
            hal_includes.extend(include_dirs)
        super().__init__(
            source_file=source_file,
            extra_source_files=hal_files,
            include_dirs=hal_includes,
            radio_power_profile=radio_power_profile,
            position=position,
            ffi_backend=ffi_backend,
            boot_delay=boot_delay,
        )
        self.radio_initialized = False

    @override
    def setup_callbacks(self):
        super().setup_callbacks()
