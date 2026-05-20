#!/bin/bash
set -e

RESULTS="examples/np-csma/results.csv"
TMPDIR="examples/np-csma/.tmp_results"

# Clean previous results
rm -f "$RESULTS"
rm -rf "$TMPDIR"
mkdir -p "$TMPDIR"

# Number of parallel jobs (use all cores minus 1)
JOBS=${JOBS:-$(( $(nproc) - 1 ))}
echo "Running with $JOBS parallel jobs"

# Launch all G values in parallel
for g in $(seq 0.2 0.2 7.0); do

    # Wait if we already have JOBS running
    while [ "$(jobs -rp | wc -l)" -ge "$JOBS" ]; do
        sleep 1
    done

    outfile="$TMPDIR/g${g}.csv"
    echo "Starting G=$g"
    python examples/np-csma/main.py "$g" "$outfile" &
done

# Wait for all background jobs to finish
echo "Waiting for all jobs to finish..."
wait

# Merge all individual CSV files into one
echo "Merging results..."
python -c "
import pandas as pd, glob
frames = [pd.read_csv(f) for f in sorted(glob.glob('$TMPDIR/*.csv'))]
pd.concat(frames, ignore_index=True).to_csv('$RESULTS', index=False)
print(f'Merged {len(frames)} results into $RESULTS')
"

# Clean up temp files
rm -rf "$TMPDIR"

echo "Done!"
