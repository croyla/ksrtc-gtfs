from datetime import datetime, timedelta
import pandas as pd
import os
import re
import zipfile
import logging
import argparse
import subprocess
import shutil
import requests
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
parser.add_argument('--skip-shapes', action='store_true',
                     help='Skip shapes.txt generation via pfaedle (default: enabled — builds pfaedle '
                          'from source and downloads OSM extracts on first run, which can take a while)')

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

# ── Shape generation via pfaedle ─────────────────────────────────────────────
#
# pfaedle (https://github.com/ad-freiburg/pfaedle) map-matches each trip's
# station-to-station path against an OSM road network to produce shapes.txt.
# It has no Homebrew formula, so we build it from source into .pfaedle/ (git-
# ignored) on first run. It also needs an OSM extract covering the trips'
# stops; we auto-select and download only the Geofabrik India zone(s) that
# actually contain a stop, cached under .osm_cache/ (also gitignored).
#
# Runs here (post reverse-direction-merge) rather than in gtfs_parallel.py so
# it map-matches the merged/final route+trip set instead of the pre-merge one.

PFAEDLE_DIR = os.path.abspath(".pfaedle")
PFAEDLE_REPO_DIR = os.path.join(PFAEDLE_DIR, "src")
PFAEDLE_BUILD_DIR = os.path.join(PFAEDLE_REPO_DIR, "build")
PFAEDLE_BIN = os.path.join(PFAEDLE_BUILD_DIR, "pfaedle")
PFAEDLE_CFG = os.path.join(PFAEDLE_REPO_DIR, "pfaedle.cfg")
OSM_CACHE_DIR = os.path.abspath(".osm_cache")
GEOFABRIK_BASE = "https://download.geofabrik.de/asia/india"
GEOFABRIK_ZONES = [
    "southern-zone", "western-zone", "central-zone",
    "eastern-zone", "northern-zone", "north-eastern-zone",
]


# pfaedle's vendored [bus, coach] config caps snapping a stop onto the road
# graph at 100m. KSRTC's own reported stop coordinates for intercity stands
# (as opposed to city-centre stops with dense nearby streets) are often
# geocoded loosely enough to land >100m from any routable way, e.g. "Hubballi
# New Bus Stand" (C132-hubballi-new-bus-stand): the only ways within 100m are
# unnamed service/driveway segments, but the real connecting road (confirmed
# via Overpass) is ~100-500m out. pfaedle then can't snap that stop at all, so
# BOTH adjacent hops fall back to a straight line -- confirmed via
# `pfaedle -T <trip_id>`, which logged "No snapping candidate found for stop
# ... falling back to straight line" for GDGMUM_SARIGE's trip 0701GDGMUM, and
# stopped happening once this was raised. This under-matches a meaningful
# fraction of trips (~40 of 1930 shapes end up as <=6-point stubs).
#
# Keep this value modest: it directly drives candidate-edge search area
# (~quadratic in the distance), and this cfg applies globally to all ~2500
# trips against a >100M-node merged OSM graph. An earlier attempt at 1000m
# (10x the default, ~100x the search area) made a single full run take
# 11+ hours before being killed, versus ~13 minutes at the 100m default --
# 500m (25x the search area) is the smallest bump confirmed (via
# `pfaedle -T 0701GDGMUM`) to actually fix the Hubballi case above.
#
# `-P` (pfaedle's CLI config override) does NOT reliably override mode-scoped
# keys like this one, so we patch the vendored cfg file directly instead.
# Matched via regex (not a plain substring) and driven off the *current*
# value rather than a hardcoded "was 100" assumption, so re-running this
# against an already-patched file is a true no-op instead of compounding
# (100 -> 500 -> 500... not 100 -> 500 -> 5000..., which a naive
# `"...100" in text` substring check would do, since "100" is also a prefix
# of "500"-adjacent values like "1000"/"10000").
_BUS_SNAP_DISTANCE_RE = re.compile(r"(\[bus, coach\].*?osm_max_snap_distance: )(\d+)", re.DOTALL)
_BUS_SNAP_DISTANCE_TARGET = 500


def patch_pfaedle_bus_snap_distance():
    if not os.path.isfile(PFAEDLE_CFG):
        return
    with open(PFAEDLE_CFG) as f:
        cfg_text = f.read()
    match = _BUS_SNAP_DISTANCE_RE.search(cfg_text)
    if match is None:
        logger.warning(f"Could not find [bus, coach] osm_max_snap_distance in {PFAEDLE_CFG} to patch "
                        "(vendored cfg format may have changed) -- leaving as-is")
        return
    current = int(match.group(2))
    if current == _BUS_SNAP_DISTANCE_TARGET:
        return  # already patched
    new_text = cfg_text[:match.start()] + match.group(1) + str(_BUS_SNAP_DISTANCE_TARGET) + cfg_text[match.end():]
    with open(PFAEDLE_CFG, "w") as f:
        f.write(new_text)
    logger.info(f"Patched pfaedle [bus, coach] osm_max_snap_distance {current}m -> "
                f"{_BUS_SNAP_DISTANCE_TARGET}m in {PFAEDLE_CFG}")


def ensure_pfaedle_built() -> str | None:
    if os.path.isfile(PFAEDLE_BIN) and os.access(PFAEDLE_BIN, os.X_OK):
        patch_pfaedle_bus_snap_distance()
        return PFAEDLE_BIN
    logger.info("pfaedle binary not found — cloning and building from source "
                "(this can take a few minutes on first run)...")
    os.makedirs(PFAEDLE_DIR, exist_ok=True)
    try:
        if not os.path.isdir(os.path.join(PFAEDLE_REPO_DIR, ".git")):
            subprocess.run(
                ["git", "clone", "--recurse-submodules",
                 "https://github.com/ad-freiburg/pfaedle", PFAEDLE_REPO_DIR],
                check=True,
            )
        os.makedirs(PFAEDLE_BUILD_DIR, exist_ok=True)
        subprocess.run(["cmake", ".."], cwd=PFAEDLE_BUILD_DIR, check=True)
        subprocess.run(["make", "-j"], cwd=PFAEDLE_BUILD_DIR, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.error(f"Failed to build pfaedle ({e}); skipping shape generation")
        return None
    if not os.path.isfile(PFAEDLE_BIN):
        logger.error("pfaedle build finished but binary is missing; skipping shape generation")
        return None
    patch_pfaedle_bus_snap_distance()
    return PFAEDLE_BIN


def _parse_poly(text: str) -> list[list[tuple[float, float]]]:
    """Parse an Osmosis .poly file into a list of (lon, lat) rings."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    rings: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] | None = None
    for line in lines[1:]:  # line 0 is the polygon file's name, skip it
        if line == "END":
            if cur is not None:
                rings.append(cur)
                cur = None
            continue
        parts = line.split()
        if cur is None and len(parts) == 1:
            cur = []
            continue
        if cur is not None and len(parts) >= 2:
            lon, lat = float(parts[0]), float(parts[1])
            cur.append((lon, lat))
    return rings


def _point_in_rings(lon: float, lat: float, rings: list[list[tuple[float, float]]]) -> bool:
    """Even-odd ray-casting test summed over all rings (correctly handles holes)."""
    inside = False
    for ring in rings:
        n = len(ring)
        for i in range(n):
            x1, y1 = ring[i]
            x2, y2 = ring[(i + 1) % n]
            if (y1 > lat) != (y2 > lat):
                x_int = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
                if lon < x_int:
                    inside = not inside
    return inside


def zones_covering_stops(stop_points: list[tuple[float, float]]) -> list[str]:
    os.makedirs(OSM_CACHE_DIR, exist_ok=True)
    needed = []
    for zone in GEOFABRIK_ZONES:
        poly_path = os.path.join(OSM_CACHE_DIR, f"{zone}.poly")
        if not os.path.isfile(poly_path):
            resp = requests.get(f"{GEOFABRIK_BASE}/{zone}.poly", timeout=30)
            resp.raise_for_status()
            with open(poly_path, "w") as f:
                f.write(resp.text)
        rings = _parse_poly(open(poly_path).read())
        if any(_point_in_rings(lon, lat, rings) for lon, lat in stop_points):
            needed.append(zone)
    return needed


def download_zone_pbf(zone: str) -> str:
    dest = os.path.join(OSM_CACHE_DIR, f"{zone}.osm.pbf")
    if os.path.isfile(dest) and os.path.getsize(dest) > 0:
        return dest
    url = f"{GEOFABRIK_BASE}/{zone}-latest.osm.pbf"
    logger.info(f"Downloading OSM extract for {zone} (this may take a while)...")
    tmp = dest + ".part"
    with requests.get(url, stream=True, timeout=600) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    os.replace(tmp, dest)
    return dest


def merge_zone_pbfs(zones: list[str], pbf_paths: list[str]) -> str | None:
    """Merge multiple zone .osm.pbf files into one via `osmium merge`, so
    pfaedle can map-match cross-zone (interstate) trips in a single run.
    Returns the merged PBF path, or None if osmium-tool isn't available."""
    if shutil.which("osmium") is None:
        return None
    merged_path = os.path.join(OSM_CACHE_DIR, f"merged-{'-'.join(sorted(zones))}.osm.pbf")
    if os.path.isfile(merged_path) and os.path.getsize(merged_path) > 0:
        return merged_path
    logger.info(f"Merging OSM zones via osmium: {', '.join(zones)}...")
    tmp = merged_path + ".part"
    cmd = ["osmium", "merge", *pbf_paths, "-o", tmp, "-f", "pbf", "--overwrite"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(f"osmium merge failed: {result.stderr[-2000:]}")
        if os.path.isfile(tmp):
            os.remove(tmp)
        return None
    os.replace(tmp, merged_path)
    return merged_path


def run_pfaedle(pfaedle_bin: str, osm_pbf: str, gtfs_zip: str, out_dir: str) -> bool:
    os.makedirs(out_dir, exist_ok=True)
    cmd = [pfaedle_bin, "-x", osm_pbf, "-m", "bus", "-c", PFAEDLE_CFG, gtfs_zip]
    logger.info(f"Running pfaedle against {os.path.basename(osm_pbf)}...")
    result = subprocess.run(cmd, cwd=out_dir, capture_output=True, text=True)
    if result.stdout:
        logger.debug(f"pfaedle stdout for {osm_pbf}:\n{result.stdout}")
    if result.returncode != 0:
        logger.error(f"pfaedle failed for {osm_pbf}: {result.stderr[-2000:]}\nstdout: {result.stdout[-2000:]}")
        return False
    return True


def merge_shapes_into_output(zone_out_dirs: list[str]):
    """Merge shapes.txt / trips.txt shape_id from one or more pfaedle gtfs-out
    dirs into OUTPUT_DIR, preferring the first zone that matched each trip."""
    trip_shape_id: dict[str, str] = {}
    shape_groups: dict[str, pd.DataFrame] = {}
    for out_dir in zone_out_dirs:
        gtfs_out = os.path.join(out_dir, "gtfs-out")
        shapes_path = os.path.join(gtfs_out, "shapes.txt")
        trips_path = os.path.join(gtfs_out, "trips.txt")
        if not os.path.isfile(shapes_path) or not os.path.isfile(trips_path):
            continue
        trips_df_out = pd.read_csv(trips_path, dtype=str)
        if "shape_id" not in trips_df_out.columns:
            continue
        for _, row in trips_df_out.iterrows():
            tid, sid = row["trip_id"], row.get("shape_id")
            if tid not in trip_shape_id and isinstance(sid, str) and sid:
                trip_shape_id[tid] = sid
        shapes_df_out = pd.read_csv(shapes_path, dtype=str)
        for sid, group in shapes_df_out.groupby("shape_id"):
            shape_groups.setdefault(sid, group)

    if not trip_shape_id:
        logger.warning("pfaedle produced no shapes; leaving GTFS feed without shapes.txt")
        return

    used_shape_ids = set(trip_shape_id.values())
    shapes_out = pd.concat(
        [shape_groups[sid] for sid in used_shape_ids if sid in shape_groups], ignore_index=True
    )
    shapes_out.to_csv(f"{OUTPUT_DIR}/shapes.txt", index=False)

    trips_df_final = pd.read_csv(f"{OUTPUT_DIR}/trips.txt", dtype=str)
    trips_df_final["shape_id"] = trips_df_final["trip_id"].map(trip_shape_id).fillna("")
    trips_df_final.to_csv(f"{OUTPUT_DIR}/trips.txt", index=False)

    n_matched = sum(1 for v in trip_shape_id.values() if v)
    logger.info(f"pfaedle matched shapes for {n_matched}/{len(trips_df_final)} trips "
                f"across {len(zone_out_dirs)} OSM zone(s)")


def generate_shapes():
    """Run pfaedle against the just-written OUTPUT_DIR GTFS files, updating
    shapes.txt and trips.txt (shape_id) in place there."""
    if args.skip_shapes:
        logger.info("Skipping shape generation (--skip-shapes)")
        return

    with open(f"{OUTPUT_DIR}/attribution.txt", "w") as f:
        f.write(
            "attribution_id,organization_name,attribution_url,is_producer,is_operator,is_authority\n"
            f"osm,OpenStreetMap Contributors,en,https://openstreetmap.org,,,\n"
        )

    stops_out_df = pd.read_csv(f"{OUTPUT_DIR}/stops.txt", dtype=str)
    stop_points = [(float(lon), float(lat)) for lat, lon in
                   zip(stops_out_df["stop_lat"], stops_out_df["stop_lon"])]
    needed_zones = zones_covering_stops(stop_points) if stop_points else []
    if not needed_zones:
        logger.warning("No stops matched any Geofabrik India zone; skipping shape generation")
        return

    logger.info(f"Stops span OSM zone(s): {', '.join(needed_zones)}")
    pfaedle_bin = ensure_pfaedle_built()
    if pfaedle_bin is None:
        logger.warning("pfaedle unavailable; continuing without shapes.txt")
        return

    # pfaedle needs a zip to read from — write one now from the current
    # (pre-shapes) OUTPUT_DIR contents.
    tmp_zip_path = os.path.join(PFAEDLE_DIR, "pre-shapes.zip")
    with zipfile.ZipFile(tmp_zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for fname in os.listdir(OUTPUT_DIR):
            if fname == ".DS_Store":
                continue
            zipf.write(os.path.join(OUTPUT_DIR, fname), arcname=fname)

    pbf_paths = {}
    for zone in needed_zones:
        try:
            pbf_paths[zone] = download_zone_pbf(zone)
        except requests.RequestException as e:
            logger.error(f"Failed to download OSM extract for {zone}: {e}")

    zone_out_dirs = []
    if len(pbf_paths) > 1:
        merged_pbf = merge_zone_pbfs(list(pbf_paths.keys()), list(pbf_paths.values()))
        if merged_pbf is not None:
            out_dir = os.path.join(PFAEDLE_DIR, "out", "merged")
            if run_pfaedle(pfaedle_bin, merged_pbf, tmp_zip_path, out_dir):
                zone_out_dirs.append(out_dir)
        else:
            logger.warning(
                "osmium-tool not found; running pfaedle once per zone instead of "
                "merging. Cross-zone (interstate) trips may get incomplete shapes "
                "since each run only sees roads within its own zone — "
                "`brew install osmium-tool` to enable automatic merging."
            )

    if not zone_out_dirs:
        for zone, pbf_path in pbf_paths.items():
            out_dir = os.path.join(PFAEDLE_DIR, "out", zone)
            if run_pfaedle(pfaedle_bin, pbf_path, tmp_zip_path, out_dir):
                zone_out_dirs.append(out_dir)

    if zone_out_dirs:
        merge_shapes_into_output(zone_out_dirs)
    else:
        logger.warning("All pfaedle runs failed; continuing without shapes.txt")

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
    # service_id is untouched by the route-merge below (only route_id/direction_id
    # are remapped per trip), so calendar_dates.txt carries over as-is -- but most
    # trips (the IRR_* "irregular" services from gtfs_parallel.py's get_service_id)
    # have NO calendar.txt row at all and rely entirely on this file for their
    # active dates. Dropping it here left every such trip with zero active service
    # days in gtfs_compat.zip -- looking like the trip/route didn't exist.
    calendar_dates_df = pd.read_csv(f"{TEMP_EXTRACT_DIR}/calendar_dates.txt", dtype={"date": str}) if os.path.exists(f"{TEMP_EXTRACT_DIR}/calendar_dates.txt") else None
    feed_info_df = pd.read_csv(f"{TEMP_EXTRACT_DIR}/feed_info.txt") if os.path.exists(f"{TEMP_EXTRACT_DIR}/feed_info.txt") else None

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
    if calendar_dates_df is not None:
        calendar_dates_df.to_csv(f"{OUTPUT_DIR}/calendar_dates.txt", index=False)
    if feed_info_df is not None:
        feed_info_df.to_csv(f"{OUTPUT_DIR}/feed_info.txt", index=False)

    logger.info("Saved all transformed GTFS files")
except Exception as e:
    logger.error(f"Error saving GTFS files: {e}")
    exit(1)

# Step 6: Generate shapes.txt via pfaedle (map-matches the merged route/trip
# set written above; see generate_shapes() for details)
generate_shapes()

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