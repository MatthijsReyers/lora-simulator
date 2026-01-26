"""
Native C Code Integration for LoRa Simulator

This package provides the ability to run native C code within the LoRa simulator
using CFFI. The C code runs in separate threads while interacting with simulated
LoRa radios.
"""

from .main import EmbeddedNode, CFFIRadioBridge, get_bridge

__all__ = ['EmbeddedNode', 'CFFIRadioBridge', 'get_bridge']
