#!/usr/bin/env python3
import sys, logging
import logging

sys.path.append('.')

from simulator.lora.phy_layer import LoraPhyLayer
from simulator.environment import simulation_env as sim

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

from packets import *
from gateway import Gateway
from node import Node

if __name__ == "__main__":
    NODES = 10

    level = logging.DEBUG
    sym_level = logging.INFO
    node_level = logging.INFO
    gateway_level = logging.INFO
    
    ch = logging.StreamHandler()
    ch.setLevel(level)
    formatter = logging.Formatter("%(levelname)s;%(message)s")
    ch.setFormatter(formatter)

    logger.addHandler(ch)

    sim.logger.setLevel(sym_level)
    sim.logger.addHandler(ch)

    phy_layer = LoraPhyLayer()
    phy_layer.logger.setLevel(sym_level)
    phy_layer.logger.addHandler(ch)

    gateway = Gateway()
    nodes = [ Node() for _ in range(NODES) ]

    gateway.radio.logger.setLevel(gateway_level)
    gateway.radio.logger.addHandler(ch)

    for node in nodes:
        node.radio.logger.setLevel(node_level)
        node.radio.logger.addHandler(ch)

    sim.run(simulation_length=120)
