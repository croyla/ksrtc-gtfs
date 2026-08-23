"""Delete stale tmp/api_cache entries (KSRTC API responses).

cached_get() (gtfs_parallel.py) already carries a 30-day logical TTL via each
entry's own `expires_at` field, but an expired entry is only ever *ignored*
at read time -- it's never deleted from disk, so the directory grows without
bound over repeated runs. This physically removes files whose mtime is older
than a threshold (default 7 days -- intentionally stricter than, and
independent of, the 30-day logical TTL, purely to bound disk usage).

Runs automatically at the start of every gtfs_parallel.py invocation (see
cleanup_stale_cache() call there), and can also be run standalone (e.g. via
cron) for out-of-band housekeeping:

    python cache_cleanup.py
    python cache_cleanup.py --max-age-days 3 --cache-dir tmp/api_cache
"""
from datetime import datetime
import argparse
import logging
import os

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = os.path.join("tmp", "api_cache")
DEFAULT_MAX_AGE_DAYS = 7


def cleanup_stale_cache(cache_dir: str = DEFAULT_CACHE_DIR, max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> int:
    """Delete files in cache_dir whose mtime is older than max_age_days.
    Returns the number of files removed.

    Uses scandir() rather than listdir()+getmtime(): cache_dir can hold
    millions of entries (one per distinct cached request), and scandir's
    DirEntry.stat() is served from the same directory read on most platforms
    instead of costing a second syscall per file."""
    if not os.path.isdir(cache_dir):
        return 0
    cutoff = datetime.now().timestamp() - max_age_days * 86400
    removed = 0
    with os.scandir(cache_dir) as it:
        for entry in it:
            try:
                if entry.is_file(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_mtime < cutoff:
                    os.remove(entry.path)
                    removed += 1
            except OSError as e:
                logger.warning(f"Failed to remove stale cache file {entry.path}: {e}")
    return removed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Delete stale KSRTC API cache entries older than N days.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--cache-dir", type=str, default=DEFAULT_CACHE_DIR,
                         help=f"Cache directory to clean (default: {DEFAULT_CACHE_DIR})")
    parser.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS,
                         help=f"Delete files older than this many days (default: {DEFAULT_MAX_AGE_DAYS})")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    n = cleanup_stale_cache(args.cache_dir, args.max_age_days)
    logger.info(f"Removed {n} stale cache file(s) from {args.cache_dir} (older than {args.max_age_days} days)")
