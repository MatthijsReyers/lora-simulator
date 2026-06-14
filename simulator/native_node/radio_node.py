#!/usr/bin/env python3
import sys
from types import ModuleType
from typing import Tuple

sys.path.append('.')

from simulator.lora.enums.code_rate import CodeRate
from simulator.lora.enums.spreading_factor import SpreadingFactor
from simulator.lora.enums.bandwidth import Bandwidth
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

        @self.export('void(int, int, int, int, int, int, int, int, int, int)')
        def sim_radio_set_rx_config( # pyright: ignore[reportUnusedFunction]
            khz: int, 
            spreading_factor: int, 
            code_rate: int,
            preamble_len: int,
            max_payload_len: int,
            symbols: int,
            fixed_payload_len: bool,
            crc_enabled: bool,
            iq_inverted: bool,
            rx_continuous: bool,
        ):
            self.radio.set_rx_config(
                bandwidth=Bandwidth.from_khz(khz),
                spreading_factor=SpreadingFactor(spreading_factor),
                code_rate=CodeRate.from_denominator(code_rate),
                preamble_len=preamble_len,
                max_payload_len=max_payload_len,
                symbols=symbols,
                fixed_payload_len=bool(fixed_payload_len),
                crc_enabled=bool(crc_enabled),
                iq_inverted=bool(iq_inverted),
                rx_continuous=bool(rx_continuous),
            )

        @self.export('void(int, int, int, int, int, int, int, int, int, int)')
        def sim_radio_set_tx_config( # pyright: ignore[reportUnusedFunction]
            power: int,
            khz: int, 
            spreading_factor: int, 
            code_rate: int,
            preamble_len: int,
            fixed_len: bool,
            crc_enabled: bool,
            freq_hop_period: int,
            iq_inverted: bool,
            timeout: int,
        ):
            self.radio.set_tx_config(
                power=power,
                bandwidth=Bandwidth.from_khz(khz),
                spreading_factor=SpreadingFactor(spreading_factor),
                code_rate=CodeRate.from_denominator(code_rate),
                preamble_len=preamble_len,
                fixed_len=bool(fixed_len),
                crc_enable=bool(crc_enabled),
                freq_hop_period=freq_hop_period,
                iq_inverted=bool(iq_inverted),
                timeout=timeout,
            )

        @self.export('int()')
        def sim_radio_id() -> int: # pyright: ignore[reportUnusedFunction]
            return self.radio._radio_id
