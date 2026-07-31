# KSRTC GTFS Generator

### Files

- gtfs.zip: website data -> GTFS
- gtfs_compat.zip: GTFS patched to merge different directions and use direction_id in trips.

# KSRTC GTFS Generator

A Python script that fetches transit data from the KSRTC website and generates a GTFS (General Transit Feed Specification) dataset.

## Overview

This tool connects to the KSRTC website to retrieve bus route information, stops, schedules, and polyline data, then processes this information to create a standards-compliant GTFS feed that can be used by transit applications and services.

## Features

- Fetches all KSRTC bus routes and their details of the next 30 days
- Parallel processing of route data for improved performance
- Automatic GTFS dataset generation with proper formatting
- Compressed ZIP archive output
- Detailed logging to both console and file
- Support for midnight-crossing trips
- Configurable via command-line arguments
- `shapes.txt` generation via [pfaedle](https://github.com/ad-freiburg/pfaedle) map-matching

## Prerequisites

- Python 3.13+
- poetry (Python package manager)
- `git`, `cmake`, and a C++ compiler (gcc ≥5.0 or clang ≥3.9) — needed the first time pfaedle
  is built from source into `.pfaedle/` (gitignored); skip with `--skip-shapes` if you don't
  need shapes or don't have a compiler toolchain available
- `libzip` — required for pfaedle to read/write GTFS `.zip` feeds; without it pfaedle builds
  successfully but fails at runtime with `Cannot read from ZIP file, was compiled without libzip`
- `osmium-tool` (optional) — lets the script auto-merge OSM extracts when stops span multiple
  Geofabrik zones, so pfaedle can map-match cross-zone (interstate) trips in one pass; without
  it, the script falls back to running pfaedle once per zone and merging results per-trip

Install these on macOS via Homebrew:

```bash
brew install libzip osmium-tool
```

## Installation

1. Clone or download this repository

2. Install required dependencies:


```bash
poetry install
```

## Usage

### Basic Usage

Run the script to generate the complete GTFS dataset:

```bash
poetry run python gtfs_parallel.py
poetry run python gtfs_compat.py
```

The script will:
1. Fetch all routes from the KSRTC website
2. Process route details in parallel (default: 10 concurrent workers)
3. Generate GTFS-compliant text files in the `gtfs/` directory
4. Build pfaedle from source on first run (cached in `.pfaedle/`), auto-download the
   Geofabrik OSM extract(s) covering the feed's stops (cached in `.osm_cache/`), and
   map-match trips to produce `shapes.txt` — pass `--skip-shapes` to disable this
5. Create a compressed `gtfs.zip` archive
6. Log all operations to `latest.log` and console
7. Create ksrtc-stops.geojson and ksrtc-services.json
_KSRTC does not give us geographic information on stops, this is stored in ksrtc-stops.geojson_

Shape data is derived from OpenStreetMap (ODbL 1.0) — credit OpenStreetMap contributors
if you distribute the shaped feed.

### Command Line Options

The application supports several command line arguments for customization:

```bash
python gtfs_parallel.py --help
```


## Output

### Output Archive

- **gtfs.zip** - Compressed archive containing all GTFS files

The script provides compression statistics:
- Uncompressed size
- Compressed size
- Compression ratio

## Logging

The application generates detailed logs:

- **Console Output**: Real-time progress and important messages
- **latest.log**: Complete log file with debug information

Log entries include:
- Route processing status
- Stop matching details
- API request status
- Error and warning messages

## GTFS Specification

The output follows the [GTFS specification](https://gtfs.org/schedule/reference/) maintained by Google and the transit community.

### Route Types

The application uses route type `3` (Bus) for all routes as per GTFS standards:
- 0 - Tram
- 1 - Subway
- 2 - Rail
- **3 - Bus** (used by this application)
- 4 - Ferry

### Time Format

Times follow GTFS conventions:
- Format: HH:MM:SS
- Supports hours > 24 for trips crossing midnight (e.g., 25:30:00)

## File Structure

```
.
├── gtfs_parallel.py          # Main application script
├── gtfs_compat.py            # Post processing script
├── api-doc.yaml              # Swagger API documentation
├── README.md                 # This file
├── latest.log                # Log file (generated)
├── gtfs/               # Output directory (generated)
│   ├── agency.txt
│   ├── routes.txt
│   ├── trips.txt
│   ├── stop_times.txt
│   ├── stops.txt
│   ├── shapes.txt
│   └── calendar.txt
├── gtfs.zip            # Compressed output (generated)
└── gtfs_compat.zip     # Compressed output of compatibility script
```

## Contributing

To contribute to this project:
1. Test changes with a small subset
2. Ensure logging captures relevant debug information
3. Validate GTFS output using [GTFS Validator](https://gtfs-validator.mobilitydata.org/)

## License

MIT-0, Shapes are generated with OSM data, requiring OSM attribution.

## Contact

For issues related to:
- **KSRTC service**: Contact KSRTC at ksrtc.in
- **This Application or dataset**: Open an issue in the project repository

## Acknowledgments

- [KSRTC Website](https://ksrtc.in)
- [GTFS Specification](https://gtfs.org/)
- [gtfs-validator](https://gtfs-validator.mobilitydata.org/)
- [bmtc-gtfs](https://github.com/Vonter/bmtc-gtfs)