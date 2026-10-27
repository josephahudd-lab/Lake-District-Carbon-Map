#!/bin/bash
set -euo pipefail

# ==============================================================================
# Lake District Carbon Mapping Pipeline - Master Orchestrator
# ==============================================================================

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="$PROJECT_DIR/data"
RAW_DIR="$DATA_DIR/raw"
NORM_DIR="$DATA_DIR/norm"
OUTPUT_DIR="$PROJECT_DIR/output"
NFI_FILE="${NFI_PATH:-$HOME/Downloads/Lakes Tree Inventory from NFI.gpkg}"
GRID_SIZE=20.0
THREADS=8

mkdir -p "$RAW_DIR" "$NORM_DIR" "$OUTPUT_DIR"

usage() {
    echo "Lake District Carbon Pipeline"
    echo "Usage: ./pipeline.sh [command] [options]"
    echo ""
    echo "Commands:"
    echo "  download   Download National LiDAR Programme point clouds from Defra API"
    echo "             Options: pass any download_lidar.py flags, e.g.:"
    echo "             ./pipeline.sh download --limit 5"
    echo "             ./pipeline.sh download --tiles NY3025 NY2520"
    echo "             ./pipeline.sh download --list"
    echo ""
    echo "  grid       Run high-speed Rust processor directly on raw LAZ files"
    echo "             (Normalises DTM on-the-fly with bilinear interpolation & calculates 20m grid)"
    echo ""
    echo "  carbon     Join volume grid with NFI woodland mask & calculate Carbon (tC / CO2e)"
    echo "             (Set NFI_PATH environment variable if NFI file is in a custom path)"
    echo ""
    echo "  normalize  (Optional/Legacy) Export pre-normalised LAZ files using PDAL TIN Delaunay"
    echo ""
    echo "  all        Run the entire pipeline end-to-end (download -> fast Rust grid -> carbon)"
    echo ""
    exit 1
}

cmd="${1:-help}"
shift || true

case "$cmd" in
    download)
        echo "=== [Stage 1/3] Downloading LiDAR Point Clouds ==="
        python3 "$PROJECT_DIR/download_lidar.py" -o "$RAW_DIR" -t 4 "$@"
        ;;

    grid)
        echo "=== [Stage 2/3] Running Rust Fast Grid Processor (On-The-Fly DTM) ==="
        # Process raw files directly; if norm dir has files, use raw by preference or fallback
        INPUT_TARGET="$RAW_DIR"
        if [ -d "$NORM_DIR" ] && [ "$(ls -A "$NORM_DIR" 2>/dev/null)" ] && [ ! "$(ls -A "$RAW_DIR" 2>/dev/null)" ]; then
            INPUT_TARGET="$NORM_DIR"
        fi
        "$PROJECT_DIR/target/release/carbon_lidar" \
            --input "$INPUT_TARGET" \
            --output-dir "$OUTPUT_DIR" \
            --grid-size "$GRID_SIZE" \
            --threads "$THREADS" \
            --combine-master "$@"
        ;;

    normalize)
        echo "=== (Optional) Normalising Heights Above Ground with PDAL ==="
        "$PROJECT_DIR/normalize_tiles.sh" "$RAW_DIR" "$NORM_DIR" "$THREADS"
        ;;

    carbon)
        echo "=== [Stage 3/3] Calculating Carbon Stocks & GeoTIFF Raster ==="
        if [ ! -f "$NFI_FILE" ]; then
            echo "NFI woodland mask file not found at: $NFI_FILE"
            echo "Fetching official Forestry Commission NFI dataset for the Lake District..."
            python3 "$PROJECT_DIR/download_nfi.py" -o "$NFI_FILE"
        fi
        python3 "$PROJECT_DIR/calculate_carbon.py" \
            --csv "$OUTPUT_DIR/master_volume_grid.csv" \
            --nfi "$NFI_FILE" \
            --output-prefix "$OUTPUT_DIR/lake_district" \
            --grid-size "$GRID_SIZE" \
            --raster
        python3 "$PROJECT_DIR/export_web_map.py"
        ;;

    all)
        echo "=== Running Full Lake District Carbon Pipeline ==="
        python3 "$PROJECT_DIR/download_lidar.py" -o "$RAW_DIR" -t 4 "$@"
        "$PROJECT_DIR/target/release/carbon_lidar" \
            --input "$RAW_DIR" \
            --output-dir "$OUTPUT_DIR" \
            --grid-size "$GRID_SIZE" \
            --threads "$THREADS" \
            --combine-master
        if [ ! -f "$NFI_FILE" ]; then
            echo "Fetching official Forestry Commission NFI dataset for the Lake District..."
            python3 "$PROJECT_DIR/download_nfi.py" -o "$NFI_FILE"
        fi
        python3 "$PROJECT_DIR/calculate_carbon.py" \
            --csv "$OUTPUT_DIR/master_volume_grid.csv" \
            --nfi "$NFI_FILE" \
            --output-prefix "$OUTPUT_DIR/lake_district" \
            --grid-size "$GRID_SIZE" \
            --raster
        python3 "$PROJECT_DIR/export_web_map.py"
        ;;

    *)
        usage
        ;;
esac
