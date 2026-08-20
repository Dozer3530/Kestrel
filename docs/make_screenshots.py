"""Generate the screenshots used in the Kestrel user guide."""
import json
import os
import sys
import tempfile
import time

PROJ = r"C:\Users\zkomarnisky\GIT\GeoSpatial_Data_Detector"
IMG = os.path.join(PROJ, "docs", "images")
sys.path.insert(0, PROJ)

import laspy
import numpy as np
import rasterio
import shapely
from PIL import Image
from pyogrio.raw import write as raw_write
from pyproj import CRS
from rasterio.transform import from_origin
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

import gui
import mapview

os.makedirs(IMG, exist_ok=True)
app = QApplication([])
gui.apply_theme(app)
tmp = tempfile.mkdtemp(prefix="kguide_")


def trim(path, pad=24):
    im = Image.open(path).convert("RGB")
    px = im.load()
    bg = px[5, 5]
    last = im.height - 1
    for y in range(im.height - 1, -1, -1):
        if any(sum(abs(a - b) for a, b in zip(px[x, y], bg)) > 24
               for x in range(0, im.width, 7)):
            last = y
            break
    im.crop((0, 0, im.width, min(im.height, last + pad))).save(path)


def shot(path, name, h=1000, w=880, wait_tiles=False):
    win = gui.MainWindow()
    win.setAttribute(Qt.WA_DontShowOnScreen, True)
    win.resize(w, h)
    win.show()
    if path:
        win.load_path(path)
    if wait_tiles:
        end = time.time() + 22
        while time.time() < end:
            app.processEvents()
            time.sleep(0.05)
            f = mapview.tile_fetcher()
            if f.memory and not f.pending:
                break
    for _ in range(30):
        app.processEvents()
        time.sleep(0.02)
    out = os.path.join(IMG, name)
    win.grab().save(out, "PNG")
    trim(out)
    print("  ", name, Image.open(out).size)
    return win


def gpkg(path, coords, epsg, layer="data"):
    geoms = [shapely.Point(*c) for c in coords]
    crops = np.array(["canola", "wheat", "barley", "canola"][:len(coords)], dtype=object)
    raw_write(path, geometry=shapely.to_wkb(np.array(geoms, dtype=object)),
              field_data=[np.arange(1, len(coords) + 1, dtype="int64"), crops],
              fields=["site_id", "crop"], geometry_type="Point",
              crs=(f"EPSG:{epsg}" if epsg else None), driver="GPKG", layer=layer)


TEMP_GDB = "/".join(["C:", "Users", "me", "AppData", "Local", "Temp",
                     "ArcGISProTemp9", "Untitled", "Default.gdb"])
LYRX_DOC = {
    "type": "CIMLayerDocument", "version": "3.6.0",
    "layerDefinitions": [{
        "type": "CIMFeatureLayer", "name": "Soil Plots", "visibility": True,
        "featureTable": {
            "definitionExpression": '"OBJECTID" <> 1 AND "OBJECTID" <> 2',
            "dataConnection": {
                "type": "CIMStandardDataConnection",
                "workspaceConnectionString": "DATABASE=" + TEMP_GDB,
                "workspaceFactory": "FileGDB", "dataset": "Soil_Plots"}}}]}

good = os.path.join(tmp, "survey_sites.gpkg")
gpkg(good, [(512345, 5651234), (514500, 5655000), (509800, 5660500), (517000, 5657400)],
     32611, "survey_sites")
bad = os.path.join(tmp, "field_boundaries.gpkg")
gpkg(bad, [(512345, 5651234), (514500, 5655000)], None, "boundaries")

hdr = laspy.LasHeader(version="1.4", point_format=6)
hdr.add_crs(CRS.from_epsg(32611))
las = laspy.LasData(hdr)
rng = np.random.default_rng(5)
n = 25000
las.x = 512000 + rng.random(n) * 380
las.y = 5651000 + rng.random(n) * 260
las.z = 1010 + rng.random(n) * 32
cloud = os.path.join(tmp, "drone_survey.las")
las.write(cloud)

lyrx = os.path.join(tmp, "soil_plots.lyrx")
json.dump(LYRX_DOC, open(lyrx, "w", encoding="utf-8-sig"))

dely = os.path.join(tmp, "delivery")
os.makedirs(dely, exist_ok=True)
gpkg(os.path.join(dely, "fields_2026.gpkg"), [(512345, 5651234), (514500, 5655000)],
     26911, "fields")
gpkg(os.path.join(dely, "no_crs_plots.gpkg"), [(512345, 5651234)], None, "plots")
gpkg(os.path.join(dely, "empty_export.gpkg"), [], 4326, "x")
json.dump(LYRX_DOC, open(os.path.join(dely, "soil_plots.lyrx"), "w", encoding="utf-8-sig"))
open(os.path.join(dely, "sample_points.csv"), "w").write(
    "id,longitude,latitude\n1,-114.05,51.10\n2,-114.02,51.05\n")
with rasterio.open(os.path.join(dely, "ortho_2026.tif"), "w", driver="GTiff",
                   height=5200, width=5200, count=1, dtype="uint8", crs="EPSG:26911",
                   transform=from_origin(512000, 5660000, 0.05, 0.05)) as d:
    d.write(np.zeros((5200, 5200), dtype="uint8"), 1)

print("screenshots:")
shot(None, "01-empty.png", h=560)
shot(good, "02-report.png", h=1180, wait_tiles=True)
shot(bad, "03-problems.png", h=1000)
shot(cloud, "04-pointcloud.png", h=1000)
shot(lyrx, "05-lyrx.png", h=760)
win = shot(dely, "06-batch.png", h=900, w=1180)

rep = gui.inspect_path(bad)
pick = gui.CrsPicker(rep, win)
pick.setAttribute(Qt.WA_DontShowOnScreen, True)
pick.resize(660, 560)
pick.show()
for _ in range(25):
    app.processEvents()
    time.sleep(0.02)
p = os.path.join(IMG, "07-crspicker.png")
pick.grab().save(p, "PNG")
print("   07-crspicker.png", Image.open(p).size)
print("DONE ->", IMG)
