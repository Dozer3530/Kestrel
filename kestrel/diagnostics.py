"""Sanity checks — the "why doesn't this layer show up in QGIS?" detector.

Each check produces a :class:`Diagnostic` (error / warning / info) with a short
explanation and a suggested fix.
"""

from __future__ import annotations

import math
from typing import List, Optional

from .models import CrsInfo, Diagnostic, InspectionReport, LocationInfo

# How far (degrees) a dataset may sit outside its CRS's stated area of use before
# we flag it. Generous, so we only catch gross errors (wrong hemisphere/continent),
# not someone using one UTM zone across a whole province.
_AREA_BUFFER_DEG = 5.0


def _bad(v) -> bool:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return True
    return math.isnan(f) or math.isinf(f)


def _looks_like_lonlat(bounds) -> bool:
    if not bounds or any(_bad(v) for v in bounds):
        return False
    xmin, ymin, xmax, ymax = bounds
    return (
        -180.5 <= xmin <= 180.5 and -180.5 <= xmax <= 180.5
        and -90.5 <= ymin <= 90.5 and -90.5 <= ymax <= 90.5
    )


def _parse_utm_zone(zone):
    """'11N' -> (11, 'N'); None if not a parseable UTM zone string."""
    if not zone:
        return None
    z = str(zone).strip().upper()
    hemi = z[-1] if z[-1:] in ("N", "S") else None
    try:
        num = int(z[:-1] if hemi else z)
    except ValueError:
        return None
    return (num, hemi) if (hemi and 1 <= num <= 60) else None


def run_diagnostics(report: InspectionReport) -> List[Diagnostic]:
    diags: List[Diagnostic] = []

    if report.error:
        diags.append(Diagnostic(
            "error", "Could not read file", report.error,
            "Make sure the file isn't corrupted and is a format Kestrel reads "
            "(shapefile, GeoPackage, GeoJSON, GeoTIFF, ...).",
        ))
        return diags

    if report.is_vector:
        # Tabular files (CSV/Excel) have no CRS — give coordinate-aware guidance instead.
        if report.driver in ("CSV", "XLSX") and report.layers:
            _table_diagnostics(diags, report.layers[0])
            return diags
        if not report.layers:
            diags.append(Diagnostic(
                "warning", "No layers found",
                "The dataset contains no readable vector layers.",
                "Confirm the file actually contains vector data.",
            ))
        if len(report.layers) > 1:
            names = ", ".join(l.name for l in report.layers)
            diags.append(Diagnostic(
                "info", "Multiple layers",
                f"This dataset has {len(report.layers)} layers: {names}.",
                "In QGIS, make sure you add the specific layer you want.",
            ))
            distinct = {l.crs.summary for l in report.layers if l.crs.defined}
            if len(distinct) > 1:
                diags.append(Diagnostic(
                    "warning", "Layers use different coordinate systems",
                    "Not all layers share one CRS: " + "; ".join(sorted(distinct)) + ".",
                    "Mixing CRSs in a single dataset is unusual — double-check you're loading "
                    "the layer you mean, in the projection you expect.",
                ))
        multi = len(report.layers) > 1
        for layer in report.layers:
            ctx = f"layer '{layer.name}'" if multi else "layer"
            if layer.read_error:
                diags.append(Diagnostic(
                    "error", "Layer could not be read",
                    f"Couldn't read {ctx}: {layer.read_error}",
                    "The rest of the dataset still loaded — this one layer may be corrupt or use "
                    "an unsupported geometry or encoding.",
                ))
                continue
            _check(diags, layer.crs, layer.native_bounds, layer.location, ctx,
                   feature_count=layer.feature_count, has_prj=layer.has_prj,
                   geometry_type=layer.geometry_type,
                   invalid_geometry_count=layer.invalid_geometry_count,
                   invalid_geometry_sampled=layer.invalid_geometry_sampled,
                   invalid_geometry_reason=layer.invalid_geometry_reason)

    elif report.is_raster and report.raster:
        r = report.raster
        _check(diags, r.crs, r.native_bounds, r.location, "raster",
               empty=(r.width == 0 or r.height == 0))

    return diags


def _table_diagnostics(diags: List[Diagnostic], layer) -> None:
    """CSV/Excel guidance: these files have no CRS, so help with the coordinates instead."""
    if layer.coord_columns:
        x, y = layer.coord_columns
        diags.append(Diagnostic(
            "info", "Coordinates found",
            f"Read point coordinates from columns '{x}' (X) and '{y}' (Y) across "
            f"{layer.feature_count} row(s). The values {layer.crs_guess}.",
            "This file type carries no CRS of its own. In QGIS, add it via Layer ▸ Add Layer ▸ "
            "Add Delimited Text Layer and pick the matching CRS (e.g. EPSG:4326 for lon/lat).",
        ))
        _check(diags, layer.crs, layer.native_bounds, layer.location, "table",
               feature_count=layer.feature_count, is_table=True)
    else:
        diags.append(Diagnostic(
            "warning", "No coordinate columns found",
            "Couldn't spot coordinate columns here (looked for names like lon/lat, x/y, "
            "easting/northing).",
            "Rename your coordinate columns to something recognizable — or this file may just "
            "be a plain table with no point geometry.",
        ))


def _check(diags: List[Diagnostic], crs: CrsInfo, bounds, location: LocationInfo,
           ctx: str, feature_count: Optional[int] = None, has_prj: Optional[bool] = None,
           geometry_type: Optional[str] = None, empty: bool = False,
           is_table: bool = False, invalid_geometry_count: Optional[int] = None,
           invalid_geometry_sampled: Optional[int] = None,
           invalid_geometry_reason: Optional[str] = None) -> None:

    # --- CRS present? (the #1 reason a layer lands in the wrong place) ---
    if not crs.defined and not is_table:
        if has_prj is False:
            diags.append(Diagnostic(
                "error", "Missing .prj (no CRS)",
                f"This shapefile is missing its .prj file, so there's no way to tell which "
                f"coordinate system it's in ({ctx}).",
                "Add the matching .prj, or set the CRS in QGIS (Layer ▸ Layer CRS ▸ Set...). "
                "Until then, QGIS can't place it correctly.",
            ))
        else:
            diags.append(Diagnostic(
                "error", "No CRS defined",
                f"This {ctx} doesn't record which coordinate system it's in.",
                "Without it, QGIS has to guess (usually your project's CRS), so the layer can "
                "end up in the wrong place. Set the correct CRS once you know what it should be.",
            ))

    # --- emptiness ---
    if feature_count == 0:
        diags.append(Diagnostic(
            "warning", "Empty layer",
            f"The {ctx} has 0 features, so there's nothing to draw.",
            "Double-check the export or clip that produced it — it may have come up empty.",
        ))
    if empty:
        diags.append(Diagnostic(
            "warning", "Empty raster",
            "The raster has zero width or height — nothing will draw.",
            "Re-export the raster; the source may be empty or clipped to nothing.",
        ))

    # --- extent validity (stop here if there's nothing usable) ---
    if bounds is None or any(_bad(v) for v in bounds):
        diags.append(Diagnostic(
            "error", "Invalid extent",
            f"The {ctx} has no valid bounding box (empty or NaN extent).",
            "This usually points to empty or corrupt geometry — try re-exporting the data.",
        ))
        return

    xmin, ymin, xmax, ymax = bounds

    if xmin == xmax and ymin == ymax:
        diags.append(Diagnostic(
            "warning", "Zero-area extent",
            f"All coordinates in the {ctx} are the same point ({xmin:g}, {ymin:g}).",
            "May be a single point, or degenerate geometry. Verify in the attribute table.",
        ))

    if abs(xmin) < 1e-6 and abs(ymin) < 1e-6 and abs(xmax) < 1e-6 and abs(ymax) < 1e-6:
        diags.append(Diagnostic(
            "warning", "Coordinates at (0, 0) — 'Null Island'",
            f"The {ctx} sits at longitude 0, latitude 0 (off the coast of Africa).",
            "Usually means coordinates were lost or zeroed on export. Re-check the source data.",
        ))

    # --- CRS vs. coordinate-magnitude mismatch (classic "lands in the ocean") ---
    if crs.defined:
        looks_ll = _looks_like_lonlat(bounds)
        unit = (crs.unit or "").lower()

        if crs.is_projected and "metre" in unit and looks_ll:
            diags.append(Diagnostic(
                "warning", "Possible CRS / coordinate mismatch",
                f"The CRS is projected (units: {crs.unit}) but the coordinates "
                f"({xmin:g} … {xmax:g}) look like longitude/latitude degrees.",
                "The data may actually be in lat/lon (e.g. EPSG:4326) but tagged with a "
                "projected CRS — a classic reason a layer lands in the ocean or off-screen. "
                "Try setting the CRS to EPSG:4326.",
            ))

        if crs.is_geographic and not looks_ll:
            diags.append(Diagnostic(
                "warning", "Possible CRS / coordinate mismatch",
                f"The CRS is geographic (lon/lat degrees) but the coordinates "
                f"({xmin:g} … {xmax:g}) are far outside ±180 / ±90.",
                "The data may actually be projected (e.g. a UTM zone in metres) but tagged "
                "as lat/lon. Set the correct projected CRS.",
            ))

        # --- data sits well outside the CRS's valid area ---
        if crs.area_bounds and location and location.available:
            w, s, e, n = crs.area_bounds
            is_world = (e - w) >= 350 or (n - s) >= 170
            if not is_world:
                lon, lat = location.center_lon, location.center_lat
                outside = not (
                    w - _AREA_BUFFER_DEG <= lon <= e + _AREA_BUFFER_DEG
                    and s - _AREA_BUFFER_DEG <= lat <= n + _AREA_BUFFER_DEG
                )
                if outside:
                    diags.append(Diagnostic(
                        "warning", "Data outside the CRS's valid area",
                        f"The data centres near ({lat:.2f}, {lon:.2f}), but {crs.summary} "
                        f"is intended for: {crs.area_of_use}.",
                        "The CRS is probably wrong for this location — double-check the projection.",
                    ))

        # --- UTM zone / hemisphere mismatch ---
        zinfo = _parse_utm_zone(crs.utm_zone)
        if zinfo and location and location.available:
            znum, zhemi = zinfo
            lat, lon = location.center_lat, location.center_lon
            if (zhemi == "N" and lat < -1) or (zhemi == "S" and lat > 1):
                where = "southern" if lat < 0 else "northern"
                diags.append(Diagnostic(
                    "warning", "UTM zone is the wrong hemisphere",
                    f"The CRS is UTM zone {crs.utm_zone}, but the data is in the {where} "
                    f"hemisphere (latitude {lat:.2f}).",
                    "A north/south UTM mix-up shifts data by thousands of km — switch to the "
                    "matching N/S zone.",
                ))
            expected = min(max(int((lon + 180) // 6) + 1, 1), 60)
            if abs(expected - znum) >= 2:
                diags.append(Diagnostic(
                    "warning", "Data is in a different UTM zone",
                    f"The CRS is UTM zone {znum}, but the data's longitude ({lon:.2f}) falls in "
                    f"zone {expected}.",
                    f"The wrong UTM zone pushes coordinates sideways. Re-project to zone "
                    f"{expected}, or confirm the source zone.",
                ))

        # --- antimeridian wrap (small data, full longitude span) ---
        if crs.is_geographic and (xmax - xmin) > 350 and (ymax - ymin) < 20:
            diags.append(Diagnostic(
                "warning", "Extent may wrap the antimeridian",
                f"The data spans {xmax - xmin:.0f}° of longitude but only {ymax - ymin:.0f}° of "
                "latitude — a hint the bounding box is wrapping across ±180°.",
                "Common near the International Date Line; it can make the layer render as a "
                "stripe across the whole map. Check for coordinates on both sides of ±180.",
            ))

    # --- informational geometry notes ---
    if geometry_type:
        gt = geometry_type
        if any(tag in gt for tag in ("25D", " Z", "Z ", "ZM", " M")):
            diags.append(Diagnostic(
                "info", "3D or measured geometry",
                f"Geometry type is '{gt}' (carries Z and/or M values).",
                "Usually fine; just noting the extra dimensions are present.",
            ))
        if gt.lower() in ("geometry", "unknown", "geometrycollection"):
            diags.append(Diagnostic(
                "info", "Mixed / unknown geometry type",
                f"Geometry type is '{gt}', which can mix points, lines and polygons.",
                "Mixed-geometry layers occasionally cause styling/rendering quirks in QGIS.",
            ))

    # --- geometry validity (sampled) ---
    if invalid_geometry_count:
        sampled = invalid_geometry_sampled or invalid_geometry_count
        reason = f" First issue: {invalid_geometry_reason}." if invalid_geometry_reason else ""
        diags.append(Diagnostic(
            "warning", "Invalid geometry",
            f"{invalid_geometry_count} of the first {sampled} feature(s) in the {ctx} have "
            f"invalid geometry (e.g. self-intersections).{reason}",
            "Invalid geometry can break rendering, selection and most processing tools. "
            "Run Vector ▸ Geometry Tools ▸ Fix Geometries in QGIS.",
        ))
