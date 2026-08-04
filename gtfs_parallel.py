from datetime import datetime, timedelta
from collections import defaultdict

import requests
import pandas as pd
import os
import zipfile
import logging
import concurrent.futures
import threading
import argparse
import hashlib
import json
import time
import random
import re
import math

# ── CLI args ──────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(
    description='Generate GTFS dataset from KSRTC API',
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog='''
Examples:
  python gtfs_parallel.py
  python gtfs_parallel.py --workers 20
  python gtfs_parallel.py --start-date 2026-07-01
  python gtfs_parallel.py --output custom_gtfs
    '''
)
parser.add_argument('--workers', type=int, default=10,
                    help='Parallel workers for route discovery and trip fetches (default: 10)')
parser.add_argument('--output', type=str, default='gtfs',
                    help='Output directory for GTFS files (default: gtfs)')
parser.add_argument('--log-level', type=str, default='INFO',
                    choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
                    help='Logging level (default: INFO)')
parser.add_argument('--start-date', type=str,
                    default=datetime.now().strftime('%Y-%m-%d'),
                    help='First date to query (default: today, format: YYYY-MM-DD)')
parser.add_argument('--sentinel-from', type=int, default=368,
                    help='fromCityID for sentinel date-range check (default: 368 Bengaluru)')
parser.add_argument('--sentinel-to', type=int, default=296,
                    help='toCityID for sentinel date-range check (default: 296 Mysuru)')
parser.add_argument('--max-dates', type=int, default=0,
                    help='Cap the number of service dates queried (0 = no cap, useful for test runs)')
parser.add_argument('--search-pairs', type=str, default='search-pairs.json',
                    help='Path to seed city-pair JSON (see scrape_abhibus_routes.py) used as the '
                         'starting candidate set for route discovery (default: search-pairs.json)')
parser.add_argument('--geojson', type=str, default='ksrtc-stops.geojson',
                    help='Path to stops GeoJSON file (default: ksrtc-stops.geojson)')
parser.add_argument('--services-map', type=str, default='ksrtc-services.json',
                    help='Path to raw ServiceType → {slug, label} map used for route_id/long_name '
                         '(default: ksrtc-services.json). Unknown service types get an auto-generated '
                         'slug/label at runtime.')
parser.add_argument('--max-expansion-rounds', type=int, default=3,
                    help='Max iterative rounds probing pairs among cities already found active, '
                         'to catch non-hub-to-non-hub routes (default: 3, 0 to disable)')
parser.add_argument('--no-city-name-dedup', action='store_true',
                     help='Disable reusing a city centre across different city_ids that share the '
                          'same name (default: enabled — skips a redundant Overpass city lookup)')

args = parser.parse_args()

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, args.log_level),
    handlers=[
        logging.FileHandler("latest.log"),
        logging.StreamHandler()
    ],
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger()

# ── Constants ─────────────────────────────────────────────────────────────────

BASE_URL = "https://ksrtcapi.iamgds.com/api/resource"
HEADERS = {"Accept": "*/*", "User-Agent": "ksrtc-gtfs-gen/1.0"}
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://z.overpass-api.de/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
]
_INDIA_BBOX = "(6.0,68.0,37.5,98.0)"  # lat_min,lon_min,lat_max,lon_max

OUTPUT_DIR = args.output
GEOJSON_PATH = args.geojson
START_DATE = datetime.strptime(args.start_date, '%Y-%m-%d').date()
SENTINEL_FROM = args.sentinel_from
SENTINEL_TO = args.sentinel_to
SEARCH_PAIRS_PATH = args.search_pairs
SERVICES_MAP_PATH = args.services_map
MAX_WORKERS = args.workers
CITY_NAME_DEDUP = not args.no_city_name_dedup

os.makedirs(OUTPUT_DIR, exist_ok=True)

CACHE_DIR = os.path.join("tmp", "api_cache")
OVERPASS_CACHE_DIR = os.path.join("tmp", "overpass_cache")
CACHE_TTL = 30 * 24 * 3600  # 1 month
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(OVERPASS_CACHE_DIR, exist_ok=True)

logger.info(f"Starting KSRTC GTFS generation from {START_DATE} (sentinel: {SENTINEL_FROM}→{SENTINEL_TO})")

# ── Service type map ──────────────────────────────────────────────────────────
# Raw KSRTC "ServiceType" string → slug (route_id suffix) and slug → display label
# (route long_name). See ksrtc-services.json — two raw ServiceType strings that
# share a slug (e.g. "NON AC SLEEPER" and "PALLAKKI (NON AC SLEEPER)" both → PALLAKKI)
# are treated as the same service, since KSRTC's own API is inconsistent about which
# branding it reports for the same product across different chart dates.

SERVICE_SLUGS: dict[str, str] = {}
SERVICE_LABELS: dict[str, str] = {}
_unknown_service_slugs: dict[str, str] = {}  # raw ServiceType → auto-generated slug, logged once

if os.path.exists(SERVICES_MAP_PATH):
    with open(SERVICES_MAP_PATH) as f:
        _services_cfg = json.load(f)
    SERVICE_SLUGS = _services_cfg.get("services", {})
    SERVICE_LABELS = _services_cfg.get("labels", {})
    logger.info(f"Loaded {len(SERVICE_SLUGS)} service type mappings from {SERVICES_MAP_PATH}")
else:
    logger.warning(f"No services map at {SERVICES_MAP_PATH} — all service types will use auto-generated slugs")


def service_slug_and_label(service_type: str) -> tuple[str, str]:
    """Return (slug, label) for a raw ServiceType string, via SERVICE_SLUGS/SERVICE_LABELS,
    falling back to an auto-generated slug/label for anything not in ksrtc-services.json."""
    service_type = service_type or "UNKNOWN"
    slug = SERVICE_SLUGS.get(service_type)
    if slug:
        return slug, SERVICE_LABELS.get(slug, slug.replace("_", " ").title())

    slug = re.sub(r'[^A-Z0-9]+', '_', service_type.upper()).strip('_') or "UNKNOWN"
    if service_type not in _unknown_service_slugs:
        _unknown_service_slugs[service_type] = slug
        logger.warning(f"ServiceType {service_type!r} not in {SERVICES_MAP_PATH} — "
                        f"using auto-generated slug {slug!r}. Consider adding it.")
    return slug, service_type.title()

# ── API cache ─────────────────────────────────────────────────────────────────

def _cache_key(url: str, params: dict | None) -> str:
    raw = url + (json.dumps(params, sort_keys=True) if params else "")
    return hashlib.sha256(raw.encode()).hexdigest()


_RETRY_EXCEPTIONS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)
_MAX_RETRIES = 4


def peek_cache(url: str, params: dict | None = None):
    """Return a cached response's parsed data without making a network request,
    or None if nothing is cached (or it's expired). Lets callers check whether a
    request has already been made — e.g. by a previous run, or by probing a
    different date — before deciding to issue a new live request."""
    cache_file = os.path.join(CACHE_DIR, f"{_cache_key(url, params)}.json")
    if not os.path.exists(cache_file):
        return None
    try:
        with open(cache_file) as f:
            entry = json.load(f)
        if time.time() < entry["expires_at"]:
            return entry["data"]
    except Exception:
        pass
    return None


def cached_get(url: str, params: dict | None = None, timeout: int = 15,
               skip_cache_if_empty=None) -> requests.Response | None:
    """skip_cache_if_empty: optional predicate(data) -> bool. When it returns True for
    a freshly-fetched response, that response is NOT written to the persistent cache —
    it's still returned to the caller for this call. Used for the sentinel booking-horizon
    check, where a negative result can be a transient "not yet open" rather than a real
    end of the booking window, and caching it for the full CACHE_TTL would incorrectly
    suppress that date for every run over the next month. Ordinary city-pair queries
    don't use this — an empty result there is a real, cacheable fact about that pair."""
    key = _cache_key(url, params)
    cache_file = os.path.join(CACHE_DIR, f"{key}.json")

    if os.path.exists(cache_file):
        try:
            with open(cache_file) as f:
                entry = json.load(f)
            if time.time() < entry["expires_at"]:
                logger.debug(f"Cache hit: {url} params={params}")
                mock = requests.models.Response()
                mock.status_code = 200
                mock._content = json.dumps(entry["data"]).encode()
                return mock
        except Exception:
            pass

    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            resp.raise_for_status()

            # Empty body → treat as empty result list and cache it
            if not resp.content:
                data = []
            else:
                try:
                    data = resp.json()
                except Exception:
                    logger.debug(f"Non-JSON body from {url}; treating as empty")
                    data = []

            if skip_cache_if_empty is None or not skip_cache_if_empty(data):
                try:
                    entry = {"expires_at": time.time() + CACHE_TTL, "data": data}
                    with open(cache_file, "w") as f:
                        json.dump(entry, f)
                except Exception as e:
                    logger.warning(f"Failed to write cache for {url}: {e}")
            else:
                logger.debug(f"Not caching empty result for {url} params={params}")

            mock = requests.models.Response()
            mock.status_code = 200
            mock._content = json.dumps(data).encode()
            return mock

        except _RETRY_EXCEPTIONS as e:
            if attempt < _MAX_RETRIES - 1:
                delay = (2 ** attempt) + random.uniform(0, 1)
                logger.debug(f"Retry {attempt + 1}/{_MAX_RETRIES} for {url} after {delay:.1f}s: {e}")
                time.sleep(delay)
            else:
                logger.debug(f"All retries exhausted for {url} params={params}: {e}")
                return None
        except Exception as e:
            logger.debug(f"Request failed {url} params={params}: {e}")
            return None

# ── Stops GeoJSON ─────────────────────────────────────────────────────────────
# stop_id (str, "C<city_id>-<slug>" — see canonical_stop_id) → {lat, lon, stop_name, city_name, city_id, source}

stops_db: dict[str, dict] = {}
stops_db_lock = threading.Lock()
stops_db_dirty = False

# city_id → (lat, lon), seeded from previously resolved stops and grown as new
# stops are resolved. Lets resolve_stop skip the Overpass city-centre lookup
# for a city once any stop in it has been geocoded (this run or a past one).
city_coords_cache: dict[int, tuple[float, float]] = {}

# Normalized city name → (lat, lon). Backs the "same name, different city_id"
# case — the KSRTC city master has duplicate/inconsistent city_ids for what's
# really the same place, so name is the more reliable join key there (stop_id
# itself can't catch this, since it's keyed by city_id). Gated by
# CITY_NAME_DEDUP / --no-city-name-dedup.
city_name_coords_cache: dict[str, tuple[float, float]] = {}


def _norm_name(name: str) -> str:
    """Case/whitespace-insensitive key for name-matching city/stop coords."""
    return re.sub(r'\s+', ' ', (name or '').strip()).lower()


def load_stops_geojson():
    if not os.path.exists(GEOJSON_PATH):
        logger.info(f"No existing stops GeoJSON at {GEOJSON_PATH}; starting fresh")
        return
    with open(GEOJSON_PATH) as f:
        gj = json.load(f)
    for feature in gj.get("features", []):
        props = feature["properties"]
        coords = feature["geometry"]["coordinates"]  # [lon, lat]
        stops_db[str(props["stop_id"])] = {
            "lat": coords[1],
            "lon": coords[0],
            "stop_name": props["stop_name"],
            "city_name": props["city_name"],
            "city_id": props.get("city_id"),
            "source": props.get("source", "manual"),
        }

    # Seed the city-centre caches, preferring "default" entries (those *are* a
    # city centre) over "estimate"/"manual" ones (a specific stop's location,
    # only an approximation of the centre but still useful within-city).
    for preferred_source in ("default", "estimate", "manual"):
        for info in stops_db.values():
            if info["source"] != preferred_source:
                continue
            coords = (info["lat"], info["lon"])
            cid = info.get("city_id")
            if cid is not None and cid not in city_coords_cache:
                city_coords_cache[cid] = coords
            cname_key = _norm_name(info["city_name"])
            if cname_key and cname_key not in city_name_coords_cache:
                city_name_coords_cache[cname_key] = coords

    logger.info(f"Loaded {len(stops_db)} stops from {GEOJSON_PATH} "
                f"({len(city_coords_cache)} known city centres)")


def save_stops_geojson():
    features = []
    with stops_db_lock:
        for stop_id, info in stops_db.items():
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [info["lon"], info["lat"]]},
                "properties": {
                    "stop_id": stop_id,
                    "stop_name": info["stop_name"],
                    "city_name": info["city_name"],
                    "city_id": info.get("city_id"),
                    "source": info["source"],
                },
            })
    gj = {"type": "FeatureCollection", "features": features}
    with open(GEOJSON_PATH, "w") as f:
        json.dump(gj, f, indent=2)
    logger.info(f"Saved {len(features)} stops to {GEOJSON_PATH}")


def recalibrate_coords():
    """Cascade coordinates down the source hierarchy (manual > estimate > default)
    without changing any stop's recorded source. A lower-tier stop's (lat, lon) is
    replaced with a higher-tier stop's at the same (stop_name, city_name) if one
    exists, else with a higher-tier stop's anywhere in the same city_name. This
    lets one human fix (manual) or a good Overpass hit (estimate) propagate to
    every less-trusted duplicate of that stop/city, instead of leaving them at
    their original, less accurate coordinates.

    Matching is name-based (via _norm_name), not city_id-based — same convention
    as city_name_coords_cache — since the KSRTC city master can have duplicate/
    inconsistent city_ids for what's really the same place.

    Runs manual→estimate before estimate→default so an estimate stop recalibrated
    from manual can, in turn, carry that position on down to matching defaults.
    """
    global stops_db_dirty

    def stop_key(info):
        return (_norm_name(info["stop_name"]), _norm_name(info["city_name"]))

    def cascade(from_source, to_source):
        by_stop, by_city = {}, {}
        for info in stops_db.values():
            if info["source"] != from_source:
                continue
            key = stop_key(info)
            by_stop.setdefault(key, (info["lat"], info["lon"]))
            by_city.setdefault(key[1], (info["lat"], info["lon"]))

        changed = 0
        for info in stops_db.values():
            if info["source"] != to_source:
                continue
            key = stop_key(info)
            coords = by_stop.get(key) or by_city.get(key[1])
            if coords and coords != (info["lat"], info["lon"]):
                info["lat"], info["lon"] = coords
                changed += 1
        return changed

    with stops_db_lock:
        n_estimate = cascade("manual", "estimate")
        n_default = cascade("estimate", "default")

    if n_estimate or n_default:
        stops_db_dirty = True
        logger.info(f"Recalibrated coords: {n_estimate} estimate stop(s) moved to match manual, "
                    f"{n_default} default stop(s) moved to match estimate (sources unchanged)")

# ── Overpass geocoding ────────────────────────────────────────────────────────

# Geographic centre of Karnataka — absolute last-resort fallback
DEFAULT_LAT, DEFAULT_LON = 15.3173, 75.7139

_overpass_lock = threading.Lock()
_overpass_last = 0.0
# In-memory cache for city centres (avoids repeated disk lookups within a run)
_city_center_cache: dict[str, tuple[float, float] | None] = {}

_OVERPASS_RETRY_EXC = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)

# Strips phone numbers: STD+subscriber, 10-digit mobiles, +91 prefixed, in brackets or bare.
_PHONE_RE = re.compile(
    r'[\s,\-]*'
    r'[\(\[]?'
    r'(?:\+?91[\s\-]?)?'       # optional +91 country code
    r'(?:0\d{2,5}[\s\-]?)?'   # optional 0-prefixed STD code
    r'\d{6,10}'                 # subscriber / mobile digits
    r'[\)\]]?'
)


def clean_name(name: str) -> str:
    """Remove phone numbers and normalise whitespace from a stop or city name."""
    s = _PHONE_RE.sub('', name or '')
    s = re.sub(r'\s+', ' ', s).strip().strip('()[],-').strip()
    return s


def _slug(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', _norm_name(clean_name(name))).strip('-') or 'stop'


def canonical_stop_id(city_id: int | None, stop_name: str, city_name: str = '') -> str:
    """Deterministic stop_id from (city_id, stop_name) — two raw KSRTC ids that
    share a city and name always resolve to the same canonical id, so
    duplicate/inconsistent KSRTC stop ids for the same physical stop collapse
    into one GTFS stop instead of needing separate coordinate-dedup logic.
    Falls back to a normalized city name when CityID is missing from the API
    response (rare, but not guaranteed present)."""
    if city_id is not None:
        return f"C{city_id}-{_slug(stop_name)}"
    return f"N{_slug(city_name)}-{_slug(stop_name)}"


def _overpass_query(query: str) -> dict | None:
    """
    POST an Overpass QL query with:
    - Disk cache (30-day TTL, same as API cache)
    - 1.5 s inter-request rate limit
    - Up to 4 attempts with exponential backoff + jitter
    - Per-status handling: 429 → long wait, 503/504 → shorter wait,
      network errors → jittered retry, Overpass timeout in body → retry
    Returns the parsed JSON dict, or None on total failure.
    """
    global _overpass_last

    cache_key = hashlib.sha256(query.encode()).hexdigest()
    cache_file = os.path.join(OVERPASS_CACHE_DIR, f"{cache_key}.json")

    # Disk cache read
    if os.path.exists(cache_file):
        try:
            with open(cache_file) as f:
                entry = json.load(f)
            if time.time() < entry["expires_at"]:
                logger.debug(f"Overpass cache hit ({cache_key[:8]}…)")
                return entry["data"]
        except Exception:
            pass  # stale or corrupt — re-fetch

    endpoint_idx = 0
    for attempt in range(_MAX_RETRIES * len(OVERPASS_ENDPOINTS)):
        endpoint = OVERPASS_ENDPOINTS[endpoint_idx % len(OVERPASS_ENDPOINTS)]

        # Enforce minimum gap between requests
        with _overpass_lock:
            elapsed = time.time() - _overpass_last
            if elapsed < 1.5:
                time.sleep(1.5 - elapsed)
            _overpass_last = time.time()

        try:
            resp = requests.post(
                endpoint,
                data={"data": query},
                headers={"User-Agent": "ksrtc-gtfs-gen/1.0 (blrtransit@gmail.com)", "Accept": "application/json"},
                timeout=90,
            )

            if resp.status_code == 429:
                # Rate-limited — back off on this endpoint, then try next
                wait = 10 * (2 ** (attempt // len(OVERPASS_ENDPOINTS))) + random.uniform(0, 3)
                logger.debug(f"Overpass 429 on {endpoint} — waiting {wait:.1f}s")
                time.sleep(wait)
                endpoint_idx += 1
                continue

            if resp.status_code in (406, 503, 504):
                # 406 = server rejected request (rate-block or bad UA); 503/504 = overloaded
                logger.debug(f"Overpass {resp.status_code} on {endpoint} — trying next endpoint")
                endpoint_idx += 1
                time.sleep(2 + random.uniform(0, 1))
                continue

            resp.raise_for_status()
            data = resp.json()

            # Overpass signals a server-side query timeout inside a 200 response
            if data.get("remark", "") and "timeout" in data["remark"].lower():
                logger.debug(f"Overpass server timeout on {endpoint} — trying next endpoint")
                endpoint_idx += 1
                time.sleep(3)
                continue

            # Write disk cache
            try:
                with open(cache_file, "w") as f:
                    json.dump({"expires_at": time.time() + CACHE_TTL, "data": data}, f)
            except Exception as e:
                logger.debug(f"Overpass cache write failed: {e}")

            return data

        except _OVERPASS_RETRY_EXC as e:
            # Connection refused / timeout — immediately try next endpoint
            logger.debug(f"Overpass connection error on {endpoint}: {e} — trying next endpoint")
            endpoint_idx += 1
            time.sleep(1 + random.uniform(0, 1))
        except Exception as e:
            logger.debug(f"Overpass unexpected error on {endpoint}: {e}")
            return None

    logger.warning("Overpass: all endpoints and retries exhausted")
    return None


def _get_city_center(city_name: str) -> tuple[float, float] | None:
    """Return OSM centroid for a city/town/village, with in-memory cache."""
    key = city_name.strip().lower()
    if key in _city_center_cache:
        return _city_center_cache[key]

    # Nodes only + exact name match — way/relation scans over India bbox time out.
    # Place nodes cover virtually all Indian cities/towns in OSM.
    safe = re.sub(r'["\\\n]', '', city_name)
    query = f"""
[out:json][timeout:60];
node["name"="{safe}"]["place"~"city|town|village|suburb|municipality|district"]{_INDIA_BBOX};
out 5;
"""
    data = _overpass_query(query)

    # Fallback: case-insensitive regex within Karnataka bbox only (handles renamed cities
    # e.g. Bellary→Ballari, Belgaum→Belagavi, Shimoga→Shivamogga).
    if not data or not data.get("elements"):
        _KA_BBOX = "(11.5,74.0,18.5,78.5)"
        query2 = f"""
[out:json][timeout:60];
node["name"~"^{safe}$",i]["place"~"city|town|village|suburb|municipality|district"]{_KA_BBOX};
out 5;
"""
        data = _overpass_query(query2)

    result = None
    if data and data.get("elements"):
        _rank = {"city": 0, "town": 1, "municipality": 2, "village": 3, "suburb": 4, "district": 5}
        best = None
        for el in data["elements"]:
            lat = el.get("lat") or (el.get("center") or {}).get("lat")
            lon = el.get("lon") or (el.get("center") or {}).get("lon")
            if lat is None or lon is None:
                continue
            rank = _rank.get(el.get("tags", {}).get("place", ""), 99)
            if best is None or rank < best[0]:
                best = (rank, float(lat), float(lon))
        if best:
            result = (best[1], best[2])

    _city_center_cache[key] = result
    if result:
        logger.debug(f"City center {city_name!r}: {result}")
    else:
        logger.debug(f"City center not found for {city_name!r}")
    return result


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def _name_score(osm_name: str, target: str) -> float:
    """Jaccard word-overlap similarity, 0..1."""
    def tokens(s):
        return set(re.sub(r'[^\w\s]', '', s.lower()).split())
    t, o = tokens(target), tokens(osm_name)
    if not t or not o:
        return 0.0
    return len(t & o) / len(t | o)


def _find_bus_stop(
    stop_name: str, city_name: str, city_lat: float, city_lon: float
) -> tuple[float, float] | None:
    """
    Query Overpass for bus stops/stations within 20 km of city_lat/city_lon.

    If stop_name == city_name (e.g. "Bengaluru, Bengaluru") → return the
    bus_station amenity closest to the city centre (the main/central stand).
    Otherwise → return the best name-matching stop (Jaccard ≥ 0.2).
    """
    RADIUS = 20000  # metres
    is_city_stop = stop_name.lower() == city_name.lower()

    if is_city_stop:
        query = f"""
[out:json][timeout:60];
(
  node["amenity"="bus_station"](around:{RADIUS},{city_lat},{city_lon});
  way["amenity"="bus_station"](around:{RADIUS},{city_lat},{city_lon});
  relation["amenity"="bus_station"](around:{RADIUS},{city_lat},{city_lon});
);
out center;
"""
    else:
        safe = re.sub(r'["\\\n]', '', stop_name)
        query = f"""
[out:json][timeout:60];
(
  node["amenity"="bus_station"]["name"~"{safe}",i](around:{RADIUS},{city_lat},{city_lon});
  node["highway"="bus_stop"]["name"~"{safe}",i](around:{RADIUS},{city_lat},{city_lon});
  way["amenity"="bus_station"]["name"~"{safe}",i](around:{RADIUS},{city_lat},{city_lon});
  relation["amenity"="bus_station"]["name"~"{safe}",i](around:{RADIUS},{city_lat},{city_lon});
);
out center;
"""

    data = _overpass_query(query)
    if not data or not data.get("elements"):
        return None

    candidates = []
    for el in data["elements"]:
        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lon = el.get("lon") or (el.get("center") or {}).get("lon")
        if lat is None or lon is None:
            continue
        lat, lon = float(lat), float(lon)
        dist = _haversine_km(city_lat, city_lon, lat, lon)
        osm_name = el.get("tags", {}).get("name", "")
        score = 0.0 if is_city_stop else _name_score(osm_name, stop_name)
        candidates.append((score, dist, lat, lon))

    if not candidates:
        return None

    if is_city_stop:
        candidates.sort(key=lambda x: x[1])          # closest to centre
    else:
        candidates.sort(key=lambda x: (-x[0], x[1])) # best match, nearest tie-break
        if candidates[0][0] < 0.2:                    # no meaningful match
            return None

    _, _, lat, lon = candidates[0]
    return lat, lon


def resolve_stop(stop_id: str, stop_name: str, city_name: str, city_id: int | None = None) -> tuple[float, float]:
    """
    Resolve (lat, lon) for a brand-new stop_id, in priority order:
    1. Overpass bus stop match → source="estimate"
    2. Overpass city centre fallback → source="default"
    3. Karnataka centre → source="default"
    Phone numbers are stripped from names before querying and before saving.

    Only ever called for a stop_id not already in stops_db — any stop_id
    already present (manual, estimate, or default) is trusted as-is by the
    caller and never re-queried, to avoid spending Overpass calls on stops
    we've already resolved. The manual short-circuit below is a defensive
    no-op for that case, kept in case this is ever called directly elsewhere.

    stop_id is the canonical id (see canonical_stop_id) — two raw KSRTC ids
    sharing (city_id, stop_name) are the same stop_id here already, so no
    separate stop-level dedup cache is needed; duplicates collapse before this
    function is even called.

    City-centre resolution first checks city_coords_cache (by city_id), then —
    if CITY_NAME_DEDUP — city_name_coords_cache (by name, catching duplicate/
    inconsistent city_ids for the same place), before falling back to Overpass.
    """
    global stops_db_dirty
    with stops_db_lock:
        if stop_id in stops_db and stops_db[stop_id].get("source") == "manual":
            return stops_db[stop_id]["lat"], stops_db[stop_id]["lon"]

    clean_stop = clean_name(stop_name)
    clean_city = clean_name(city_name)
    cname_key = _norm_name(clean_city)

    city_coords = city_coords_cache.get(city_id) if city_id is not None else None
    if city_coords:
        logger.debug(f"City centre cache hit for city_id={city_id} ({clean_city!r}) — skipping Overpass")
    elif CITY_NAME_DEDUP and city_name_coords_cache.get(cname_key):
        city_coords = city_name_coords_cache[cname_key]
        logger.debug(f"City-name match for {clean_city!r} — reused coords, skipped Overpass")
        if city_id is not None:
            city_coords_cache[city_id] = city_coords
    else:
        city_coords = _get_city_center(clean_city)
        if city_coords:
            if city_id is not None:
                city_coords_cache[city_id] = city_coords
            city_name_coords_cache[cname_key] = city_coords

    if city_coords:
        city_lat, city_lon = city_coords
        stop_coords = _find_bus_stop(clean_stop, clean_city, city_lat, city_lon)
        if stop_coords:
            lat, lon = stop_coords
            source = "estimate"
            logger.debug(f"Bus stop found for {clean_stop!r} in {clean_city!r}: ({lat:.4f}, {lon:.4f})")
        else:
            lat, lon = city_lat, city_lon
            source = "default"
            logger.debug(f"Using city centre for {clean_stop!r} in {clean_city!r}")
    else:
        lat, lon = DEFAULT_LAT, DEFAULT_LON
        source = "default"
        logger.warning(f"City not found on Overpass for {clean_city!r} — using Karnataka default")

    with stops_db_lock:
        stops_db[stop_id] = {
            "lat": lat, "lon": lon,
            "stop_name": clean_stop,
            "city_name": clean_city,
            "city_id": city_id,
            "source": source,
        }
        stops_db_dirty = True

    return lat, lon

# ── Time helpers ──────────────────────────────────────────────────────────────

_API_TIME_FMT = "%d-%b-%Y %I:%M %p"  # "16-Jun-2026 04:00 AM"


def parse_api_dt(time_str: str) -> datetime:
    return datetime.strptime(time_str.strip(), _API_TIME_FMT)


def dt_to_gtfs(dt: datetime, base_date: datetime.date) -> str:
    """Convert datetime to GTFS HH:MM:SS, allowing hours ≥ 24 for post-midnight."""
    delta_seconds = (dt - datetime.combine(base_date, datetime.min.time())).total_seconds()
    total_minutes = int(delta_seconds // 60)
    h = total_minutes // 60
    m = total_minutes % 60
    return f"{h:02}:{m:02}:00"

# ── Phase 1: fetch cities ─────────────────────────────────────────────────────

logger.info("Phase 1: Fetching city list...")
all_cities: dict[int, str] = {}  # city_id → city_name
city_codes: dict[int, str] = {}  # city_id → KSRTC's own short code (e.g. 368 → "BNG")

resp = cached_get(f"{BASE_URL}/getStaticCityList")
if resp:
    data = resp.json().get("data", {})
    for entry in data.values():
        all_cities[entry["ID"]] = entry["Name"]
        if entry.get("Key"):
            city_codes[entry["ID"]] = entry["Key"]
    logger.info(f"Loaded {len(all_cities)} cities")
else:
    logger.error("Failed to fetch city list; aborting")
    exit(1)

# ── Phase 2: discover date range, then discover trips across all valid dates ───

logger.info("Phase 2a: Probing service date range via sentinel route...")


def _filter_trips(data) -> list[dict]:
    """Drop non-dict entries (e.g. the [null] sentinel some empty responses use)."""
    if not isinstance(data, list):
        return []
    return [t for t in data if t is not None]


def _parse_trips(resp) -> list[dict]:
    """Parse a route search response, treating [null] and [] as empty."""
    try:
        return _filter_trips(resp.json())
    except Exception:
        return []


def has_service_on_date(date_str: str) -> bool:
    """Return True if the sentinel city pair has any trips on date_str.

    A negative result is never cached (see skip_cache_if_empty on cached_get): the
    sentinel pair can come back empty transiently — the vendor's chart for that date
    not yet being open at request time — rather than because the booking horizon has
    actually ended. Caching that would suppress the date for every run over the next
    CACHE_TTL, not just this one."""
    resp = cached_get(
        f"{BASE_URL}/searchRoutesV4",
        params={
            "fromCityID": SENTINEL_FROM,
            "toCityID": SENTINEL_TO,
            "journeyDate": date_str,
            "mode": "oneway",
        },
        timeout=60,
        skip_cache_if_empty=lambda d: len(_filter_trips(d)) == 0,
    )
    if not resp:
        return False
    return len(_parse_trips(resp)) > 0


# A single empty/not-yet-open sentinel day shouldn't truncate the whole remaining
# booking horizon, so tolerate a run of consecutive empty days before concluding the
# horizon has actually ended.
EMPTY_DATE_TOLERANCE = 5

service_dates: list[str] = []
current = START_DATE
consecutive_empty = 0
while True:
    date_str = current.strftime("%Y-%m-%d")
    if has_service_on_date(date_str):
        service_dates.append(date_str)
        logger.debug(f"  {date_str}: service available")
        consecutive_empty = 0
    else:
        consecutive_empty += 1
        logger.debug(f"  {date_str}: no service ({consecutive_empty}/{EMPTY_DATE_TOLERANCE} consecutive empty)")
        if consecutive_empty >= EMPTY_DATE_TOLERANCE:
            logger.info(f"  {EMPTY_DATE_TOLERANCE} consecutive empty sentinel days ending {date_str} "
                        "— end of booking horizon")
            break
    current += timedelta(days=1)

if not service_dates:
    logger.error(f"No service found from {START_DATE}; check sentinel city IDs or start date")
    exit(1)

if args.max_dates and len(service_dates) > args.max_dates:
    service_dates = service_dates[:args.max_dates]
    logger.info(f"  Capped to {args.max_dates} dates via --max-dates")

logger.info(f"Phase 2a complete: {len(service_dates)} service dates ({service_dates[0]} → {service_dates[-1]})")

discovered_trips: dict[int, dict] = {}  # trip_id → trip metadata (first occurrence wins)
discovered_lock = threading.Lock()
active_pairs: list[tuple[int, int]] = []
active_pairs_lock = threading.Lock()

# (from_city_id, to_city_id) → full set of trip_ids seen for that pair on
# whichever probe date within PROBE_DATE_WINDOW first turned up trips. Written
# once per pair in probe_pair (Phase 2b / 2b.5 only — search_active_pair
# doesn't touch it), then used to drop pairs whose trips are entirely
# subsumed by another pair before the expensive Phase 2c date expansion
# (see "Phase 2c prep" below).
pair_trip_ids: dict[tuple[int, int], set[int]] = {}

# trip_id → {raw ServiceType → set of date strings (within service_dates) the
# trip was seen active on with that ServiceType}. Phase 2c probes every active
# pair across every remaining service date, so by the end of Phase 2 this is a
# complete per-trip attendance record over the full horizon, split by observed
# ServiceType — it's what lets Phase 5 tell a daily trip apart from one that
# only runs certain weekdays or specific dates, AND split a trip into separate
# GTFS trips/routes if KSRTC reports genuinely different service slugs for it
# on different dates (see service_slug_and_label — same-slug ServiceType
# strings, e.g. "NON AC SLEEPER" vs "PALLAKKI (NON AC SLEEPER)", do NOT cause
# a split).
trip_service_dates: dict[int, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))


def _ingest_trips(trips: list[dict], date_str: str):
    """Add new trips from a parsed route-search result into discovered_trips."""
    with discovered_lock:
        for trip in trips:
            trip_id = trip.get("TripID")
            if not trip_id:
                continue
            if trip_id not in discovered_trips:
                discovered_trips[trip_id] = {
                    "trip_id": trip_id,
                    "service_id": trip.get("ServiceID"),
                    "route_name": trip.get("RouteName", ""),
                    "trip_code": trip.get("TripCode", ""),
                    "service_type": trip.get("ServiceType", ""),
                    "from_city_id": trip.get("FromCityID"),
                    "to_city_id": trip.get("ToCityID"),
                    "departure_time": trip.get("DepartureTime", ""),
                    "arrival_time": trip.get("ArrivalTime", ""),
                    "chart_date": trip.get("ChartDate", date_str),
                }
            trip_service_dates[trip_id][trip.get("ServiceType", "")].add(date_str)


def probe_pair(from_city_id: int, to_city_id: int, dates: list[str],
               backward_peek_dates: list[str] = ()):
    """Query one city pair across candidate dates, stopping at the first date with
    trips. A pair is only considered failed once every candidate date comes up empty.

    Cache-aware: before issuing any live request, scan for a date already cached
    (a prior run, or another phase having probed that date/pair) and use it if it
    has trips. This checks backward_peek_dates (days before the reference date —
    e.g. yesterday — cache-only, never live-fetched, since they're not part of the
    service horizon being built) first, then the forward window, so it avoids
    burning live requests on dates in the window when either a prior day's cache
    or a later date already proves the pair active."""
    url = f"{BASE_URL}/searchRoutesV4"

    for date_str in list(backward_peek_dates) + list(dates):
        params = {"fromCityID": from_city_id, "toCityID": to_city_id,
                  "journeyDate": date_str, "mode": "oneway"}
        cached_data = peek_cache(url, params)
        if cached_data is None:
            continue
        trips = _filter_trips(cached_data)
        if trips:
            pair = (from_city_id, to_city_id)
            trip_ids = {t.get("TripID") for t in trips if t.get("TripID")}
            with active_pairs_lock:
                active_pairs.append(pair)
                pair_trip_ids[pair] = trip_ids
            _ingest_trips(trips, date_str)
            return

    for date_str in dates:
        resp = cached_get(
            url,
            params={"fromCityID": from_city_id, "toCityID": to_city_id,
                    "journeyDate": date_str, "mode": "oneway"},
            timeout=60,
        )
        if not resp:
            continue
        trips = _parse_trips(resp)
        if trips:
            pair = (from_city_id, to_city_id)
            trip_ids = {t.get("TripID") for t in trips if t.get("TripID")}
            with active_pairs_lock:
                active_pairs.append(pair)
                pair_trip_ids[pair] = trip_ids
            _ingest_trips(trips, date_str)
            return


def search_active_pair(from_city_id: int, to_city_id: int, date_str: str):
    """Query a known-active city pair for a subsequent service date."""
    resp = cached_get(
        f"{BASE_URL}/searchRoutesV4",
        params={"fromCityID": from_city_id, "toCityID": to_city_id,
                "journeyDate": date_str, "mode": "oneway"},
        timeout=60,
    )
    if not resp:
        return
    _ingest_trips(_parse_trips(resp), date_str)


def load_seed_pairs(path: str) -> list[tuple[int, int]]:
    """Load candidate city pairs from a search-pairs.json produced by scrape_abhibus_routes.py.

    Each matched entry seeds both directions, since abhibus lists a route once
    but KSRTC service generally runs both ways.
    """
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        logger.error(f"Seed pairs file not found: {path}. Run scrape_abhibus_routes.py first, "
                     f"or pass --search-pairs to point at one.")
        exit(1)

    pairs = {
        pair
        for entry in data.get("pairs", [])
        for pair in [(entry["from_city_id"], entry["to_city_id"]), (entry["to_city_id"], entry["from_city_id"])]
        if entry["from_city_id"] != entry["to_city_id"]
    }
    return list(pairs)


def append_new_pairs_to_seed_file(path: str, new_pairs: set[tuple[int, int]], cities: dict[int, str]):
    """Append newly-discovered active pairs (found via Phase 2b.5 reachable×reachable
    expansion, not present in the original seed file) to search-pairs.json, so future
    runs seed Phase 2b directly from them instead of having to rediscover them via
    expansion every time. Re-reads the file fresh (rather than reusing the in-memory
    copy from load_seed_pairs) so concurrent edits to the file aren't clobbered."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning(f"Could not update {path} with newly discovered pairs: {e}")
        return

    existing = {(e["from_city_id"], e["to_city_id"]) for e in data.get("pairs", [])}
    added = 0
    for from_id, to_id in sorted(new_pairs):
        if (from_id, to_id) in existing:
            continue
        name_from = cities.get(from_id, str(from_id))
        name_to = cities.get(to_id, str(to_id))
        data.setdefault("pairs", []).append({
            "from_city_id": from_id,
            "to_city_id": to_id,
            "from_name": name_from,
            "to_name": name_to,
            "from_matched_name": name_from,
            "to_matched_name": name_to,
            "from_match_method": "ksrtc-discovered",
            "to_match_method": "ksrtc-discovered",
        })
        existing.add((from_id, to_id))
        added += 1

    if added:
        data["matched_count"] = len(data.get("pairs", []))
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        logger.info(f"Appended {added} newly discovered pair(s) to {path}")


city_ids = list(all_cities.keys())
base_pairs = load_seed_pairs(SEARCH_PAIRS_PATH)
probed_pairs: set[tuple[int, int]] = set(base_pairs)

# ── Phase 2b: probe seed city pairs on the reference date ────────────────────
reference_date = service_dates[0]
# A pair with no service on the reference date may still run later in the week
# (e.g. a weekly or weekday-only route), so give each pair up to 7 of the
# earliest service dates before writing it off as inactive.
PROBE_DATE_WINDOW = service_dates[:7]
# Cache-only lookback: if a prior run (or Phase 3+'s per-trip fetches from a
# previous invocation) already cached a query for a day just before the
# reference date, a cache hit there is free evidence the pair is active —
# check it before spending any live request on the forward window.
_ref_dt = datetime.strptime(reference_date, "%Y-%m-%d")
BACKWARD_PEEK_DATES = [(_ref_dt - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(1, 8)]
logger.info(f"Phase 2b: Probing {len(base_pairs)} seed city pairs from {SEARCH_PAIRS_PATH} "
            f"across up to {len(PROBE_DATE_WINDOW)} candidate dates starting {reference_date} "
            f"(plus cache-only lookback to {BACKWARD_PEEK_DATES[-1]})...")

with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
    futures = [executor.submit(probe_pair, f, t, PROBE_DATE_WINDOW, BACKWARD_PEEK_DATES)
               for f, t in base_pairs]
    done = 0
    for future in concurrent.futures.as_completed(futures):
        done += 1
        if done % 1000 == 0:
            logger.info(f"  Pair probe: {done}/{len(base_pairs)} done, "
                        f"{len(active_pairs)} active pairs, {len(discovered_trips)} trips")
    concurrent.futures.wait(futures)

logger.info(f"Phase 2b complete: {len(active_pairs)} active pairs, "
            f"{len(discovered_trips)} trips from the probe date window")

# ── Phase 2b.5: iterative expansion among cities already found active ────────
# The seed pairs only cover routes abhibus happened to list, so a direct route
# between two cities that are each reachable (but whose pairing wasn't in the
# seed file) is invisible to Phase 2b. Reachable cities (those seen in any
# active pair) are almost always a small fraction of all_cities, so probing
# the reachable×reachable grid is far cheaper than the full N² search while
# still catching those seed-blind routes. Repeat until the reachable set
# stops growing (or the round cap is hit) since each round's discoveries can
# reveal new reachable cities for the next round.
for round_num in range(1, args.max_expansion_rounds + 1):
    with active_pairs_lock:
        reachable = {c for pair in active_pairs for c in pair}
    expansion_pairs = [
        (a, b)
        for a in reachable
        for b in reachable
        if a != b and (a, b) not in probed_pairs
    ]
    if not expansion_pairs:
        logger.info(f"Phase 2b.5: no new pairs to probe after round {round_num - 1}; expansion converged")
        break

    logger.info(f"Phase 2b.5 round {round_num}: probing {len(expansion_pairs)} pairs among "
                f"{len(reachable)} reachable cities...")
    probed_pairs.update(expansion_pairs)

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(probe_pair, f, t, PROBE_DATE_WINDOW, BACKWARD_PEEK_DATES)
                   for f, t in expansion_pairs]
        done = 0
        for future in concurrent.futures.as_completed(futures):
            done += 1
            if done % 1000 == 0:
                logger.info(f"  Expansion probe: {done}/{len(expansion_pairs)} done, "
                            f"{len(active_pairs)} active pairs, {len(discovered_trips)} trips")
        concurrent.futures.wait(futures)

    logger.info(f"Phase 2b.5 round {round_num} complete: {len(active_pairs)} active pairs, "
                f"{len(discovered_trips)} trips so far")
else:
    if args.max_expansion_rounds:
        logger.info(f"Phase 2b.5: reached round cap ({args.max_expansion_rounds}) without full convergence")

# ── Phase 2c prep: drop pairs whose probed trips are subsumed ────────────────
# by another pair — e.g. if every BNG→PNA trip found during probing also
# shows up in BNG→HBL (a longer route passing through PNA), continuing to
# re-query BNG→PNA across every remaining date is redundant: BNG→HBL will
# surface those same trip_ids again there, and discovered_trips already
# dedups by trip_id (first-seen-wins), so nothing is lost by not re-finding
# them via BNG→PNA too. A pair with no dominating superset (or that's the
# largest/tie-break survivor among equal-set pairs) is kept as-is.
trip_to_pairs: dict[int, set[tuple[int, int]]] = defaultdict(set)
for pair, trip_ids in pair_trip_ids.items():
    for tid in trip_ids:
        trip_to_pairs[tid].add(pair)


def _is_dominated(pair: tuple[int, int]) -> bool:
    trips = pair_trip_ids[pair]
    if not trips:
        return False
    candidates = None
    for tid in trips:
        holders = trip_to_pairs[tid]
        candidates = set(holders) if candidates is None else (candidates & holders)
        if not candidates:
            return False
    candidates.discard(pair)
    for other in candidates:
        other_trips = pair_trip_ids[other]
        if len(other_trips) > len(trips):
            return True
        if len(other_trips) == len(trips) and other < pair:
            return True
    return False


cover_pairs = [p for p in active_pairs if not _is_dominated(p)]
logger.info(f"Phase 2c prep: {len(active_pairs)} active pairs → {len(cover_pairs)} after dropping "
            f"{len(active_pairs) - len(cover_pairs)} pair(s) subsumed by another pair's trips")

# search-pairs.json is treated as an accumulating record of known-real KSRTC
# pairs (scraped from abhibus, or appended below from a previous run's own
# Phase 2b.5 discoveries) -- so a seed pair that came back empty across the
# whole Phase 2b/2b.5 probe window (e.g. a weekly service whose running day
# fell outside PROBE_DATE_WINDOW) still deserves the full remaining-date
# sweep, rather than being dropped for having shown no service today.
_before_seed_union = len(cover_pairs)
cover_pairs = list(set(cover_pairs) | set(base_pairs))
if len(cover_pairs) > _before_seed_union:
    logger.info(f"Phase 2c prep: added {len(cover_pairs) - _before_seed_union} seed pair(s) from "
                f"{SEARCH_PAIRS_PATH} with no confirmed probe activity to the full date sweep")

# ── Phase 2c: query surviving pairs across remaining service dates ───────────
remaining_dates = service_dates[1:]
if remaining_dates and cover_pairs:
    search_tasks = [(f, t, d) for d in remaining_dates for f, t in cover_pairs]
    logger.info(f"Phase 2c: Querying {len(cover_pairs)} pairs × "
                f"{len(remaining_dates)} remaining dates ({len(search_tasks)} requests)...")

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(search_active_pair, f, t, d) for f, t, d in search_tasks]
        done = 0
        for future in concurrent.futures.as_completed(futures):
            done += 1
            if done % 500 == 0:
                logger.info(f"  Date expansion: {done}/{len(search_tasks)} done, "
                            f"{len(discovered_trips)} unique trips")
        concurrent.futures.wait(futures)

logger.info(f"Phase 2 complete: {len(discovered_trips)} unique trips across {len(service_dates)} dates")

# Feed pairs discovered this run (via Phase 2b.5 expansion) but absent from the
# original seed file back into search-pairs.json, so next run's Phase 2b seeds
# from them directly and -- combined with the unconditional seed union above
# -- they still get the full date-range sweep on a run where they don't show
# up during the probe window.
new_pairs = set(active_pairs) - set(base_pairs)
if new_pairs:
    append_new_pairs_to_seed_file(SEARCH_PAIRS_PATH, new_pairs, all_cities)

# ── Phase 3: fetch stop sequences per trip ────────────────────────────────────

logger.info(f"Phase 3: Fetching stop sequences for {len(discovered_trips)} trips...")

trip_stops: dict[int, dict] = {}  # trip_id → {pickup: [...], dropoff: [...]}
trip_stops_lock = threading.Lock()


def fetch_trip_details(trip_id: int, chart_date: str):
    resp = cached_get(
        f"{BASE_URL}/APIMSTripsSummaryPkpDrp",
        params={"tripId": trip_id, "chartDate": chart_date},
    )
    if not resp:
        return
    try:
        data = resp.json()
        pickups = data.get("PickupTimings", [])
        dropoffs = data.get("DropoffTimings", [])
        if not pickups and not dropoffs:
            return
        with trip_stops_lock:
            trip_stops[trip_id] = {"pickups": pickups, "dropoffs": dropoffs}
    except Exception as e:
        logger.debug(f"Trip detail parse error trip {trip_id}: {e}")


with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
    futures = {
        executor.submit(fetch_trip_details, meta["trip_id"], meta["chart_date"]): meta["trip_id"]
        for meta in discovered_trips.values()
    }
    done = 0
    for future in concurrent.futures.as_completed(futures):
        done += 1
        if done % 100 == 0:
            logger.info(f"  Trip fetch: {done}/{len(futures)} done")
    concurrent.futures.wait(futures)

logger.info(f"Phase 3 complete: {len(trip_stops)} trips with stop data")

# ── Phase 4: load stops GeoJSON, resolve coordinates ─────────────────────────

load_stops_geojson()

logger.info("Phase 4: Resolving stop coordinates...")

# stop_id is now canonical_stop_id(CityID, name) (see canonical_stop_id) — so
# city_id is baked into the id itself whenever the trip-detail API reports a
# CityID, which is the normal case. The only entries that can still have a
# missing city_id *property* are ones built via the name-based ("N…") id
# fallback, when CityID was genuinely absent from the API. Fix that property
# up via a city-name lookup — this never changes the id itself (already
# fixed at creation), and never touches manual entries' lat/lon/stop_name/
# city_name/source.
city_name_to_id = {name.strip().lower(): cid for cid, name in all_cities.items()}
name_backfilled = 0
for stop_id, info in stops_db.items():
    if info.get("city_id") is not None:
        continue
    cid = city_name_to_id.get((info.get("city_name") or "").strip().lower())
    if cid is not None:
        info["city_id"] = cid
        city_coords_cache.setdefault(cid, (info["lat"], info["lon"]))
        name_backfilled += 1
if name_backfilled:
    stops_db_dirty = True
    logger.info(f"  Backfilled city_id for {name_backfilled} stops via city-name lookup")

still_missing = sum(1 for info in stops_db.values() if info.get("city_id") is None)
if still_missing:
    logger.warning(f"  {still_missing} stops still have no city_id after backfill "
                    f"(city_name didn't match any known KSRTC city — may need manual fix)")

# Collect all unique stop IDs that need coordinates. Any stop_id already in
# stops_db — manual, estimate, or default — is trusted as-is and skipped, so
# Overpass is only ever queried for stop_ids seen for the first time. Two raw
# KSRTC ids sharing (city_id, name) compute the same canonical stop_id, so
# they collapse into one entry here for free.
stops_to_resolve: dict[str, tuple[str, str, int | None]] = {}  # stop_id → (stop_name, city_name, city_id)
for trip_id, stops in trip_stops.items():
    for p in stops["pickups"]:
        cid = p.get("CityID")
        sid = canonical_stop_id(cid, p["PickupName"], p["CityName"])
        if sid not in stops_db:
            stops_to_resolve[sid] = (p["PickupName"], p["CityName"], cid)
    for d in stops["dropoffs"]:
        cid = d.get("CityID")
        sid = canonical_stop_id(cid, d["DropoffName"], d["CityName"])
        if sid not in stops_db:
            stops_to_resolve[sid] = (d["DropoffName"], d["CityName"], cid)

logger.info(f"  {len(stops_to_resolve)} stops need geocoding (Overpass, may be slow)")

# Geocode sequentially to respect Nominatim's 1-req/sec policy
for i, (stop_id, (stop_name, city_name, city_id)) in enumerate(stops_to_resolve.items(), 1):
    resolve_stop(stop_id, stop_name, city_name, city_id)
    if i % 20 == 0:
        logger.info(f"  Geocoded {i}/{len(stops_to_resolve)} stops")

recalibrate_coords()

if stops_db_dirty:
    save_stops_geojson()

logger.info("Phase 4 complete")

# ── Phase 4.5: derive per-trip service calendars ──────────────────────────────
# For each (trip, service-type split), decide whether its observed active dates
# reduce to a clean weekly weekday pattern, or are irregular. A weekday is
# "clean" for a trip only if every service_date on that weekday is uniformly
# active or uniformly inactive — any mix means the horizon's evidence doesn't
# factor into a simple weekly rule, so we fall back to explicit dates instead
# of guessing. Trips sharing an identical pattern (the common case — e.g. an
# everyday service) share one service_id instead of getting a row each.

start_date = service_dates[0].replace("-", "")
end_date = service_dates[-1].replace("-", "")

service_dates_dt = [datetime.strptime(d, "%Y-%m-%d").date() for d in service_dates]

# If discovery started today, a trip missing from just today's probe can be a
# same-day booking-chart timing artifact (the chart may not be open yet, or
# may already be past a per-day cutoff by the time this run queried it) --
# not evidence the service doesn't actually run today. So when judging
# regularity/full-coverage below, drop today from the horizon dates a service
# is required to cover; a service matching on every *other* day still gets
# treated as covering today too (the emitted calendar.txt start_date is the
# unmodified global start_date either way, so this never shortens a service's
# declared range -- it only widens which observed patterns qualify for
# calendar.txt over calendar_dates.txt).
_today = datetime.now().date()
_regularity_horizon = (
    [d for d in service_dates_dt if d != _today]
    if service_dates_dt and service_dates_dt[0] == _today
    else service_dates_dt
)

dates_by_weekday: dict[int, list] = defaultdict(list)
for d in _regularity_horizon:
    dates_by_weekday[d.weekday()].append(d)  # Monday=0 … Sunday=6, matches GTFS calendar.txt column order

weekday_service_ids: dict[tuple[int, ...], str] = {}
calendar_rows = [["service_id", "monday", "tuesday", "wednesday", "thursday", "friday",
                   "saturday", "sunday", "start_date", "end_date"]]

irregular_service_ids: dict[frozenset, str] = {}
calendar_dates_rows = [["service_id", "date", "exception_type"]]

# Keyed by the service's own (start_date, end_date) -- distinct from
# weekday_service_ids, which always uses the *global* start_date/end_date.
# Covers a service that runs every single day from some point through the end
# of the discovered horizon, but wasn't active for the horizon's earlier days
# (e.g. a schedule that only launched a few days into the discovery window) --
# still fully regular (no gaps once it starts), just not aligned to
# start_date, so the weekday-uniformity check below would otherwise reject it
# as irregular and force it into calendar_dates.txt for no good reason.
full_coverage_service_ids: dict[tuple[str, str], str] = {}


def get_service_id(date_strs: set[str]) -> str:
    """Return a GTFS service_id for a set of active-date strings, creating a
    shared calendar.txt row for a clean weekday pattern or a full-coverage
    date range, or a shared calendar_dates.txt entry set for an irregular one
    — whichever the observed dates actually support."""
    active_dates = set()
    for ds in date_strs:
        try:
            active_dates.add(datetime.strptime(ds, "%Y-%m-%d").date())
        except ValueError:
            continue
    if not active_dates:
        active_dates = {service_dates_dt[0]}

    weekday_flags = [0] * 7
    is_regular = True
    for wd in range(7):
        wd_dates = dates_by_weekday.get(wd, [])
        if not wd_dates:
            continue
        active_flags = [d in active_dates for d in wd_dates]
        if all(active_flags):
            weekday_flags[wd] = 1
        elif not any(active_flags):
            weekday_flags[wd] = 0
        else:
            is_regular = False
            break

    if is_regular:
        key = tuple(weekday_flags)
        service_id = weekday_service_ids.get(key)
        if service_id is None:
            service_id = f"WD_{''.join(str(f) for f in weekday_flags)}"
            weekday_service_ids[key] = service_id
            calendar_rows.append([service_id, *weekday_flags, start_date, end_date])
        return service_id

    # Not a clean weekly pattern -- but still collapsible into calendar.txt if
    # it's active on literally every remaining day of the horizon starting
    # from its own first active date (a gap-free run through to the end,
    # e.g. dates_by_weekday would reject this above only because the horizon's
    # *earlier* days -- before this service started -- are inactive).
    sorted_active = sorted(active_dates)
    svc_start = None
    if sorted_active and sorted_active[-1] == service_dates_dt[-1]:
        # Same today tolerance as above: a service active on every horizon day
        # except (at most) today still counts as covering the full range,
        # assumed to include today too -- so its calendar.txt entry keeps the
        # unmodified global start_date rather than getting shifted to tomorrow.
        if [d for d in sorted_active if d != _today] == _regularity_horizon:
            svc_start = start_date
        else:
            expected = [d for d in _regularity_horizon if d >= sorted_active[0]]
            if sorted_active == expected:
                svc_start = sorted_active[0].strftime("%Y%m%d")

    if svc_start is not None:
        key = (svc_start, end_date)
        service_id = full_coverage_service_ids.get(key)
        if service_id is None:
            service_id = f"FULL_{svc_start}"
            full_coverage_service_ids[key] = service_id
            calendar_rows.append([service_id, 1, 1, 1, 1, 1, 1, 1, svc_start, end_date])
        return service_id

    key = frozenset(active_dates)
    service_id = irregular_service_ids.get(key)
    if service_id is None:
        digest = hashlib.sha1("|".join(sorted(d.isoformat() for d in active_dates)).encode()).hexdigest()[:10]
        service_id = f"IRR_{digest}"
        irregular_service_ids[key] = service_id
        for d in sorted(active_dates):
            calendar_dates_rows.append([service_id, d.strftime("%Y%m%d"), 1])
    return service_id


# ── Phase 5: build GTFS ───────────────────────────────────────────────────────

logger.info("Phase 5: Building GTFS tables...")

trips_rows = [["route_id", "service_id", "trip_id", "trip_headsign", "direction_id"]]
stop_times_rows = [["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence", "timepoint"]]
stops_rows = [["stop_id", "stop_name", "stop_lat", "stop_lon"]]
routes_rows = [["route_id", "agency_id", "route_short_name", "route_long_name", "route_type"]]

seen_stop_ids: set[str] = set()
seen_route_ids: set[str] = set()

base_date = datetime.strptime(service_dates[0], "%Y-%m-%d").date()

trips_skipped = 0

# TripCode is "<4-digit departure time><origin-destination code>", e.g.
# "2131PMRHBL" → od_code "PMRHBL". This od_code is the route's own identity
# (see canonical_stop_id-style stability across probes): a trip surfaced via
# an intermediate-city probe (e.g. Davanagere→Hubballi turning up a
# Bengaluru→Panaji service) still groups under its own route's od_code, not
# the queried city pair.
_OD_CODE_RE = re.compile(r'^\d+')


def od_code_from_trip_code(trip_code: str) -> str:
    return _OD_CODE_RE.sub('', trip_code) or trip_code

for trip_id, stops in trip_stops.items():
    meta = discovered_trips.get(trip_id)
    if not meta:
        continue

    pickups = stops["pickups"]
    dropoffs = stops["dropoffs"]

    # Build unified stop list with (datetime, stop_id, stop_name, city_name, city_id)
    stop_events: list[tuple[datetime, str, str, str, int | None]] = []

    for p in pickups:
        try:
            dt = parse_api_dt(p["PickupTime"])
            sid = canonical_stop_id(p.get("CityID"), p["PickupName"], p["CityName"])
            stop_events.append((dt, sid, p["PickupName"], p["CityName"], p.get("CityID")))
        except Exception:
            pass

    for d in dropoffs:
        try:
            dt = parse_api_dt(d["DropoffTime"])
            sid = canonical_stop_id(d.get("CityID"), d["DropoffName"], d["CityName"])
            stop_events.append((dt, sid, d["DropoffName"], d["CityName"], d.get("CityID")))
        except Exception:
            pass

    if len(stop_events) < 2:
        trips_skipped += 1
        continue

    # Sort by time; handle midnight crossings by detecting backward jumps
    stop_events.sort(key=lambda x: x[0])

    # Resolve departure date: assume the first stop's datetime is anchored to base_date.
    # If first stop is in the future relative to base_date, that's fine.
    # Adjust post-midnight stops: if a stop's time is before the previous stop's time,
    # it crossed midnight — add 1 day.
    adjusted: list[tuple[datetime, str, str, str, int | None]] = []
    prev_dt = None
    day_offset = timedelta(0)
    for dt, sid, sname, cname, cid in stop_events:
        adj_dt = dt + day_offset
        if prev_dt and adj_dt < prev_dt:
            day_offset += timedelta(days=1)
            adj_dt = dt + day_offset
        adjusted.append((adj_dt, sid, sname, cname, cid))
        prev_dt = adj_dt

    # Anchor to base_date: find the date of the first event
    first_dt = adjusted[0][0]
    anchor_date = first_dt.date()

    trip_code = meta["trip_code"] or str(trip_id)
    od_code = od_code_from_trip_code(trip_code)
    # Stop-level names (e.g. "Kempegowda Intl Airport Terminal-1") for the
    # rider-facing headsign; city-level names (e.g. "Kempegowda Intl Airport" —
    # shared by both airport terminals) for route_long_name, so a route's
    # long name doesn't leak terminal/stop-specific detail and reverse-direction
    # counterparts (e.g. BIALMYS_FLYBUS / MYSBIAL_FLYBUS) resolve to the same
    # pair of place names for merging in gtfs_compat.py.
    origin_stop_name = adjusted[0][2].title()
    destination_stop_name = adjusted[-1][2].title()
    origin_city = adjusted[0][3].title()
    destination_city = adjusted[-1][3].title()
    origin_code = city_codes.get(adjusted[0][4])
    destination_code = city_codes.get(adjusted[-1][4])
    trip_headsign = destination_stop_name

    # Normally one split (the common case): KSRTC reported the same ServiceType
    # for this trip_id on every date it was observed. If it genuinely reported
    # different service slugs on different dates (see trip_service_dates), each
    # slug becomes its own GTFS trip/route sharing the same stop sequence, with
    # a disambiguated trip_id.
    #
    # Splits are grouped by resolved slug, not raw ServiceType: two ServiceType
    # strings that map to the same slug (e.g. "NON AC SLEEPER" and "PALLAKKI
    # (NON AC SLEEPER)" both -> PALLAKKI) are the same product, just
    # inconsistently branded by KSRTC across chart dates -- treating them as
    # separate splits produced two rows with the identical route_id and
    # disambiguated trip_id (same slug -> same "{trip_code}_{slug}"), differing
    # only in which subset of dates got service_id'd, i.e. duplicate trip_ids.
    raw_splits = trip_service_dates.get(trip_id) or {meta["service_type"]: {meta["chart_date"]}}
    slug_splits: dict[str, set[str]] = defaultdict(set)
    slug_labels: dict[str, str] = {}
    for service_type, date_strs in raw_splits.items():
        slug, label = service_slug_and_label(service_type)
        slug_splits[slug] |= date_strs
        slug_labels[slug] = label
    multi_split = len(slug_splits) > 1

    for slug, date_strs in slug_splits.items():
        label = slug_labels[slug]
        route_id = f"{od_code}_{slug}"
        trip_out_id = trip_code if not multi_split else f"{trip_code}_{slug}"

        service_id = get_service_id(date_strs)
        trips_rows.append([route_id, service_id, trip_out_id, trip_headsign, 0])

        if route_id not in seen_route_ids:
            seen_route_ids.add(route_id)
            long_name = f"{label}: {origin_city} to {destination_city}"
            # Built from this trip's actual first/last stop cities (same source as
            # long_name above), not KSRTC's raw RouteName -- that field names the
            # bus's overall nominal route (e.g. a Bengaluru->Mumbai through-service
            # surfaced via a Bengaluru->Pune search reports RouteName "BNG-MUM-BRA"),
            # which can disagree with this route's real origin/destination and make
            # the route unfindable by riders searching for the actual city pair.
            # Prefer KSRTC's own city codes (e.g. "BNG"/"PNA", the same ones baked
            # into od_code from the trip_code) so the short name matches how riders
            # already know these routes; fall back to full city names if a code is
            # missing from the static city list.
            if origin_code and destination_code:
                route_short_name = f"{origin_code}-{destination_code} {label}"
            else:
                route_short_name = f"{origin_city}-{destination_city} {label}"
            routes_rows.append([route_id, "KSRTC", route_short_name, long_name, 3])

        for seq, (adj_dt, sid, sname, cname, cid) in enumerate(adjusted, 1):
            gtfs_time = dt_to_gtfs(adj_dt, anchor_date)
            timepoint = 1 if seq == 1 or seq == len(adjusted) else 0
            stop_times_rows.append([trip_out_id, gtfs_time, gtfs_time, str(sid), seq, timepoint])

            if sid not in seen_stop_ids:
                seen_stop_ids.add(sid)
                info = stops_db.get(sid)
                if info:
                    stops_rows.append([str(sid), sname.title(), info["lat"], info["lon"]])

logger.info(f"Phase 5 complete: {len(trips_rows)-1} trips, {len(routes_rows)-1} routes, "
            f"{len(stops_rows)-1} stops, {len(stop_times_rows)-1} stop times "
            f"({trips_skipped} trips skipped — fewer than 2 stops)")

# ── Write GTFS files ──────────────────────────────────────────────────────────

logger.info("Writing GTFS files...")

def write_csv(rows: list[list], path: str, dedup_subset: list[str] | None = None):
    df = pd.DataFrame(rows[1:], columns=rows[0]).drop_duplicates()
    if dedup_subset:
        before = len(df)
        df = df.drop_duplicates(subset=dedup_subset, keep="first")
        if len(df) < before:
            logger.warning(f"!! Dropped {before - len(df)} rows with duplicate {dedup_subset} while writing {path}")
    df.to_csv(path, index=False)

write_csv(routes_rows, f"{OUTPUT_DIR}/routes.txt")
write_csv(trips_rows, f"{OUTPUT_DIR}/trips.txt", dedup_subset=["trip_id"])
write_csv(stop_times_rows, f"{OUTPUT_DIR}/stop_times.txt")
write_csv(stops_rows, f"{OUTPUT_DIR}/stops.txt")
write_csv(calendar_rows, f"{OUTPUT_DIR}/calendar.txt")
write_csv(calendar_dates_rows, f"{OUTPUT_DIR}/calendar_dates.txt")

logger.info(f"  {len(calendar_rows)-1} weekday-pattern services, "
            f"{len(irregular_service_ids)} irregular services "
            f"({len(calendar_dates_rows)-1} calendar_dates rows)")

with open(f"{OUTPUT_DIR}/agency.txt", "w") as f:
    f.write(
        "agency_id,agency_name,agency_url,agency_timezone,agency_phone\n"
        "KSRTC,Karnataka State Road Transport Corporation,https://www.ksrtc.in,Asia/Kolkata,+91 8022483700\n"
    )

with open(f"{OUTPUT_DIR}/feed_info.txt", "w") as f:
    f.write(
        "feed_publisher_name,feed_publisher_url,feed_lang,feed_start_date,feed_end_date,feed_contact_url\n"
        f"BLRTransit,https://blrtransit.com,en,{start_date},{end_date},https://blrtransit.com\n"
    )

# Shape generation via pfaedle now lives in gtfs_compat.py, run as a
# post-processing step against gtfs.zip (see --skip-shapes there).

# ── Zip ───────────────────────────────────────────────────────────────────────

logger.info("Compressing GTFS archive...")
gtfs_zip_path = "gtfs.zip"
with zipfile.ZipFile(gtfs_zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
    for fname in os.listdir(OUTPUT_DIR):
        if fname == ".DS_Store":
            continue
        zipf.write(os.path.join(OUTPUT_DIR, fname), arcname=fname)

uncompressed = sum(os.path.getsize(os.path.join(OUTPUT_DIR, f)) for f in os.listdir(OUTPUT_DIR))
compressed = os.path.getsize(gtfs_zip_path)
ratio = (1 - compressed / uncompressed) * 100 if uncompressed else 0

logger.info(f"GTFS written to {gtfs_zip_path}")
logger.info(f"Uncompressed: {uncompressed/1024:.1f} KB | Compressed: {compressed/1024:.1f} KB | Ratio: {ratio:.1f}%")