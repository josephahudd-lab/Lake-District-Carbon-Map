#!/usr/bin/env python3
"""
export_web_map.py

High-performance PMTiles vector archive compiler and hardware-accelerated
WebGL web map generator for Lake District carbon canopy cells (800k+ features).
"""

import os
import sys
import csv
import json
import time
import subprocess
from osgeo import osr

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(PROJECT_DIR, "output")
CSV_PATH = os.path.join(OUTPUT_DIR, "lake_district_carbon.csv")
OUT_HTML = os.path.join(OUTPUT_DIR, "carbon_map.html")
OUT_PMTILES = os.path.join(OUTPUT_DIR, "lake_district_carbon.pmtiles")
TEMP_NDGEOJSON = "/tmp/carbon_cells_wgs84.ndjson"

if not os.path.exists(CSV_PATH):
    print(f"Error: {CSV_PATH} not found. Run pipeline first.")
    sys.exit(1)

print("=======================================================")
print(" Lake District Carbon Pipeline - Fast Web Map Compiler")
print("=======================================================")
print(f"Reading carbon records from: {CSV_PATH}")

t0 = time.time()
src_srs = osr.SpatialReference()
src_srs.ImportFromEPSG(27700)
tgt_srs = osr.SpatialReference()
tgt_srs.ImportFromEPSG(4326)
transform = osr.CoordinateTransformation(src_srs, tgt_srs)

total_c = 0.0
total_co2e = 0.0
total_features = 0

BATCH_SIZE = 50000
records_batch = []

print("Streaming 20m grid boxes to WGS84 newline-delimited GeoJSON...")
with open(CSV_PATH, 'r', buffering=65536) as f_in, open(TEMP_NDGEOJSON, 'w', buffering=65536) as f_out:
    reader = csv.DictReader(f_in)
    
    def flush_batch(batch):
        global total_c, total_co2e, total_features
        if not batch:
            return
        
        # Prepare all 5 corner points per cell for batch C++ coordinate transformation
        # corners: [SW, SE, NE, NW, SW]
        pts = []
        for r in batch:
            e = float(r['easting'])
            n = float(r['northing'])
            pts.extend([
                (e - 10, n - 10),
                (e + 10, n - 10),
                (e + 10, n + 10),
                (e - 10, n + 10),
                (e - 10, n - 10),
            ])
            c = float(r['carbon_tonnes'])
            co2e = float(r['co2e_tonnes'])
            total_c += c
            total_co2e += co2e
        
        # Batch transform all coordinates in C++
        transformed = transform.TransformPoints(pts)
        
        lines = []
        for i, r in enumerate(batch):
            base_idx = i * 5
            # transformed[k] is (lat, lon, z) or (lon, lat, z) depending on GDAL version
            # In GDAL 3+ with EPSG:4326 default axis order is (lat, lon) unless SetAxisMappingStrategy is used
            p0 = transformed[base_idx]
            p1 = transformed[base_idx + 1]
            p2 = transformed[base_idx + 2]
            p3 = transformed[base_idx + 3]
            p4 = transformed[base_idx + 4]
            
            # WGS84 GeoJSON requires [lon, lat]
            # Detect coordinate format
            lon0, lat0 = (p0[1], p0[0]) if p0[0] > 0 and p0[1] < 0 else (p0[0], p0[1])
            lon1, lat1 = (p1[1], p1[0]) if p1[0] > 0 and p1[1] < 0 else (p1[0], p1[1])
            lon2, lat2 = (p2[1], p2[0]) if p2[0] > 0 and p2[1] < 0 else (p2[0], p2[1])
            lon3, lat3 = (p3[1], p3[0]) if p3[0] > 0 and p3[1] < 0 else (p3[0], p3[1])
            lon4, lat4 = (p4[1], p4[0]) if p4[0] > 0 and p4[1] < 0 else (p4[0], p4[1])

            feature = {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[round(lon0, 6), round(lat0, 6)],
                                     [round(lon1, 6), round(lat1, 6)],
                                     [round(lon2, 6), round(lat2, 6)],
                                     [round(lon3, 6), round(lat3, 6)],
                                     [round(lon4, 6), round(lat4, 6)]]]
                },
                "properties": {
                    "woodland_type": r['woodland_type'],
                    "h90": round(float(r['h90']), 1),
                    "volume_ha": round(float(r['volume_ha']), 1),
                    "carbon_tonnes": round(float(r['carbon_tonnes']), 2),
                    "co2e_tonnes": round(float(r['co2e_tonnes']), 2),
                    "carbon_density_ha": round(float(r['carbon_density_ha']), 1)
                }
            }
            lines.append(json.dumps(feature))
            total_features += 1
            
        f_out.write("\n".join(lines) + "\n")

    for row in reader:
        records_batch.append(row)
        if len(records_batch) >= BATCH_SIZE:
            flush_batch(records_batch)
            records_batch = []
            print(f"  Streamed {total_features:,} features...")
            
    if records_batch:
        flush_batch(records_batch)

elapsed_prep = time.time() - t0
print(f"Prepared {total_features:,} polygon features in {elapsed_prep:.2f}s ({total_features/elapsed_prep:,.0f} features/sec)")

# Step 2: Compile PMTiles with tippecanoe
print("\nCompiling PMTiles pyramid with tippecanoe...")
t_tip = time.time()
cmd_tip = [
    "tippecanoe",
    "-z15", "-Z8",
    "--force",
    "-o", OUT_PMTILES,
    "--drop-densest-as-needed",
    "--extend-zooms-if-still-dropping",
    "-l", "carbon_cells",
    TEMP_NDGEOJSON
]
subprocess.run(cmd_tip, check=True)
if os.path.exists(TEMP_NDGEOJSON):
    os.remove(TEMP_NDGEOJSON)

pmtiles_size_mb = os.path.getsize(OUT_PMTILES) / (1024 * 1024)
print(f"  PMTiles created in {time.time() - t_tip:.2f}s: {OUT_PMTILES} ({pmtiles_size_mb:.1f} MB)")

# Step 3: Generate Hardware-Accelerated WebGL Web Map (MapLibre GL + PMTiles)
total_area_ha = total_features * 0.04
mean_density = round((total_c / total_area_ha), 1) if total_area_ha > 0 else 0.0

print(f"\nGenerating hardware-accelerated WebGL viewer: {OUT_HTML}...")

html_content = f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Lake District Carbon Map - 20m Canopy Viewer</title>
  
  <!-- MapLibre GL JS & PMTiles -->
  <link rel="stylesheet" href="https://unpkg.com/maplibre-gl@3.6.2/dist/maplibre-gl.css" />
  <script src="https://unpkg.com/maplibre-gl@3.6.2/dist/maplibre-gl.js"></script>
  <script src="https://unpkg.com/pmtiles@2.11.0/dist/index.js"></script>

  <style>
    * {{ box-sizing: border-box; }}
    body, html {{ margin: 0; padding: 0; height: 100%; width: 100%; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f172a; }}
    #map {{ height: 100vh; width: 100vw; }}

    .header-panel {{
      position: absolute;
      top: 16px;
      left: 16px;
      z-index: 1000;
      background: rgba(15, 23, 42, 0.92);
      backdrop-filter: blur(14px);
      padding: 16px 20px;
      border-radius: 14px;
      border: 1px solid rgba(51, 65, 85, 0.8);
      color: #f8fafc;
      box-shadow: 0 10px 30px -5px rgba(0, 0, 0, 0.6);
      max-width: 400px;
    }}
    .header-panel h1 {{
      margin: 0 0 4px 0;
      font-size: 18px;
      font-weight: 700;
      display: flex;
      align-items: center;
      gap: 8px;
    }}
    .pulse-dot {{
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: #10b981;
      display: inline-block;
      box-shadow: 0 0 10px #10b981;
    }}
    .header-panel p {{
      margin: 0;
      font-size: 12px;
      color: #94a3b8;
      line-height: 1.4;
    }}
    .stats-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
      margin-top: 14px;
      padding-top: 12px;
      border-top: 1px solid rgba(51, 65, 85, 0.6);
    }}
    .stat-item {{
      background: rgba(30, 41, 59, 0.7);
      padding: 8px 12px;
      border-radius: 8px;
      border: 1px solid rgba(51, 65, 85, 0.4);
    }}
    .stat-label {{
      font-size: 10px;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: #94a3b8;
    }}
    .stat-val {{
      font-size: 16px;
      font-weight: 700;
      color: #10b981;
      margin-top: 2px;
    }}

    .legend-panel {{
      position: absolute;
      bottom: 24px;
      right: 24px;
      z-index: 1000;
      background: rgba(15, 23, 42, 0.92);
      backdrop-filter: blur(14px);
      padding: 14px 18px;
      border-radius: 12px;
      border: 1px solid rgba(51, 65, 85, 0.8);
      color: #f8fafc;
      box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.5);
      width: 260px;
    }}
    .legend-title {{
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      margin-bottom: 8px;
      display: flex;
      justify-content: space-between;
    }}
    .legend-bar {{
      height: 12px;
      border-radius: 6px;
      background: linear-gradient(to right, #d1fae5, #6ee7b7, #10b981, #059669, #047857, #064e3b);
      margin-bottom: 6px;
    }}
    .legend-labels {{
      display: flex;
      justify-content: space-between;
      color: #94a3b8;
      font-family: monospace;
      font-size: 10px;
    }}

    .maplibregl-popup-content {{
      background: #0f172a !important;
      color: #f8fafc !important;
      border: 1px solid #334155 !important;
      border-radius: 10px !important;
      padding: 12px 14px !important;
      box-shadow: 0 15px 30px rgba(0,0,0,0.5) !important;
    }}
    .maplibregl-popup-anchor-top .maplibregl-popup-tip {{ border-bottom-color: #0f172a !important; }}
    .maplibregl-popup-anchor-bottom .maplibregl-popup-tip {{ border-top-color: #0f172a !important; }}
    .maplibregl-popup-anchor-left .maplibregl-popup-tip {{ border-right-color: #0f172a !important; }}
    .maplibregl-popup-anchor-right .maplibregl-popup-tip {{ border-left-color: #0f172a !important; }}
  </style>
</head>
<body>
  <div id="map"></div>

  <div class="header-panel">
    <h1><span class="pulse-dot"></span> Lake District Carbon Map</h1>
    <p>High-resolution 20m LiDAR canopy volume fused with Forestry Commission woodland inventory across all 174 5km tiles.</p>
    <div class="stats-grid">
      <div class="stat-item">
        <div class="stat-label">Total Carbon</div>
        <div class="stat-val">{total_c:,.1f} <span style="font-size:11px; font-weight:normal;">tC</span></div>
      </div>
      <div class="stat-item">
        <div class="stat-label">CO₂ Equivalent</div>
        <div class="stat-val" style="color:#2dd4bf;">{total_co2e:,.1f} <span style="font-size:11px; font-weight:normal;">tCO₂e</span></div>
      </div>
      <div class="stat-item">
        <div class="stat-label">Canopy Area</div>
        <div class="stat-val" style="color:#38bdf8;">{total_area_ha:,.1f} <span style="font-size:11px; font-weight:normal;">ha</span></div>
      </div>
      <div class="stat-item">
        <div class="stat-label">Mean Density</div>
        <div class="stat-val" style="color:#fbbf24;">{mean_density:,.1f} <span style="font-size:11px; font-weight:normal;">tC/ha</span></div>
      </div>
    </div>
  </div>

  <div class="legend-panel">
    <div class="legend-title">
      <span>Carbon Density</span>
      <span style="color:#34d399;">tC / ha</span>
    </div>
    <div class="legend-bar"></div>
    <div class="legend-labels">
      <span>0.1</span>
      <span>30</span>
      <span>60</span>
      <span>90</span>
      <span>120+</span>
    </div>
  </div>

  <script>
    // Register PMTiles protocol for hardware-accelerated streaming
    const protocol = new pmtiles.Protocol();
    maplibregl.addProtocol("pmtiles", protocol.tile);

    const map = new maplibregl.Map({{
      container: 'map',
      style: {{
        version: 8,
        sources: {{
          'esri-satellite': {{
            type: 'raster',
            tiles: ['https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}'],
            tileSize: 256,
            attribution: '&copy; Esri, Maxar, Earthstar Geographics'
          }},
          'carbon-tiles': {{
            type: 'vector',
            url: 'pmtiles://lake_district_carbon.pmtiles'
          }}
        }},
        layers: [
          {{
            id: 'satellite-base',
            type: 'raster',
            source: 'esri-satellite'
          }},
          {{
            id: 'carbon-cells-layer',
            type: 'fill',
            source: 'carbon-tiles',
            'source-layer': 'carbon_cells',
            paint: {{
              'fill-color': [
                'interpolate',
                ['linear'],
                ['get', 'carbon_density_ha'],
                0, '#d1fae5',
                20, '#6ee7b7',
                40, '#10b981',
                70, '#059669',
                100, '#047857',
                130, '#064e3b'
              ],
              'fill-opacity': 0.85,
              'fill-outline-color': 'rgba(0, 0, 0, 0.15)'
            }}
          }},
          {{
            id: 'carbon-cells-highlight',
            type: 'line',
            source: 'carbon-tiles',
            'source-layer': 'carbon_cells',
            paint: {{
              'line-color': '#f8fafc',
              'line-width': 2,
              'line-opacity': 0.0
            }}
          }}
        ]
      }},
      center: [-3.12, 54.48],
      zoom: 10,
      maxZoom: 17,
      minZoom: 8
    }});

    map.addControl(new maplibregl.NavigationControl(), 'top-right');
    map.addControl(new maplibregl.ScaleControl({{ maxWidth: 100, unit: 'metric' }}), 'bottom-left');

    const popup = new maplibregl.Popup({{
      closeButton: false,
      closeOnClick: false
    }});

    map.on('mousemove', 'carbon-cells-layer', (e) => {{
      map.getCanvas().style.cursor = 'pointer';
      if (e.features.length > 0) {{
        const p = e.features[0].properties;
        popup.setLngLat(e.lngLat)
          .setHTML(`
            <div style="min-width: 190px;">
              <div style="display:flex; justify-content:space-between; align-items:center; border-bottom: 1px solid #334155; padding-bottom: 4px; margin-bottom: 6px;">
                <strong style="color: #34d399; font-size: 13px;">${{p.woodland_type}}</strong>
                <span style="background: #1e293b; padding: 2px 6px; border-radius: 4px; font-size: 10px; font-family: monospace; color: #94a3b8;">20m cell</span>
              </div>
              <div style="font-size: 12px; line-height: 1.5; color: #cbd5e1;">
                <div><strong>Carbon Density:</strong> <span style="color:#f59e0b; font-weight:bold;">${{p.carbon_density_ha}} tC/ha</span></div>
                <div><strong>Cell Carbon:</strong> <span style="color:#34d399; font-weight:bold;">${{p.carbon_tonnes}} tC</span></div>
                <div><strong>CO₂ Equivalent:</strong> <span style="color:#2dd4bf;">${{p.co2e_tonnes}} tCO₂e</span></div>
                <div><strong>Canopy Height (h90):</strong> ${{p.h90}} m</div>
                <div><strong>Timber Volume:</strong> ${{p.volume_ha}} m³/ha</div>
              </div>
            </div>
          `)
          .addTo(map);
      }}
    }});

    map.on('mouseleave', 'carbon-cells-layer', () => {{
      map.getCanvas().style.cursor = '';
      popup.remove();
    }});
  </script>
</body>
</html>
'''

with open(OUT_HTML, 'w') as f:
    f.write(html_content)

print(f"All done! You can open {OUT_HTML} in your web browser.")
print(f"Total Lake District Stored Carbon: {total_c:,.1f} tonnes C ({total_co2e:,.1f} tonnes CO2e)")
