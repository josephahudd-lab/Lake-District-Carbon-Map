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
    echo "  compare      Compare carbon storage across 2 survey dates (e.g. ./pipeline.sh compare 2009 2020)
  process-year Download, grid, and calculate carbon for a single year (e.g. ./pipeline.sh process-year 2009)
  epoch-run    Download, grid, and calculate an entire survey epoch (baseline, midterm, recent)
  epoch-build  Assemble all available survey epochs into the composite PMTiles pyramid
  verify       Verify that all point cloud files are present and uncorrupted (e.g. ./pipeline.sh verify 2009)
"
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

    compare)
        echo "=== Multi-Temporal Carbon Stock Comparison ==="
        BASE_YEAR="${1:-2009}"
        TARGET_YEAR="${2:-2020}"
        shift 2 || shift $# || true
        python3 "$PROJECT_DIR/compare_carbon.py" --base-year "$BASE_YEAR" --target-year "$TARGET_YEAR" "$@"
        ;;

    process-year)
        YEAR="${1:-2009}"
        shift || true
        echo "======================================================="
        echo " Running Full Pipeline for Survey Year: $YEAR"
        echo "======================================================="
        YEAR_RAW="$DATA_DIR/raw/$YEAR"
        YEAR_OUT="$OUTPUT_DIR/$YEAR"
        mkdir -p "$YEAR_RAW" "$YEAR_OUT"

        echo "--- [1/4] Downloading $YEAR LiDAR Tiles from Defra (Multi-Pass Auto-Retry) ---"
        python3 "$PROJECT_DIR/download_lidar.py" --year "$YEAR" -o "$YEAR_RAW" -t 3 "$@"

        echo "--- Pre-Flight Verification Gate: Ensuring 100% of files downloaded ---"
        python3 "$PROJECT_DIR/download_lidar.py" --verify --year "$YEAR" -o "$YEAR_RAW" "$@"

        echo "--- [2/4] Running Rust Fast Grid Processor ($YEAR) ---"
        "$PROJECT_DIR/target/release/carbon_lidar" \
            --input "$YEAR_RAW" \
            --output-dir "$YEAR_OUT" \
            --grid-size "$GRID_SIZE" \
            --threads "$THREADS" \
            --combine-master

        echo "--- [3/4] Calculating Carbon Stocks with NFI Mask ---"
        if [ ! -f "$NFI_FILE" ]; then
            echo "Fetching official Forestry Commission NFI dataset for the Lake District..."
            python3 "$PROJECT_DIR/download_nfi.py" -o "$NFI_FILE"
        fi
        python3 "$PROJECT_DIR/calculate_carbon.py" \
            --csv "$YEAR_OUT/master_volume_grid.csv" \
            --nfi "$NFI_FILE" \
            --output-prefix "$OUTPUT_DIR/lake_district_carbon_${YEAR}" \
            --grid-size "$GRID_SIZE" \
            --boundary "$PROJECT_DIR/docs/lake_district_boundary.geojson"

        echo "--- [4/4] Comparing $YEAR Baseline vs 2020 Recent Stock ---"
        python3 "$PROJECT_DIR/compare_carbon.py" --base-year "$YEAR" --target-year "2020"
        echo "======================================================="
        echo " Finished! Multi-temporal PMTiles updated in docs/ and output/"
        echo "======================================================="
        ;;

    epoch-run)
        EPOCH="${1:-baseline}"
        shift || true
        echo "======================================================="
        echo " Running Master Pipeline for Survey Epoch: $EPOCH"
        echo "======================================================="
        EPOCH_RAW="$DATA_DIR/raw/$EPOCH"
        EPOCH_OUT="$OUTPUT_DIR/$EPOCH"
        mkdir -p "$EPOCH_RAW" "$EPOCH_OUT"

        echo "--- [1/3] Downloading $EPOCH LiDAR Tiles from Defra (Multi-Pass Auto-Retry) ---"
        python3 "$PROJECT_DIR/download_lidar.py" --epoch "$EPOCH" -o "$EPOCH_RAW" -t 3 "$@"

        echo "--- Pre-Flight Verification Gate: Ensuring 100% of files downloaded ---"
        python3 "$PROJECT_DIR/download_lidar.py" --verify --epoch "$EPOCH" -o "$EPOCH_RAW" "$@"

        echo "--- [2/3] Running Rust Fast Grid Processor ($EPOCH) ---"
        "$PROJECT_DIR/target/release/carbon_lidar" \
            --input "$EPOCH_RAW" \
            --output-dir "$EPOCH_OUT" \
            --grid-size "$GRID_SIZE" \
            --threads "$THREADS" \
            --combine-master

        echo "--- [3/3] Calculating Carbon Stocks with NFI Mask ---"
        if [ ! -f "$NFI_FILE" ]; then
            echo "Fetching official Forestry Commission NFI dataset for the Lake District..."
            python3 "$PROJECT_DIR/download_nfi.py" -o "$NFI_FILE"
        fi
        python3 "$PROJECT_DIR/calculate_carbon.py" \
            --csv "$EPOCH_OUT/master_volume_grid.csv" \
            --nfi "$NFI_FILE" \
            --output-prefix "$OUTPUT_DIR/lake_district_carbon_${EPOCH}" \
            --grid-size "$GRID_SIZE" \
            --boundary "$PROJECT_DIR/docs/lake_district_boundary.geojson"

        echo "--- Building Composite Vector Tiles ---"
        python3 "$PROJECT_DIR/build_composite.py"
        echo "======================================================="
        echo " Finished Epoch $EPOCH! Vector tiles updated in docs/"
        echo "======================================================="
        ;;

    verify)
        TARGET="${1:-modern}"
        echo "=== Pre-Flight Dataset Verification: $TARGET ==="
        if [ "$TARGET" = "modern" ]; then
            python3 "$PROJECT_DIR/download_lidar.py" --verify -o "$RAW_DIR"
        elif [[ "$TARGET" =~ ^[0-9]{4}$ ]]; then
            python3 "$PROJECT_DIR/download_lidar.py" --verify --year "$TARGET" -o "$DATA_DIR/raw/$TARGET"
        else
            python3 "$PROJECT_DIR/download_lidar.py" --verify --epoch "$TARGET" -o "$DATA_DIR/raw/$TARGET"
        fi
        ;;

    epoch-build)
        echo "=== Compiling Multi-Epoch Composite Vector Pyramid ==="
        python3 "$PROJECT_DIR/build_composite.py"
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
