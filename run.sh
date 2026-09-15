#!/usr/bin/env bash
set -euo pipefail
echo ""
echo "Cleaning cache"
poetry run python cache_cleanup.py
echo ""
echo "Starting generator..."
poetry run python gtfs_parallel.py > debug_0.log
echo "skipping compat step"
git commit -am "Automated dataset generation"
echo "committed"
git push
echo "pushed"
