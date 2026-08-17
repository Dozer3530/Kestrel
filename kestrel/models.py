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
    read_error: Optional[str] = None         # set when this one layer failed to read
    invalid_geometry_count: Optional[int] = None   # None = not checked; >=0 = # invalid in sample
    invalid_geometry_sampled: Optional[int] = None # how many features were validity-checked
    invalid_geometry_reason: Optional[str] = None  # first invalidity reason, if any
    coord_columns: Optional[Tuple[str, str]] = None  # CSV/Excel: detected (x, y) column names
    crs_guess: Optional[str] = None          # CSV/Excel: inferred CRS note (no CRS in the file)
    preview: Optional[dict] = None           # WGS84 shapes for the map: points/lines/polygons
    # ArcGIS layer files (.lyrx) reference data rather than containing it:
    source_path: Optional[str] = None        # the data this layer points at
    source_kind: Optional[str] = None        # FileGDB, Shapefile, service, ...
    source_missing: Optional[bool] = None    # True when that data isn't there any more
    definition_query: Optional[str] = None   # a filter that can hide features
    visible: Optional[bool] = None           # layer turned off in the map
    # ArcGIS REST services publish an extent that can drift from the real data:
    declared_bounds: Optional[Tuple[float, float, float, float]] = None
    max_record_count: Optional[int] = None   # server's per-request cap
    sampled: Optional[bool] = None           # True when we hit our own read cap
    declared_wgs: Optional[object] = None    # published extent as LocationInfo
    actual_wgs: Optional[Tuple[float, float, float, float]] = None  # measured footprint
    # LAS / LAZ point clouds:
    z_min: Optional[float] = None
    z_max: Optional[float] = None
    las_version: Optional[str] = None
    point_format: Optional[int] = None
    returns: Optional[List[int]] = None
    point_density: Optional[float] = None    # points per m², when the CRS allows it


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
    service_title: Optional[str] = None            # for ArcGIS REST services
    service_capabilities: Optional[str] = None
    portal_access: Optional[str] = None            # 'private'/'public' from a .pitemx

    @property
    def is_pointcloud(self) -> bool:
        return self.kind == "pointcloud"

    @property
    def is_service(self) -> bool:
        return self.kind == "service"

    @property
    def is_layer_file(self) -> bool:
        return self.kind == "layerfile"

    @property
    def is_vector(self) -> bool:
        return self.kind == "vector"

    @property
    def is_raster(self) -> bool:
        return self.kind == "raster"
