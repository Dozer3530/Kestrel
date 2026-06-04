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


def run_diagnostics(report: InspectionReport) -> List[Diagnostic]:
    diags: List[Diagnostic] = []

    if report.error:
        diags.append(Diagnostic(
            "error", "Could not read file", report.error,
            "Check the file isn't corrupt and is a supported geospatial format.",
        ))
        return diags

    if report.is_vector:
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
        multi = len(report.layers) > 1
        for layer in report.layers:
            ctx = f"layer '{layer.name}'" if multi else "layer"
            _check(diags, layer.crs, layer.native_bounds, layer.location, ctx,
                   feature_count=layer.feature_count, has_prj=layer.has_prj,
                   geometry_type=layer.geometry_type)

    elif report.is_raster and report.raster:
        r = report.raster
        _check(diags, r.crs, r.native_bounds, r.location, "raster",
               empty=(r.width == 0 or r.height == 0))

    return diags


def _check(diags: List[Diagnostic], crs: CrsInfo, bounds, location: LocationInfo,
           ctx: str, feature_count: Optional[int] = None, has_prj: Optional[bool] = None,
           geometry_type: Optional[str] = None, empty: bool = False) -> None:

    # --- CRS present? (the #1 reason a layer lands in the wrong place) ---
    if not crs.defined:
        if has_prj is False:
            diags.append(Diagnostic(
                "error", "Missing .prj (no CRS)",
                f"The shapefile has no .prj file, so its coordinate system is unknown ({ctx}).",
                "Assign the correct CRS in QGIS (Layer ▸ Layer CRS ▸ Set...), or add a .prj. "
                "Until then QGIS can't place it correctly.",
            ))
        else:
            diags.append(Diagnostic(
                "error", "No CRS defined",
                f"No coordinate reference system is set on this {ctx}.",
                "QGIS will fall back to a default (often the project CRS) and may draw the "
                "data in the wrong spot. Assign the correct CRS.",
            ))

    # --- emptiness ---
    if feature_count == 0:
        diags.append(Diagnostic(
            "warning", "Empty layer",
            f"The {ctx} has 0 features — nothing will draw in QGIS.",
            "Confirm the export/clip that produced this actually contained data.",
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
            "Usually caused by empty or corrupt geometry. Re-export the data.",
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
