#!/usr/bin/env python3
"""
Example: Running multiple embedded C nodes in the simulator.

This example demonstrates how to run multiple instances of native C code
within the LoRa simulator, each with their own radio instance.
"""

import sys
import os
import logging

# Add the project root to the path
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, project_root)

# Import directly from the local main.py
from main import EmbeddedNode
from simulator.environment import simulation_env as sim
from simulator.lora.phy_layer import LoraPhyLayer


def main():
    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(levelname)s - %(name)s - %(message)s'
    )
    
    # Initialize the PHY layer (required for radio communication)
    phy_layer = LoraPhyLayer()
    
    # Get the path to the C source file
    script_dir = os.path.dirname(os.path.abspath(__file__))
    c_source_file = os.path.join(script_dir, "embedded", "main.c")
    
    # Create multiple embedded nodes running the same C code
    # Each node gets its own unique node_id automatically
    node1 = EmbeddedNode(
        c_source_file=c_source_file,
        position=(0.0, 0.0)
    )
    
    node2 = EmbeddedNode(
        c_source_file=c_source_file,
        position=(100.0, 0.0)
    )
    
    node3 = EmbeddedNode(
        c_source_file=c_source_file,
        position=(50.0, 50.0)
    )
    
    print(f"Created nodes with IDs: {node1.node_id}, {node2.node_id}, {node3.node_id}")
    
    # Configure radio settings for each node
    for node in [node1, node2, node3]:
        node.radio.set_rx_config(spreading_factor=7, bandwidth=125)
        node.radio.set_tx_config(power=14, spreading_factor=7, bandwidth=125)
    
    # Register the nodes as simulation tasks
    sim.create_task(node1.run())
    sim.create_task(node2.run())
    sim.create_task(node3.run())
    
    # Run the simulation for 10 seconds
    print("Starting simulation...")
    sim.run(simulation_length=10)
    print("Simulation complete.")


if __name__ == "__main__":
    main()
