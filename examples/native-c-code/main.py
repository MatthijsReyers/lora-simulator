
"""
Native C Code Integration for LoRa Simulator

This module provides the EmbeddedNode class that allows running native C code
within the simulator. Each EmbeddedNode instance runs its own C code in a 
separate thread while still interacting with the simulated LoRa radio.
"""

import os
import asyncio
import threading
from typing import Optional, Callable
from pathlib import Path

import cffi

from simulator.lora.radio import LoraRadio
from simulator.lora.radio_power_profile import RadioPowerProfile, Stm32wl55PowerProfile
from simulator.environment import simulation_env as sim


# Global node ID counter for unique identification
_node_id_counter = 0


class CFFIRadioBridge:
    """
    Bridge between C code and Python radio instances.
    
    This class manages the mapping between node IDs and their radio instances,
    and provides the callback implementations for the C functions.
    """
    
    _instance: Optional['CFFIRadioBridge'] = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._radios: dict[int, LoraRadio] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock = threading.Lock()
    
    def register_radio(self, node_id: int, radio: LoraRadio) -> None:
        """Register a radio instance with a node ID."""
        with self._lock:
            self._radios[node_id] = radio
    
    def unregister_radio(self, node_id: int) -> None:
        """Unregister a radio instance."""
        with self._lock:
            self._radios.pop(node_id, None)
    
    def get_radio(self, node_id: int) -> Optional[LoraRadio]:
        """Get the radio instance for a node ID."""
        with self._lock:
            return self._radios.get(node_id)
    
    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Set the event loop to use for async operations."""
        self._loop = loop
    
    def _run_async(self, coro):
        """
        Run an async coroutine from a synchronous context (C thread).
        This schedules the coroutine on the main event loop and waits for completion.
        """
        if self._loop is None:
            raise RuntimeError("Event loop not set. Call set_event_loop() first.")
        
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()  # Block until the coroutine completes
    
    def radio_off(self, node_id: int) -> None:
        """Turn the radio off."""
        radio = self.get_radio(node_id)
        if radio is None:
            raise ValueError(f"No radio registered for node_id {node_id}")
        self._run_async(radio.off())
    
    def radio_standby(self, node_id: int) -> None:
        """Put the radio into standby mode."""
        radio = self.get_radio(node_id)
        if radio is None:
            raise ValueError(f"No radio registered for node_id {node_id}")
        self._run_async(radio.standby())
    
    def radio_transmit_data_blocking(self, node_id: int, data: bytes) -> None:
        """Transmit data over the radio (blocking)."""
        radio = self.get_radio(node_id)
        if radio is None:
            raise ValueError(f"No radio registered for node_id {node_id}")
        self._run_async(radio.transmit_data_blocking(data))
    
    def radio_receive_data_nowait(self, node_id: int, buffer_size: int) -> Optional[bytes]:
        """Try to receive data without blocking."""
        radio = self.get_radio(node_id)
        if radio is None:
            raise ValueError(f"No radio registered for node_id {node_id}")
        packet = self._run_async(radio.receive_data_nowait())
        if packet is not None:
            return packet.payload[:buffer_size]
        return None


# Get the singleton bridge instance
def get_bridge() -> CFFIRadioBridge:
    return CFFIRadioBridge()


class EmbeddedNode:
    """
    A node that runs native C code within the simulator.
    
    Each EmbeddedNode instance has its own LoRa radio and runs its C code
    in a separate thread. The C code can interact with the radio through
    the provided API functions (radio_off, radio_standby, radio_transmit_data_blocking,
    radio_receive_data_nowait).
    
    Multiple EmbeddedNode instances can be created to simulate multiple
    devices running the same or different C code.
    
    Example usage:
        node1 = EmbeddedNode(c_source_file="embedded/main.c")
        node2 = EmbeddedNode(c_source_file="embedded/main.c")
        
        sim.create_task(node1.run())
        sim.create_task(node2.run())
        sim.run(simulation_length=100)
    """
    
    # Class-level FFI and compiled library (shared across instances)
    _ffi: Optional[cffi.FFI] = None
    _lib = None
    _module = None  # Keep reference to prevent GC
    _compiled_source: Optional[str] = None
    
    def __init__(
        self, 
        radio_power_profile: RadioPowerProfile = None,
        c_source_file: Optional[str] = None,
        c_source_code: Optional[str] = None,
        position: tuple[float, float] = (0.0, 0.0),
    ):
        """
        Initialize an EmbeddedNode.
        
        Args:
            radio_power_profile: Power profile for the radio (defaults to Stm32wl55PowerProfile)
            c_source_file: Path to the C source file to compile and run
            c_source_code: C source code as a string (alternative to c_source_file)
            position: Position of the node in the simulation (x, y)
        """
        if radio_power_profile is None:
            radio_power_profile = Stm32wl55PowerProfile()
        
        self.radio = LoraRadio(position=position, power_profile=radio_power_profile)
        
        # Assign unique node ID
        global _node_id_counter
        _node_id_counter += 1
        self._node_id = _node_id_counter
        
        # Register this node's radio with the bridge
        bridge = get_bridge()
        bridge.register_radio(self._node_id, self.radio)
        
        # Store C source information
        self._c_source_file = c_source_file
        self._c_source_code = c_source_code
        
        # Thread for running C code
        self._thread: Optional[threading.Thread] = None
        self._running = False
    
    @property
    def node_id(self) -> int:
        """Get the unique node ID."""
        return self._node_id
    
    @classmethod
    def _compile_c_code(cls, c_source: str) -> None:
        """
        Compile C code using CFFI.
        
        This is a class method because the compiled code is shared across
        all instances (the C functions use node_id to distinguish instances).
        """
        if cls._ffi is not None and cls._compiled_source == c_source:
            return  # Already compiled
        
        cls._ffi = cffi.FFI()
        
        # Define the C interface that we expose to the C code
        cls._ffi.cdef("""
            // Radio API functions (implemented in Python, called from C)
            extern "Python" void radio_off(int node_id);
            extern "Python" void radio_standby(int node_id);
            extern "Python" void radio_transmit_data_blocking(int node_id, const uint8_t* data, size_t length);
            extern "Python" size_t radio_receive_data_nowait(int node_id, uint8_t* buffer, size_t buffer_size);
            
            // Entry point (implemented in C, called from Python)
            void embedded_main(int node_id);
        """)
        
        # Use set_source for modern CFFI API
        cls._ffi.set_source(
            "_embedded_radio",  # Module name
            c_source,  # C source code
            extra_compile_args=['-std=c99'],
        )
        
        # Compile to a cache directory that persists
        import tempfile
        import importlib.util
        import sys
        import hashlib
        
        # Create a cache directory based on the source hash
        source_hash = hashlib.md5(c_source.encode()).hexdigest()[:12]
        cache_dir = Path(tempfile.gettempdir()) / f"lora_sim_cffi_{source_hash}"
        cache_dir.mkdir(exist_ok=True)
        
        # Compile the extension module
        lib_path = cls._ffi.compile(tmpdir=str(cache_dir))
        
        # Add cache dir to path so the module can be found
        if str(cache_dir) not in sys.path:
            sys.path.insert(0, str(cache_dir))
        
        # Load the compiled module
        spec = importlib.util.spec_from_file_location("_embedded_radio", lib_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["_embedded_radio"] = module
        spec.loader.exec_module(module)
        cls._lib = module.lib
        cls._module = module  # Keep reference to prevent GC
        
        # Set up the Python callback implementations on the ffi from the module
        module_ffi = module.ffi
        
        @module_ffi.def_extern()
        def radio_off(node_id: int) -> None:
            try:
                get_bridge().radio_off(node_id)
            except Exception as e:
                print(f"Error in radio_off: {e}")
        
        @module_ffi.def_extern()
        def radio_standby(node_id: int) -> None:
            try:
                get_bridge().radio_standby(node_id)
            except Exception as e:
                print(f"Error in radio_standby: {e}")
        
        @module_ffi.def_extern()
        def radio_transmit_data_blocking(node_id: int, data, length: int) -> None:
            try:
                data_bytes = module_ffi.buffer(data, length)[:]
                get_bridge().radio_transmit_data_blocking(node_id, bytes(data_bytes))
            except Exception as e:
                print(f"Error in radio_transmit_data_blocking: {e}")
        
        @module_ffi.def_extern()
        def radio_receive_data_nowait(node_id: int, buffer, buffer_size: int) -> int:
            try:
                result = get_bridge().radio_receive_data_nowait(node_id, buffer_size)
                if result is not None:
                    module_ffi.memmove(buffer, result, len(result))
                    return len(result)
                return 0
            except Exception as e:
                print(f"Error in radio_receive_data_nowait: {e}")
                return 0
        
        cls._compiled_source = c_source
    
    def _load_c_source(self) -> str:
        """Load C source code from file or return the provided string."""
        if self._c_source_code is not None:
            return self._c_source_code
        
        if self._c_source_file is not None:
            with open(self._c_source_file, 'r') as f:
                return f.read()
        
        raise ValueError("No C source code provided. Set c_source_file or c_source_code.")
    
    def _run_c_code(self) -> None:
        """Run the C code's embedded_main function in a thread."""
        try:
            self._lib.embedded_main(self._node_id)
        except Exception as e:
            print(f"Error running C code for node {self._node_id}: {e}")
        finally:
            self._running = False
    
    async def run(self) -> None:
        """
        Start running the embedded C code.
        
        This should be called as a simulation task:
            sim.create_task(node.run())
        """
        # Set up the event loop for the bridge
        bridge = get_bridge()
        bridge.set_event_loop(asyncio.get_event_loop())
        
        # Compile the C code if needed
        c_source = self._load_c_source()
        self._compile_c_code(c_source)
        
        # Start the C code in a separate thread
        self._running = True
        self._thread = threading.Thread(
            target=self._run_c_code,
            name=f"EmbeddedNode-{self._node_id}",
            daemon=True
        )
        self._thread.start()
        
        # Keep the task alive while the C code is running
        while self._running and sim.is_running():
            await sim.sleep(0.001)  # Check every millisecond of simulation time
        
        # Wait for the thread to finish
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)
    
    def stop(self) -> None:
        """Stop the embedded C code."""
        self._running = False
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)
    
    def __del__(self):
        """Clean up when the node is destroyed."""
        self.stop()
        bridge = get_bridge()
        bridge.unregister_radio(self._node_id)
