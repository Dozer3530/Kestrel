"""Core inspection logic: turn a file path into an :class:`InspectionReport`.

Reads metadata only (no full geometry load) via pyogrio for vector data and
rasterio for rasters, derives CRS / UTM details with pyproj, and reprojects the
extent to WGS84 so we can tell *where on Earth* the data sits.
"""

from __future__ import annotations

import math
import os
import zipfile
from typing import Optional, Tuple

import pyogrio
from pyproj import CRS, Transformer

from .models import (
    CrsInfo,
    InspectionReport,
    LayerInfo,
    LocationInfo,
    RasterInfo,
)

# Extensions we route straight to a raster reader. Anything else is tried as
# vector first (pyogrio), then raster as a fallback, so unknown types still work.
RASTER_EXTS = {
    ".tif", ".tiff", ".img", ".vrt", ".jp2", ".asc", ".dem", ".bil",
    ".bsq", ".bip", ".ecw", ".sid", ".hgt", ".nc", ".grd",
}
VECTOR_EXTS = {
    ".shp", ".gpkg", ".geojson", ".json", ".kml", ".gml", ".gpx",
    ".tab", ".mif", ".fgb", ".sqlite", ".geojsonl", ".gdb",
    # more drivers GDAL already ships with — routing them costs nothing
    ".dxf",                      # CAD: never carries a CRS, which is the whole point
    ".topojson", ".osm", ".pbf", ".pmtiles", ".mvt", ".gmt", ".dgn", ".map",
}

# Datasets that are a folder rather than a file.
DIR_EXTS = {".gdb", ".gpkg.zip"}


# --------------------------------------------------------------------------- #
# CRS analysis
# --------------------------------------------------------------------------- #
def _crs_info_from_obj(crs: CRS) -> CrsInfo:
    try:
        unit = crs.axis_info[0].unit_name if crs.axis_info else None
    except Exception:
        unit = None
    try:
        datum = crs.datum.name if crs.datum else None
    except Exception:
        datum = None
    aou = crs.area_of_use
    return CrsInfo(
        defined=True,
        epsg=crs.to_epsg(),
        name=crs.name,
        is_projected=bool(crs.is_projected),
        is_geographic=bool(crs.is_geographic),
        utm_zone=crs.utm_zone,
        unit=unit,
        datum=datum,
        area_of_use=aou.name if aou else None,
        area_bounds=tuple(aou.bounds) if aou else None,
        wkt=crs.to_wkt(),
    )


def _parse_crs(crs_input) -> Tuple[CrsInfo, Optional[CRS]]:
    """Return ``(CrsInfo, pyproj.CRS or None)`` from any CRS-ish input."""
    if crs_input is None:
        return CrsInfo(defined=False), None
    if isinstance(crs_input, str) and not crs_input.strip():
        return CrsInfo(defined=False), None
    try:
        crs = CRS.from_user_input(crs_input)
    except Exception:
        return CrsInfo(defined=False), None
    return _crs_info_from_obj(crs), crs


def analyze_crs(crs_input) -> CrsInfo:
    """Public helper: describe a CRS given a pyproj CRS, WKT, EPSG, etc."""
    return _parse_crs(crs_input)[0]


# --------------------------------------------------------------------------- #
# Location (reproject extent to WGS84 lon/lat)
# --------------------------------------------------------------------------- #
def _bad(v) -> bool:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return True
    return math.isnan(f) or math.isinf(f)


def bounds_to_wgs84(bounds, crs_obj: Optional[CRS]) -> LocationInfo:
    """Reproject a native bounding box to WGS84 lon/lat degrees."""
    if bounds is None or any(_bad(v) for v in bounds):
        return LocationInfo(available=False, note="no valid spatial extent")
    if crs_obj is None:
        return LocationInfo(available=False, note="no CRS, so the location is unknown")

    xmin, ymin, xmax, ymax = (float(b) for b in bounds)
    try:
        if crs_obj.is_geographic:
            w, s, e, n = xmin, ymin, xmax, ymax
        else:
            tr = Transformer.from_crs(crs_obj, 4326, always_xy=True)
            lons, lats = [], []
            for x, y in ((xmin, ymin), (xmin, ymax), (xmax, ymin), (xmax, ymax)):
                lon, lat = tr.transform(x, y)
                lons.append(lon)
                lats.append(lat)
            w, e = min(lons), max(lons)
            s, n = min(lats), max(lats)
        if any(_bad(v) for v in (w, s, e, n)):
            return LocationInfo(
                available=False,
                note="coordinates fall outside the valid range for this CRS",
            )
        return LocationInfo(
            available=True,
            west=w, south=s, east=e, north=n,
            center_lat=(s + n) / 2.0,
            center_lon=(w + e) / 2.0,
        )
    except Exception as exc:  # pragma: no cover - defensive
        return LocationInfo(available=False, note=f"reprojection failed: {exc}")


# --------------------------------------------------------------------------- #
# Vector
# --------------------------------------------------------------------------- #
def _looks_like_gdb(path) -> bool:
    """A File Geodatabase is a folder full of gdbtable files."""
    try:
        names = os.listdir(path)
    except OSError:
        return False
    return any(n.lower().endswith((".gdbtable", ".gdbindexes")) for n in names)


def _dir_size(path, cap: int = 4000) -> Optional[int]:
    total = 0
    try:
        for n, name in enumerate(os.listdir(path)):
            if n >= cap:
                break
            try:
                total += os.path.getsize(os.path.join(path, name))
            except OSError:
                pass
    except OSError:
        return None
    return total


def _gdal_path(p):
    r"""A path GDAL can open even with awkward characters (e.g. ';' in the name).

    On Windows, GDAL truncates such paths at the special character; the \\?\ extended-length
    form is passed through literally (and also lifts the 260-char limit). Applies only to
    absolute local/UNC filesystem paths — GDAL virtual paths (/vsi...) are left alone.
    """
    if os.name != "nt" or not p:
        return p
    if p.startswith("/vsi") or p.startswith("\\\\?\\") or not os.path.isabs(p):
        return p
    if p.startswith("\\\\"):                 # UNC: \\server\share -> \\?\UNC\server\share
        return "\\\\?\\UNC\\" + p.lstrip("\\")
    return "\\\\?\\" + p                      # drive letter: C:\... -> \\?\C:\...


def _read_layer_info(source: str, layer_name: Optional[str]) -> Tuple[LayerInfo, Optional[str]]:
    info = pyogrio.read_info(source, layer=layer_name) if layer_name else pyogrio.read_info(source)

    crs_info, crs_obj = _parse_crs(info.get("crs"))

    raw_bounds = info.get("total_bounds")
    bounds = tuple(float(b) for b in raw_bounds) if raw_bounds is not None else None

    raw_names = info.get("fields")
    names = list(raw_names) if raw_names is not None else []
    raw_dtypes = info.get("dtypes")
    dtypes = [str(d) for d in raw_dtypes] if raw_dtypes is not None else []
    fields = list(zip(names, dtypes)) if len(names) == len(dtypes) else [(n, "") for n in names]

    # Some drivers return -1 for "I'd have to scan the whole file to tell you".
    count = info.get("features")
    if count is not None and int(count) < 0:
        count = None

    layer = LayerInfo(
        name=layer_name or info.get("layer_name") or "(default)",
        geometry_type=info.get("geometry_type"),
        feature_count=count,
        fields=fields,
        native_bounds=bounds,
        crs=crs_info,
        location=bounds_to_wgs84(bounds, crs_obj),
    )

    # One geometry pass: validity (lines/polygons only — a point can't be invalid) plus a
    # WGS84 preview so the map can draw the real features rather than a box around them.
    gt = info.get("geometry_type") or ""
    if gt:
        (inv, sampled, reason), preview, sample_bounds = _sample_geometry(
            source, layer_name, crs_obj)
        if "Point" not in gt:
            layer.invalid_geometry_count = inv
            layer.invalid_geometry_sampled = sampled
            layer.invalid_geometry_reason = reason
        layer.preview = preview
        if layer.native_bounds is None and sample_bounds:
            layer.native_bounds = sample_bounds
            layer.location = bounds_to_wgs84(sample_bounds, crs_obj)
        if layer.feature_count is None and sampled:
            layer.feature_count = sampled

    return layer, info.get("driver")


def _sample_geometry(source, layer_name, crs_obj, max_features: int = 2000):
    """One geometry read that serves two purposes.

    Returns ``((invalid_count, sampled_count, first_reason), preview)`` where preview is a
    dict of WGS84 shapes for the map — so the map can draw the actual features instead of
    a box around them, without paying for a second pass over the file.
    """
    empty = (None, None, None)
    try:
        import shapely
        from pyogrio.raw import read as _raw_read

        res = _raw_read(source, layer=layer_name, columns=[], read_geometry=True,
                        max_features=max_features)
        geom_wkb = res[2]
        if geom_wkb is None:
            return empty, None, None
        geoms = shapely.from_wkb(geom_wkb)
        present = [g for g in geoms if g is not None]
        if not present:
            return (0, 0, None), None, None

        valid = shapely.is_valid(present)
        invalid_idx = [i for i, ok in enumerate(valid) if not ok]
        reason = None
        if invalid_idx:
            try:
                reason = shapely.is_valid_reason(present[invalid_idx[0]])
            except Exception:
                reason = None
        validity = (len(invalid_idx), len(present), reason)

        # Some drivers (DXF especially) don't report an extent or a feature count without
        # a full scan. We've already read geometry, so measure it here rather than
        # reporting an "invalid extent" that isn't.
        try:
            import numpy as np

            all_bounds = shapely.bounds(np.asarray(present, dtype=object))
            sample_bounds = (float(np.nanmin(all_bounds[:, 0])),
                             float(np.nanmin(all_bounds[:, 1])),
                             float(np.nanmax(all_bounds[:, 2])),
                             float(np.nanmax(all_bounds[:, 3])))
        except Exception:
            sample_bounds = None

        return validity, _build_preview(present, crs_obj), sample_bounds
    except Exception:
        return empty, None, None


def _build_preview(geoms, crs_obj, vertex_budget: int = 12000):
    """Reproject sampled geometry to WGS84 and thin it enough to draw quickly."""
    try:
        import numpy as np
        import shapely

        if crs_obj is None:
            return None

        collection = shapely.geometrycollections(np.asarray(geoms, dtype=object))
        total = shapely.get_num_coordinates(collection)
        if total == 0:
            return None

        geoms = np.asarray(geoms, dtype=object)
        if total > vertex_budget:
            # simplify first; if still heavy, drop features evenly across the sample
            span = max(shapely.bounds(collection)[2] - shapely.bounds(collection)[0], 1e-9)
            geoms = shapely.simplify(geoms, span / 400.0, preserve_topology=False)
            total = int(shapely.get_num_coordinates(
                shapely.geometrycollections(geoms)))
            if total > vertex_budget and len(geoms) > 1:
                step = max(1, int(total / vertex_budget))
                geoms = geoms[::step]

        if not crs_obj.is_geographic:
            tr = Transformer.from_crs(crs_obj, 4326, always_xy=True)
            out = []
            for g in geoms:
                if g is None or g.is_empty:
                    continue
                coords = shapely.get_coordinates(g)
                if not len(coords):
                    continue
                lon, lat = tr.transform(coords[:, 0], coords[:, 1])
                if not (np.isfinite(lon).all() and np.isfinite(lat).all()):
                    continue
                out.append(shapely.set_coordinates(
                    shapely.from_wkb(shapely.to_wkb(g)),
                    np.column_stack([lon, lat])))
            geoms = out

        points, lines, polygons = [], [], []
        for g in geoms:
            if g is None or g.is_empty:
                continue
            for part in (g.geoms if hasattr(g, "geoms") else [g]):
                if part.is_empty:
                    continue
                kind = part.geom_type
                if kind == "Point":
                    points.append([part.x, part.y])
                elif kind in ("LineString", "LinearRing"):
                    lines.append([[x, y] for x, y in part.coords])
                elif kind == "Polygon":
                    polygons.append([[x, y] for x, y in part.exterior.coords])
        if not (points or lines or polygons):
            return None
        return {"points": points, "lines": lines, "polygons": polygons}
    except Exception:
        return None


def _error_layer(name, exc) -> LayerInfo:
    return LayerInfo(
        name=str(name) if name else "(default)",
        geometry_type=None, feature_count=None, fields=[], native_bounds=None,
        crs=CrsInfo(defined=False),
        location=LocationInfo(available=False, note="layer could not be read"),
        read_error=str(exc),
    )


def _inspect_vector(path: str, file_name: str, size: Optional[int],
                    source: Optional[str] = None) -> InspectionReport:
    src = source or path
    gsrc = _gdal_path(src)   # GDAL-safe form (handles ';' etc. and long/UNC paths)
    report = InspectionReport(
        path=path, file_name=file_name, size_bytes=size, kind="vector", driver=None,
    )
    try:
        layers = pyogrio.list_layers(gsrc)
    except Exception as exc:
        return InspectionReport(
            path=path, file_name=file_name, size_bytes=size,
            kind="vector", driver=None, error=str(exc),
        )

    layer_names = [None] if (layers is None or len(layers) == 0) else [row[0] for row in layers]
    for lname in layer_names:
        try:
            layer, driver = _read_layer_info(gsrc, lname)
            report.layers.append(layer)
            report.driver = report.driver or driver
        except Exception as exc:
            # One bad layer shouldn't sink the whole dataset — flag it and keep going.
            report.layers.append(_error_layer(lname, exc))

    # If nothing could be read, treat as fatal so the raster fallback can have a turn.
    if report.layers and all(l.read_error for l in report.layers):
        return InspectionReport(
            path=path, file_name=file_name, size_bytes=size,
            kind="vector", driver=report.driver, error=report.layers[0].read_error,
        )

    # Note whether a plain on-disk shapefile has its .prj sidecar.
    if src.lower().endswith(".shp") and os.path.exists(src):
        has_prj = os.path.exists(src[:-4] + ".prj")
        for layer in report.layers:
            layer.has_prj = has_prj

    return report


def _inspect_zipped(path: str, file_name: str, size: Optional[int]) -> InspectionReport:
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
    except Exception as exc:
        return InspectionReport(
            path=path, file_name=file_name, size_bytes=size,
            kind="vector", driver=None, error=f"could not open zip: {exc}",
        )

    zip_uri = path.replace("\\", "/")
    shp = next((n for n in names if n.lower().endswith(".shp")), None)

    if shp:
        stem = shp[:-4].lower()
        has_prj = any(n.lower() == stem + ".prj" for n in names)
        report = _inspect_vector(path, file_name, size, source=f"/vsizip/{zip_uri}/{shp}")
        for layer in report.layers:
            layer.has_prj = has_prj
        return report

    other = next(
        (n for n in names if os.path.splitext(n)[1].lower() in VECTOR_EXTS), None
    )
    if other:
        return _inspect_vector(path, file_name, size, source=f"/vsizip/{zip_uri}/{other}")

    rast = next(
        (n for n in names if os.path.splitext(n)[1].lower() in RASTER_EXTS), None
    )
    if rast:
        return _inspect_raster(path, file_name, size, source=f"/vsizip/{zip_uri}/{rast}")

    # Let GDAL try to figure it out from the archive itself.
    return _inspect_vector(path, file_name, size, source=f"/vsizip/{zip_uri}")


# --------------------------------------------------------------------------- #
# Raster
# --------------------------------------------------------------------------- #
def _inspect_raster(path: str, file_name: str, size: Optional[int],
                    source: Optional[str] = None) -> InspectionReport:
    src = source or path
    try:
        import rasterio
    except Exception as exc:  # pragma: no cover
        return InspectionReport(
            path=path, file_name=file_name, size_bytes=size,
            kind="raster", driver=None, error=f"rasterio unavailable: {exc}",
        )
    try:
        with rasterio.open(_gdal_path(src)) as ds:
            crs_info, crs_obj = _parse_crs(ds.crs.to_wkt() if ds.crs else None)
            b = ds.bounds
            bounds = (float(b.left), float(b.bottom), float(b.right), float(b.top))
            res = ds.res
            nodata = ds.nodata
            raster = RasterInfo(
                width=ds.width,
                height=ds.height,
                band_count=ds.count,
                dtype=str(ds.dtypes[0]) if ds.dtypes else None,
                nodata=float(nodata) if nodata is not None else None,
                res_x=float(res[0]) if res else None,
                res_y=float(res[1]) if res else None,
                native_bounds=bounds,
                crs=crs_info,
                location=bounds_to_wgs84(bounds, crs_obj),
            )
            driver = ds.driver
    except Exception as exc:
        return InspectionReport(
            path=path, file_name=file_name, size_bytes=size,
            kind="raster", driver=None, error=str(exc),
        )
    return InspectionReport(
        path=path, file_name=file_name, size_bytes=size,
        kind="raster", driver=driver, raster=raster,
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def inspect_path(path) -> InspectionReport:
    """Inspect a geospatial file, an ArcGIS layer file, or a service URL."""
    path = os.fspath(path)

    # An ArcGIS REST service URL is inspected over the network, not from disk.
    from .esri import inspect_service, is_service_url
    if is_service_url(path):
        report = inspect_service(path)
        from .diagnostics import run_diagnostics
        report.diagnostics = run_diagnostics(report)
        return report

    # Normalize slashes / UNC form so GDAL can open the file. Network paths arrive as
    # //server/share/x.gpkg, which GDAL mangles into /share/x.gpkg (host dropped);
    # os.path.normpath turns it into \\server\share\x.gpkg, which GDAL opens fine.
    path = os.path.normpath(path)
    file_name = os.path.basename(path.rstrip("/\\")) or path
    size = None
    try:
        size = os.path.getsize(path)
    except OSError:
        pass

    if not os.path.exists(path):
        return InspectionReport(
            path=path, file_name=file_name, size_bytes=size,
            kind="unknown", driver=None, error=f"file not found: {path}",
        )

    ext = os.path.splitext(path)[1].lower()

    # A File Geodatabase is a directory, so it needs handling before the file routing.
    if os.path.isdir(path):
        if ext in DIR_EXTS or _looks_like_gdb(path):
            report = _inspect_vector(path, file_name, _dir_size(path))
            from .diagnostics import run_diagnostics
            report.diagnostics = run_diagnostics(report)
            return report
        return InspectionReport(
            path=path, file_name=file_name, size_bytes=None, kind="unknown", driver=None,
            error="that's a folder, not a dataset — drop a file, or a .gdb geodatabase")

    from .arcgis import LAYER_EXTS, inspect_layer_file
    from .pointcloud import LAS_EXTS, inspect_pointcloud
    from .tabular import TABLE_EXTS, inspect_table
    if ext in LAS_EXTS:
        report = inspect_pointcloud(path, file_name, size)
    elif ext in LAYER_EXTS:
        report = inspect_layer_file(path, file_name, size)
    elif ext in TABLE_EXTS:
        report = inspect_table(path, file_name, size, ext)
    elif ext in (".zip", ".kmz"):
        report = _inspect_zipped(path, file_name, size)
    elif ext in RASTER_EXTS:
        report = _inspect_raster(path, file_name, size)
        if report.error:
            alt = _inspect_vector(path, file_name, size)
            if not alt.error:
                report = alt
    else:
        report = _inspect_vector(path, file_name, size)
        if report.error:
            alt = _inspect_raster(path, file_name, size)
            if not alt.error:
                report = alt

    # Attach diagnostics (imported lazily to avoid a circular import).
    from .diagnostics import run_diagnostics
    report.diagnostics = run_diagnostics(report)
    return report
