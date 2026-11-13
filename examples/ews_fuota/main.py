"""
    Energy cost of an EWS firmware update over the air.

    Sensors ping the gateway once per hour, the gateway uses one of those pings per hour to push
    a firmware image to a sensor that does not have it yet. Run from the repository root:

        pipenv run python examples/ews_fuota/main.py --sensors 10 --hours 24
"""
import argparse
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
from simulator.layout import read_layout
from simulator.lora.radio_usage import update_cost

from gateway import Gateway
from scenario import Scenario
from sensor import Sensor


def parse_args() -> tuple[Scenario, Path]:
    defaults = Scenario()
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--sensors", type=int, default=defaults.sensors)
    parser.add_argument("--hours", type=float, default=defaults.hours)
    parser.add_argument("--image-bytes", type=int, default=defaults.image_bytes)
    parser.add_argument("--ping-interval", type=float, default=defaults.ping_interval,
                        help="seconds between the pings of one sensor")
    parser.add_argument("--ping-slot", type=float, default=defaults.ping_slot,
                        help="seconds between the ping slots at the start of an interval")
    parser.add_argument("--fuota-offset", type=float, default=defaults.fuota_offset,
                        help="seconds from the start of an interval to the FUOTA moment")
    parser.add_argument("--burst-packets", type=int, default=None,
                        help="fragments per burst, default: as many as fit in 1 s")
    parser.add_argument("--sf", type=int, default=defaults.spreading_factor)
    parser.add_argument("--downlink-loss", type=float, default=defaults.downlink_loss,
                        help="share of downlink frames a sensor does not hear")
    parser.add_argument("--uplink-loss", type=float, default=defaults.uplink_loss,
                        help="share of uplink frames the gateway does not hear")
    parser.add_argument("--layout-file", type=Path, default=None,
                        help="CSV with a position per sensor; sets the number of sensors and "
                             "replaces the fixed path loss by a distance based one")
    parser.add_argument("--exponent", type=float, default=defaults.path_loss_exponent,
                        help="path loss exponent used with --layout-file")
    parser.add_argument("--seed", type=int, default=defaults.seed)
    parser.add_argument("--out", type=Path, default=None,
                        help="output directory, default: data/<run name> next to this script")
    args = parser.parse_args()

    positions = read_layout(args.layout_file) if args.layout_file else None
    scenario = Scenario(
        positions=positions, path_loss_exponent=args.exponent,
        sensors=len(positions) if positions else args.sensors, hours=args.hours, image_bytes=args.image_bytes,
        ping_interval=args.ping_interval, ping_slot=args.ping_slot,
        fuota_offset=args.fuota_offset, burst_packets=args.burst_packets,
        spreading_factor=args.sf, downlink_loss=args.downlink_loss,
        uplink_loss=args.uplink_loss, seed=args.seed,
    )
    name = f"n{scenario.sensors}_h{args.hours:g}_img{args.image_bytes}_sf{args.sf}_seed{args.seed}"
    if args.burst_packets:
        name += f"_burst{args.burst_packets}"
    if args.downlink_loss:
        name += f"_loss{args.downlink_loss:g}"
    if args.uplink_loss:
        name += f"_uploss{args.uplink_loss:g}"
    out = args.out or Path(os.path.dirname(os.path.abspath(__file__))) / "data" / name
    return (scenario, out)


def radio_energy(sensor: Sensor, end: float) -> tuple[float, float, float, float]:
    """ (TX joules, RX joules, TX seconds, RX seconds) of a sensor's radio over the whole run. """
    events = sensor.radio.power_consumer.events
    assert events is not None
    duration = events["time"].shift(-1, fill_value=end) - events["time"]
    tx_power = sensor.profile.tx_power(sensor.radio.tx_power, sensor.radio.tx_config)
    tx = (events["power"] - tx_power).abs() < 1e-9
    rx = (events["power"] - sensor.profile.RX_POWER).abs() < 1e-9
    return (
        float((duration[tx] * events["power"][tx]).sum()),
        float((duration[rx] * events["power"][rx]).sum()),
        float(duration[tx].sum()),
        float(duration[rx].sum()),
    )


def main() -> None:
    (scenario, out) = parse_args()
    random.seed(scenario.seed)
    scenario.setup_phy()

    net_ids = range(1, scenario.sensors + 1)
    gateway = Gateway(scenario, pending=list(net_ids))
    sensors = [Sensor(scenario, net_id) for net_id in net_ids]

    length = int(scenario.hours * 3600)
    started = time.time()
    sim.run(simulation_length=length)
    wall = time.time() - started

    (out / "radio_log").mkdir(parents=True, exist_ok=True)
    gateway.log.write(out / "radio_log")
    for sensor in sensors:
        sensor.log.write(out / "radio_log")

    cycles = pd.DataFrame([asdict(cycle) for sensor in sensors for cycle in sensor.cycles])
    cycles = cycles.sort_values("start").rename(columns={
        "start": "start_s", "awake": "awake_s", "energy": "energy_j",
    })
    cycles.to_csv(out / "cycles.csv", index=False, float_format="%.9g")

    rows = []
    for sensor in sensors:
        (tx_j, rx_j, tx_s, rx_s) = radio_energy(sensor, float(length))
        mine = cycles[cycles["sensor"] == sensor.name]
        # A sensor that never got the update is charged until the end of the run.
        updated = sensor.update_completed is not None
        cost = update_cost(
            sensor.radio, start=sensor.update_started or 0.0,
            end=sensor.update_completed if updated else float(length), updated=updated,
        )
        (x, y) = sensor.radio.position
        rows.append({
            "sensor": sensor.name,
            "x": x,
            "y": y,
            "distance": math.hypot(x, y),
            "completed_at_s": sensor.update_completed if updated else math.nan,
            **cost,
            "updated_at_s": gateway.updated.get(sensor.net_id, math.nan),
            "pings": int((mine["kind"] == "ping").sum()),
            "pings_without_ack": int((mine["outcome"] == "no_ack").sum()),
            "updates": int((mine["kind"] == "update").sum()),
            "tx_s": tx_s,
            "rx_s": rx_s,
            "awake_s": float(mine["awake_s"].sum()),
            "radio_tx_j": tx_j,
            "radio_rx_j": rx_j,
            "mcu_j": sensor.mcu.get_total_energy_consumed(),
            "session_j": sensor.sessions * sensor.profile.SESSION_ENERGY,
            "total_j": sensor.energy(),
        })
    summary = pd.DataFrame(rows)
    summary.to_csv(out / "sensors.csv", index=False, float_format="%.9g")

    print(f"{scenario.sensors} sensors, {scenario.hours:g} h, simulated in {wall:.1f} s")
    print(f"image: {scenario.image_bytes} B = {scenario.total_packets} fragments, "
          f"{scenario.packets_per_burst} per burst, FUOTA window {scenario.fuota_window:.3f} s")
    print(f"updated: {len(gateway.updated)}/{scenario.sensors}, "
          f"failed transfers: {gateway.failed_transfers}")
    for (label, selection) in [
        ("ping, acknowledged", cycles["outcome"] == "ack"),
        ("ping, slot moved", cycles["outcome"] == "command"),
        ("ping, no answer", cycles["outcome"] == "no_ack"),
        ("update, complete", cycles["outcome"] == "complete"),
        ("update, failed", cycles["outcome"].isin(["lost", "cancelled"])),
    ]:
        chosen = cycles[selection]
        if len(chosen) == 0:
            continue
        print(f"  {label:20s} n={len(chosen):4d}  "
              f"awake {statistics.mean(chosen['awake_s']):9.3f} s  "
              f"energy {statistics.mean(chosen['energy_j']) * 1000:10.3f} mJ")
    print(f"  mean energy per sensor over the run: {summary['total_j'].mean():.3f} J")
    print(f"results in {out}")


if __name__ == "__main__":
    main()
