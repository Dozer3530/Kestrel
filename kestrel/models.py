"""Dataclasses describing the result of inspecting a geospatial file."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class CrsInfo:
    """Everything we could learn about a layer/raster coordinate reference system."""

    defined: bool
    epsg: Optional[int] = None
    name: Optional[str] = None
    is_projected: Optional[bool] = None
    is_geographic: Optional[bool] = None
    utm_zone: Optional[str] = None          # e.g. "11N", or None when not a UTM CRS
    unit: Optional[str] = None              # e.g. "metre", "degree"
    datum: Optional[str] = None
    area_of_use: Optional[str] = None       # human description of where the CRS is valid
    area_bounds: Optional[Tuple[float, float, float, float]] = None  # (w, s, e, n) lon/lat
    wkt: Optional[str] = None

    @property
    def summary(self) -> str:
        if not self.defined:
            return "Undefined / missing"
        code = f"EPSG:{self.epsg}" if self.epsg else "no EPSG code"
        return f"{self.name} ({code})"


@dataclass
class LocationInfo:
    """Real-world location of the data, in WGS84 lon/lat degrees."""

    available: bool
    west: Optional[float] = None
    south: Optional[float] = None
    east: Optional[float] = None
    north: Optional[float] = None
    center_lat: Optional[float] = None
    center_lon: Optional[float] = None
    note: Optional[str] = None   # why it is unavailable, when applicable


@dataclass
class LayerInfo:
    """A single vector layer (a dataset may contain several, e.g. a GeoPackage)."""

    name: str
    geometry_type: Optional[str]
    feature_count: Optional[int]
    fields: List[Tuple[str, str]]                       # (name, type) pairs
    native_bounds: Optional[Tuple[float, float, float, float]]  # (xmin, ymin, xmax, ymax)
    crs: CrsInfo
    location: LocationInfo
    has_prj: Optional[bool] = None   # for shapefiles: whether a .prj sidecar is present


@dataclass
class RasterInfo:
    """A raster dataset (GeoTIFF, etc.)."""

    width: int
    height: int
    band_count: int
    dtype: Optional[str]
    nodata: Optional[float]
    res_x: Optional[float]
    res_y: Optional[float]
    native_bounds: Optional[Tuple[float, float, float, float]]
    crs: CrsInfo
    location: LocationInfo


@dataclass
class Diagnostic:
    """One thing worth flagging — typically a reason a layer won't draw in QGIS."""

    severity: str   # "error" | "warning" | "info"
    title: str
    detail: str
    suggested_fix: str = ""


@dataclass
class InspectionReport:
    """The full result of inspecting a file."""

    path: str
    file_name: str
    size_bytes: Optional[int]
    kind: str                                      # "vector" | "raster" | "unknown"
    driver: Optional[str]
    layers: List[LayerInfo] = field(default_factory=list)        # for vector
    raster: Optional[RasterInfo] = None                          # for raster
    diagnostics: List[Diagnostic] = field(default_factory=list)
    error: Optional[str] = None                    # fatal read error message

    @property
    def is_vector(self) -> bool:
        return self.kind == "vector"

    @property
    def is_raster(self) -> bool:
        return self.kind == "raster"
