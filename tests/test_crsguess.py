"""Tests for the CRS suggestion engine.

    py tests\\test_crsguess.py
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import shapely
from pyogrio.raw import write as raw_write

from kestrel.crsguess import latitude_from_northing, search_crs, suggest_crs
from kestrel.inspector import inspect_path


def _write(path, coords, crs, driver="GPKG"):
    kw = {"layer": "x"} if driver == "GPKG" else {}
    raw_write(path,
              geometry=shapely.to_wkb(np.array([shapely.Point(*c) for c in coords],
                                               dtype=object)),
              field_data=[np.arange(1, len(coords) + 1, dtype="int64")], fields=["id"],
              geometry_type="Point", crs=crs, driver=driver, **kw)


def test_lonlat_is_high_confidence(tmp):
    p = os.path.join(tmp, "ll.gpkg")
    _write(p, [(-114.0, 51.0), (-113.5, 51.5)], None)
    cands = suggest_crs(inspect_path(p), use_siblings=False)
    assert cands and cands[0].epsg == 4326 and cands[0].confidence == "high", cands
    print("OK  test_lonlat_is_high_confidence ->", cands[0].label)


def test_sibling_file_supplies_the_zone(tmp):
    """The real case: a delivery where one shapefile lost its .prj."""
    folder = os.path.join(tmp, "delivery")
    os.makedirs(folder)
    # a correctly-tagged neighbour
    _write(os.path.join(folder, "fields_ok.gpkg"), [(500000, 5650000)], "EPSG:26911")
    # the casualty: same grid, no CRS
    bad = os.path.join(folder, "fields_broken.gpkg")
    _write(bad, [(501000, 5651000), (502000, 5652000)], None)

    cands = suggest_crs(inspect_path(bad))
    assert cands, "no suggestions at all"
    top = cands[0]
    assert top.epsg == 26911, [c.label for c in cands]
    assert top.confidence == "high", top
    assert "same folder" in top.reason, top.reason
    print("OK  test_sibling_file_supplies_the_zone ->", top.label, "|", top.confidence)


def test_recent_crs_used_when_no_siblings(tmp):
    folder = os.path.join(tmp, "lonely")
    os.makedirs(folder)
    p = os.path.join(folder, "solo.gpkg")
    _write(p, [(500000, 5650000)], None)
    cands = suggest_crs(inspect_path(p), recent=[32611], use_siblings=False)
    assert any(c.epsg == 32611 for c in cands), [c.label for c in cands]
    print("OK  test_recent_crs_used_when_no_siblings ->", cands[0].label)


def test_no_context_explains_the_ambiguity(tmp):
    """With nothing to go on, say why the zone is unknowable rather than guessing."""
    folder = os.path.join(tmp, "bare")
    os.makedirs(folder)
    p = os.path.join(folder, "bare.gpkg")
    _write(p, [(500000, 5650000)], None)
    cands = suggest_crs(inspect_path(p), use_siblings=False)
    assert cands, "should still say something useful"
    assert "zone" in cands[0].reason.lower(), cands[0].reason
    print("OK  test_no_context_explains_the_ambiguity ->", cands[0].reason[:60], "...")


def test_latitude_from_northing():
    assert 50 < latitude_from_northing(5650000) < 52
    assert -1 < latitude_from_northing(0) < 1
    print("OK  test_latitude_from_northing")


def test_search_by_code_and_name():
    exact = search_crs("26911")
    assert exact and exact[0].epsg == 26911, exact
    assert search_crs("EPSG:4326")[0].epsg == 4326
    named = search_crs("NAD83 UTM zone 11N")
    assert any(c.epsg == 26911 for c in named), [c.label for c in named[:5]]
    assert search_crs("") == []
    print("OK  test_search_by_code_and_name ->", exact[0].label)


def main():
    tmp = tempfile.mkdtemp(prefix="kestrel_crs_")
    try:
        test_latitude_from_northing()
        test_lonlat_is_high_confidence(tmp)
        test_sibling_file_supplies_the_zone(tmp)
        test_recent_crs_used_when_no_siblings(tmp)
        test_no_context_explains_the_ambiguity(tmp)
        test_search_by_code_and_name()
        print("\nALL CRS-GUESS TESTS PASSED")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
