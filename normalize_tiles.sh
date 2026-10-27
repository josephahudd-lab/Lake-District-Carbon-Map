#!/bin/bash
set -euo pipefail

# ==============================================================================
# Lake District Carbon Project - Batch LiDAR Normaliser
# Uses PDAL hag_delaunay (TIN terrain interpolation) and ferries HAG to Z
# ==============================================================================

RAW_DIR="${1:-$HOME/Downloads/RawLaz}"
NORM_DIR="${2:-$HOME/Downloads/NormLaz}"
THREADS="${3:-4}"

if [ ! -d "$RAW_DIR" ]; then
    echo "Error: Raw directory '$RAW_DIR' does not exist."
    echo "Usage: ./normalize_tiles.sh [RAW_DIR] [NORM_DIR] [NUM_THREADS]"
    exit 1
fi

mkdir -p "$NORM_DIR"

echo "======================================================="
echo " LiDAR Height Normaliser (PDAL Delaunay TIN)"
echo "======================================================="
echo " Raw Directory  : $RAW_DIR"
echo " Norm Directory : $NORM_DIR"
echo " Parallel Jobs  : $THREADS"
echo "======================================================="

normalize_tile() {
    local raw_file="$1"
    local norm_dir="$2"
    local filename
    filename=$(basename -- "$raw_file")
    local stem="${filename%.*}"
    local norm_file="$norm_dir/${stem}_norm.laz"

    if [ -f "$norm_file" ]; then
        echo " [SKIP] $filename (already normalised)"
        return 0
    fi

    echo " [PROCESSING] $filename..."
    # 1. Compute HeightAboveGround using TIN Delaunay interpolation (smooth on steep fells)
    # 2. Ferry HeightAboveGround into the Z coordinate so any standard LAS reader reads height directly
    if pdal translate "$raw_file" "$norm_file" hag_delaunay ferry --filters.ferry.dimensions="HeightAboveGround=>Z" >/dev/null 2>&1; then
        echo " [SUCCESS] $filename -> $(basename "$norm_file")"
    else
        echo " [ERROR] Failed to normalise $filename. Check if tile has classified ground points (class 2)."
    fi
}

export -f normalize_tile

find "$RAW_DIR" -maxdepth 1 -type f \( -name "*.laz" -o -name "*.las" \) | \
    xargs -I {} -P "$THREADS" bash -c 'normalize_tile "$@"' _ {} "$NORM_DIR"

echo "-------------------------------------------------------"
echo "Normalisation batch finished!"
