#!/bin/bash
set -e

RESULTS="examples/p-csma/results.csv"
TMPDIR="examples/p-csma/.tmp_results"

# Clean previous results
rm -f "$RESULTS"
rm -rf "$TMPDIR"
mkdir -p "$TMPDIR"

# Number of parallel jobs (use all cores minus 1)
JOBS=${JOBS:-$(( $(nproc) - 1 ))}
echo "Running with $JOBS parallel jobs"

# Launch all (G, p) combinations in parallel
for p in 0.1 0.3 0.5 1.0; do
    for g in 0.25 0.5 0.75 1.0 1.25 1.5 2.0 2.5 3.0; do

        # Wait if we already have JOBS running
        while [ "$(jobs -rp | wc -l)" -ge "$JOBS" ]; do
            sleep 1
        done

        outfile="$TMPDIR/g${g}_p${p}.csv"
        echo "Starting G=$g p=$p"
        python examples/p-csma/main.py "$g" "$p" "$outfile" &
    done
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
