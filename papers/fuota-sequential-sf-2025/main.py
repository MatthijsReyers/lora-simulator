#!/usr/bin/env python3
"""
Runs one FUOTA session of the paper's evaluation (one scheme, one seed) and writes the results
to `data/`.

    pipenv run python papers/fuota-sequential-sf-2025/main.py --scheme proposed --seed 1

Run from the repository root. See README.md for the parameters and the output format.
"""
from __future__ import annotations

import argparse
import logging
import math
import os
import random
import statistics
import sys
import time
from dataclasses import asdict
from pathlib import Path

sys.path.append(".")  # the simulator package, when running from the repository root
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import pandas as pd

from simulator.environment import simulation_env as sim

import analytical
from gateway import Gateway
from interference import InterferenceModel, distances_within
from interferers import Interferer
from recipient import Recipient
from scenario import Scenario, read_layout, write_layout
from session import Session

# Frames start one second into the simulation, after every task has set up its radio.
SESSION_START = 1.0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    defaults = Scenario()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--scheme", choices=["proposed", "fsf", "gb_e", "gb_l"], default=defaults.scheme)
    parser.add_argument("-L", "--first-sf", type=int, default=defaults.first_sf, help="L, smallest SF of the proposed scheme")
    parser.add_argument("-M", "--last-sf", type=int, default=defaults.last_sf, help="M, largest SF of the proposed scheme")
    parser.add_argument("-w", "--transmissions-per-sf", type=int, default=defaults.transmissions_per_sf, help="w, frames per SF")
    parser.add_argument("--sf", type=int, default=defaults.fixed_sf, help="the SF of the FSF benchmark")
    parser.add_argument("--code", choices=["ideal", "raptor"], default=None, help="decoding model (default: the paper's choice per scheme)")
    parser.add_argument("--nodes", type=int, default=defaults.nodes)
    parser.add_argument("--radius", type=float, default=defaults.radius, help="R0 in meters")
    parser.add_argument("--layout", choices=["radial", "uniform"], default=defaults.layout)
    parser.add_argument("--layout-file", type=Path, default=None, help="read the node positions from this CSV instead of placing them")
    parser.add_argument("--firmware-kb", type=float, default=defaults.firmware_size / 1000, help="firmware size in kB (1000 bytes)")
    parser.add_argument("--fragment-size", type=int, default=defaults.fragment_size, help="bytes per fragment / frame payload")
    parser.add_argument("--duty-cycle", type=float, default=defaults.duty_cycle_percent, help="gateway duty cycle in percent")
    parser.add_argument("--exponent", type=float, default=defaults.path_loss_exponent, help="path loss exponent α")
    parser.add_argument("--reference-loss", type=float, default=defaults.reference_loss_db, help="path loss at 1 m in dB (see README)")
    parser.add_argument("--no-fading", action="store_true", help="disable the Rayleigh fading")
    parser.add_argument("--interference", choices=["model", "simulated", "none"], default=defaults.interference)
    parser.add_argument("--interferer-density", type=float, default=defaults.interferer_density, help="λ_I, interferers per m²")
    parser.add_argument("--interferer-period", type=float, default=1 / defaults.interferer_frame_rate, help="mean seconds between an interferer's frames")
    parser.add_argument("--collision-rule", choices=["sir", "any_overlap"], default=defaults.collision_rule, help="model mode: how overlaps are resolved")
    parser.add_argument("--no-capture-effect", action="store_true", help="simulated mode: every overlap of the simulator's model is a loss")
    parser.add_argument("--normalisation", choices=["payload", "frame"], default=defaults.normalisation, help="energy normalisation (see README)")
    parser.add_argument("--control-airtime", type=float, default=defaults.control_airtime, help="l_c in seconds")
    parser.add_argument("--max-hours", type=float, default=500.0, help="stop the session after this much simulated time")
    parser.add_argument("--analytical", action="store_true", help="also compute the analytical prediction per node (proposed and FSF)")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).parent / "data")
    parser.add_argument("--tag", default="", help="extra text for the run name")
    parser.add_argument("--log-level", default="WARNING")
    return parser.parse_args(argv)


def scenario_from_args(args: argparse.Namespace) -> Scenario:
    return Scenario(
        scheme=args.scheme,
        first_sf=args.first_sf,
        last_sf=args.last_sf,
        transmissions_per_sf=args.transmissions_per_sf,
        fixed_sf=args.sf,
        code=args.code,
        firmware_size=int(round(args.firmware_kb * 1000)),
        fragment_size=args.fragment_size,
        duty_cycle_percent=args.duty_cycle,
        nodes=args.nodes,
        radius=args.radius,
        layout=args.layout,
        path_loss_exponent=args.exponent,
        reference_loss_db=args.reference_loss,
        rayleigh_fading=not args.no_fading,
        interference=args.interference,
        interferer_density=args.interferer_density,
        interferer_frame_rate=1 / args.interferer_period,
        collision_rule=args.collision_rule,
        capture_effect=not args.no_capture_effect,
        normalisation=args.normalisation,
        control_airtime=args.control_airtime,
        seed=args.seed,
    )


def run_name(scenario: Scenario, args: argparse.Namespace) -> str:
    s = scenario
    if s.scheme == "proposed":
        scheme = f"proposed_L{s.first_sf}M{s.last_sf}w{s.transmissions_per_sf}"
    elif s.scheme == "fsf":
        scheme = f"fsf{s.fixed_sf}"
    else:
        scheme = s.scheme
    density = f"{s.interferer_density:g}".replace("-", "m")
    name = f"{scheme}_n{s.nodes}_r{s.radius:.0f}_{s.layout[0]}_i{s.interference}_d{density}_s{s.seed}"
    return name + (f"_{args.tag}" if args.tag else "")


def assign_groups(scenario: Scenario, session: Session, positions: list[tuple[float, float]]) -> None:
    """ GB-E / GB-L: equations (19) and (20) decide every recipient's SF. """
    for (node, position) in enumerate(positions):
        d0 = math.hypot(*position)
        sf = analytical.group_sf(scenario, d0, energy_based=(scenario.scheme == "gb_e"))
        session.assigned_sf[node] = sf
        session.pending_per_group.setdefault(sf, set()).add(node)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s: %(message)s")
    scenario = scenario_from_args(args)
    rng = random.Random(scenario.seed)
    random.seed(scenario.seed)  # the channel model (fading) draws from the global generator

    scenario.setup_phy()
    positions = read_layout(args.layout_file) if args.layout_file else scenario.node_positions(rng)
    scenario.nodes = len(positions)

    session = Session(scenario=scenario, start_time=SESSION_START, total_recipients=len(positions))
    if scenario.scheme in ("gb_e", "gb_l"):
        assign_groups(scenario, session, positions)

    interferer_positions: list[tuple[float, float]] = []
    interferers: list[Interferer] = []
    models: dict[int, InterferenceModel] = {}
    if scenario.interference != "none":
        interferer_positions = scenario.interferer_positions(rng)
    if scenario.interference == "simulated":
        interferers = [Interferer(p, scenario, session, rng) for p in interferer_positions]
    elif scenario.interference == "model":
        radius = scenario.interference_radius()
        for (node, position) in enumerate(positions):
            models[node] = InterferenceModel(scenario, distances_within(position, interferer_positions, radius), rng)

    recipients = [
        Recipient(node, position, scenario, session, rng, models.get(node))
        for (node, position) in enumerate(positions)
    ]
    gateway = Gateway(scenario, session, max_duration=args.max_hours * 3600)

    print(
        f"{run_name(scenario, args)}: {scenario.nodes} recipients within {scenario.radius:.0f} m, "
        f"{scenario.fragments} fragments of {scenario.fragment_size} B, code {scenario.effective_code}, "
        f"{len(interferer_positions)} interferers ({scenario.interference}), seed {scenario.seed}"
    )
    wall_start = time.time()
    sim.run(simulation_length=int((args.max_hours + 24) * 3600))
    wall_time = time.time() - wall_start

    nodes = pd.DataFrame([r.row() for r in recipients])
    if args.analytical and scenario.scheme in ("proposed", "fsf"):
        predictions = [analytical.predict(scenario, max(r.distance, 1.0), fast=True) for r in recipients]
        nodes["analytical_ec"] = [p.energy for p in predictions]
        nodes["analytical_ec_without_control"] = [p.energy_without_control for p in predictions]
        nodes["analytical_delivery_time"] = [p.delivery_time for p in predictions]

    decoded = nodes[nodes["decoded"]]
    mean_ec = decoded["ec"].mean()
    mean_ec_wo = decoded["ec_without_control"].mean()
    mean_dt = decoded["delivery_time"].mean() / 3600
    session_hours = ((session.end_time or sim.current_time()) - session.start_time) / 3600
    print(
        f"{len(decoded)}/{scenario.nodes} decoded, session {session_hours:.1f} h, "
        f"{gateway.frames_sent} frames {dict(sorted(session.frames_sent.items()))}, "
        f"mean EC {mean_ec:.2f} (without E_C {mean_ec_wo:.2f}), mean DT {mean_dt:.2f} h, "
        f"{sum(i.frames_sent for i in interferers)} interfering frames, {wall_time:.0f} s wall time"
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    name = run_name(scenario, args)
    nodes.insert(0, "run", name)
    nodes.to_csv(args.output_dir / f"{name}_nodes.csv", index=False)
    if not args.layout_file:
        write_layout(args.output_dir / f"layout_n{scenario.nodes}_r{scenario.radius:.0f}_{scenario.layout}_s{scenario.seed}.csv", positions)
    summary: dict[str, object] = {"run": name}
    summary.update({k: v for (k, v) in asdict(scenario).items() if not isinstance(v, dict)})
    summary.update({
        "code_used": scenario.effective_code,
        "fragments": scenario.fragments,
        "decoded": len(decoded),
        "session_hours": session_hours,
        "frames_sent": gateway.frames_sent,
        "interferers": len(interferer_positions),
        "interfering_frames": sum(i.frames_sent for i in interferers),
        "mean_ec": mean_ec,
        "mean_ec_without_control": mean_ec_wo,
        "mean_delivery_hours": mean_dt,
        "max_delivery_hours": decoded["delivery_time"].max() / 3600 if len(decoded) else float("nan"),
        "wall_time": wall_time,
    })
    pd.DataFrame([summary]).to_csv(args.output_dir / f"{name}_summary.csv", index=False)


if __name__ == "__main__":
    main()
