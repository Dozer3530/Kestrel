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
- **Details** — geometry type, feature count and fields (vector); size, bands, data type,
  pixel size and NoData (raster); native extent
- **Diagnostics** — common reasons a layer won't show up in QGIS:
  - No CRS defined / missing `.prj`
  - Coordinates that don't match the declared CRS (e.g. lat/lon tagged as a projected CRS —
    the classic "lands in the ocean")
  - Empty layers, zero-area extents, "Null Island" (0, 0) coordinates
  - Data sitting well outside the CRS's valid area
  - Multiple layers in a GeoPackage (you may be loading the wrong one)

## Supported inputs

Zipped or plain **shapefiles**, **GeoPackage** (`.gpkg`, multi-layer), **GeoJSON**, KML/GML/GPX,
and **rasters** via rasterio (**GeoTIFF**, IMG, VRT, JPEG2000, …). Unknown extensions are tried
as vector first, then raster, so most things just work.

## Getting it

### Download (recommended)

Get it from the **[Kestrel website](https://dozer3530.github.io/Kestrel/)**, or straight from the
[**Releases**](../../releases) page (no Python required):

- **`KestrelSetup.exe`** — installer (recommended): per-user, no admin needed; adds a Start-Menu
  shortcut and an uninstaller.
- **`Kestrel-windows.zip`** — portable: unzip anywhere and run `Kestrel\Kestrel.exe`.

It's a one-folder build, so it starts fast (about a second) after the first launch. The app is
unsigned, so Windows SmartScreen may warn on first launch — choose *More info → Run anyway*.

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
