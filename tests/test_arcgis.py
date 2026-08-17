"""Tests for reading ArcGIS Pro layer files (.lyrx).

    py tests\\test_arcgis.py
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import shapely
from pyogrio.raw import write as raw_write

from kestrel.arcgis import resolve_source
from kestrel.inspector import inspect_path


def _titles(rep):
    return {d.title for d in rep.diagnostics}


def _lyrx(path, workspace, factory, dataset, query=None, visible=True, name="Test Layer"):
    table = {
        "type": "CIMFeatureTable",
        "dataConnection": {
            "type": "CIMStandardDataConnection",
            "workspaceConnectionString": f"DATABASE={workspace}",
            "workspaceFactory": factory,
            "dataset": dataset,
            "datasetType": "esriDTFeatureClass",
        },
    }
    if query:
        table["definitionExpression"] = query
    doc = {
        "type": "CIMLayerDocument",
        "version": "3.6.0",
        "layers": ["CIMPATH=test.json"],
        "layerDefinitions": [{
            "type": "CIMFeatureLayer",
            "name": name,
            "uRI": "CIMPATH=test.json",
            "visibility": visible,
            "featureTable": table,
        }],
    }
    with open(path, "w", encoding="utf-8-sig") as fh:
        json.dump(doc, fh, indent=2)
    return path


def _shapefile(folder, stem="pts"):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, stem + ".shp")
    raw_write(path,
              geometry=shapely.to_wkb(np.array([shapely.Point(500000, 5650000),
                                                shapely.Point(505000, 5655000)],
                                               dtype=object)),
              field_data=[np.array([1, 2], dtype="int64")], fields=["id"],
              geometry_type="Point", crs="EPSG:32611", driver="ESRI Shapefile")
    return path


def test_resolves_and_reads_the_real_source(tmp):
    """A layer pointing at data that exists gets the full Kestrel treatment."""
    folder = os.path.join(tmp, "data")
    _shapefile(folder)
    lyrx = _lyrx(os.path.join(tmp, "good.lyrx"), folder, "Shapefile", "pts")

    rep = inspect_path(lyrx)
    assert rep.is_layer_file and not rep.error, rep.error
    layer = rep.layers[0]
    assert layer.source_missing is False, layer.source_path
    assert layer.crs.epsg == 32611, layer.crs.summary
    assert layer.feature_count == 2
    assert layer.location.available
    print("OK  test_resolves_and_reads_the_real_source ->", layer.crs.summary)


def test_broken_source_is_an_error(tmp):
    # deliberately NOT under a temp folder, so this is the plain broken-link case
    missing = os.path.join("C:\\", "GIS_Projects", "NoSuchPlace", "Missing.gdb")
    lyrx = _lyrx(os.path.join(tmp, "broken.lyrx"), missing, "FileGDB", "Things")
    rep = inspect_path(lyrx)
    assert rep.layers[0].source_missing is True
    assert "Broken data source" in _titles(rep), _titles(rep)
    print("OK  test_broken_source_is_an_error")


def test_temp_scratch_gdb_is_called_out(tmp):
    """Pro's scratch geodatabase is the classic silently-lost-data trap."""
    ws = os.path.join("C:\\", "Users", "x", "AppData", "Local", "Temp",
                      "ArcGISProTemp1234", "Untitled", "Default.gdb")
    lyrx = _lyrx(os.path.join(tmp, "temp.lyrx"), ws, "FileGDB", "Points")
    rep = inspect_path(lyrx)
    assert "Broken link to a temporary geodatabase" in _titles(rep), _titles(rep)
    print("OK  test_temp_scratch_gdb_is_called_out")


def test_definition_query_is_flagged(tmp):
    folder = os.path.join(tmp, "data2")
    _shapefile(folder)
    lyrx = _lyrx(os.path.join(tmp, "query.lyrx"), folder, "Shapefile", "pts",
                 query='"OBJECTID" <> 1')
    rep = inspect_path(lyrx)
    assert rep.layers[0].definition_query == '"OBJECTID" <> 1'
    assert "A definition query is filtering this layer" in _titles(rep), _titles(rep)
    print("OK  test_definition_query_is_flagged")


def test_hidden_layer_is_noted(tmp):
    folder = os.path.join(tmp, "data3")
    _shapefile(folder)
    lyrx = _lyrx(os.path.join(tmp, "off.lyrx"), folder, "Shapefile", "pts", visible=False)
    rep = inspect_path(lyrx)
    assert "Layer is turned off" in _titles(rep), _titles(rep)
    print("OK  test_hidden_layer_is_noted")


def test_relative_workspace_resolves(tmp):
    """.lyrx files often store the workspace relative to themselves."""
    folder = os.path.join(tmp, "rel", "data")
    _shapefile(folder)
    lyrx = _lyrx(os.path.join(tmp, "rel", "relative.lyrx"), "data", "Shapefile", "pts")
    rep = inspect_path(lyrx)
    assert rep.layers[0].source_missing is False, rep.layers[0].source_path
    assert rep.layers[0].feature_count == 2
    print("OK  test_relative_workspace_resolves")


def test_service_connection_is_not_a_missing_file(tmp):
    doc = {
        "type": "CIMLayerDocument", "version": "3.6.0",
        "layerDefinitions": [{
            "type": "CIMFeatureLayer", "name": "Service", "visibility": True,
            "featureTable": {"dataConnection": {
                "type": "CIMStandardDataConnection",
                "workspaceConnectionString": "URL=https://example.com/arcgis/rest/services/x",
                "workspaceFactory": "SDE", "dataset": "x"}},
        }],
    }
    p = os.path.join(tmp, "svc.lyrx")
    with open(p, "w", encoding="utf-8-sig") as fh:
        json.dump(doc, fh)
    rep = inspect_path(p)
    assert rep.layers[0].source_missing is None, "a service is not a broken file"
    assert "Layer reads from a service or database" in _titles(rep), _titles(rep)
    print("OK  test_service_connection_is_not_a_missing_file")


def test_resolve_source_shapes():
    p, sub, kind = resolve_source(
        {"workspaceConnectionString": r"DATABASE=C:\d\x.gdb",
         "workspaceFactory": "FileGDB", "dataset": "Roads"}, r"C:\base")
    assert p.endswith("x.gdb") and sub == "Roads" and kind == "FileGDB", (p, sub, kind)
    p, sub, kind = resolve_source(
        {"workspaceConnectionString": r"DATABASE=C:\d",
         "workspaceFactory": "Shapefile", "dataset": "roads"}, r"C:\base")
    assert p.endswith("roads.shp") and kind == "Shapefile", p
    print("OK  test_resolve_source_shapes")


def test_garbage_file_fails_gracefully(tmp):
    p = os.path.join(tmp, "junk.lyrx")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("this is not json at all")
    rep = inspect_path(p)
    assert rep.error and "Could not read file" in _titles(rep), (rep.error, _titles(rep))
    print("OK  test_garbage_file_fails_gracefully")


def main():
    tmp = tempfile.mkdtemp(prefix="kestrel_lyrx_")
    try:
        test_resolve_source_shapes()
        test_resolves_and_reads_the_real_source(tmp)
        test_broken_source_is_an_error(tmp)
        test_temp_scratch_gdb_is_called_out(tmp)
        test_definition_query_is_flagged(tmp)
        test_hidden_layer_is_noted(tmp)
        test_relative_workspace_resolves(tmp)
        test_service_connection_is_not_a_missing_file(tmp)
        test_garbage_file_fails_gracefully(tmp)
        print("\nALL ARCGIS TESTS PASSED")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
