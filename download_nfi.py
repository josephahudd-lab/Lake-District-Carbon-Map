#!/usr/bin/env python3
"""
download_nfi.py

Downloads the official Forestry Commission National Forest Inventory (NFI) woodland polygons
for the Lake District area directly from the ArcGIS REST FeatureServer, and saves it as a GeoPackage (.gpkg).
"""

import os
import sys
import json
import urllib.request
import urllib.parse
import argparse
from osgeo import ogr, osr, gdal

gdal.UseExceptions()

FEATURE_SERVER_URL = "https://services2.arcgis.com/mHXjwgl3OARRqqD4/arcgis/rest/services/National_Forest_Inventory_England_2022/FeatureServer/0/query"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; LakeDistrictCarbonProject)"

# Default Lake District BNG bounding box (EPSG:27700)
DEFAULT_BBOX = [290000, 470000, 360000, 550000]

def get_feature_count(bbox):
    params = {
        "geometry": f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}",
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": "27700",
        "returnCountOnly": "true",
        "f": "json",
    }
    url = f"{FEATURE_SERVER_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        return data.get("count", 0)

def download_nfi_geopackage(output_gpkg: str, bbox=DEFAULT_BBOX, page_size: int = 1000):
    total_count = get_feature_count(bbox)
    print("=======================================================")
    print(" Forestry Commission National Forest Inventory (NFI)")
    print("=======================================================")
    print(f"Bounding Box (BNG) : {bbox}")
    print(f"Total Features     : {total_count:,}")
    print(f"Output GeoPackage  : {output_gpkg}")
    print("=======================================================")

    if total_count == 0:
        print("No features found in bounding box!")
        return

    os.makedirs(os.path.dirname(os.path.abspath(output_gpkg)), exist_ok=True)
    if os.path.exists(output_gpkg):
        os.remove(output_gpkg)

    # Initialize output GPKG
    driver = ogr.GetDriverByName("GPKG")
    out_ds = driver.CreateDataSource(output_gpkg)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(27700)

    out_layer = out_ds.CreateLayer("lakes_tree_inventory", srs, ogr.wkbMultiPolygon)

    fields_initialized = False
    offset = 0
    fetched_count = 0

    while offset < total_count:
        params = {
            "geometry": f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}",
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects",
            "inSR": "27700",
            "outSR": "27700",
            "outFields": "*",
            "resultOffset": str(offset),
            "resultRecordCount": str(page_size),
            "f": "geojson",
        }
        url = f"{FEATURE_SERVER_URL}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        features = data.get("features", [])
        if not features:
            break

        # Convert page GeoJSON to in-memory OGR layer
        geojson_str = json.dumps(data)
        page_ds = ogr.Open(geojson_str)
        page_layer = page_ds.GetLayer(0)

        # Initialize fields once from the first page
        if not fields_initialized:
            page_defn = page_layer.GetLayerDefn()
            for i in range(page_defn.GetFieldCount()):
                f_defn = page_defn.GetFieldDefn(i)
                out_layer.CreateField(f_defn)
            fields_initialized = True

        out_defn = out_layer.GetLayerDefn()
        for feat in page_layer:
            geom = feat.GetGeometryRef()
            if geom:
                # Force to MultiPolygon if needed
                new_feat = ogr.Feature(out_defn)
                for i in range(out_defn.GetFieldCount()):
                    new_feat.SetField(i, feat.GetField(i))

                if geom.GetGeometryType() == ogr.wkbPolygon:
                    multi = ogr.Geometry(ogr.wkbMultiPolygon)
                    multi.AddGeometry(geom)
                    new_feat.SetGeometry(multi)
                else:
                    new_feat.SetGeometry(geom)

                out_layer.CreateFeature(new_feat)

        fetched_count += len(features)
        offset += page_size
        print(f"Downloaded [{fetched_count:,} / {total_count:,}] woodland polygons...")

    out_ds = None
    print("\n-------------------------------------------------------")
    print(f"NFI Download Complete! Saved to: {output_gpkg}")
    print("=======================================================")

def main():
    parser = argparse.ArgumentParser(description="Download National Forest Inventory for the Lake District")
    parser.add_argument(
        "-o", "--output",
        default=os.path.expanduser("~/Downloads/Lakes Tree Inventory from NFI.gpkg"),
        help="Target output GeoPackage path"
    )
    args = parser.parse_args()
    download_nfi_geopackage(args.output)

if __name__ == "__main__":
    main()
