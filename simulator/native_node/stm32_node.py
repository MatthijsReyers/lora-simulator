from typing import Tuple, override
from os import path
from simulator.environment import simulation_env as sim
from simulator.lora.radio_power_profile import RadioPowerProfile, NucleoWL55PowerProfile
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
            radio_power_profile: RadioPowerProfile = NucleoWL55PowerProfile(),
            position: Tuple[float, float] = (0.0, 0.0),
            ffi_backend = None,
            boot_delay: float = 0.0,
            uid: Tuple[int, int, int] | None = None,
        ):
        """
        :param uid: The chip's 96-bit unique device ID as three 32-bit words, what firmware reads
            with HAL_GetUIDw0/1/2(). Defaults to the radio ID followed by two fixed words. Set it
            to make a simulated node behave like one specific real board when the firmware
            derives something (a name, a timeout) from the ID.
        """
        self._uid = uid
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

        # Firmware never returns from main(), the HAL stubs use this to stop it when the
        # simulation is over.
        @self.export('int(void)')
        def sim_is_finished() -> int: # pyright: ignore[reportUnusedFunction]
            return int(sim.is_finished())

        @self.export('uint32_t(int)')
        def sim_stm32_uid(word: int) -> int: # pyright: ignore[reportUnusedFunction]
            uid = self._uid or (self.radio._radio_id, 0xDEADBEEF, 0x12345678)
            return uid[word] & 0xFFFFFFFF
