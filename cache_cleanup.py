"""Delete stale rows from tmp/api_cache.db -- both the KSRTC API response
cache and the Phase 2b.5 dead-pairs table.

`cache` (KSRTC API responses): cached_get() (gtfs_parallel.py) already
carries a 30-day logical TTL via each entry's own `expires_at` column, but
an expired row is only ever *ignored* at read time -- it's never deleted, so
the db grows without bound over repeated runs (one row per distinct
(url, params, date), and date keeps advancing). This physically removes
rows whose `created_at` is older than a threshold (default 7 days --
intentionally stricter than, and independent of, the 30-day logical TTL,
purely to bound disk usage).

The cache used to be one JSON file per request under tmp/api_cache/ --
at ~1.8M files (7.2GB, mostly filesystem block overhead) that became slow
to even list. It's now a single SQLite db (tmp/api_cache.db); see
migrate_api_cache.py for the one-time migration of the old file-per-entry
cache into this db.

`dead_pairs` (Phase 2b.5 confirmed-inactive city pairs): unlike `cache`,
this table doesn't grow unboundedly -- it's INSERT OR REPLACE'd per
(from_id, to_id), capped at the size of the reachable×reachable grid, and
already self-corrects at read time (_is_dead_pair in gtfs_parallel.py
treats a row past DEAD_PAIR_TTL as not-dead, letting it get re-probed). So
pruning it isn't needed to bound disk usage -- this just tidies up rows
that are logically expired anyway, using the same TTL as the app-level
check rather than the stricter 7-day default used for `cache`.

Runs automatically at the start of every gtfs_parallel.py invocation (see
cleanup_stale_cache() / cleanup_stale_dead_pairs() calls there), and can
also be run standalone (e.g. via cron) for out-of-band housekeeping:

    python cache_cleanup.py
    python cache_cleanup.py --max-age-days 3 --cache-db tmp/api_cache.db
    python cache_cleanup.py --vacuum
"""
from datetime import datetime
import argparse
import logging
import os
import sqlite3

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DB = os.path.join("tmp", "api_cache.db")
DEFAULT_MAX_AGE_DAYS = 7
DEFAULT_DEAD_PAIR_MAX_AGE_DAYS = 30  # matches DEAD_PAIR_TTL in gtfs_parallel.py


def cleanup_stale_cache(cache_db: str = DEFAULT_CACHE_DB, max_age_days: int = DEFAULT_MAX_AGE_DAYS,
                         vacuum: bool = False) -> int:
    """Delete rows in the `cache` table whose created_at is older than max_age_days.
    Returns the number of rows removed.

    vacuum=True reclaims the freed space on disk immediately (VACUUM rewrites
    the whole db file, so it's skipped by default on the per-run call in
    gtfs_parallel.py -- it'd be needless I/O on every invocation. Pass
    --vacuum when running this standalone for periodic housekeeping."""
    if not os.path.isfile(cache_db):
        return 0
    cutoff = datetime.now().timestamp() - max_age_days * 86400
    conn = sqlite3.connect(cache_db)
    try:
        cur = conn.execute("DELETE FROM cache WHERE created_at < ?", (cutoff,))
        removed = cur.rowcount
        conn.commit()
        if vacuum:
            conn.execute("VACUUM")
    finally:
        conn.close()
    return removed


def cleanup_stale_dead_pairs(cache_db: str = DEFAULT_CACHE_DB,
                              max_age_days: int = DEFAULT_DEAD_PAIR_MAX_AGE_DAYS,
                              vacuum: bool = False) -> int:
    """Delete rows in the `dead_pairs` table whose confirmed_at is older than
    max_age_days. Returns the number of rows removed. A no-op (returns 0) if
    the db or table doesn't exist yet -- e.g. an older db predating this
    feature, or one that's never had gtfs_parallel.py populate it."""
    if not os.path.isfile(cache_db):
        return 0
    cutoff = datetime.now().timestamp() - max_age_days * 86400
    conn = sqlite3.connect(cache_db)
    try:
        try:
            cur = conn.execute("DELETE FROM dead_pairs WHERE confirmed_at < ?", (cutoff,))
        except sqlite3.OperationalError:
            return 0
        removed = cur.rowcount
        conn.commit()
        if vacuum:
            conn.execute("VACUUM")
    finally:
        conn.close()
    return removed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Delete stale KSRTC API cache and dead-pair rows.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--cache-db", type=str, default=DEFAULT_CACHE_DB,
                         help=f"Cache db to clean (default: {DEFAULT_CACHE_DB})")
    parser.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS,
                         help=f"Delete `cache` rows older than this many days (default: {DEFAULT_MAX_AGE_DAYS})")
    parser.add_argument("--dead-pair-max-age-days", type=int, default=DEFAULT_DEAD_PAIR_MAX_AGE_DAYS,
                         help=f"Delete `dead_pairs` rows older than this many days "
                              f"(default: {DEFAULT_DEAD_PAIR_MAX_AGE_DAYS})")
    parser.add_argument("--vacuum", action="store_true",
                         help="Reclaim freed disk space immediately after deleting (rewrites the db file)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    n_cache = cleanup_stale_cache(args.cache_db, args.max_age_days, vacuum=args.vacuum)
    logger.info(f"Removed {n_cache} stale cache row(s) from {args.cache_db} (older than {args.max_age_days} days)")
    n_dead = cleanup_stale_dead_pairs(args.cache_db, args.dead_pair_max_age_days, vacuum=args.vacuum)
    logger.info(f"Removed {n_dead} stale dead_pairs row(s) from {args.cache_db} "
                f"(older than {args.dead_pair_max_age_days} days)")
