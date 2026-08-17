"""Read ArcGIS REST services (FeatureServer / MapServer) straight from a URL.

Paste a service URL and Kestrel asks the same questions it asks of a file: what CRS is
this in, where on Earth does it sit, and is anything obviously wrong. Layer metadata
comes from the service's own ``?f=json`` description, and the features themselves are
read through GDAL's ESRIJSON driver.

Nothing is uploaded — Kestrel only issues GET requests to the URL you give it.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from typing import List, Optional, Tuple

from .models import CrsInfo, InspectionReport, LayerInfo, LocationInfo

TIMEOUT = 25
USER_AGENT = "Kestrel (GIS sanity checker)"
MAX_PREVIEW_FEATURES = 1000

_SERVICE_RE = re.compile(
    r"^(?P<base>https?://.+?/(?:FeatureServer|MapServer|ImageServer))"
    r"(?:/(?P<layer>\d+))?/?(?:\?.*)?$", re.IGNORECASE)


def is_service_url(text: str) -> bool:
    return bool(text) and bool(_SERVICE_RE.match(str(text).strip()))


def split_service_url(text: str) -> Tuple[str, Optional[int]]:
    """('https://.../FeatureServer', 0) — layer index is None for a whole service."""
    m = _SERVICE_RE.match(str(text).strip())
    if not m:
        return str(text).strip().rstrip("/"), None
    layer = m.group("layer")
    return m.group("base"), (int(layer) if layer is not None else None)


def _get_json(url: str) -> dict:
    sep = "&" if "?" in url else "?"
    request = urllib.request.Request(url + sep + "f=json",
                                     headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        payload = json.loads(response.read().decode("utf-8", "replace"))
    if isinstance(payload, dict) and "error" in payload:
        err = payload["error"] or {}
        raise RuntimeError(f"{err.get('message') or 'service error'} "
                           f"(code {err.get('code')})")
    return payload


def _crs_from_sr(sr) -> Tuple[CrsInfo, Optional[object]]:
    """Esri spatial references use wkid; latestWkid is the modern EPSG equivalent."""
    from .inspector import _parse_crs

    if not isinstance(sr, dict):
        return CrsInfo(defined=False), None
    code = sr.get("latestWkid") or sr.get("wkid")
    if code:
        return _parse_crs(f"EPSG:{int(code)}")
    if sr.get("wkt"):
        return _parse_crs(sr["wkt"])
    return CrsInfo(defined=False), None


def _extent_bounds(extent) -> Optional[Tuple[float, float, float, float]]:
    if not isinstance(extent, dict):
        return None
    try:
        return (float(extent["xmin"]), float(extent["ymin"]),
                float(extent["xmax"]), float(extent["ymax"]))
    except (KeyError, TypeError, ValueError):
        return None


_GEOM = {
    "esriGeometryPoint": "Point", "esriGeometryMultipoint": "MultiPoint",
    "esriGeometryPolyline": "LineString", "esriGeometryPolygon": "Polygon",
    "esriGeometryEnvelope": "Polygon",
}


def _read_features(base: str, layer_id: int, crs_obj):
    """Pull a capped sample of real features through GDAL's ESRIJSON driver."""
    from .inspector import _sample_geometry

    query = (f"{base}/{layer_id}/query?where=1%3D1&outFields=*&f=json"
             f"&resultRecordCount={MAX_PREVIEW_FEATURES}&returnGeometry=true")
    source = "ESRIJSON:" + query
    try:
        import pyogrio

        info = pyogrio.read_info(source)
        (_inv, _n, _reason), preview, _bounds = _sample_geometry(
            source, None, crs_obj, MAX_PREVIEW_FEATURES)
        return info, preview
    except Exception:
        return None, None


def inspect_service(url: str) -> InspectionReport:
    base, layer_id = split_service_url(url)
    report = InspectionReport(path=url, file_name=base.rstrip("/").split("/")[-2] + "/"
                              + base.rstrip("/").split("/")[-1],
                              size_bytes=None, kind="service",
                              driver="ArcGIS REST service")

    try:
        root = _get_json(base if layer_id is None else f"{base}/{layer_id}")
    except Exception as exc:
        report.error = str(exc)
        return report

    if layer_id is None:
        entries = [(l.get("id"), l.get("name"))
                   for l in (root.get("layers") or []) if isinstance(l, dict)]
        entries += [(t.get("id"), t.get("name"))
                    for t in (root.get("tables") or []) if isinstance(t, dict)]
        report.service_title = root.get("serviceDescription") or root.get("description") or None
        report.service_capabilities = root.get("capabilities")
        if not entries:
            report.error = "the service reports no layers"
            return report
        for lid, name in entries:
            if lid is None:
                continue
            report.layers.append(_inspect_service_layer(base, int(lid), name))
    else:
        report.service_capabilities = root.get("capabilities")
        report.layers.append(_inspect_service_layer(base, layer_id, root.get("name"),
                                                    meta=root))
    return report


def _inspect_service_layer(base: str, layer_id: int, name, meta=None) -> LayerInfo:
    try:
        meta = meta if meta is not None else _get_json(f"{base}/{layer_id}")
    except Exception as exc:
        return LayerInfo(
            name=str(name or f"layer {layer_id}"), geometry_type=None, feature_count=None,
            fields=[], native_bounds=None, crs=CrsInfo(defined=False),
            location=LocationInfo(available=False, note="layer description unavailable"),
            read_error=str(exc), source_kind="ArcGIS REST layer",
            source_path=f"{base}/{layer_id}")

    from .inspector import bounds_to_wgs84

    crs_info, crs_obj = _crs_from_sr((meta.get("extent") or {}).get("spatialReference"))
    declared = _extent_bounds(meta.get("extent"))
    fields = [(f.get("name"), str(f.get("type", "")).replace("esriFieldType", ""))
              for f in (meta.get("fields") or []) if isinstance(f, dict)]

    info, preview = _read_features(base, layer_id, crs_obj)
    bounds = declared
    count = None
    if info is not None:
        count = info.get("features")
        raw = info.get("total_bounds")
        if raw is not None:
            bounds = tuple(float(b) for b in raw)
            if not crs_info.defined:
                crs_info, crs_obj = _crs_from_sr({"wkid": 4326})

    # GDAL reports the service's *published* extent as total_bounds rather than
    # measuring the features, so work the real footprint out from the geometry we read.
    actual_wgs = _preview_bounds(preview)

    layer = LayerInfo(
        name=str(meta.get("name") or name or f"layer {layer_id}"),
        geometry_type=_GEOM.get(meta.get("geometryType"), meta.get("geometryType")),
        feature_count=count,
        fields=fields,
        native_bounds=bounds,
        crs=crs_info,
        location=(_location_from_wgs(actual_wgs) if actual_wgs
                  else bounds_to_wgs84(bounds, crs_obj)),
        preview=preview,
        source_path=f"{base}/{layer_id}",
        source_kind="ArcGIS REST layer",
    )
    layer.declared_bounds = declared
    layer.declared_wgs = bounds_to_wgs84(declared, crs_obj) if declared else None
    layer.actual_wgs = actual_wgs
    layer.max_record_count = meta.get("maxRecordCount")
    layer.sampled = bool(info is not None and count == MAX_PREVIEW_FEATURES)
    return layer


def _preview_bounds(preview) -> Optional[Tuple[float, float, float, float]]:
    """WGS84 bounding box of the geometry we actually downloaded."""
    if not preview:
        return None
    xs, ys = [], []
    for point in preview.get("points") or []:
        xs.append(point[0])
        ys.append(point[1])
    for group in ("lines", "polygons"):
        for shape in preview.get(group) or []:
            for c in shape:
                xs.append(c[0])
                ys.append(c[1])
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _location_from_wgs(bounds) -> LocationInfo:
    west, south, east, north = bounds
    return LocationInfo(available=True, west=west, south=south, east=east, north=north,
                        center_lat=(south + north) / 2.0, center_lon=(west + east) / 2.0)
