#!/usr/bin/env python3
"""
filter_ldnp.py

Filters the carbon dataset strictly to the official statutory Lake District National
Park boundary polygon, updates the CSV, and re-compiles the PMTiles archive.
"""

import os
import sys
import csv
import json
import time
import subprocess
from osgeo import ogr, osr

ogr.UseExceptions()

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(PROJECT_DIR, "output")
DOCS_DIR = os.path.join(PROJECT_DIR, "docs")
GPKG_PATH = os.path.join(OUTPUT_DIR, "lake_district_carbon.gpkg")
BOUNDARY_PATH = "/tmp/lake_district_boundary_bng.geojson"
CSV_OUT = os.path.join(OUTPUT_DIR, "lake_district_carbon.csv")
NDJSON_TEMP = "/tmp/lake_carbon_ldnp.ndjson"
MBTILES_TEMP = "/tmp/lake_ldnp.mbtiles"
PMTILES_DOCS = os.path.join(DOCS_DIR, "lake_district_carbon.pmtiles")
PMTILES_OUTPUT = os.path.join(OUTPUT_DIR, "lake_district_carbon.pmtiles")

if not os.path.exists(GPKG_PATH):
    print(f"Error: {GPKG_PATH} not found.")
    sys.exit(1)

print("Loading official Lake District National Park boundary...")
b_ds = ogr.Open(BOUNDARY_PATH)
b_layer = b_ds.GetLayer()
b_feat = b_layer.GetNextFeature()
boundary_geom = b_feat.GetGeometryRef().Clone()

print("Opening carbon GeoPackage with spatial index...")
g_ds = ogr.Open(GPKG_PATH)
layer = g_ds.GetLayer()
layer.SetSpatialFilter(boundary_geom)

total_park_cells = layer.GetFeatureCount()
print(f"Found {total_park_cells:,} woodland cells strictly within Lake District National Park.")

# Coordinate transformation setup: EPSG:27700 -> EPSG:4326
src_srs = osr.SpatialReference()
src_srs.ImportFromEPSG(27700)
src_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

tgt_srs = osr.SpatialReference()
tgt_srs.ImportFromEPSG(4326)
tgt_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

transform = osr.CoordinateTransformation(src_srs, tgt_srs)

t0 = time.time()
csv_headers = [
    "easting", "northing", "woodland_type", "h90", "volume_ha",
    "biomass_tonnes", "carbon_tonnes", "co2e_tonnes", "carbon_density_ha"
]

print(f"Writing strictly LDNP CSV to {CSV_OUT} and NDJSON to {NDJSON_TEMP}...")

BATCH_SIZE = 50000
batch = []
count = 0
total_c = 0.0
total_co2e = 0.0

with open(CSV_OUT, "w", newline="", buffering=65536) as f_csv, open(NDJSON_TEMP, "w", buffering=65536) as f_ndjson:
    writer = csv.writer(f_csv)
    writer.writerow(csv_headers)
    
    def process_batch(items):
        global total_c, total_co2e
        if not items:
            return
        
        # Batch coordinate transformation for all 5 box corners per cell
        all_pts = []
        for it in items:
            cx, cy = it[0], it[1]
            all_pts.extend([
                (cx - 10.0, cy - 10.0), # SW
                (cx + 10.0, cy - 10.0), # SE
                (cx + 10.0, cy + 10.0), # NE
                (cx - 10.0, cy + 10.0), # NW
                (cx - 10.0, cy - 10.0)  # Close ring
            ])
            
        transformed = transform.TransformPoints(all_pts)
        
        lines = []
        for i, it in enumerate(items):
            c_base = i * 5
            ring = []
            for k in range(5):
                p = transformed[c_base + k]
                lon, lat = (p[1], p[0]) if p[0] > 0 and p[1] < 0 else (p[0], p[1])
                ring.append([round(lon, 6), round(lat, 6)])
            
            feat_geojson = {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [ring]
                },
                "properties": {
                    "type": it[2],
                    "h90": it[3],
                    "vol": it[4],
                    "c": it[6],
                    "co2e": it[7],
                    "density": it[8]
                }
            }
            lines.append(json.dumps(feat_geojson) + "\n")
            
        f_ndjson.writelines(lines)

    for feat in layer:
        h90 = round(feat.GetField("h90") or 0.0, 2)
        if h90 > 50.0:
            continue

        geom = feat.GetGeometryRef()
        cx = geom.GetX()
        cy = geom.GetY()
        wtype = feat.GetField("woodland_type") or "Woodland"
        vol = round(feat.GetField("volume_ha") or 0.0, 1)
        bm = round(feat.GetField("biomass_tonnes") or 0.0, 3)
        c = round(feat.GetField("carbon_tonnes") or 0.0, 3)
        co2e = round(feat.GetField("co2e_tonnes") or 0.0, 3)
        dens = round(feat.GetField("carbon_density_ha") or 0.0, 1)
        
        total_c += c
        total_co2e += co2e
        count += 1
        
        # Write CSV row
        writer.writerow([cx, cy, wtype, h90, vol, bm, c, co2e, dens])
        
        # Buffer for geojson
        batch.append((cx, cy, wtype, h90, vol, bm, c, co2e, dens))
        if len(batch) >= BATCH_SIZE:
            process_batch(batch)
            batch = []
            
    if batch:
        process_batch(batch)

elapsed = time.time() - t0
print(f"Exported {count:,} cells in {elapsed:.2f}s ({count/elapsed:,.0f} cells/sec).")
print(f"Total Stored Carbon: {total_c:,.1f} tC")
print(f"Total CO2e: {total_co2e:,.1f} tCO2e")

# Run tippecanoe
print("\nSlicing vector tiles with tippecanoe (zooms 8 to 14)...")
cmd_tippecanoe = [
    "tippecanoe",
    "-z14",
    "-Z8",
    "-l", "carbon_cells",
    "--force",
    "--no-tile-size-limit",
    "--no-feature-limit",
    "-o", MBTILES_TEMP,
    NDJSON_TEMP
]
subprocess.run(cmd_tippecanoe, check=True)

# Convert to PMTiles v3
print("\nConverting MBTiles to PMTiles v3 format...")
cmd_pmtiles = [
    "pmtiles",
    "convert",
    MBTILES_TEMP,
    PMTILES_DOCS
]
subprocess.run(cmd_pmtiles, check=True)

# Copy to output
subprocess.run(["cp", PMTILES_DOCS, PMTILES_OUTPUT], check=True)

pmtiles_size_mb = os.path.getsize(PMTILES_DOCS) / (1024 * 1024)
print(f"\nSuccessfully generated {PMTILES_DOCS}: {pmtiles_size_mb:.2f} MB")
