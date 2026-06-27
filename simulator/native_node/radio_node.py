#!/usr/bin/env python3
import asyncio
from enum import IntEnum
from logging import Logger
import logging
import sys
from types import ModuleType
from typing import Tuple
from os import path

from simulator.exceptions import SimulationFinishedException
from simulator.lora.enums.radio_state import RadioState

sys.path.append('.')

from simulator.lora.enums.code_rate import CodeRate
from simulator.lora.enums.spreading_factor import SpreadingFactor
from simulator.lora.enums.bandwidth import Bandwidth
from simulator.lora.radio import LoraRadio
from simulator.lora.radio_power_profile import RadioPowerProfile, Stm32wl55PowerProfile
from simulator.environment import simulation_env as sim
from simulator.native_node.native_node import NativeNode

class RadioNodeError(IntEnum):
    NONE = 0
    SIM_ENDED = -1
    ALREADY_TRANSMITTING = -2
    TIMEOUT = -3
    UNKNOWN_ERR = -4

class RadioNode(NativeNode):
    """
        A network node with a radio running native C code via CFFI.
    """
    radio: LoraRadio
    logger: Logger

    def __init__(
            self, 
            source_file: str, 
            extra_source_files: list[str]|None = None,
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
        if extra_source_files is None:
            extra_source_files = []
        extra_source_files = list(extra_source_files)  # Ensure it's a mutable list
        extra_source_files.append(
            path.dirname(path.realpath(__file__))+'/radio_node.c'
        )
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
        self.logger = logging.getLogger(f'RadioNode-{self.radio._radio_id}')


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
            fixed_len: bool,
            crc_enabled: bool,
            iq_inverted: bool,
            rx_continuous: bool,
        ):
            self.logger.debug(
                f"RadioNode::sim_radio_set_rx_config(khz={khz}, "+
                f"sf=SF{spreading_factor}, "
                f"cr=4/{code_rate}, " +
                f"preamble={preamble_len}sym, " +
                f"max_payload_len={max_payload_len}, " +
                f"symbols={symbols}, " +
                f"fixed_len={fixed_len}, " +
                f"crc_enabled={crc_enabled}, " +
                f"iq_inverted={iq_inverted}, " +
                f"rx_continuous={rx_continuous})"
            )
            self.radio.set_rx_config(
                bandwidth=Bandwidth.from_khz(khz),
                spreading_factor=SpreadingFactor(spreading_factor),
                code_rate=CodeRate.from_denominator(code_rate),
                preamble_len=preamble_len,
                max_payload_len=max_payload_len,
                symbols=symbols,
                fixed_payload_len=bool(fixed_len),
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
            self.logger.debug(
                f"RadioNode::sim_radio_set_tx_config(khz={khz}, " +
                f"power={power}, " +
                f"sf=SF{spreading_factor}, " +
                f"cr=4/{code_rate}, " +
                f"preamble_len={preamble_len}sym, " +
                f"fixed_len={fixed_len}, " +
                f"crc_enabled={crc_enabled}, " +
                f"freq_hop_period={freq_hop_period}, " +
                f"iq_inverted={iq_inverted}, " +
                f"timeout={timeout})"
            )
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
            self.logger.debug("RadioNode::sim_radio_id()")
            return self.radio._radio_id

        @self.export('bool()')
        def sim_already_transmitting() -> int: # pyright: ignore[reportUnusedFunction]
            self.logger.debug(f"RadioNode::sim_already_transmitting() -> {self.radio.get_state() == RadioState.TX}")
            return self.radio.get_state() == RadioState.TX
        
        self.cdef('void sim_tx_done_callback(int);')
        self.cdef('void sim_rx_done_callback(int, int);')

        @self.export('int(uint8_t*, size_t)')
        def sim_transmit_start(buf, length: int) -> int: # pyright: ignore[reportUnusedFunction]
            self.logger.debug(f'RadioNode::sim_transmit_start(length={length})')

            state = self.radio.get_state()
            if state == RadioState.TX:
                return RadioNodeError.ALREADY_TRANSMITTING
            
            if sim.is_finished():
                return RadioNodeError.SIM_ENDED

            data = self.buffer(buf, length)[:]

            async def transmit_task():
                try:
                    await self.radio.transmit_data_blocking(data)
                    self.lib.sim_tx_done_callback(RadioNodeError.NONE)
                except SimulationFinishedException as _e:
                    self.lib.sim_tx_done_callback(RadioNodeError.SIM_ENDED)
                except TimeoutError as _e:
                    self.lib.sim_tx_done_callback(RadioNodeError.TIMEOUT)
                except Exception as e:
                    self.logger.error(f"Error during transmission: {e}")
                    self.lib.sim_tx_done_callback(RadioNodeError.UNKNOWN_ERR)

            async def start_transmit_task():
                await transmit_task()

            # Note that the sleep task does NOT run in the simulator tasks since we do not want to
            # create a new sim lock.
            self.loop.create_task(transmit_task())
            return 0

        @self.export('int(uint8_t*, int, int)')
        def sim_start_receive( # pyright: ignore[reportUnusedFunction]
            buf, max_len: int, timeout_ms: int
        ) -> int:
            self.logger.debug(f'RadioNode::sim_start_receive(max_len={max_len}, timeout_ms={timeout_ms})')

            state = self.radio.get_state()
            if state == RadioState.TX:
                return 1  # Cannot receive while transmitting

            async def receive_task():
                bytes_received = 0
                start_t = sim.current_time()
                try:
                    # Put radio in receive mode and wait for a packet (or timeout)
                    timeout_s = timeout_ms / 1000.0 if timeout_ms >= 0 else None
                    if timeout_s is not None:
                        packet = await self.radio.receive_data_within(timeout_s)
                    else:
                        packet = await self.radio.receive_data_wait()

                    payload = packet.payload
                    bytes_received = min(len(payload), max_len)
                    # Copy received data into the C buffer
                    buf_view = self.buffer(buf, max_len)
                    buf_view[:bytes_received] = payload[:bytes_received]
                    self.lib.sim_rx_done_callback(bytes_received, RadioNodeError.NONE)
                except asyncio.TimeoutError:
                    end_t = sim.current_time() - start_t
                    self.logger.debug(f"sim_start_receive timed out after {end_t}s")
                    self.lib.sim_rx_done_callback(0, RadioNodeError.TIMEOUT)
                except SimulationFinishedException as e:
                    self.lib.sim_rx_done_callback(0, RadioNodeError.SIM_ENDED)
                except Exception as e:
                    self.logger.error(f"Error during receive: {e}")
                    self.lib.sim_rx_done_callback(0, RadioNodeError.UNKNOWN_ERR)

            self.loop.create_task(receive_task())
            return 0
