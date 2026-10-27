#!/usr/bin/env python3
"""
download_lidar.py

Automated bulk downloader for UK Environment Agency National LiDAR Programme point clouds.
Queries the Defra Data Services Platform API and downloads/extracts .laz tiles directly.
"""

import os
import sys
import json
import io
import time
import zipfile
import argparse
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Optional

DEFRA_SEARCH_URL = "https://environment.data.gov.uk/tiles/collections/survey/search"
SUBSCRIPTION_KEY = "dspui"
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": "https://environment.data.gov.uk/survey"
}
USER_AGENT = BROWSER_HEADERS["User-Agent"]

# Default bounding box for the Lake District (WGS84)
LAKE_DISTRICT_BBOX = [-3.55, 54.15, -2.65, 54.75]

def query_tiles_by_bbox(bbox: List[float]) -> List[Dict]:
    """Query Defra API for all point cloud tiles within a WGS84 bounding box [min_lon, min_lat, max_lon, max_lat]"""
    min_lon, min_lat, max_lon, max_lat = bbox
    geojson = {
        "type": "Polygon",
        "coordinates": [[
            [min_lon, min_lat],
            [max_lon, min_lat],
            [max_lon, max_lat],
            [min_lon, max_lat],
            [min_lon, min_lat]
        ]]
    }
    return query_tiles_by_geojson(geojson)

def query_tiles_by_geojson(geojson: Dict) -> List[Dict]:
    """Query Defra API with a GeoJSON geometry"""
    print("Querying Defra Data Services Platform survey catalog...")
    req = urllib.request.Request(
        DEFRA_SEARCH_URL,
        data=json.dumps(geojson).encode("utf-8"),
        headers={
            "Content-Type": "application/geo+json",
            "User-Agent": USER_AGENT
        }
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    results = data.get("results", [])
    # Filter for National LiDAR Programme point cloud products
    pc_tiles = [r for r in results if r.get("product", {}).get("id") == "national_lidar_programme_point_cloud"]
    return pc_tiles

def deduplicate_tiles(tiles: List[Dict]) -> List[Dict]:
    """If a tile has multiple capture years, keep only the newest year"""
    by_tile_id: Dict[str, Dict] = {}
    for t in tiles:
        tile_id = t["tile"]["id"]
        year_str = t["year"]["id"]
        try:
            year = int(year_str)
        except ValueError:
            year = 0

        if tile_id not in by_tile_id:
            by_tile_id[tile_id] = (year, t)
        else:
            if year > by_tile_id[tile_id][0]:
                by_tile_id[tile_id] = (year, t)

    deduped = [v[1] for v in by_tile_id.values()]
    deduped.sort(key=lambda x: x["tile"]["id"])
    return deduped

def download_and_extract_tile(tile_info: Dict, out_dir: str, keep_zip: bool = False, max_retries: int = 3) -> str:
    """Download a single tile zip and extract the .laz directly into out_dir with year fallback and retry"""
    tile_id = tile_info["tile"]["id"]
    year = tile_info["year"]["id"]
    base_uri = tile_info["uri"]

    os.makedirs(out_dir, exist_ok=True)

    # Check if a .laz file for this tile already exists
    existing = [f for f in os.listdir(out_dir) if f.startswith(tile_id) and f.endswith(".laz")]
    if existing:
        return f"[SKIP] {tile_id} ({year}) already exists: {existing[0]}"

    # Fallback survey years to check if the cataloged year returns 403/404
    candidate_years = [year] + [y for y in ["2022", "2021", "2020", "2019", "2018"] if y != year]

    last_error = None
    for cand_year in candidate_years:
        cand_uri = base_uri.replace(f"/{year}/", f"/{cand_year}/")
        download_url = f"{cand_uri}?subscription-key={SUBSCRIPTION_KEY}"

        for attempt in range(1, max_retries + 1):
            start_time = time.time()
            req = urllib.request.Request(download_url, headers=BROWSER_HEADERS)

            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    if resp.status != 200:
                        raise urllib.error.HTTPError(download_url, resp.status, "Non-200 status", resp.headers, None)
                    data = resp.read()

                size_mb = len(data) / (1024 * 1024)
                elapsed = time.time() - start_time

                # Extract LAZ from the downloaded zip in-memory
                extracted_names = []
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    for member in z.infolist():
                        if member.filename.lower().endswith(".laz"):
                            filename = os.path.basename(member.filename)
                            out_path = os.path.join(out_dir, filename)
                            with z.open(member) as source, open(out_path, "wb") as target:
                                target.write(source.read())
                            extracted_names.append(filename)

                if keep_zip:
                    zip_path = os.path.join(out_dir, f"{tile_info.get('label', tile_id)}.zip")
                    with open(zip_path, "wb") as zf:
                        zf.write(data)

                if extracted_names:
                    ext_str = ", ".join(extracted_names)
                    speed = size_mb / elapsed if elapsed > 0 else 0.0
                    note = f" (fallback year {cand_year})" if cand_year != year else ""
                    return f"[OK] {tile_id}{note} -> {ext_str} ({size_mb:.1f} MB at {speed:.1f} MB/s)"
                else:
                    return f"[WARN] {tile_id} ({cand_year}) zip contained no .laz files ({size_mb:.1f} MB)"

            except urllib.error.HTTPError as e:
                last_error = e
                # 403/404 usually means file is under a different year in Azure Blob storage
                if e.code in (403, 404):
                    break # Break retry loop to try next candidate year
                time.sleep(1.5 * attempt)
            except Exception as e:
                last_error = e
                time.sleep(1.5 * attempt)

    return f"[ERROR] Failed downloading {tile_id} (tried years {candidate_years[:3]}): {str(last_error)}"

def main():
    parser = argparse.ArgumentParser(
        description="Automated downloader for Environment Agency National LiDAR Programme point clouds"
    )
    parser.add_argument(
        "--output-dir", "-o",
        default=os.path.expanduser("~/carbon_lidar/data/raw"),
        help="Directory to save extracted .laz files (default: ~/carbon_lidar/data/raw)"
    )
    parser.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        default=LAKE_DISTRICT_BBOX,
        metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"),
        help="Bounding box in WGS84 coordinates (default: Lake District [-3.55, 54.15, -2.65, 54.75])"
    )
    parser.add_argument(
        "--polygon",
        help="Path to a GeoJSON file containing the boundary polygon"
    )
    parser.add_argument(
        "--tiles",
        nargs="+",
        help="Specific tile IDs to download (e.g. --tiles NY3025 NY2520 SD1090)"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limit number of tiles to download (useful for testing, 0 = all)"
    )
    parser.add_argument(
        "--threads", "-t",
        type=int,
        default=4,
        help="Number of concurrent download threads (default: 4)"
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available tiles without downloading"
    )

    args = parser.parse_args()

    # Step 1: Query catalog
    if args.polygon:
        with open(args.polygon, "r") as pf:
            geom_data = json.load(pf)
            # Support both FeatureCollection and geometry directly
            if geom_data.get("type") == "FeatureCollection":
                geom = geom_data["features"][0]["geometry"]
            elif geom_data.get("type") == "Feature":
                geom = geom_data["geometry"]
            else:
                geom = geom_data
            tiles = query_tiles_by_geojson(geom)
    else:
        tiles = query_tiles_by_bbox(args.bbox)

    tiles = deduplicate_tiles(tiles)

    if args.tiles:
        filter_set = set(t.upper() for t in args.tiles)
        tiles = [t for t in tiles if t["tile"]["id"].upper() in filter_set]

    if args.limit > 0:
        tiles = tiles[:args.limit]

    print("=======================================================")
    print(" Defra National LiDAR Programme Downloader")
    print("=======================================================")
    print(f"Total matching 5km tiles : {len(tiles)}")
    print(f"Target Output Directory  : {args.output_dir}")
    print(f"Concurrent Workers       : {args.threads}")
    print("=======================================================")

    if args.list:
        print("\nAvailable Tiles:")
        for t in tiles:
            print(f"  - {t['tile']['id']} | Year: {t['year']['id']} | {t['label']}")
        print(f"\nTotal: {len(tiles)} tiles.")
        return

    if not tiles:
        print("No matching tiles found for the given criteria.")
        return

    # Check already downloaded count
    os.makedirs(args.output_dir, exist_ok=True)
    existing_count = sum(1 for t in tiles if any(f.startswith(t["tile"]["id"]) and f.endswith(".laz") for f in os.listdir(args.output_dir)))
    print(f"Already downloaded: {existing_count} / {len(tiles)} tiles.")
    remaining = len(tiles) - existing_count
    print(f"Remaining to fetch: {remaining} tiles (~{remaining * 150 / 1024:.1f} GB estimated).")
    print("Starting download...\n")

    completed = 0
    with ThreadPoolExecutor(max_workers=args.threads) as executor:
        futures = {
            executor.submit(download_and_extract_tile, t, args.output_dir): t
            for t in tiles
        }
        for future in as_completed(futures):
            res = future.result()
            completed += 1
            print(f"[{completed}/{len(tiles)}] {res}")

    print("\n-------------------------------------------------------")
    print(f"All downloads finished! Files saved to: {args.output_dir}")
    print("=======================================================")

if __name__ == "__main__":
    main()
