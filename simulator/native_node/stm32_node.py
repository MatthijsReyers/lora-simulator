from typing import Tuple, override
from simulator.lora.radio_power_profile import RadioPowerProfile, Stm32wl55PowerProfile
from .radio_node import RadioNode
from .stm32_enums import HAL_StatusTypeDef

class STM32Node(RadioNode):
    """
    A simulated node running native STM32 code via CFFI.
    """
    def __init__(
            self, 
            source_file: str, 
            radio_power_profile: RadioPowerProfile = Stm32wl55PowerProfile(),
            position: Tuple[float, float] = (0.0, 0.0),
            ffi_backend = None,
            boot_delay: float = 0.0,
        ):
        super().__init__(
            source_file=source_file,
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
        