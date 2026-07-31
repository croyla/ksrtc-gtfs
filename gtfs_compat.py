from datetime import datetime, timedelta
import pandas as pd
import os
import re
import zipfile
import logging
import argparse
from collections import defaultdict

# Parse command line arguments
parser = argparse.ArgumentParser(
    description='Transform GTFS dataset by merging reverse-direction routes',
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog='''
Examples:
  python gtfs_compat.py                              # Use default input/output
  python gtfs_compat.py --input gtfs.zip       # Specify input ZIP
  python gtfs_compat.py --output gtfs_compat          # Custom output directory
    '''
)
parser.add_argument('--input', type=str, default='gtfs.zip',
                    help='Input GTFS ZIP file (default: gtfs.zip)')
parser.add_argument('--output', type=str, default='tmp/gtfs',
                    help='Output directory for transformed GTFS files (default: tmp/gtfs)')
parser.add_argument('--log-level', type=str, default='INFO',
                    choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
                    help='Logging level (default: INFO)')

args = parser.parse_args()

# Setup Logging
logging.basicConfig(
    level=getattr(logging, args.log_level),
    handlers=[
        logging.FileHandler("gtfs_user.log"),  # Log to file
        logging.StreamHandler()  # Log to console
    ],
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger()

# Configuration
INPUT_ZIP = args.input
OUTPUT_DIR = args.output
TEMP_EXTRACT_DIR = f"{OUTPUT_DIR}_temp"

# Create output directories
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(TEMP_EXTRACT_DIR, exist_ok=True)

logger.info("Starting GTFS transformation process...")
logger.info(f"Input: {INPUT_ZIP}")
logger.info(f"Output: {OUTPUT_DIR}")

# Step 1: Extract the input GTFS ZIP
logger.info("Extracting input GTFS ZIP file...")
try:
    with zipfile.ZipFile(INPUT_ZIP, 'r') as zip_ref:
        zip_ref.extractall(TEMP_EXTRACT_DIR)
    logger.info(f"Extracted GTFS files to {TEMP_EXTRACT_DIR}")
except Exception as e:
    logger.error(f"Error extracting ZIP file: {e}")
    exit(1)

# Step 2: Read GTFS files into DataFrames
logger.info("Reading GTFS files...")
try:
    routes_df = pd.read_csv(f"{TEMP_EXTRACT_DIR}/routes.txt")
    trips_df = pd.read_csv(f"{TEMP_EXTRACT_DIR}/trips.txt")
    stop_times_df = pd.read_csv(f"{TEMP_EXTRACT_DIR}/stop_times.txt")
    stops_df = pd.read_csv(f"{TEMP_EXTRACT_DIR}/stops.txt")

    # Read optional files
    agency_df = pd.read_csv(f"{TEMP_EXTRACT_DIR}/agency.txt") if os.path.exists(f"{TEMP_EXTRACT_DIR}/agency.txt") else None
    calendar_df = pd.read_csv(f"{TEMP_EXTRACT_DIR}/calendar.txt") if os.path.exists(f"{TEMP_EXTRACT_DIR}/calendar.txt") else None
    feed_info_df = pd.read_csv(f"{TEMP_EXTRACT_DIR}/feed_info.txt") if os.path.exists(f"{TEMP_EXTRACT_DIR}/feed_info.txt") else None
    shapes_df = pd.read_csv(f"{TEMP_EXTRACT_DIR}/shapes.txt") if os.path.exists(f"{TEMP_EXTRACT_DIR}/shapes.txt") else None

    logger.info(f"Read {len(routes_df)} routes, {len(trips_df)} trips, {len(stop_times_df)} stop times, {len(stops_df)} stops")
except Exception as e:
    logger.error(f"Error reading GTFS files: {e}")
    exit(1)

# Step 3: Merge reverse-direction routes
# route_id is "<od_code>_<service_slug>" (see gtfs_parallel.py), where od_code
# is an opaque fragment of KSRTC's own TripCode -- station abbreviations are
# variable-length (BIAL=4, MYS=3, ...) so od_code can't be split and reversed
# by string manipulation to find a route's reverse-direction counterpart
# (e.g. BIALMYS_FLYBUS's counterpart MYSBIAL_FLYBUS).
#
# Instead, identify reverse pairs structurally from the stops each route
# actually serves: every stop_id is "C<city_id>-<slug>" (canonical_stop_id in
# gtfs_parallel.py), so a route's first/last stop resolves to a city_id that's
# stable regardless of which specific stop/terminal text is used. Two routes
# are a reverse-direction pair when they share the same service_slug and the
# same unordered pair of endpoint city_ids.
logger.info("Merging reverse-direction routes...")

CITY_KEY_RE = re.compile(r'^(C\d+)-')


def city_key(stop_id):
    match = CITY_KEY_RE.match(str(stop_id))
    return match.group(1) if match else str(stop_id)


# route_id -> (origin_city_key, destination_city_key), from the first trip
# encountered for that route (all trips on a route share the same stop
# sequence, so any one trip is representative). Order is kept (not just the
# unordered pair) so direction_id can be derived from actual travel direction
# rather than from which specific route_id happens to be "first".
stop_times_sorted = stop_times_df.sort_values(['trip_id', 'stop_sequence'])
trip_endpoints = stop_times_sorted.groupby('trip_id')['stop_id'].agg(['first', 'last'])

route_endpoints = {}
for trip_id, route_id in zip(trips_df['trip_id'], trips_df['route_id']):
    if route_id in route_endpoints or trip_id not in trip_endpoints.index:
        continue
    endpoint_row = trip_endpoints.loc[trip_id]
    route_endpoints[route_id] = (city_key(endpoint_row['first']), city_key(endpoint_row['last']))

# route_long_name is now clean city names (see gtfs_parallel.py), only parsed
# here to reformat "A to B" into "A ⇆ B" for display -- not used for merge
# decisions, which rely solely on the city_id keys above.
LONG_NAME_RE = re.compile(r'^(?P<service>[^:]+):\s*(?P<origin>.+?)\s+to\s+(?P<dest>.+)$', re.IGNORECASE)

routes_by_id = {row['route_id']: row for _, row in routes_df.iterrows()}

# Group by service_slug + the *unordered* endpoint pair, so every route
# between the same two places on the same service -- regardless of how many
# separate KSRTC route_ids exist per direction (e.g. Manipal and Udupi share
# a city_id in KSRTC's own city master, so BNGMNP_PALLAKKI, BNGUDP_PALLAKKI
# and UDPBNG_PALLAKKI all land in one group) -- ends up on one merged route.
pair_groups = defaultdict(list)
for route_id in routes_df['route_id']:
    service_slug = route_id.split('_', 1)[1] if '_' in route_id else route_id
    endpoints = route_endpoints.get(route_id)
    key = (service_slug, frozenset(endpoints)) if endpoints is not None else ('__no_stops__', route_id)
    pair_groups[key].append(route_id)

# Create new routes data
new_routes = []
route_id_mapping = {}   # Maps old route_id -> new route_id
direction_mapping = {}  # Maps old route_id -> direction_id

for key, route_ids in pair_groups.items():
    # Sort for a deterministic canonical route_id/short_name
    route_ids_sorted = sorted(route_ids)
    canonical_id = route_ids_sorted[0]

    if len(route_ids_sorted) > 1:
        # direction_id is derived from actual travel direction, not from
        # route_id: pick a canonical (origin, dest) ordering -- direction 0
        # is "away from" the alphabetically-first endpoint city, direction 1
        # is the reverse -- so every route_id, however many share each
        # direction, is assigned consistently.
        city_a, city_b = sorted(route_endpoints[canonical_id])
        for route_id in route_ids_sorted:
            origin, _dest = route_endpoints[route_id]
            direction_mapping[route_id] = 0 if origin == city_a else 1
            route_id_mapping[route_id] = canonical_id
        logger.debug(f"Merged bidirectional routes {route_ids_sorted} -> {canonical_id}")

        # Build the display name/short_name from a route_id actually running
        # direction 0, so short_name and long_name describe the same direction.
        display_id = next((rid for rid in route_ids_sorted if direction_mapping[rid] == 0), canonical_id)
        display_row = routes_by_id[display_id]
        long_name = display_row['route_long_name']
        name_match = LONG_NAME_RE.match(str(long_name).strip())
        if name_match:
            long_name = f"{name_match.group('service').strip()}: {name_match.group('origin').strip()} ⇆ {name_match.group('dest').strip()}"
    else:
        route_id_mapping[canonical_id] = canonical_id
        direction_mapping[canonical_id] = 0
        display_row = routes_by_id[canonical_id]
        long_name = display_row['route_long_name']

    new_routes.append({
        'route_id': canonical_id,
        'agency_id': display_row['agency_id'],
        'route_short_name': display_row['route_short_name'],
        'route_long_name': long_name,
        'route_type': display_row['route_type']
    })

new_routes_df = pd.DataFrame(new_routes)
logger.info(f"Merged {len(routes_df)} routes into {len(new_routes_df)} routes")

# Step 4: Update trips with new route_ids and direction_ids
logger.info("Updating trips with new route IDs and direction IDs...")
trips_df['direction_id'] = trips_df['route_id'].map(direction_mapping)
trips_df['route_id'] = trips_df['route_id'].map(route_id_mapping)

# Verify all route_ids were mapped
unmapped = trips_df[trips_df['route_id'].isna()]
if len(unmapped) > 0:
    logger.warning(f"!! {len(unmapped)} trips have unmapped route_ids")

logger.info(f"Updated {len(trips_df)} trips")

# Step 5: Save transformed GTFS files
logger.info("Saving transformed GTFS files...")

try:
    new_routes_df.to_csv(f"{OUTPUT_DIR}/routes.txt", index=False)
    trips_df.to_csv(f"{OUTPUT_DIR}/trips.txt", index=False)
    stop_times_df.to_csv(f"{OUTPUT_DIR}/stop_times.txt", index=False)
    stops_df.to_csv(f"{OUTPUT_DIR}/stops.txt", index=False)

    # Copy unchanged files
    if agency_df is not None:
        agency_df.to_csv(f"{OUTPUT_DIR}/agency.txt", index=False)
    if calendar_df is not None:
        calendar_df.to_csv(f"{OUTPUT_DIR}/calendar.txt", index=False)
    if feed_info_df is not None:
        feed_info_df.to_csv(f"{OUTPUT_DIR}/feed_info.txt", index=False)
    if shapes_df is not None:
        shapes_df.to_csv(f"{OUTPUT_DIR}/shapes.txt", index=False)

    logger.info("Saved all transformed GTFS files")
except Exception as e:
    logger.error(f"Error saving GTFS files: {e}")
    exit(1)

# Step 7: Create output ZIP file
logger.info("Creating output ZIP file...")
output_zip_path = "./gtfs_compat.zip"
logger.info(os.path.dirname(output_zip_path))
os.makedirs(os.path.dirname(output_zip_path), exist_ok=True)

try:
    ignored_files = ['.DS_Store']
    with zipfile.ZipFile(output_zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for file in os.listdir(OUTPUT_DIR):
            if file in ignored_files:
                continue
            file_path = os.path.join(OUTPUT_DIR, file)
            zipf.write(file_path, arcname=file)
            logger.debug(f"Added {file} to archive")

    # Get file sizes for logging
    uncompressed_size = sum(os.path.getsize(os.path.join(OUTPUT_DIR, f)) for f in os.listdir(OUTPUT_DIR) if f not in ignored_files)
    compressed_size = os.path.getsize(output_zip_path)
    compression_ratio = (1 - compressed_size / uncompressed_size) * 100 if uncompressed_size > 0 else 0

    logger.info(f"Created output ZIP: {output_zip_path}")
    logger.info(f"Uncompressed size: {uncompressed_size / 1024:.2f} KB")
    logger.info(f"Compressed size: {compressed_size / 1024:.2f} KB")
    logger.info(f"Compression ratio: {compression_ratio:.1f}%")
except Exception as e:
    logger.error(f"Error creating ZIP file: {e}")
    exit(1)

# Step 8: Cleanup temporary directory
logger.info("Cleaning up temporary files...")
try:
    import shutil
    shutil.rmtree(TEMP_EXTRACT_DIR)
    logger.info(f"Removed temporary directory: {TEMP_EXTRACT_DIR}")
except Exception as e:
    logger.warning(f"!! Could not remove temporary directory: {e}")

# Summary
logger.info("\n" + "="*60)
logger.info("TRANSFORMATION SUMMARY")
logger.info("="*60)
logger.info(f"Routes: {len(routes_df)} -> {len(new_routes_df)} (merged)")
logger.info(f"Trips: {len(trips_df)} (updated route references)")
logger.info(f"Output: {output_zip_path}")
logger.info("="*60)
logger.info("GTFS transformation completed successfully!")
if os.path.exists('summary.txt'):
    os.remove('summary.txt')
with open('summary.txt', 'w') as f:
    f.write(f"""
    Routes: {len(routes_df)} -> {len(new_routes_df)} (merged)
    Trips: {len(trips_df)} (updated route references)
    Uncompressed size: {uncompressed_size / 1024:.2f} KB
    Compressed size: {compressed_size / 1024:.2f} KB
    Compression ratio: {compression_ratio:.1f}%
    """)