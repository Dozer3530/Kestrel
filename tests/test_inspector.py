"""Self-contained tests for the inspector core.

Runs with plain Python (no pytest required):

    py tests\\test_inspector.py

It is also pytest-compatible (`py -m pytest tests`). Fixtures are generated in a
temp directory at runtime, so no sample data needs to ship with the project.
"""

import os
import shutil
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import geopandas as gpd
from shapely.geometry import Point, Polygon

from kestrel.inspector import analyze_crs, inspect_path


def _titles(report):
    return {d.title for d in report.diagnostics}


def test_utm_geopackage(tmp):
    """A GeoPackage in UTM zone 11N: CRS, UTM zone, units, count, and location."""
    path = os.path.join(tmp, "utm.gpkg")
    gdf = gpd.GeoDataFrame(
        {"id": [1, 2], "name": ["a", "b"]},
        geometry=[Point(500000, 5650000), Point(510000, 5660000)],
        crs="EPSG:32611",
    )
    gdf.to_file(path, driver="GPKG", layer="pts")

    report = inspect_path(path)
    assert report.is_vector and not report.error, report.error
    layer = report.layers[0]
    assert layer.crs.defined
    assert layer.crs.epsg == 32611
    assert layer.crs.utm_zone == "11N"
    assert layer.crs.is_projected
    assert "metre" in (layer.crs.unit or "").lower()
    assert layer.feature_count == 2
    assert layer.location.available
    assert 50 < layer.location.center_lat < 52, layer.location.center_lat
    assert -118 < layer.location.center_lon < -116, layer.location.center_lon
    # A correctly-tagged, well-located file should raise no errors/warnings.
    assert not any(d.severity in ("error", "warning") for d in report.diagnostics), _titles(report)
    print("OK  test_utm_geopackage")


def test_no_crs(tmp):
    """A GeoPackage with no CRS triggers the 'No CRS defined' diagnostic."""
    path = os.path.join(tmp, "nocrs.gpkg")
    gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[Point(10, 10)], crs=None)
    gdf.to_file(path, driver="GPKG", layer="x")

    report = inspect_path(path)
    layer = report.layers[0]
    assert not layer.crs.defined
    assert "No CRS defined" in _titles(report), _titles(report)
    assert not layer.location.available
    print("OK  test_no_crs")


def test_zipped_shapefile_without_prj(tmp):
    """A zipped shapefile missing its .prj triggers the 'Missing .prj' diagnostic."""
    sdir = os.path.join(tmp, "shp")
    os.makedirs(sdir)
    base = os.path.join(sdir, "pts")
    gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[Point(500000, 5650000)], crs="EPSG:32611")
    gdf.to_file(base + ".shp", driver="ESRI Shapefile")

    zpath = os.path.join(tmp, "noprj.zip")
    with zipfile.ZipFile(zpath, "w") as zf:
        for ext in (".shp", ".shx", ".dbf"):   # deliberately omit .prj
            zf.write(base + ext, arcname="pts" + ext)

    report = inspect_path(zpath)
    assert report.is_vector, report.error
    assert "Missing .prj (no CRS)" in _titles(report), _titles(report)
    print("OK  test_zipped_shapefile_without_prj")


def test_unit_mismatch(tmp):
    """Lon/lat-range coordinates tagged with a metre-based CRS -> mismatch warning."""
    path = os.path.join(tmp, "mismatch.gpkg")
    gdf = gpd.GeoDataFrame(
        {"id": [1, 2]},
        geometry=[Point(-114.0, 51.0), Point(-113.0, 52.0)],
        crs="EPSG:32611",  # projected/metres, but the coords look like degrees
    )
    gdf.to_file(path, driver="GPKG", layer="x")

    report = inspect_path(path)
    assert "Possible CRS / coordinate mismatch" in _titles(report), _titles(report)
    print("OK  test_unit_mismatch")


def test_zipped_shapefile_with_prj(tmp):
    """A complete zipped shapefile reads cleanly via the /vsizip path."""
    sdir = os.path.join(tmp, "shp2")
    os.makedirs(sdir)
    base = os.path.join(sdir, "ok")
    gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[Point(500000, 5650000)], crs="EPSG:32611")
    gdf.to_file(base + ".shp", driver="ESRI Shapefile")

    zpath = os.path.join(tmp, "ok.zip")
    with zipfile.ZipFile(zpath, "w") as zf:
        for ext in (".shp", ".shx", ".dbf", ".prj"):
            zf.write(base + ext, arcname="ok" + ext)

    report = inspect_path(zpath)
    assert report.is_vector, report.error
    assert report.layers[0].crs.epsg == 32611
    assert "Missing .prj (no CRS)" not in _titles(report)
    print("OK  test_zipped_shapefile_with_prj")


def test_csv_latlon(tmp):
    """CSV with longitude/latitude columns is detected, located, and guided."""
    path = os.path.join(tmp, "sites.csv")
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("id,longitude,latitude\n1,-114.05,51.10\n2,-114.00,51.00\n")
    report = inspect_path(path)
    assert report.is_vector and report.driver == "CSV", report.error
    layer = report.layers[0]
    assert layer.geometry_type == "Point"
    assert layer.feature_count == 2
    assert layer.coord_columns == ("longitude", "latitude")
    assert layer.location.available and 50 < layer.location.center_lat < 52
    assert "Coordinates found" in _titles(report)
    print("OK  test_csv_latlon")


def test_csv_no_coords(tmp):
    """A CSV with no recognizable coordinate columns says so."""
    path = os.path.join(tmp, "table.csv")
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("id,name,population\n1,Calgary,1306000\n")
    report = inspect_path(path)
    assert "No coordinate columns found" in _titles(report), _titles(report)
    print("OK  test_csv_no_coords")


def test_invalid_geometry(tmp):
    """A self-intersecting polygon is flagged as invalid geometry."""
    path = os.path.join(tmp, "invalid.gpkg")
    bowtie = Polygon([(0, 0), (1, 1), (1, 0), (0, 1), (0, 0)])
    gpd.GeoDataFrame({"id": [1]}, geometry=[bowtie], crs="EPSG:4326").to_file(
        path, driver="GPKG", layer="x")
    report = inspect_path(path)
    assert report.layers[0].invalid_geometry_count == 1, report.layers[0].invalid_geometry_count
    assert "Invalid geometry" in _titles(report), _titles(report)
    print("OK  test_invalid_geometry")


def test_mixed_crs_layers(tmp):
    """A GeoPackage whose two layers use different CRSs is flagged."""
    path = os.path.join(tmp, "mixed.gpkg")
    gpd.GeoDataFrame({"id": [1]}, geometry=[Point(-114, 51)], crs="EPSG:4326").to_file(
        path, driver="GPKG", layer="wgs84")
    gpd.GeoDataFrame({"id": [1]}, geometry=[Point(500000, 5650000)], crs="EPSG:32611").to_file(
        path, driver="GPKG", layer="utm")
    report = inspect_path(path)
    assert len(report.layers) == 2, [l.name for l in report.layers]
    assert "Layers use different coordinate systems" in _titles(report), _titles(report)
    print("OK  test_mixed_crs_layers")


def test_utm_hemisphere_mismatch(tmp):
    """Data tagged UTM 11N but sitting in the southern hemisphere is flagged."""
    path = os.path.join(tmp, "hemi.gpkg")
    gpd.GeoDataFrame(
        {"id": [1, 2]},
        geometry=[Point(500000, -1000000), Point(505000, -1010000)],  # negative northing -> south
        crs="EPSG:32611",
    ).to_file(path, driver="GPKG", layer="x")
    report = inspect_path(path)
    assert "UTM zone is the wrong hemisphere" in _titles(report), _titles(report)
    print("OK  test_utm_hemisphere_mismatch")


def test_special_char_filename(tmp):
    """A filename with ';' and ',' (which GDAL truncates) still reads via the \\?\\ fix."""
    path = os.path.join(tmp, "AB;CD,EF.gpkg")
    gpd.GeoDataFrame({"id": [1]}, geometry=[Point(-114, 51)], crs="EPSG:4326").to_file(
        path, driver="GPKG", layer="pts")
    report = inspect_path(path)
    assert report.is_vector and not report.error, report.error
    assert report.layers and report.layers[0].feature_count == 1
    print("OK  test_special_char_filename")


def test_gdal_path_helper():
    from kestrel.inspector import _gdal_path
    if os.name == "nt":
        assert _gdal_path(r"C:\a;b\f.gpkg") == r"\\?\C:\a;b\f.gpkg"
        assert _gdal_path(r"\\srv\share\f.gpkg") == r"\\?\UNC\srv\share\f.gpkg"
        assert _gdal_path("/vsizip/x/y.shp") == "/vsizip/x/y.shp"  # virtual path untouched
    print("OK  test_gdal_path_helper")


def test_analyze_crs_none():
    assert not analyze_crs(None).defined
    info = analyze_crs("EPSG:4326")
    assert info.defined and info.is_geographic and info.epsg == 4326
    print("OK  test_analyze_crs_none")


def main():
    tmp = tempfile.mkdtemp(prefix="geodet_test_")
    try:
        test_utm_geopackage(tmp)
        test_no_crs(tmp)
        test_zipped_shapefile_without_prj(tmp)
        test_zipped_shapefile_with_prj(tmp)
        test_unit_mismatch(tmp)
        test_csv_latlon(tmp)
        test_csv_no_coords(tmp)
        test_invalid_geometry(tmp)
        test_mixed_crs_layers(tmp)
        test_utm_hemisphere_mismatch(tmp)
        test_special_char_filename(tmp)
        test_gdal_path_helper()
        test_analyze_crs_none()
        print("\nALL TESTS PASSED")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
