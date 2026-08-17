"""Read ArcGIS Pro layer files (``.lyrx``) and map files (``.mapx``).

These are Esri CIM documents — JSON that *references* data rather than containing it.
So the interesting questions are different from a normal geospatial file:

* Where does this layer actually point, and **is that data still there?**  A layer whose
  source has moved or been deleted is the red exclamation mark in ArcGIS Pro, and it's
  the single most common reason a layer shows nothing.
* Is the source a **scratch geodatabase**? ArcGIS Pro writes new features to a temporary
  ``Default.gdb`` unless you say otherwise, and that folder gets cleaned up — so the
  layer works today and is broken next week.
* Is there a **definition query** quietly filtering the features away?

When the source does resolve, Kestrel inspects it normally, so you get the real CRS and
extent of the data behind the layer.

ArcMap's older binary ``.lyr`` is not supported: it's an undocumented proprietary format
that needs Esri's own libraries to read.
"""

from __future__ import annotations

import json
import os
from typing import List, Optional, Tuple

from .models import CrsInfo, InspectionReport, LayerInfo, LocationInfo

LAYER_EXTS = {".lyrx", ".mapx", ".pitemx"}

# CIM layer types that carry data (group layers just hold others)
_DATA_LAYERS = {
    "CIMFeatureLayer", "CIMRasterLayer", "CIMMosaicLayer", "CIMImageServiceLayer",
    "CIMTiledServiceLayer", "CIMVectorTileLayer", "CIMAnnotationLayer",
    "CIMDimensionLayer", "CIMSubtypeGroupLayer",
}

# Signs the workspace is somewhere ArcGIS Pro cleans up.
_TEMP_MARKERS = ("arcgisprotemp", os.path.join("appdata", "local", "temp").lower(),
                 os.path.join("windows", "temp").lower())


def _load(path) -> Optional[dict]:
    with open(path, "rb") as fh:
        raw = fh.read()
    for enc in ("utf-8-sig", "utf-8", "utf-16"):
        try:
            doc = json.loads(raw.decode(enc))
            return doc if isinstance(doc, dict) else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    return None


def _iter_layer_defs(doc) -> List[dict]:
    defs = doc.get("layerDefinitions")
    if isinstance(defs, list):
        return [d for d in defs if isinstance(d, dict)]
    # .mapx nests the map's layers one level down
    for key in ("mapDefinition", "mapDefinitions"):
        node = doc.get(key)
        for candidate in (node if isinstance(node, list) else [node]):
            if isinstance(candidate, dict):
                inner = candidate.get("layerDefinitions")
                if isinstance(inner, list):
                    return [d for d in inner if isinstance(d, dict)]
    return []


def _data_connection(layer: dict) -> Optional[dict]:
    """The dataConnection for a layer, wherever CIM happens to keep it."""
    for holder in (layer.get("featureTable"), layer):
        if isinstance(holder, dict):
            conn = holder.get("dataConnection")
            if isinstance(conn, dict):
                return conn
    return None


def _parse_connection_string(text: str) -> dict:
    """``DATABASE=C:\\x.gdb;VERSION=...`` -> {'DATABASE': 'C:\\x.gdb', ...}"""
    out = {}
    for part in str(text or "").split(";"):
        if "=" in part:
            key, _, value = part.partition("=")
            out[key.strip().upper()] = value.strip()
    return out


def resolve_source(conn: dict, base_dir: str) -> Tuple[Optional[str], Optional[str], str]:
    """Work out what a dataConnection points at.

    Returns ``(path, layer_name, kind)``. ``path`` is None for services and database
    connections, which aren't files on disk.
    """
    factory = str(conn.get("workspaceFactory") or "").strip()
    dataset = str(conn.get("dataset") or "").strip()
    parts = _parse_connection_string(conn.get("workspaceConnectionString"))

    if factory in ("SDE", "Sql") or any(k in parts for k in ("SERVER", "INSTANCE", "URL")):
        return None, dataset or None, factory or "database/service"
    if str(conn.get("type", "")).endswith("ServiceConnection") or "URL" in parts:
        return None, dataset or None, "service"

    workspace = parts.get("DATABASE") or parts.get("PATH") or ""
    if not workspace:
        return None, dataset or None, factory or "unknown"

    workspace = os.path.expandvars(workspace)
    if not os.path.isabs(workspace):
        workspace = os.path.join(base_dir, workspace)   # .lyrx often stores relative paths
    workspace = os.path.normpath(workspace)

    if factory == "FileGDB" or workspace.lower().endswith(".gdb"):
        return workspace, dataset or None, "FileGDB"
    if factory == "Shapefile":
        name = dataset if dataset.lower().endswith(".shp") else dataset + ".shp"
        return os.path.join(workspace, name), None, "Shapefile"
    if factory == "Raster":
        return os.path.join(workspace, dataset) if dataset else workspace, None, "Raster"
    if dataset:
        return os.path.join(workspace, dataset), None, factory or "file"
    return workspace, None, factory or "file"


def temp_kind(path: str) -> Optional[str]:
    """'pro' for ArcGIS Pro's scratch area, 'temp' for any other temp folder, else None."""
    low = (path or "").lower()
    if "arcgisprotemp" in low:
        return "pro"
    return "temp" if any(marker in low for marker in _TEMP_MARKERS) else None


def _looks_temporary(path: str) -> bool:
    return temp_kind(path) is not None


def _broken_layer(name, source, kind, missing, query, visible, note) -> LayerInfo:
    return LayerInfo(
        name=name, geometry_type=None, feature_count=None, fields=[], native_bounds=None,
        crs=CrsInfo(defined=False),
        location=LocationInfo(available=False, note=note),
        source_path=source, source_kind=kind, source_missing=missing,
        definition_query=query, visible=visible,
    )


def inspect_layer_file(path, file_name, size) -> InspectionReport:
    doc = _load(path)
    if doc is None:
        return InspectionReport(
            path=path, file_name=file_name, size_bytes=size, kind="layerfile",
            driver="ArcGIS layer file",
            error="this doesn't look like a readable ArcGIS layer file (expected CIM JSON)")

    # A .pitemx is a portal item: a pointer to a hosted service. Follow it.
    item_url = doc.get("url")
    if item_url and str(doc.get("type", "")).endswith("Service"):
        from .esri import inspect_service, is_service_url

        if is_service_url(item_url):
            report = inspect_service(item_url)
            report.path = path
            report.file_name = file_name
            report.size_bytes = size
            report.service_title = doc.get("title") or report.service_title
            report.portal_access = doc.get("access")
            return report

    base_dir = os.path.dirname(os.path.abspath(path))
    report = InspectionReport(
        path=path, file_name=file_name, size_bytes=size, kind="layerfile",
        driver="ArcGIS Pro layer file (%s)" % (doc.get("version") or "CIM"))

    defs = _iter_layer_defs(doc)
    data_layers = [d for d in defs if d.get("type") in _DATA_LAYERS]
    if not data_layers:
        report.error = ("no data layers found — this file may only contain group layers "
                        "or symbology")
        return report

    from .inspector import _read_layer_info

    for layer in data_layers:
        name = str(layer.get("name") or "(unnamed layer)")
        visible = layer.get("visibility")
        table = layer.get("featureTable") if isinstance(layer.get("featureTable"), dict) else {}
        query = table.get("definitionExpression") or None
        conn = _data_connection(layer)

        if not conn:
            report.layers.append(_broken_layer(
                name, None, None, None, query, visible,
                "this layer records no data source"))
            continue

        source, sublayer, kind = resolve_source(conn, base_dir)
        if source is None:
            report.layers.append(_broken_layer(
                name, None, kind, None, query, visible,
                f"points at a {kind}, not a file on disk"))
            continue

        if not os.path.exists(source):
            report.layers.append(_broken_layer(
                name, source, kind, True, query, visible,
                "the data this layer points at is missing"))
            continue

        try:
            info, _driver = _read_layer_info(source, sublayer)
            info.name = name
            info.source_path = source
            info.source_kind = kind
            info.source_missing = False
            info.definition_query = query
            info.visible = visible
            report.layers.append(info)
        except Exception as exc:
            bad = _broken_layer(name, source, kind, False, query, visible,
                                "the data source could not be read")
            bad.read_error = str(exc)
            report.layers.append(bad)

    return report
