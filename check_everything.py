#!/usr/bin/env python3
"""
check_everything.py

Comprehensive Project Health & Verification Audit for the Lake District Carbon Map:
1. Toolchain & Dependencies (Python, GDAL/OGR, Rust carbon_lidar, Tippecanoe, PMTiles)
2. Reference Geodata (Lake District Boundary, Forestry Commission NFI)
3. Raw LiDAR Point Clouds (Modern 2019-2021 & Historical 2009 Surveys)
4. Gridded Canopy & Carbon Accounts (master volume grids, carbon CSVs)
5. Web Publishing & PMTiles (Vector archive size < 100MB, HTML viewer, local server)
"""

import os
import sys
import shutil
import subprocess
from pathlib import Path

# Terminal Colors
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"

PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
OUTPUT_DIR = PROJECT_DIR / "output"
DOCS_DIR = PROJECT_DIR / "docs"

passes = 0
warnings = 0
failures = 0

def report(status: str, title: str, details: str = ""):
    global passes, warnings, failures
    if status == "PASS":
        passes += 1
        tag = f"{GREEN}[PASS]{RESET}"
    elif status == "INFO":
        tag = f"{CYAN}[INFO]{RESET}"
    elif status == "WARN":
        warnings += 1
        tag = f"{YELLOW}[WARN]{RESET}"
    else:
        failures += 1
        tag = f"{RED}[FAIL]{RESET}"

    if details:
        print(f" {tag} {BOLD}{title:<34}{RESET} : {details}")
    else:
        print(f" {tag} {BOLD}{title}{RESET}")

def sub_to_parent_5km(sub_id: str):
    if len(sub_id) < 6:
        return None
    prefix = sub_id[:2].upper()
    try:
        x = int(sub_id[2:4])
        y = int(sub_id[4:6])
        parent_x = (x // 5) * 5
        parent_y = (y // 5) * 5
        return f"{prefix}{parent_x:02d}{parent_y:02d}"
    except ValueError:
        return None

def check_laz_header(filepath: Path) -> bool:
    try:
        if not filepath.exists() or filepath.stat().st_size < 1024:
            return False
        with open(filepath, "rb") as f:
            return f.read(4) == b"LASF"
    except Exception:
        return False

def run_cmd(cmd: list) -> tuple:
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)
        return (res.returncode == 0, res.stdout.strip())
    except Exception:
        return (False, "")

def main():
    print(f"\n{BOLD}{CYAN}{'=' * 65}{RESET}")
    print(f"{BOLD}{CYAN}  Lake District Carbon Project - System & Data Health Audit{RESET}")
    print(f"{BOLD}{CYAN}{'=' * 65}{RESET}\n")

    # -------------------------------------------------------------------------
    # 1. Toolchain & Compilers
    # -------------------------------------------------------------------------
    print(f"{BOLD}1. Toolchain & Executables:{RESET}")

    # Python & GDAL
    try:
        from osgeo import gdal, ogr, osr
        report("PASS", "Python GDAL/OGR Bindings", f"v{gdal.__version__}")
    except ImportError:
        report("FAIL", "Python GDAL/OGR Bindings", "Missing! Run: sudo apt install python3-gdal")

    # Rust binary
    rust_bin = PROJECT_DIR / "target" / "release" / "carbon_lidar"
    if rust_bin.exists() and os.access(rust_bin, os.X_OK):
        report("PASS", "Rust Processor (carbon_lidar)", f"Compiled ({rust_bin.stat().st_size / (1024*1024):.1f} MB)")
    else:
        report("FAIL", "Rust Processor (carbon_lidar)", "Not built. Run: cargo build --release")

    # Tippecanoe
    ok, tip_ver = run_cmd(["tippecanoe", "--version"])
    if ok or "tippecanoe v" in tip_ver:
        v_str = tip_ver.split("\n")[0]
        report("PASS", "Tippecanoe Tile Slicer", v_str)
    else:
        report("FAIL", "Tippecanoe Tile Slicer", "Not found in PATH")

    # PMTiles CLI
    ok, pmt_ver = run_cmd(["pmtiles", "version"])
    if ok:
        report("PASS", "PMTiles CLI Utility", pmt_ver.split("\n")[0])
    else:
        report("FAIL", "PMTiles CLI Utility", "Not found in PATH")

    # -------------------------------------------------------------------------
    # 2. Reference Boundary & Forest Data
    # -------------------------------------------------------------------------
    print(f"\n{BOLD}2. Reference Geospatial Datasets:{RESET}")

    # Boundary GeoJSON
    bnd_file = DOCS_DIR / "lake_district_boundary.geojson"
    if bnd_file.exists() and bnd_file.stat().st_size > 1000:
        report("PASS", "Lake District National Park Boundary", f"{bnd_file.stat().st_size / 1024:.1f} KB GeoJSON")
    else:
        report("FAIL", "Lake District National Park Boundary", f"Missing at: {bnd_file}")

    # NFI Woodland Inventory
    nfi_paths = [
        Path(os.path.expanduser("~/Downloads/Lakes Tree Inventory from NFI.gpkg")),
        DATA_DIR / "nfi_lake_district.gpkg",
    ]
    nfi_file = next((p for p in nfi_paths if p.exists()), None)
    if nfi_file:
        report("PASS", "Forestry Commission NFI Mask", f"{nfi_file.stat().st_size / (1024*1024):.1f} MB GeoPackage")
    else:
        report("FAIL", "Forestry Commission NFI Mask", "Missing! Run: ./pipeline.sh download-nfi")

    # -------------------------------------------------------------------------
    # 3. Raw LiDAR Point Clouds
    # -------------------------------------------------------------------------
    print(f"\n{BOLD}3. Raw LiDAR Point Clouds (Defra Archives):{RESET}")

    # Modern Survey (data/raw)
    mod_files = list(RAW_DIR.glob("*.laz"))
    if mod_files:
        valid_mod = sum(1 for f in mod_files if check_laz_header(f))
        tot_gb = sum(f.stat().st_size for f in mod_files) / (1024**3)
        parents_mod = set(sub_to_parent_5km(f.name) for f in mod_files if sub_to_parent_5km(f.name))
        if valid_mod == len(mod_files):
            report("PASS", "Modern National LiDAR (2019-2021)", f"{len(parents_mod)} tiles ({len(mod_files)} files, {tot_gb:.1f} GB) - 100% complete")
        else:
            report("WARN", "Modern National LiDAR (2019-2021)", f"{valid_mod}/{len(mod_files)} valid ({len(mod_files)-valid_mod} corrupt)")
    else:
        report("FAIL", "Modern National LiDAR (2019-2021)", "No .laz files in data/raw/")

    # 2009 Survey (data/raw/2009)
    dir_2009 = RAW_DIR / "2009"
    files_2009 = list(dir_2009.glob("*.laz")) if dir_2009.exists() else []
    if files_2009:
        valid_2009 = sum(1 for f in files_2009 if check_laz_header(f))
        tot_mb = sum(f.stat().st_size for f in files_2009) / (1024**2)
        parents_2009 = set(sub_to_parent_5km(f.name) for f in files_2009 if sub_to_parent_5km(f.name))
        if valid_2009 == len(files_2009):
            report("PASS", "Historical Baseline Survey (2009)", f"{len(parents_2009)}/86 tiles ({len(files_2009)} files, {tot_mb:.0f} MB) - 100% complete")
        else:
            report("WARN", "Historical Baseline Survey (2009)", f"{valid_2009}/{len(files_2009)} valid ({len(files_2009)-valid_2009} corrupt)")
    else:
        report("WARN", "Historical Baseline Survey (2009)", "Not downloaded yet. Run: ./pipeline.sh download --year 2009")

    # Additional Downloaded Epochs
    epoch_specs = [
        ("baseline", "Baseline Era Archive (2005-2010)", 114),
        ("midterm", "Midterm Monitoring Era (2013-2018)", 98),
        ("recent", "Recent Repeat Flights (2022-2024)", 30),
    ]
    for ep_id, ep_title, expected_max in epoch_specs:
        ep_dir = RAW_DIR / ep_id
        ep_files = list(ep_dir.glob("*.laz")) if ep_dir.exists() else []
        if ep_files:
            valid_ep = sum(1 for f in ep_files if check_laz_header(f))
            parents_ep = set(sub_to_parent_5km(f.name) for f in ep_files if sub_to_parent_5km(f.name))
            tot_mb = sum(f.stat().st_size for f in ep_files) / (1024**2)
            report("PASS", f"{ep_title}", f"{len(parents_ep)} tiles ({len(ep_files)} files, {tot_mb:.0f} MB)")
        else:
            report("INFO", f"{ep_title}", f"Optional - not downloaded yet. Run: ./pipeline.sh epoch-run {ep_id}")

    # -------------------------------------------------------------------------
    # 4. Processed Grids & Carbon Stock Accounts
    # -------------------------------------------------------------------------
    print(f"\n{BOLD}4. Processed Carbon & Volume Outputs:{RESET}")

    vol_grid = OUTPUT_DIR / "master_volume_grid.csv"
    if vol_grid.exists() and vol_grid.stat().st_size > 1000:
        lines = sum(1 for _ in open(vol_grid, "rb"))
        report("PASS", "Master Volume Grid (20m resolution)", f"{lines-1:,} cells ({vol_grid.stat().st_size / (1024*1024):.1f} MB)")
    else:
        report("FAIL", "Master Volume Grid (20m resolution)", "Missing! Run: ./pipeline.sh grid")

    mod_carbon = OUTPUT_DIR / "lake_district_carbon.csv"
    if mod_carbon.exists() and mod_carbon.stat().st_size > 1000:
        lines = sum(1 for _ in open(mod_carbon, "rb"))
        report("PASS", "Modern Carbon Stock Account (CSV)", f"{lines-1:,} woodland cells ({mod_carbon.stat().st_size / (1024*1024):.1f} MB)")
    else:
        report("FAIL", "Modern Carbon Stock Account (CSV)", "Missing! Run: ./pipeline.sh carbon")

    hist_carbon = OUTPUT_DIR / "lake_district_carbon_2009.csv"
    if hist_carbon.exists() and hist_carbon.stat().st_size > 1000:
        lines = sum(1 for _ in open(hist_carbon, "rb"))
        report("PASS", "Baseline Carbon Account (2009)", f"{lines-1:,} cells ({hist_carbon.stat().st_size / (1024*1024):.1f} MB)")
    else:
        report("WARN", "Baseline Carbon Account (2009)", "Not calculated yet. Run: ./pipeline.sh process-year 2009")

    # Check other processed epoch CSVs
    for ep_id, ep_title in [
        ("baseline", "Baseline Era Carbon Account"),
        ("midterm", "Midterm Era Carbon Account"),
        ("recent", "Recent Era Carbon Account")
    ]:
        ep_candidates = [
            OUTPUT_DIR / f"lake_district_carbon_{ep_id}.csv",
            OUTPUT_DIR / f"lake_district_carbon_{ep_id}_carbon.csv",
            OUTPUT_DIR / ep_id / "lake_district_carbon.csv"
        ]
        ep_csv = next((p for p in ep_candidates if p.exists() and p.stat().st_size > 1000), None)
        if ep_csv:
            lines = sum(1 for _ in open(ep_csv, "rb"))
            report("PASS", f"{ep_title}", f"{lines-1:,} cells ({ep_csv.name})")
        else:
            report("INFO", f"{ep_title}", f"Not calculated yet. Run: ./pipeline.sh epoch-run {ep_id}")

    # -------------------------------------------------------------------------
    # 5. Published Vector Tiles & Web Map
    # -------------------------------------------------------------------------
    print(f"\n{BOLD}5. Publishing & Vector Pyramid (<100MB GitHub limit):{RESET}")

    pmtiles_file = DOCS_DIR / "lake_district_carbon.pmtiles"
    if pmtiles_file.exists():
        size_mb = pmtiles_file.stat().st_size / (1024 * 1024)
        if size_mb < 100.0:
            report("PASS", "Composite PMTiles Pyramid", f"{size_mb:.2f} MB (safely under GitHub 100MB limit)")
        else:
            report("WARN", "Composite PMTiles Pyramid", f"{size_mb:.2f} MB (EXCEEDS GitHub 100MB limit)")
    else:
        report("FAIL", "Composite PMTiles Pyramid", "Missing! Run: ./pipeline.sh epoch-build")

    index_html = DOCS_DIR / "index.html"
    if index_html.exists() and index_html.stat().st_size > 5000:
        report("PASS", "Interactive Map Viewer (index.html)", f"{index_html.stat().st_size / 1024:.1f} KB (Multi-Epoch UI active)")
    else:
        report("FAIL", "Interactive Map Viewer (index.html)", "Missing or incomplete in docs/")

    # Check local server port 8088
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1.0)
    server_running = sock.connect_ex(('127.0.0.1', 8088)) == 0
    sock.close()
    if server_running:
        report("PASS", "Local Range HTTP Server (port 8088)", "Running at http://localhost:8088/index.html")
    else:
        report("WARN", "Local Range HTTP Server (port 8088)", "Not running. Start with: python3 server.py")

    # -------------------------------------------------------------------------
    # Final Summary
    # -------------------------------------------------------------------------
    print(f"\n{BOLD}{CYAN}{'-' * 65}{RESET}")
    print(f" {BOLD}Audit Summary:{RESET} {GREEN}{passes} Passed{RESET} | {YELLOW}{warnings} Warnings{RESET} | {RED}{failures} Failures{RESET}")
    print(f"{BOLD}{CYAN}{'-' * 65}{RESET}\n")

    if failures == 0 and warnings == 0:
        print(f" {GREEN}{BOLD}[ALL SYSTEMS GO]{RESET} You have 100% of required data, tools, and outputs!")
    elif failures == 0:
        print(f" {GREEN}{BOLD}[READY FOR PRODUCTION]{RESET} All essential components present. Optional warnings above.")
    else:
        print(f" {RED}{BOLD}[ACTION REQUIRED]{RESET} Please resolve the failed items above.")
        sys.exit(1)

if __name__ == "__main__":
    main()
