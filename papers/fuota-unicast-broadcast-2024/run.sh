#!/usr/bin/env bash
# Runs the simulation batteries of section 5 of the paper for the only unicast method.
# Run from the repository root. The paper averages 40 repetitions per point, set REPEATS to
# change the number of seeds per configuration (default 5).
#
#   REPEATS=3 ./papers/fuota-unicast-broadcast-2024/run.sh
#
set -euo pipefail

MAIN="papers/fuota-unicast-broadcast-2024/main.py"
REPEATS="${REPEATS:-5}"
METHOD="${METHOD:-unicast_only}"
# Set PAPER_FRAMES=1 to leave the MiWi header off the air like the paper's simulations did.
EXTRA_ARGS=()
if [[ "${PAPER_FRAMES:-0}" == "1" ]]; then
    EXTRA_ARGS+=(--paper-frames)
fi

run() {
    pipenv run python "$MAIN" --method "$METHOD" "${EXTRA_ARGS[@]}" "$@"
}

echo "== Single runs of 10 nodes (Figures 8 and 9) =="
run --nodes 10 --radius 400 --seed 0 --frame-log
run --nodes 10 --radius 2000 --seed 0 --frame-log

echo "== Effect of the size of the scenario (Figure 14) =="
for radius in $(seq 100 100 2000); do
    for seed in $(seq 1 "$REPEATS"); do
        run --nodes 10 --radius "$radius" --seed "$seed"
    done
done

echo "== Effect of the chunk size (Figures 15 and 16) =="
for frame_len in 39 55 71 87 103 119 135 151 167 183 199 215; do
    for radius in 400 2000; do
        for seed in $(seq 1 "$REPEATS"); do
            run --nodes 10 --radius "$radius" --frame-len "$frame_len" --seed "$seed"
        done
    done
done

echo "== Effect of the number of nodes (Figures 17 and 18) =="
for nodes in 10 30 50 70 90 110 130 150; do
    for radius in 400 2000; do
        for seed in $(seq 1 "$REPEATS"); do
            run --nodes "$nodes" --radius "$radius" --seed "$seed"
        done
    done
done
