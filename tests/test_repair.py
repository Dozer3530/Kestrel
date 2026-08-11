"""Tests for the repair engine — every repair is verified by re-inspecting its output.

    py tests\\test_repair.py
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import shapely
from pyogrio.raw import write as raw_write

from kestrel import repair
from kestrel.inspector import inspect_path


def _write_gpkg(path, geoms, crs, layer="x"):
    raw_write(path, geometry=shapely.to_wkb(np.array(geoms, dtype=object)),
              field_data=[np.arange(1, len(geoms) + 1, dtype="int64")], fields=["id"],
              geometry_type=geoms[0].geom_type, crs=crs, driver="GPKG", layer=layer)


def _run(report, op, out, **kw):
    plan = repair.plan_repair(report, op, out, **kw)
    assert plan.ok, f"plan blocked: {plan.blocker}"
    res = repair.apply_repair(report, plan, **kw)
    assert res.ok, f"repair failed: {res.message}"
    return res


def test_assign_crs_keeps_coordinates(tmp):
    """Assigning a CRS tags the file without moving anything."""
    src = os.path.join(tmp, "nocrs.gpkg")
    pts = [shapely.Point(500000, 5650000), shapely.Point(505000, 5655000)]
    _write_gpkg(src, pts, None)
    rep = inspect_path(src)
    assert not rep.layers[0].crs.defined

    out = os.path.join(tmp, "out_assign")
    res = _run(rep, repair.ASSIGN_CRS, out, epsg=32611)

    after = inspect_path(res.target)
    assert after.layers[0].crs.epsg == 32611, after.layers[0].crs.summary
    assert after.layers[0].native_bounds == rep.layers[0].native_bounds, "coordinates moved!"
    assert os.path.exists(src), "original must still exist"
    assert not inspect_path(src).layers[0].crs.defined, "original must be untouched"
    print("OK  test_assign_crs_keeps_coordinates")


def test_assign_crs_shapefile_prj_is_readable(tmp):
    """The .prj GDAL writes must be readable back (WKT2 would give crs=None)."""
    sdir = os.path.join(tmp, "shpsrc"); os.makedirs(sdir)
    src = os.path.join(sdir, "pts.shp")
    raw_write(src, geometry=shapely.to_wkb(np.array([shapely.Point(500000, 5650000)],
                                                    dtype=object)),
              field_data=[np.array([1], dtype="int64")], fields=["id"],
              geometry_type="Point", crs=None, driver="ESRI Shapefile")
    rep = inspect_path(src)
    out = os.path.join(tmp, "out_shp")
    res = _run(rep, repair.ASSIGN_CRS, out, epsg=26911)

    after = inspect_path(res.target)
    assert after.layers[0].crs.epsg == 26911, f"prj unreadable: {after.layers[0].crs.summary}"
    assert os.path.exists(os.path.splitext(res.target)[0] + ".prj"), "no .prj written"
    print("OK  test_assign_crs_shapefile_prj_is_readable")


def test_esri_wkt_flavour():
    """esri_wkt() must emit WKT1_ESRI — pyproj's default WKT2 reads back as no CRS."""
    wkt = repair.esri_wkt("EPSG:26911")
    assert wkt.startswith("PROJCS"), wkt[:40]
    print("OK  test_esri_wkt_flavour")


def test_reproject_moves_coordinates(tmp):
    src = os.path.join(tmp, "wgs.gpkg")
    _write_gpkg(src, [shapely.Point(-114.0, 51.0), shapely.Point(-113.5, 51.5)], "EPSG:4326")
    rep = inspect_path(src)
    out = os.path.join(tmp, "out_proj")
    res = _run(rep, repair.REPROJECT, out, epsg=32611)

    after = inspect_path(res.target)
    assert after.layers[0].crs.epsg == 32611
    xmin = after.layers[0].native_bounds[0]
    assert xmin > 10000, f"expected projected metres, got {xmin}"
    assert after.layers[0].location.available
    assert 50 < after.layers[0].location.center_lat < 52, "reprojected to the wrong place"
    print("OK  test_reproject_moves_coordinates")


def test_reproject_without_crs_is_blocked(tmp):
    src = os.path.join(tmp, "nocrs2.gpkg")
    _write_gpkg(src, [shapely.Point(1, 1)], None)
    plan = repair.plan_repair(inspect_path(src), repair.REPROJECT,
                              os.path.join(tmp, "o"), epsg=4326)
    assert not plan.ok and "assign" in (plan.blocker or "").lower(), plan.blocker
    print("OK  test_reproject_without_crs_is_blocked")


def test_fix_invalid_geometry(tmp):
    src = os.path.join(tmp, "bad.gpkg")
    bowtie = shapely.from_wkt("POLYGON((0 0,1 1,1 0,0 1,0 0))")
    _write_gpkg(src, [bowtie], "EPSG:4326")
    rep = inspect_path(src)
    assert rep.layers[0].invalid_geometry_count == 1

    out = os.path.join(tmp, "out_fix")
    res = _run(rep, repair.FIX_GEOMETRY, out)
    after = inspect_path(res.target)
    assert after.layers[0].invalid_geometry_count == 0, "geometry still invalid"
    print("OK  test_fix_invalid_geometry")


def test_fix_geometry_blocked_when_clean(tmp):
    src = os.path.join(tmp, "good.gpkg")
    _write_gpkg(src, [shapely.from_wkt("POLYGON((0 0,0 1,1 1,1 0,0 0))")], "EPSG:4326")
    plan = repair.plan_repair(inspect_path(src), repair.FIX_GEOMETRY, os.path.join(tmp, "o"))
    assert not plan.ok, "should refuse to 'fix' valid geometry"
    print("OK  test_fix_geometry_blocked_when_clean")


def test_convert_to_geojson_is_wgs84(tmp):
    src = os.path.join(tmp, "utm.gpkg")
    _write_gpkg(src, [shapely.Point(500000, 5650000)], "EPSG:32611")
    rep = inspect_path(src)
    out = os.path.join(tmp, "out_gj")
    res = _run(rep, repair.CONVERT, out, target_format="geojson")

    after = inspect_path(res.target)
    assert res.target.endswith(".geojson")
    assert after.layers[0].crs.epsg == 4326, after.layers[0].crs.summary
    b = after.layers[0].native_bounds
    assert -180 <= b[0] <= 180 and -90 <= b[1] <= 90, b
    print("OK  test_convert_to_geojson_is_wgs84")


def test_convert_to_gpkg_and_shp(tmp):
    src = os.path.join(tmp, "src2.gpkg")
    _write_gpkg(src, [shapely.Point(500000, 5650000)], "EPSG:32611")
    rep = inspect_path(src)
    res = _run(rep, repair.CONVERT, os.path.join(tmp, "out_shp2"), target_format="shp")
    after = inspect_path(res.target)
    assert res.target.endswith(".shp") and after.layers[0].crs.epsg == 32611
    print("OK  test_convert_to_gpkg_and_shp")


def test_csv_to_points(tmp):
    src = os.path.join(tmp, "sites.csv")
    with open(src, "w", encoding="utf-8", newline="") as fh:
        fh.write("id,longitude,latitude\n1,-114.05,51.10\n2,-114.00,51.00\nbad,x,y\n")
    rep = inspect_path(src)
    assert rep.layers[0].coord_columns == ("longitude", "latitude")

    out = os.path.join(tmp, "out_pts")
    res = _run(rep, repair.TABLE_TO_POINTS, out, epsg=4326)
    after = inspect_path(res.target)
    assert after.layers[0].geometry_type == "Point"
    assert after.layers[0].feature_count == 2, "should skip the unparseable row"
    assert after.layers[0].crs.epsg == 4326
    print("OK  test_csv_to_points")


def test_utf16_csv_is_readable(tmp):
    """A UTF-16 export (common from ArcGIS/Excel) must not become mojibake."""
    src = os.path.join(tmp, "utf16.csv")
    with open(src, "w", encoding="utf-16", newline="") as fh:
        fh.write("id,longitude,latitude\n1,-114.05,51.10\n")
    rep = inspect_path(src)
    assert rep.layers[0].coord_columns == ("longitude", "latitude"), rep.layers[0].fields
    assert rep.layers[0].location.available
    print("OK  test_utf16_csv_is_readable")


def test_output_never_overwrites(tmp):
    src = os.path.join(tmp, "dup.gpkg")
    _write_gpkg(src, [shapely.Point(500000, 5650000)], None)
    rep = inspect_path(src)
    out = os.path.join(tmp, "out_dup")
    a = _run(rep, repair.ASSIGN_CRS, out, epsg=32611)
    b = _run(rep, repair.ASSIGN_CRS, out, epsg=32611)
    assert a.target != b.target, "second repair overwrote the first"
    assert os.path.exists(a.target) and os.path.exists(b.target)
    print("OK  test_output_never_overwrites")


def test_failed_repair_leaves_no_partial_file(tmp):
    """A repair that can't complete must not leave a readable, truncated dataset."""
    src = os.path.join(tmp, "far.gpkg")
    _write_gpkg(src, [shapely.Point(-114.0, 51.0)], "EPSG:4326")
    rep = inspect_path(src)
    out = os.path.join(tmp, "out_fail")
    # UTM zone 60N on Alberta data -> coordinates outside the target's domain
    plan = repair.plan_repair(rep, repair.REPROJECT, out, epsg=32660)
    res = repair.apply_repair(rep, plan, epsg=32660)
    leftovers = [f for f in os.listdir(out)] if os.path.isdir(out) else []
    if not res.ok:
        assert not leftovers, f"partial output left behind: {leftovers}"
        print("OK  test_failed_repair_leaves_no_partial_file (refused cleanly)")
    else:
        after = inspect_path(res.target)
        assert not after.error, "wrote an unreadable file"
        print("OK  test_failed_repair_leaves_no_partial_file (succeeded, output valid)")


def main():
    tmp = tempfile.mkdtemp(prefix="kestrel_repair_")
    try:
        test_esri_wkt_flavour()
        test_assign_crs_keeps_coordinates(tmp)
        test_assign_crs_shapefile_prj_is_readable(tmp)
        test_reproject_moves_coordinates(tmp)
        test_reproject_without_crs_is_blocked(tmp)
        test_fix_invalid_geometry(tmp)
        test_fix_geometry_blocked_when_clean(tmp)
        test_convert_to_geojson_is_wgs84(tmp)
        test_convert_to_gpkg_and_shp(tmp)
        test_csv_to_points(tmp)
        test_utf16_csv_is_readable(tmp)
        test_output_never_overwrites(tmp)
        test_failed_repair_leaves_no_partial_file(tmp)
        print("\nALL REPAIR TESTS PASSED")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
