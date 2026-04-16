from typing import Tuple, override
from os import path
from simulator.lora.radio_power_profile import RadioPowerProfile, Stm32wl55PowerProfile
from .radio_node import RadioNode
from .stm32_enums import HAL_StatusTypeDef

class STM32Node(RadioNode):
    """
    A simulated node running native STM32 code via CFFI.
    """
    _STM32_HAL_SOURCE = path.join(path.dirname(path.realpath(__file__)), 'stm32_hal.c')
    _STM32_HAL_SHIM_DIR = path.join(path.dirname(path.realpath(__file__)), 'stm32_hal_shim')

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
        hal_files = [self._STM32_HAL_SOURCE]
        if extra_source_files:
            hal_files.extend(extra_source_files)
        hal_includes = [self._STM32_HAL_SHIM_DIR]
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

        @self.export('int(void*)')
        def HAL_SUBGHZ_Init(_ptr: int) -> int:
            self.radio_initialized = True
            return HAL_StatusTypeDef.HAL_OK
        