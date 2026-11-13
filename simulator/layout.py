"""
    Node positions as a CSV file (`node,x,y`, meters, gateway at the origin), so that different
    scenarios can be run on exactly the same deployment.
"""
import csv
import math
import random
from pathlib import Path
from typing import Iterable


def uniform_disc(count: int, radius: float, rng: random.Random) -> list[tuple[float, float]]:
    """ `count` positions spread uniformly over a disc around the origin. """
    positions = []
    for _ in range(count):
        # The square root keeps the density uniform instead of crowding the centre.
        distance = radius * math.sqrt(rng.random())
        angle = rng.uniform(0.0, 2 * math.pi)
        positions.append((distance * math.cos(angle), distance * math.sin(angle)))
    return positions


def write_layout(path: Path | str, positions: Iterable[tuple[float, float]]) -> None:
    with open(path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["node", "x", "y"])
        for (node, (x, y)) in enumerate(positions):
            writer.writerow([node, f"{x:.3f}", f"{y:.3f}"])


def read_layout(path: Path | str) -> list[tuple[float, float]]:
    with open(path, newline="") as handle:
        return [(float(row["x"]), float(row["y"])) for row in csv.DictReader(handle)]
