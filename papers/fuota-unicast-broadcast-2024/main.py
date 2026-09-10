#!/usr/bin/env python3
"""
Runs one firmware update of a whole project and writes the results to `data/`.

    pipenv run python papers/fuota-unicast-broadcast-2024/main.py --nodes 10 --radius 400

Run from the repository root. See README.md for the parameters and the output format.
"""
import argparse, logging, math, os, random, sys, time

sys.path.append('.')  # To allow importing the simulator package while running from root folder.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import pandas as pd

from simulator.environment import simulation_env as sim
from simulator.lora.phy_layer import LoraPhyLayer

from frames import GATEWAY_ADDRESS, MiWiFrame, decode_gateway_message, decode_node_message
from scenario import Scenario, make_duty_cycles
from unicast_only.gateway import UnicastGateway
from unicast_only.node import UnicastNode


METHODS = {
    "unicast_only": (UnicastGateway, UnicastNode),
}

# Long enough that no realistic update runs into the end of the simulation. Advancing to the end
# is a single event so a large value costs nothing.
SIMULATION_LENGTH = 10 * 365 * 24 * 3600


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--method", choices=METHODS.keys(), default="unicast_only")
    parser.add_argument("--nodes", type=int, default=10, help="number of nodes to update")
    parser.add_argument("--radius", type=float, default=400.0, help="scenario radius in meters")
    parser.add_argument("--firmware-kb", type=float, default=100.0, help="firmware size in kB")
    parser.add_argument("--frame-len", type=int, default=215, help="frame length in bytes")
    parser.add_argument(
        "--paper-frames", action="store_true",
        help="leave the 23 byte MiWi header off the air like the paper's ns-3 simulations",
    )
    parser.add_argument("--duty-cycle", type=float, default=1.0, help="duty cycle in percent")
    parser.add_argument(
        "--per-device-duty-cycle", action="store_true",
        help="limit each transmitter individually instead of the paper's medium wide quiet time",
    )
    parser.add_argument("--exponent", type=float, default=3.2, help="path loss exponent")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--frame-log", action="store_true",
        help="also write a CSV with every frame and its RSSI at the receiver (memory hungry)",
    )
    parser.add_argument("--output-dir", default=os.path.join(os.path.dirname(__file__), "data"))
    parser.add_argument("--log-level", default="WARNING")
    return parser.parse_args()


def run_name(args: argparse.Namespace) -> str:
    frames = "p" if args.paper_frames else "f"
    return f"{args.method}_n{args.nodes}_r{int(args.radius)}_{frames}{args.frame_len}_s{args.seed}"


def frame_log(gateway, nodes, scenario: Scenario) -> pd.DataFrame:
    """
        One row per transmitted frame with the RSSI it had at its destination and whether it got
        there, the data behind Figures 8 and 9 of the paper.
    """
    phy_log = LoraPhyLayer().packets_log
    radio_ids = {gateway.radio._radio_id: GATEWAY_ADDRESS}
    receivers = {GATEWAY_ADDRESS: gateway.radio.packets_log.set_index("id")}
    for node in nodes:
        radio_ids[node.radio._radio_id] = node.address
        receivers[node.address] = node.radio.packets_log.set_index("id")

    rows = []
    for packet in phy_log.itertuples():
        frame = MiWiFrame.from_bytes(packet.payload, compact=not scenario.header_on_air)
        if frame.source == GATEWAY_ADDRESS:
            message = decode_gateway_message(frame.payload)
        else:
            message = decode_node_message(frame.payload)
        fragment = getattr(message, "number", getattr(message, "expected", None))
        received = receivers[frame.destination]
        if packet.id in received.index:
            rx = received.loc[packet.id]
            rssi = rx.rssi
            delivered = not (
                rx.collision or rx.missed_start or rx.missed_end or rx.interrupted or
                rx.demodulate_failure
            )
        else:
            rssi, delivered = float("nan"), False
        rows.append({
            "time": packet.start_time,
            "source": frame.source,
            "destination": frame.destination,
            "message": type(message).__name__,
            "fragment": fragment,
            "length": len(packet.payload),
            "airtime": packet.airtime,
            "rssi": rssi,
            "delivered": delivered,
        })
    return pd.DataFrame(rows)


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s: %(message)s")

    scenario = Scenario(
        nodes=args.nodes,
        radius=args.radius,
        firmware_size=int(args.firmware_kb * 1024),
        frame_len=args.frame_len,
        header_on_air=not args.paper_frames,
        duty_cycle=args.duty_cycle / 100,
        shared_duty_cycle=not args.per_device_duty_cycle,
        path_loss_exponent=args.exponent,
        seed=args.seed,
    )
    rng = random.Random(scenario.seed)
    random.seed(scenario.seed)  # The channel model draws from the global generator.

    scenario.setup_phy()
    gateway_type, node_type = METHODS[args.method]
    duty_cycles = make_duty_cycles(scenario, scenario.nodes + 1)

    positions = scenario.node_positions(rng)
    nodes = [
        node_type(address=i + 1, position=pos, scenario=scenario, duty_cycle=duty_cycles[i + 1])
        for (i, pos) in enumerate(positions)
    ]
    if not args.frame_log:
        for node in nodes:
            node.radio.log_packets = False

    distances = {node.address: math.hypot(*node.radio.position) for node in nodes}
    gateway = gateway_type(
        nodes=distances,
        firmware=scenario.make_firmware(rng),
        scenario=scenario,
        duty_cycle=duty_cycles[0],
    )

    print(
        f"{args.method}: {scenario.nodes} nodes within {scenario.radius:.0f}m, "
        f"{scenario.fragments} fragments of {scenario.chunk_size} bytes "
        f"({scenario.airtime(scenario.frame_len) * 1000:.1f}ms on air), seed {scenario.seed}"
    )
    wall_start = time.time()
    sim.run(simulation_length=SIMULATION_LENGTH)
    wall_time = time.time() - wall_start

    results = pd.DataFrame([
        {
            "node": r.address,
            "distance": r.distance,
            "start": r.start,
            "binary_start": r.binary_start,
            "binary_end": r.binary_end,
            "end": r.end,
            "binary_time": r.binary_time,
            "total_time": r.total_time,
            "frames_sent": r.frames_sent,
            "retransmissions": r.retransmissions,
            "restarts": r.restarts,
            "updated": r.updated,
        }
        for r in gateway.results.values()
    ])
    binary_total = results["binary_end"].max() - gateway.start_time
    total = gateway.finish_time - gateway.start_time
    print(results.to_string(index=False, float_format=lambda f: f"{f:.1f}"))
    print(
        f"binary exchange finished after {binary_total:.0f}s ({binary_total / 3600:.1f}h), "
        f"whole update after {total:.0f}s ({total / 3600:.1f}h), "
        f"{results['retransmissions'].sum()} retransmissions, "
        f"{results['updated'].sum()}/{scenario.nodes} nodes updated, "
        f"{wall_time:.0f}s wall time"
    )

    os.makedirs(args.output_dir, exist_ok=True)
    name = run_name(args)
    results.insert(0, "run", name)
    results.to_csv(os.path.join(args.output_dir, f"{name}_nodes.csv"), index=False)
    summary = pd.DataFrame([{
        "run": name,
        "method": args.method,
        "nodes": scenario.nodes,
        "radius": scenario.radius,
        "frame_len": scenario.frame_len,
        "header_on_air": scenario.header_on_air,
        "chunk_size": scenario.chunk_size,
        "firmware_size": scenario.firmware_size,
        "fragments": scenario.fragments,
        "duty_cycle": scenario.duty_cycle,
        "shared_duty_cycle": scenario.shared_duty_cycle,
        "exponent": scenario.path_loss_exponent,
        "seed": scenario.seed,
        "binary_total_time": binary_total,
        "total_time": total,
        "retransmissions": results["retransmissions"].sum(),
        "updated": results["updated"].sum(),
        "wall_time": wall_time,
    }])
    summary.to_csv(os.path.join(args.output_dir, f"{name}_summary.csv"), index=False)
    if args.frame_log:
        frame_log(gateway, nodes, scenario).to_csv(
            os.path.join(args.output_dir, f"{name}_frames.csv"), index=False,
        )


if __name__ == "__main__":
    main()
