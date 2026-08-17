"""Read LAS / LAZ point clouds — lidar and drone photogrammetry output.

Only the header is read, so this stays fast on multi-gigabyte files: the header
carries the CRS, point count, bounds (including elevation), point format, and the
per-return counts. That's everything needed to answer "is this in the right place
and the right coordinate system" without touching a single point record.
"""

from __future__ import annotations

import os
from typing import Optional

from .models import CrsInfo, InspectionReport, LayerInfo, LocationInfo

LAS_EXTS = {".las", ".laz"}

# ASPRS standard classes worth naming; anything else is reported by number.
CLASSES = {
    0: "never classified", 1: "unclassified", 2: "ground", 3: "low vegetation",
    4: "medium vegetation", 5: "high vegetation", 6: "building", 7: "low point (noise)",
    9: "water", 10: "rail", 11: "road surface", 13: "wire guard", 14: "wire conductor",
    15: "transmission tower", 17: "bridge deck", 18: "high noise",
}


def inspect_pointcloud(path, file_name, size) -> InspectionReport:
    try:
        import laspy
    except ImportError:
        return InspectionReport(
            path=path, file_name=file_name, size_bytes=size, kind="pointcloud",
            driver="LAS/LAZ",
            error="reading point clouds needs the laspy package "
                  "(py -m pip install laspy lazrs)")

    try:
        with laspy.open(path) as reader:
            header = reader.header
            return _from_header(path, file_name, size, header)
    except Exception as exc:
        return InspectionReport(
            path=path, file_name=file_name, size_bytes=size, kind="pointcloud",
            driver="LAS/LAZ", error=str(exc))


def _from_header(path, file_name, size, header) -> InspectionReport:
    from .inspector import _parse_crs, bounds_to_wgs84

    crs_info, crs_obj = CrsInfo(defined=False), None
    try:
        crs = header.parse_crs()
        if crs is not None:
            crs_info, crs_obj = _parse_crs(crs)
    except Exception:
        pass

    mins, maxs = list(header.mins), list(header.maxs)
    bounds = (float(mins[0]), float(mins[1]), float(maxs[0]), float(maxs[1]))

    fields = [("X", "coord"), ("Y", "coord"), ("Z", "coord")]
    try:
        extra = [d.name for d in header.point_format.dimensions]
        fields = [(n, "") for n in extra]
    except Exception:
        pass

    layer = LayerInfo(
        name=os.path.splitext(file_name)[0],
        geometry_type="PointCloud",
        feature_count=int(header.point_count),
        fields=fields,
        native_bounds=bounds,
        crs=crs_info,
        location=bounds_to_wgs84(bounds, crs_obj),
    )
    layer.z_min = float(mins[2])
    layer.z_max = float(maxs[2])
    layer.las_version = f"{header.version.major}.{header.version.minor}"
    layer.point_format = int(header.point_format.id)
    layer.returns = _returns(header)
    layer.point_density = _density(header, bounds, crs_info)
    layer.preview = None            # a header read gives us no geometry to draw

    report = InspectionReport(
        path=path, file_name=file_name, size_bytes=size, kind="pointcloud",
        driver=f"LAS {layer.las_version} (point format {layer.point_format})",
        layers=[layer])
    return report


def _returns(header) -> Optional[list]:
    try:
        counts = list(header.number_of_points_by_return)
    except Exception:
        return None
    return [int(c) for c in counts if c] or None


def _density(header, bounds, crs_info) -> Optional[float]:
    """Points per square metre, when the CRS is in linear units we can trust."""
    try:
        if not crs_info.defined or not crs_info.is_projected:
            return None
        unit = (crs_info.unit or "").lower()
        if "metre" not in unit and "meter" not in unit:
            return None
        area = (bounds[2] - bounds[0]) * (bounds[3] - bounds[1])
        if area <= 0:
            return None
        return float(header.point_count) / area
    except Exception:
        return None
