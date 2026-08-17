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


def _is_linear_unit(unit: str) -> bool:
    """True for any linear ground unit (metre, foot, chain...), false for degrees.

    Gating on 'metre' alone silently exempted every foot-based State Plane CRS from the
    CRS/coordinate mismatch check.
    """
    u = (unit or "").lower()
    if not u:
        return True                      # unknown unit on a projected CRS: still check it
    if "degree" in u or "grad" in u or "radian" in u:
        return False
    return True


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

    if report.is_service:
        _service_diagnostics(diags, report)
        return _dedupe(diags)

    if report.is_layer_file:
        _layer_file_diagnostics(diags, report)
        return _dedupe(diags)

    if report.is_vector:
        # Tabular files (CSV/Excel) have no CRS — give coordinate-aware guidance instead.
        if report.driver in ("CSV", "XLSX", "XLS") and report.layers:
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
            attr_only = (layer.geometry_type is None and layer.native_bounds is None
                         and not layer.crs.defined)
            _check(diags, layer.crs, layer.native_bounds, layer.location, ctx,
                   feature_count=layer.feature_count, has_prj=layer.has_prj,
                   geometry_type=layer.geometry_type,
                   invalid_geometry_count=layer.invalid_geometry_count,
                   invalid_geometry_sampled=layer.invalid_geometry_sampled,
                   invalid_geometry_reason=layer.invalid_geometry_reason,
                   is_attribute_table=attr_only)

    elif report.is_raster and report.raster:
        r = report.raster
        _check(diags, r.crs, r.native_bounds, r.location, "raster",
               empty=(r.width == 0 or r.height == 0))

    return diags


def _service_diagnostics(diags: List[Diagnostic], report) -> None:
    """A hosted service is reachable and self-describing — check what it claims."""
    if report.portal_access and report.portal_access.lower() != "public":
        diags.append(Diagnostic(
            "info", "This service is not public",
            f"The item is shared as '{report.portal_access}', so anyone opening it needs "
            "permission in your ArcGIS organisation.",
            "Kestrel read what the service allows anonymously. If numbers look wrong or "
            "empty, sign-in is probably required for the rest.",
        ))
    if len(report.layers) > 1:
        diags.append(Diagnostic(
            "info", "Service has several layers",
            "This service publishes %d layers: %s."
            % (len(report.layers), ", ".join(l.name for l in report.layers)),
            "Add the specific layer you want rather than the whole service.",
        ))

    multi = len(report.layers) > 1
    for layer in report.layers:
        ctx = f"'{layer.name}'" if multi else "this layer"

        if layer.read_error:
            diags.append(Diagnostic(
                "error", "Layer could not be read",
                f"{ctx} did not return a usable description: {layer.read_error}",
                "The layer may need sign-in, or the service may be offline.",
            ))
            continue

        if layer.feature_count is None:
            diags.append(Diagnostic(
                "warning", "Features could not be read",
                f"{ctx} describes itself, but querying its features failed. That usually "
                "means the service requires sign-in for data access.",
                "Kestrel can still show the published CRS and extent. To read the "
                "features, export the layer or make it public.",
            ))
        elif layer.feature_count == 0:
            diags.append(Diagnostic(
                "warning", "No features returned",
                f"{ctx} answered a query but returned nothing.",
                "The service may be empty, or it may be filtering what anonymous "
                "users can see.",
            ))
        elif layer.sampled:
            diags.append(Diagnostic(
                "info", "Showing a sample",
                f"{ctx} returned the first {layer.feature_count} features — Kestrel caps "
                "how much it downloads, so the real total may be larger.",
                "The CRS and geometry type are accurate; treat the count and extent as "
                "a sample.",
            ))

        # A service's published extent drives 'Zoom to Layer' in every client. Compare it
        # against the footprint measured from the features we actually downloaded.
        pub, actual = layer.declared_wgs, layer.actual_wgs
        if pub is not None and getattr(pub, "available", False) and actual and not layer.sampled:
            pw = max(pub.east - pub.west, 0.0) * max(pub.north - pub.south, 0.0)
            aw = max(actual[2] - actual[0], 0.0) * max(actual[3] - actual[1], 0.0)
            span_km = max(actual[3] - actual[1], actual[2] - actual[0]) * 111.32
            if aw > 0 and pw > aw * 50:
                diags.append(Diagnostic(
                    "warning", "Published extent is far bigger than the data",
                    f"{ctx} advertises an extent about {pw / aw:.0f}x larger than the area "
                    f"its features actually cover (the data spans roughly "
                    f"{span_km:.0f} km).",
                    "Clients use the published extent for Zoom to Layer, so it zooms to "
                    "the wrong place. Re-calculate it in ArcGIS Online "
                    "(item ▸ Data ▸ Update Extent).",
                ))

        if layer.crs is not None and layer.native_bounds is not None:
            _check(diags, layer.crs, layer.native_bounds, layer.location, ctx,
                   feature_count=layer.feature_count,
                   geometry_type=layer.geometry_type,
                   invalid_geometry_count=layer.invalid_geometry_count,
                   invalid_geometry_sampled=layer.invalid_geometry_sampled,
                   invalid_geometry_reason=layer.invalid_geometry_reason)


def _dedupe(diags: List[Diagnostic]) -> List[Diagnostic]:
    """Drop repeats — several layers often share one broken source, and saying so once
    is more useful than saying it five times."""
    seen = set()
    out = []
    for d in diags:
        key = (d.severity, d.title, d.detail)
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return out


def _layer_file_diagnostics(diags: List[Diagnostic], report) -> None:
    """An ArcGIS layer file points at data — so check the pointer, then the data."""
    from .arcgis import temp_kind

    multi = len(report.layers) > 1
    for layer in report.layers:
        ctx = f"'{layer.name}'" if multi else "this layer"
        where = temp_kind(layer.source_path) if layer.source_path else None

        if layer.source_missing:
            if where == "pro":
                diags.append(Diagnostic(
                    "error", "Broken link to a temporary geodatabase",
                    f"{ctx} points at {layer.source_path}, which is gone. That's inside a "
                    "folder ArcGIS Pro creates for scratch data and later cleans up.",
                    "The features were saved to Pro's default scratch geodatabase rather "
                    "than a real one, so they're likely lost. Re-export from the original "
                    "source into a permanent geodatabase, then repoint the layer.",
                ))
            elif where == "temp":
                diags.append(Diagnostic(
                    "error", "Broken data source (it was in a temp folder)",
                    f"{ctx} points at {layer.source_path}, which is gone — and it lived in "
                    "a temporary folder, so it was probably cleaned up automatically.",
                    "Re-create the data somewhere permanent, then repoint the layer.",
                ))
            else:
                diags.append(Diagnostic(
                    "error", "Broken data source",
                    f"{ctx} points at {layer.source_path}, which doesn't exist.",
                    "This is the red exclamation mark in ArcGIS Pro. If the data moved, "
                    "repoint the layer (right-click ▸ Properties ▸ Source ▸ Set Data Source).",
                ))
        elif where:
            diags.append(Diagnostic(
                "warning", "Data lives in a temporary folder",
                f"{ctx} reads from {layer.source_path}, which "
                + ("ArcGIS Pro cleans up." if where == "pro"
                   else "Windows clears out periodically."),
                "It works now but will break. Copy the data somewhere permanent and "
                "repoint the layer before sharing this file.",
            ))
        elif layer.source_path is None and layer.source_kind:
            diags.append(Diagnostic(
                "info", "Layer reads from a service or database",
                f"{ctx} connects to a {layer.source_kind} rather than a file, so Kestrel "
                "can't check the data itself.",
                "Whether it draws depends on that connection and your credentials.",
            ))

        if layer.definition_query:
            query = layer.definition_query
            shown = query if len(query) <= 160 else query[:160] + "…"
            diags.append(Diagnostic(
                "warning", "A definition query is filtering this layer",
                f"{ctx} only shows features matching: {shown}",
                "This is a very common reason features seem to be missing — the data is "
                "there, the layer is hiding it. Clear the query in Layer Properties ▸ "
                "Definition Query to see everything.",
            ))

        if layer.visible is False:
            diags.append(Diagnostic(
                "info", "Layer is turned off",
                f"{ctx} is unchecked, so it won't draw when the file is added.",
                "Tick it in the Contents pane.",
            ))

        if layer.read_error:
            diags.append(Diagnostic(
                "error", "Data source could not be read",
                f"{ctx} points at {layer.source_path}, but it wouldn't open: "
                f"{layer.read_error}",
                "The file may be locked by another program, corrupt, or need a driver "
                "Kestrel doesn't bundle.",
            ))
            continue

        if layer.crs is not None and layer.native_bounds is not None:
            _check(diags, layer.crs, layer.native_bounds, layer.location, ctx,
                   feature_count=layer.feature_count,
                   geometry_type=layer.geometry_type,
                   invalid_geometry_count=layer.invalid_geometry_count,
                   invalid_geometry_sampled=layer.invalid_geometry_sampled,
                   invalid_geometry_reason=layer.invalid_geometry_reason)


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
           invalid_geometry_reason: Optional[str] = None,
           is_attribute_table: bool = False) -> None:

    # --- attribute-only table: normal inside a GeoPackage, not a fault ---
    if is_attribute_table:
        diags.append(Diagnostic(
            "info", "Attribute table (no geometry)",
            f"The {ctx} holds attributes only — no geometry column, so there's nothing to "
            "draw. That's perfectly normal for lookup and join tables inside a GeoPackage.",
            "Nothing to fix. QGIS lists it as a non-spatial table rather than a map layer.",
        ))
        return

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

    # An empty layer's bounding box is meaningless — running the coordinate checks on it
    # produced false "CRS mismatch" warnings on perfectly ordinary empty exports.
    if feature_count == 0:
        return

    # --- extent validity (stop here if there's nothing usable) ---
    if bounds is None or any(_bad(v) for v in bounds):
        diags.append(Diagnostic(
            "error", "Invalid extent",
            f"The {ctx} has no valid bounding box (empty or NaN extent).",
            "This usually points to empty or corrupt geometry — try re-exporting the data.",
        ))
        return

    xmin, ymin, xmax, ymax = bounds

    # A single point legitimately has a zero-area extent — only flag it when several
    # features somehow share one location.
    if xmin == xmax and ymin == ymax and (feature_count is None or feature_count > 1):
        diags.append(Diagnostic(
            "warning", "Zero-area extent",
            f"All {feature_count if feature_count else ''} features in the {ctx} sit at the "
            f"same point ({xmin:g}, {ymin:g}).",
            "Usually means the coordinates were lost or overwritten on export. "
            "Check a few rows in the attribute table.",
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

        if crs.is_projected and _is_linear_unit(unit) and looks_ll:
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

        # --- UTM sanity, judged on the NATIVE coordinates ---
        #
        # These deliberately do NOT use the reprojected lat/lon: that is derived *from* the
        # declared CRS, so it always agrees with it and can never reveal a mislabelled zone.
        # Raw eastings/northings are an independent signal.
        zinfo = _parse_utm_zone(crs.utm_zone)
        if zinfo and crs.is_projected:
            znum, zhemi = zinfo
            if zhemi == "N" and ymin < -1000:
                diags.append(Diagnostic(
                    "warning", "UTM zone is the wrong hemisphere",
                    f"The CRS is UTM zone {crs.utm_zone} (northern), but the northings go "
                    f"negative ({ymin:g}), which only happens south of the equator.",
                    f"A north/south mix-up shifts data by thousands of km. Try UTM zone "
                    f"{znum}S instead.",
                ))
            elif zhemi == "S" and ymax > 10_100_000:
                diags.append(Diagnostic(
                    "warning", "UTM zone is the wrong hemisphere",
                    f"The CRS is UTM zone {crs.utm_zone} (southern), but the northings "
                    f"({ymax:g}) exceed the southern-hemisphere range.",
                    f"Try the northern zone, UTM {znum}N.",
                ))
            # Eastings in any UTM zone stay near the 500 000 m central meridian; a long way
            # outside means the numbers belong to a different zone.
            if xmin < -200_000 or xmax > 1_200_000:
                off = max(abs(500_000 - xmin), abs(xmax - 500_000))
                zones_off = int(off // 400_000)
                diags.append(Diagnostic(
                    "warning", "Eastings don't fit this UTM zone",
                    f"The CRS is UTM zone {crs.utm_zone}, but the eastings run "
                    f"{xmin:g} … {xmax:g} — far outside the ~100 000–900 000 m a single zone "
                    f"covers (roughly {zones_off} zone(s) away).",
                    "The data was probably created in a different UTM zone than the one "
                    "recorded. Confirm the source zone before using these coordinates.",
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
