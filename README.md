# Lake District Carbon Mapping Pipeline

A high-performance, automated LiDAR remote sensing pipeline to compute tree volume, biomass, and carbon stocks ($t\text{C}$ and $t\text{CO}_2\text{e}$) across the Lake District National Park (Cumbria, UK).

---

## Architecture Overview

```
Defra Data Services Platform API
          │
          ▼
1. download_lidar.py   ──► Automated query & multi-threaded download from Defra API
          │                Streams & extracts .laz files without leaving zip archives on disk
          ▼
2. carbon_lidar (Rust) ──► On-the-fly DTM normalisation + 20m Holmgren volume grid (~10s/tile)
          │                Multi-threaded, snapped to British National Grid (EPSG:27700)
          ▼
3. calculate_carbon.py ──► Spatial join with Forestry Commission NFI woodland mask
          │                (Auto-downloads NFI via download_nfi.py if missing)
          ▼
Output Files:
├── output/master_volume_grid.csv
├── output/lake_district_carbon.csv
├── output/lake_district_carbon.gpkg (GIS-ready vector points for QGIS)
└── output/lake_district_carbon_20m.tif (20m raster for QGIS / WebGL)
```

---

## One-Click Master Pipeline (`./pipeline.sh`)

You can run individual stages or the entire pipeline using the master orchestrator:

```bash
cd ~/carbon_lidar

# 1. Download point clouds (e.g. 5 tiles for testing, or all Lake District tiles)
./pipeline.sh download --limit 5

# 2. Fast in-memory Rust grid & volume processor (~10s per 5km tile)
./pipeline.sh grid

# 3. Join with Forestry Commission NFI mask to calculate carbon, PMTiles & WebGL map
./pipeline.sh carbon
```

*(Note: If the NFI woodland mask is not present, `./pipeline.sh carbon` automatically fetches the official Forestry Commission dataset for the Lake District via `download_nfi.py`!)*

---

## Detailed Tool Documentation

### 1. Automated Tile Downloader (`download_lidar.py`)
Queries the Defra Survey catalog for 5km National LiDAR Programme point cloud tiles across the Lake District.

* **List available tiles without downloading**:
  ```bash
  python3 download_lidar.py --list
  ```
* **Download specific tiles**:
  ```bash
  python3 download_lidar.py --tiles NY3025 NY2520 SD1090
  ```
* **Download entire Lake District (multi-threaded)**:
  ```bash
  python3 download_lidar.py -o data/raw -t 4
  ```

### 2. Normaliser (`normalize_tiles.sh`)
Uses PDAL's TIN Delaunay terrain interpolation (`hag_delaunay`) to prevent slope stair-stepping artifacts on Lake District fells, and ferries `HeightAboveGround` into `Z`:

```bash
./normalize_tiles.sh data/raw data/norm 8
```

### 3. Rust Grid & Volume Processor (`carbon_lidar`)
Streams point clouds across all CPU cores with minimal memory overhead (<40MB per thread) and snaps grid cells directly to British National Grid (EPSG:27700):

```bash
target/release/carbon_lidar \
  --input data/norm \
  --output-dir output \
  --grid-size 20.0 \
  --hag-mode z \
  --threads 8 \
  --combine-master
```

### 4. Carbon Stock Derivation (`calculate_carbon.py`)
Performs a spatial intersection against the Forestry Commission National Forest Inventory (NFI) layer, applies blended species multipliers, and derives carbon stocks:

```bash
python3 calculate_carbon.py \
  --csv output/master_volume_grid.csv \
  --nfi "~/Downloads/Lakes Tree Inventory from NFI.gpkg" \
  --output-prefix output/lake_district \
  --raster
```

### Conversion Standards Applied:
* **Holmgren Volume**: $\ln(V) = -2.50 + 1.49 \ln(h_{90}) + 0.87 \ln(dv) + 0.44 dp - 2.44 relstd$
* **Biomass (tonnes)**: $\text{Volume} (\text{m}^3/\text{ha}) \times \text{Species Multiplier} \times 0.04$
* **Stored Carbon**: $\text{Biomass} \times 0.50$ (IPCC dry wood standard)
* **$\text{CO}_2$ Equivalent**: $\text{Carbon} \times \frac{44}{12} \approx 3.667$

---

## Interactive Web Map & GitHub Pages Deployment

The interactive map streams all 862,439 20m vector squares using PMTiles v3 and MapLibre GL JS without loading massive datasets into browser memory.

### Deploying to GitHub Pages:
1. Ensure the web assets exist in `docs/`:
   ```bash
   mkdir -p docs
   cp output/index.html docs/index.html
   cp output/lake_district_carbon.pmtiles docs/lake_district_carbon.pmtiles
   ```
2. Commit and push to GitHub:
   ```bash
   git add docs/ .gitignore README.md Cargo.toml src/ *.py *.sh
   git commit -m "Publish Lake District Carbon Map"
   git push origin main
   ```
3. In your GitHub repository:
   * Go to **Settings** > **Pages**
   * Under **Build and deployment** > **Source**, choose **Deploy from a branch**
   * Select branch **main** and folder **/docs**, then click **Save**.
4. GitHub Pages will serve the interactive vector map at `https://<your-username>.github.io/<repo-name>/`.

