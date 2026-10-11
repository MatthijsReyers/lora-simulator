#!/usr/bin/env bash
# The simulation batteries of Section VII of the paper. Run from the repository root:
#
#   REPEATS=20 JOBS=10 ./papers/fuota-sequential-sf-2025/run.sh fig2 tables fig3
#
# The paper averages 100 runs per point; REPEATS sets the number of seeds per configuration
# (default 20), JOBS the number of parallel processes (default: all cores). The default
# interference mode is the fast statistical model; set INTERFERENCE=simulated for real
# interferer radios (orders of magnitude slower, see README). Without arguments every battery
# runs.
set -euo pipefail

MAIN="papers/fuota-sequential-sf-2025/main.py"
REPEATS="${REPEATS:-20}"
JOBS="${JOBS:-$(nproc)}"
INTERFERENCE="${INTERFERENCE:-model}"
EXTRA_ARGS="${EXTRA_ARGS:-}"
BATTERIES=("$@")
if [[ ${#BATTERIES[@]} -eq 0 ]]; then
    BATTERIES=(fig2 tables fig3)
fi

# Emits one command line per run; they are executed in parallel below.
commands() {
    for battery in "${BATTERIES[@]}"; do
        case "$battery" in
        fig2)
            # Figure 2, Tables I and II: every scheme at the default interferer density. The
            # proposed scheme also gets the analytical prediction per node.
            for seed in $(seq 1 "$REPEATS"); do
                echo "--scheme proposed --analytical --seed $seed"
                for sf in 10 11 12; do echo "--scheme fsf --sf $sf --seed $seed"; done
                echo "--scheme gb_e --seed $seed"
                echo "--scheme gb_l --seed $seed"
            done
            ;;
        tables)
            # Table III: three higher interferer densities for three schemes.
            for density in 0.0005 0.001 0.002; do
                for seed in $(seq 1 "$REPEATS"); do
                    echo "--scheme proposed --interferer-density $density --seed $seed"
                    echo "--scheme fsf --sf 11 --interferer-density $density --seed $seed"
                    echo "--scheme gb_l --interferer-density $density --seed $seed"
                done
            done
            ;;
        fig3)
            # Figure 3: the averages against w for L = 7..11 (M = 12).
            for L in 7 8 9 10 11; do
                for w in 25 50 75 100 150 200 250 300 350 400 500 600 700 800 900 1000; do
                    for seed in $(seq 1 "$REPEATS"); do
                        echo "--scheme proposed -L $L -w $w --seed $seed"
                    done
                done
            done
            ;;
        *)
            echo "unknown battery $battery (fig2, tables, fig3)" >&2
            exit 1
            ;;
        esac
    done
}

commands | xargs -P "$JOBS" -I {} sh -c "pipenv run python $MAIN --interference $INTERFERENCE $EXTRA_ARGS {} 2>&1 | grep -v 'Cancelling 0'"
