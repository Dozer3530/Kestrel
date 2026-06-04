"""Plain-text rendering of an :class:`InspectionReport`.

Shared by the CLI (printed to the terminal) and the GUI's "Copy report" button.
Kept ASCII-only so it never trips Windows console encodings.
"""

from __future__ import annotations

import math
from typing import List

from .models import CrsInfo, InspectionReport, LocationInfo

_SEV = {"error": "[ERROR]", "warning": "[WARN] ", "info": "[INFO] "}


def _fmt_bytes(n) -> str:
    if n is None:
        return "unknown"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _ok(v) -> bool:
    try:
        return not (math.isnan(float(v)) or math.isinf(float(v)))
    except (TypeError, ValueError):
        return False


def _fmt_bounds(b) -> str:
    xmin, ymin, xmax, ymax = b
    return f"{xmin:.3f}, {ymin:.3f}  ->  {xmax:.3f}, {ymax:.3f}"


def _crs_lines(crs: CrsInfo) -> List[str]:
    if not crs.defined:
        return ["  CRS:            (!) UNDEFINED / MISSING"]
    code = f"EPSG:{crs.epsg}" if crs.epsg else "(no EPSG code)"
    kind = "Projected" if crs.is_projected else ("Geographic" if crs.is_geographic else "?")
    lines = [
        f"  CRS:            {crs.name}  [{code}]",
        f"  Type:           {kind}" + (f", units: {crs.unit}" if crs.unit else ""),
    ]
    if crs.utm_zone:
        lines.append(f"  UTM zone:       {crs.utm_zone}")
    if crs.datum:
        lines.append(f"  Datum:          {crs.datum}")
    if crs.area_of_use:
        lines.append(f"  CRS valid for:  {crs.area_of_use}")
    return lines


def _loc_lines(loc: LocationInfo) -> List[str]:
    if not loc.available:
        return [f"  Location:       (unavailable - {loc.note})"]
    return [
        f"  Center lat/lon: {loc.center_lat:.5f}, {loc.center_lon:.5f}",
        f"  Lat/Lon bbox:   W {loc.west:.5f}  S {loc.south:.5f}  "
        f"E {loc.east:.5f}  N {loc.north:.5f}",
    ]


def format_report_text(report: InspectionReport) -> str:
    out: List[str] = []
    bar = "=" * 66
    out += [bar, f"  {report.file_name}", bar]
    if report.error:
        out.append(f"ERROR: {report.error}")
    out.append(f"Path:    {report.path}")
    out.append(f"Size:    {_fmt_bytes(report.size_bytes)}")
    out.append(f"Type:    {report.kind}" + (f"  (driver: {report.driver})" if report.driver else ""))
    out.append("")

    if report.is_vector:
        for layer in report.layers:
            head = f"LAYER: {layer.name}" if len(report.layers) > 1 else "LAYER"
            out.append(head)
            out.append(f"  Geometry:       {layer.geometry_type}")
            out.append(f"  Features:       {layer.feature_count}")
            out += _crs_lines(layer.crs)
            out += _loc_lines(layer.location)
            if layer.native_bounds and all(_ok(v) for v in layer.native_bounds):
                out.append(f"  Native extent:  {_fmt_bounds(layer.native_bounds)}")
            if layer.fields:
                joined = ", ".join(f"{n} ({t})" if t else n for n, t in layer.fields)
                out.append(f"  Fields ({len(layer.fields)}):    {joined}")
            out.append("")

    elif report.is_raster and report.raster:
        r = report.raster
        out.append("RASTER")
        out.append(f"  Dimensions:     {r.width} x {r.height} px, {r.band_count} band(s)")
        out.append(f"  Data type:      {r.dtype}" + (f", nodata: {r.nodata}" if r.nodata is not None else ""))
        if r.res_x:
            out.append(f"  Pixel size:     {r.res_x} x {r.res_y}")
        out += _crs_lines(r.crs)
        out += _loc_lines(r.location)
        if r.native_bounds and all(_ok(v) for v in r.native_bounds):
            out.append(f"  Native extent:  {_fmt_bounds(r.native_bounds)}")
        out.append("")

    out.append("-" * 66)
    if report.diagnostics:
        out.append(f"DIAGNOSTICS ({len(report.diagnostics)})")
        out.append("-" * 66)
        for d in report.diagnostics:
            out.append(f"{_SEV.get(d.severity, '[?]')}  {d.title}")
            out.append(f"           {d.detail}")
            if d.suggested_fix:
                out.append(f"           -> {d.suggested_fix}")
            out.append("")
    else:
        out.append("No problems detected. OK")
        out.append("")

    return "\n".join(out)
