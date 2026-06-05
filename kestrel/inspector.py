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
}


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

    layer = LayerInfo(
        name=layer_name or info.get("layer_name") or "(default)",
        geometry_type=info.get("geometry_type"),
        feature_count=info.get("features"),
        fields=fields,
        native_bounds=bounds,
        crs=crs_info,
        location=bounds_to_wgs84(bounds, crs_obj),
    )

    # Lines/polygons: sample geometries and flag invalid ones. (Points can't be invalid.)
    gt = info.get("geometry_type") or ""
    if gt and "Point" not in gt:
        inv, sampled, reason = _check_geometry_validity(source, layer_name)
        layer.invalid_geometry_count = inv
        layer.invalid_geometry_sampled = sampled
        layer.invalid_geometry_reason = reason

    return layer, info.get("driver")


def _check_geometry_validity(source, layer_name, max_features: int = 2000):
    """Sample up to ``max_features`` geometries and count invalid ones (no geopandas).

    Returns (invalid_count, sampled_count, first_reason); (None, None, None) if it can't run.
    """
    try:
        import shapely
        from pyogrio.raw import read as _raw_read

        res = _raw_read(source, layer=layer_name, columns=[], read_geometry=True,
                        max_features=max_features)
        geom_wkb = res[2]
        if geom_wkb is None:
            return None, None, None
        geoms = shapely.from_wkb(geom_wkb)
        present = [g for g in geoms if g is not None]
        if not present:
            return 0, 0, None
        valid = shapely.is_valid(present)
        invalid_idx = [i for i, ok in enumerate(valid) if not ok]
        reason = None
        if invalid_idx:
            try:
                reason = shapely.is_valid_reason(present[invalid_idx[0]])
            except Exception:
                reason = None
        return len(invalid_idx), len(present), reason
    except Exception:
        return None, None, None


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
    report = InspectionReport(
        path=path, file_name=file_name, size_bytes=size, kind="vector", driver=None,
    )
    try:
        layers = pyogrio.list_layers(src)
    except Exception as exc:
        return InspectionReport(
            path=path, file_name=file_name, size_bytes=size,
            kind="vector", driver=None, error=str(exc),
        )

    layer_names = [None] if (layers is None or len(layers) == 0) else [row[0] for row in layers]
    for lname in layer_names:
        try:
            layer, driver = _read_layer_info(src, lname)
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
        with rasterio.open(src) as ds:
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
    """Inspect a geospatial file and return a structured :class:`InspectionReport`."""
    path = os.fspath(path)
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

    from .tabular import TABLE_EXTS, inspect_table
    if ext in TABLE_EXTS:
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
