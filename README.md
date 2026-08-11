<div align="center">

<img src="assets/logo.png" alt="Kestrel" width="150">

# Kestrel

**A sharp eye on your geospatial data.**

</div>

We've all had this moment: you drag a layer into QGIS and… nothing. No error, no shape on the
map — just empty space. **Kestrel tells you why.**

Drop a file on it and it reads back the **CRS / UTM zone**, the data's **real-world location**,
and a plain-English list of anything that would stop it from drawing. A quick, friendly sanity
check for the everyday *"wait, where did my layer go?"* — and it never changes your data, it
just takes a careful look.

<div align="center">
<img src="assets/screenshot.png" alt="Kestrel inspecting a GeoPackage in UTM zone 11N" width="540">
</div>

## What it tells you

- **Format & driver** (Shapefile, GeoPackage, GeoJSON, GeoTIFF, …)
- **CRS** — name, EPSG code, **UTM zone**, projected vs. geographic, units, datum, and the
  region the CRS is valid for
- **Location (WGS84)** — the extent reprojected to lon/lat, so you can confirm the data lands
  where you expect on Earth
- **Map preview** — a globe showing where on Earth the data sits, next to a zoomed view of the
  extent. Offline by default (coastlines, lakes, borders and town names, so it works with no
  network at all); flick on **Satellite** for Esri World Imagery and zoom right in on the field.
  No API key, and it falls back to the offline map if there's no connection.
- **Details** — geometry type, feature count and fields (vector); size, bands, data type,
  pixel size and NoData (raster); native extent
- **Diagnostics** — common reasons a layer won't show up in QGIS:
  - No CRS defined / missing `.prj`
  - Coordinates that don't match the declared CRS (e.g. lat/lon tagged as a projected CRS —
    the classic "lands in the ocean")
  - Empty layers, zero-area extents, "Null Island" (0, 0) coordinates
  - Data outside the CRS's valid area, the wrong UTM hemisphere/zone, or an extent that
    wraps the antimeridian
  - Invalid / self-intersecting geometry, and layers in mismatched coordinate systems
  - Multiple layers in a GeoPackage (you may be loading the wrong one)

## …and now it can fix them

Spotting the problem is half the job. Kestrel can repair the common ones for you:

| Fix | What it does |
| --- | --- |
| **Set the CRS** | Tags a file with the coordinate system it's really in. Coordinates are not moved. |
| **Reproject** | Transforms the coordinates into another CRS. |
| **Fix geometry** | Repairs self-intersections and invalid rings. |
| **Convert** | Writes the data out as GeoPackage, Shapefile or GeoJSON. |
| **Table → points** | Turns a CSV/Excel coordinate table into a real point layer. |

**Your original file is never modified.** Every repair writes a *new* file into an output
folder you choose, and Kestrel shows you exactly what it's going to do before it writes
anything — then re-reads the result and tells you what it actually produced.

When a file has no CRS, Kestrel suggests likely candidates. Coordinates alone can't identify
a UTM zone — the same easting/northing is valid in all 60 — so it draws on context: other
files in the same folder that *do* declare a CRS, and the ones you've picked before. For the
usual case (one file in a delivery lost its `.prj`) that's often the exactly-right answer.
You can also type an EPSG code or search the EPSG database by name.

## Supported inputs

Zipped or plain **shapefiles**, **GeoPackage** (`.gpkg`, multi-layer), **GeoJSON**, KML/KMZ/GML/GPX,
**CSV / Excel** with coordinate columns (it finds the lon/lat or x/y and infers the CRS), and
**rasters** via rasterio (**GeoTIFF**, IMG, VRT, JPEG2000, …). Unknown extensions are tried as
vector first, then raster, so most things just work.

## Getting it

### Download (recommended)

Get it from the **[Kestrel website](https://dozer3530.github.io/Kestrel/)**, or straight from the
[**Releases**](../../releases) page (no Python required):

- **`KestrelSetup.exe`** — installer (recommended): per-user, no admin needed; adds a Start-Menu
  shortcut and an uninstaller.
- **`Kestrel-windows.zip`** — portable: unzip anywhere and run `Kestrel\Kestrel.exe`.

It's a one-folder build, so it starts fast (about a second) after the first launch. The app is
unsigned, so Windows SmartScreen may warn on first launch — choose *More info → Run anyway*.

The installer also adds a **right-click → "Inspect with Kestrel"** entry for geospatial files
(on Windows 11 it's under *Show more options*). Portable/zip users can enable it once with
`Kestrel.exe --register` (and remove it with `--unregister`).

### Run from source

- **GUI:** double-click **`run.bat`**, or run `py gui.py`.
  (Right-click `run.bat` → *Send to → Desktop* to make a desktop shortcut.)
- **Command line:** `py cli.py path\to\data.gpkg` prints the same report in the terminal.

Needs Python 3.10+ and the packages in [`requirements.txt`](requirements.txt):

```
py -m pip install -r requirements.txt
```

### Build the .exe yourself

```
py -m pip install pyinstaller pyinstaller-hooks-contrib
build.bat            REM  ->  dist\Kestrel\Kestrel.exe   (one-folder app)
build_installer.bat  REM  ->  dist\KestrelSetup.exe      (needs Inno Setup 6)
```

The build recipe is [`Kestrel.spec`](Kestrel.spec) (bundles the GDAL/PROJ data) and the installer
script is [`Kestrel.iss`](Kestrel.iss).


## License

[MIT](LICENSE) © 2026 Dozer3530
