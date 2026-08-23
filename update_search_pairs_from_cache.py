"""Scan cached KSRTC searchRoutesV4 responses (tmp/api_cache/) for city pairs
that returned real trips, and append any not already in search-pairs.json.

Each cache entry stores the raw response body under "data", and a
searchRoutesV4 response is a list of trip dicts carrying FromCityID/ToCityID
directly -- so active pairs can be recovered from the cached response bodies
alone, without needing the original request params (which aren't stored in
the cache file; see _cache_key in gtfs_parallel.py).

This complements the pair-discovery gtfs_parallel.py already does inline
during a run (see append_new_pairs_to_seed_file there, which only catches
pairs surfaced via Phase 2b.5 expansion): running this script separately
also picks up pairs that only ever showed up incidentally -- e.g. via a
different pair's cached response, a manual/ad-hoc query, or a stale cache
predating that inline logic -- without needing a live KSRTC probe.

Usage:
  python update_search_pairs_from_cache.py
  python update_search_pairs_from_cache.py --dry-run
  python update_search_pairs_from_cache.py --cache-dir tmp/api_cache --search-pairs search-pairs.json
"""
import argparse
import json
import logging
import os
import time

parser = argparse.ArgumentParser(
    description=__doc__,
    formatter_class=argparse.RawDescriptionHelpFormatter,
)
parser.add_argument('--cache-dir', type=str, default=os.path.join('tmp', 'api_cache'),
                     help='Directory of cached API responses (default: tmp/api_cache)')
parser.add_argument('--search-pairs', type=str, default='search-pairs.json',
                     help='search-pairs.json to read from and append to (default: search-pairs.json)')
parser.add_argument('--include-expired', action='store_true',
                     help='Also consider cache entries past their TTL (default: only fresh entries, '
                          'since an expired entry is no longer known-current evidence of service)')
parser.add_argument('--dry-run', action='store_true',
                     help='Report what would be added without writing search-pairs.json')
parser.add_argument('--log-level', type=str, default='INFO',
                     choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'])
args = parser.parse_args()

logging.basicConfig(level=getattr(logging, args.log_level),
                     format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger()


def scan_cache(cache_dir: str, include_expired: bool) -> tuple[set[tuple[int, int]], dict[int, str]]:
    """Single pass over every cache file, returning:
      - every (from_city_id, to_city_id) pair with at least one cached
        searchRoutesV4 response containing real trips
      - a best-effort city_id -> name map, scraped from a cached getStaticCityList
        response if one is present (its "data" is {key: {"ID": ..., "Name": ...}, ...},
        unlike the trip-list responses this otherwise scans)
    A single pass matters here: the cache directory can hold well over a million
    small files, so opening+parsing it twice (once per lookup) roughly doubles an
    already slow scan for no benefit -- both are cheap to accumulate together."""
    pairs: set[tuple[int, int]] = set()
    cities: dict[int, str] = {}
    now = time.time()
    logger.info(f"Scanning {cache_dir} ...")
    scanned = 0
    with os.scandir(cache_dir) as it:
        for dirent in it:
            if not dirent.name.endswith(".json"):
                continue
            scanned += 1
            if scanned % 100_000 == 0:
                logger.info(f"  ...{scanned} file(s) scanned, {len(pairs)} pair(s), "
                            f"{'have' if cities else 'no'} city names yet")
            try:
                with open(dirent.path) as f:
                    entry = json.load(f)
            except Exception:
                continue
            if not include_expired and entry.get("expires_at", 0) < now:
                continue
            data = entry.get("data")
            if isinstance(data, dict):
                if not cities:
                    # getStaticCityList's cached body is the raw API response,
                    # {"success": ..., "data": {"0": {"ID":.., "Name":..}, ...}} --
                    # not the city map itself, so unwrap one level before checking.
                    candidate = data.get("data") if isinstance(data.get("data"), dict) else data
                    if candidate and all(
                        isinstance(v, dict) and "ID" in v and "Name" in v for v in list(candidate.values())[:5]
                    ):
                        cities = {v["ID"]: v["Name"] for v in candidate.values()}
                continue
            if not isinstance(data, list):
                continue
            for trip in data:
                if not isinstance(trip, dict):
                    continue
                from_id, to_id = trip.get("FromCityID"), trip.get("ToCityID")
                if from_id is not None and to_id is not None and from_id != to_id:
                    pairs.add((from_id, to_id))
    logger.info(f"Scanned {scanned} cache file(s)")
    return pairs, cities


def main():
    active_pairs, cities = scan_cache(args.cache_dir, args.include_expired)
    if cities:
        logger.info(f"Found {len(cities)} city names from a cached getStaticCityList response")
    else:
        logger.warning("No cached city list found; new pairs will use numeric IDs as names")
    logger.info(f"Found {len(active_pairs)} distinct active (from,to) pair(s) in the cache")

    try:
        with open(args.search_pairs) as f:
            search_data = json.load(f)
    except FileNotFoundError:
        logger.info(f"{args.search_pairs} not found; starting a new one")
        search_data = {"source": "cache-scan", "scraped_pairs_total": 0,
                        "matched_count": 0, "pairs": [], "unmatched": []}
    except json.JSONDecodeError as e:
        logger.error(f"Could not parse {args.search_pairs}: {e}")
        return

    existing = {(e["from_city_id"], e["to_city_id"]) for e in search_data.get("pairs", [])}
    to_add = sorted(p for p in active_pairs if p not in existing)

    if not to_add:
        logger.info(f"No new pairs to add; {args.search_pairs} already covers everything in the cache")
        return

    logger.info(f"{len(to_add)} new pair(s) found in the cache but not in {args.search_pairs}")
    for from_id, to_id in to_add:
        name_from = cities.get(from_id, str(from_id))
        name_to = cities.get(to_id, str(to_id))
        search_data.setdefault("pairs", []).append({
            "from_city_id": from_id,
            "to_city_id": to_id,
            "from_name": name_from,
            "to_name": name_to,
            "from_matched_name": name_from,
            "to_matched_name": name_to,
            "from_match_method": "cache-scan",
            "to_match_method": "cache-scan",
        })
        existing.add((from_id, to_id))

    search_data["matched_count"] = len(search_data.get("pairs", []))

    if args.dry_run:
        logger.info(f"Dry run: would append {len(to_add)} pair(s) to {args.search_pairs} (not writing)")
        return

    with open(args.search_pairs, "w") as f:
        json.dump(search_data, f, indent=2)
    logger.info(f"Appended {len(to_add)} pair(s) to {args.search_pairs}")


if __name__ == "__main__":
    main()