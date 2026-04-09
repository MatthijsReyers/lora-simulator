#!/bin/bash

# Test different persistence probabilities
for p in 1.0; do

    # Test different offered loads
    for g in $(seq 3 0.25 6); do

        python examples/p-csma/main.py $g $p
    done
done
