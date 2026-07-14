"""Scrape KSRTC route pairs listed on abhibus.com and match them to KSRTC city IDs.

abhibus.com publishes a paginated index of "City A to City B" route links for
KSRTC Karnataka. That list is a useful seed of *known-real* routes, including
non-hub-to-non-hub pairs the hub-probing method in gtfs_parallel.py can miss.
This script scrapes it, resolves each plain-text city name to a KSRTC city ID
(via api getStaticCityList), and writes the resolved (and unresolved) pairs to
search-pairs.json for use as an additional candidate-pair seed.
"""

import argparse
import difflib
import json
import re
import time

import requests

LISTING_URL = "https://www.abhibus.com/ksrtc-karnataka-bus-routes"
CITY_LIST_URL = "https://ksrtcapi.iamgds.com/api/resource/getStaticCityList"
HEADERS = {
    "Accept": "text/html,application/xhtml+xml",
    "User-Agent": "Mozilla/5.0 (compatible; ksrtc-gtfs-gen/1.0; +route-pair-scraper)",
}

ROUTE_LINK_RE = re.compile(
    r'<a[^>]*href="(https://www\.abhibus\.com/bus-tickets/ksrtc-karnataka-[^"]+-bus-booking)"'
    r'[^>]*>\s*KSRTC\s+(.*?)\s+to\s+(.*?)\s*</a>',
    re.IGNORECASE,
)
PAGE_OFFSET_RE = re.compile(r'ksrtc-karnataka-bus-routes/(\d+)"')

# Common old-name/official-name variants for Karnataka cities. abhibus mixes
# both forms (sometimes on the same page), while the KSRTC API generally uses
# the official renamed form. Matching is tried both ways.
NAME_ALIASES = {
    "bangalore": "bengaluru",
    "mysore": "mysuru",
    "belgaum": "belagavi",
    "mangalore": "mangaluru",
    "shimoga": "shivamogga",
    "hubli": "hubballi",
    "gulbarga": "kalaburagi",
    "bijapur": "vijayapura",
    "bellary": "ballari",
    "tumkur": "tumakuru",
    "chikmagalur": "chikkamagaluru",
    "hospet": "hosapete",
    "davangere": "davanagere",
    "chikballapur": "chikkaballapur",
}


def fetch(url: str, retries: int = 3) -> str:
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            return resp.text
        except requests.exceptions.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))
    return ""


def scrape_route_pairs(delay: float) -> list[tuple[str, str]]:
    """Return deduped (from_name, to_name) pairs scraped from the abhibus listing."""
    first_page = fetch(LISTING_URL)
    seen_offsets = [int(n) for n in PAGE_OFFSET_RE.findall(first_page)]
    max_offset = max(seen_offsets) if seen_offsets else 0
    offsets = list(range(0, max_offset + 90, 90))  # nav widget only shows neighbors + "Last"

    pairs: set[tuple[str, str]] = set()
    for i, offset in enumerate(offsets):
        page_url = LISTING_URL if offset == 0 else f"{LISTING_URL}/{offset}"
        html = first_page if offset == 0 else fetch(page_url)
        found = ROUTE_LINK_RE.findall(html)
        for _link, from_name, to_name in found:
            pairs.add((_clean_name(from_name), _clean_name(to_name)))
        print(f"  page offset {offset}: {len(found)} routes ({len(pairs)} unique pairs so far)")
        if i < len(offsets) - 1:
            time.sleep(delay)

    return sorted(pairs)


def _clean_name(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip()


def _normalize(name: str) -> str:
    name = name.lower()
    name = re.sub(r"\([^)]*\)", "", name)  # drop parenthetical disambiguators
    name = re.sub(r"[^a-z0-9 ]", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def _aliased(normalized: str) -> str:
    words = normalized.split(" ")
    return " ".join(NAME_ALIASES.get(w, w) for w in words)


def fetch_city_index() -> dict[str, list[tuple[int, str]]]:
    """normalized name -> [(city_id, original_name), ...]"""
    resp = requests.get(CITY_LIST_URL, headers={"Accept": "*/*", "User-Agent": "ksrtc-gtfs-gen/1.0"}, timeout=15)
    resp.raise_for_status()
    data = resp.json().get("data", {})

    index: dict[str, list[tuple[int, str]]] = {}
    for entry in data.values():
        norm = _normalize(entry["Name"])
        index.setdefault(norm, []).append((entry["ID"], entry["Name"]))
    return index


def match_city(name: str, index: dict[str, list[tuple[int, str]]]) -> tuple[int | None, str | None, str]:
    """Resolve a scraped plain-text city name to a KSRTC city ID.

    Returns (city_id, matched_name, method) where method is one of
    "exact", "alias", "fuzzy", or "none".
    """
    norm = _normalize(name)

    candidates = index.get(norm)
    if candidates:
        return candidates[0][0], candidates[0][1], "exact"

    aliased = _aliased(norm)
    candidates = index.get(aliased)
    if candidates:
        return candidates[0][0], candidates[0][1], "alias"

    close = difflib.get_close_matches(aliased, index.keys(), n=1, cutoff=0.84)
    if close:
        cid, orig = index[close[0]][0]
        return cid, orig, "fuzzy"

    return None, None, "none"


def main():
    parser = argparse.ArgumentParser(description="Scrape abhibus.com KSRTC route pairs into search-pairs.json")
    parser.add_argument("--output", type=str, default="search-pairs.json")
    parser.add_argument("--delay", type=float, default=0.5, help="Seconds to sleep between listing-page fetches")
    args = parser.parse_args()

    print("Scraping abhibus.com KSRTC route listing...")
    raw_pairs = scrape_route_pairs(args.delay)
    print(f"Scraped {len(raw_pairs)} unique named route pairs")

    print("Fetching KSRTC city list for name matching...")
    city_index = fetch_city_index()
    print(f"Loaded {len(city_index)} distinct normalized city names")

    matched = []
    unmatched = []
    for from_name, to_name in raw_pairs:
        from_id, from_matched, from_method = match_city(from_name, city_index)
        to_id, to_matched, to_method = match_city(to_name, city_index)

        if from_id is not None and to_id is not None:
            matched.append({
                "from_city_id": from_id,
                "to_city_id": to_id,
                "from_name": from_name,
                "to_name": to_name,
                "from_matched_name": from_matched,
                "to_matched_name": to_matched,
                "from_match_method": from_method,
                "to_match_method": to_method,
            })
        else:
            unmatched.append({
                "from_name": from_name,
                "to_name": to_name,
                "from_resolved": from_id is not None,
                "to_resolved": to_id is not None,
            })

    print(f"Matched {len(matched)}/{len(raw_pairs)} pairs "
          f"({len(unmatched)} with at least one unresolved city name)")

    out = {
        "source": LISTING_URL,
        "scraped_pairs_total": len(raw_pairs),
        "matched_count": len(matched),
        "unmatched_count": len(unmatched),
        "pairs": matched,
        "unmatched": unmatched,
    }
    with open(args.output, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()