"""Work out which CRS a file with no CRS is *probably* in.

The hard part is that raw coordinates are ambiguous. A UTM northing pins the **latitude**
tightly, but the same easting/northing pair is a perfectly valid location in every one of
the 60 zones — only the longitude changes::

    (500000, 5650000) as UTM 11N -> 51.00N, 117.00W
    (500000, 5650000) as UTM 12N -> 51.00N, 111.00W
    (500000, 5650000) as UTM 13N -> 51.00N, 105.00W

So the zone can't come from the numbers. It has to come from *context*: neighbouring files
in the same folder that do declare a CRS, and the CRSs this user has picked before. That
turns the common real-world case — one shapefile in a delivery lost its ``.prj`` — into a
high-confidence answer instead of a guess.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional, Sequence

# Extensions worth peeking at when hunting for a neighbour's CRS.
_SIBLING_EXTS = {".shp", ".gpkg", ".geojson", ".tif", ".tiff", ".kml", ".gml", ".fgb"}
_MAX_SIBLINGS = 40          # keep folder scans cheap, especially on network drives
_METRES_PER_DEGREE = 111320.0


@dataclass
class CrsCandidate:
    """One suggested CRS, with the reasoning shown to the user."""

    epsg: int
    name: str
    confidence: str           # "high" | "medium" | "low"
    reason: str

    @property
    def label(self) -> str:
        return f"EPSG:{self.epsg} — {self.name}"


def _crs_name(epsg: int) -> Optional[str]:
    from pyproj import CRS

    try:
        return CRS.from_epsg(epsg).name
    except Exception:
        return None


def _looks_lonlat(bounds) -> bool:
    xmin, ymin, xmax, ymax = bounds
    return (-180.0 <= xmin <= 180.0 and -180.0 <= xmax <= 180.0
            and -90.0 <= ymin <= 90.0 and -90.0 <= ymax <= 90.0)


def _looks_utm(bounds) -> bool:
    """Easting/northing magnitudes typical of a UTM grid in metres."""
    xmin, ymin, xmax, ymax = bounds
    return (0.0 < xmin and xmax < 1_200_000.0
            and -1_000_000.0 < ymin and ymax < 10_100_000.0
            and (xmax > 1000 or ymax > 1000))


def latitude_from_northing(northing: float) -> float:
    """Approximate latitude implied by a UTM northing (southern uses a 10 000 km offset)."""
    if northing > 10_000_000:
        northing -= 10_000_000
    if northing < 0:                       # southern hemisphere written as negative
        return northing / _METRES_PER_DEGREE
    return northing / _METRES_PER_DEGREE


def context_crs_list(path: str, limit: int = _MAX_SIBLINGS) -> List[int]:
    """EPSG codes used by other geospatial files sitting in the same folder."""
    found: List[int] = []
    folder = os.path.dirname(os.path.abspath(path))
    target = os.path.abspath(path)
    try:
        names = sorted(os.listdir(folder))[:limit * 3]
    except OSError:
        return found

    import pyogrio

    from .inspector import _gdal_path, _parse_crs

    checked = 0
    for name in names:
        if checked >= limit:
            break
        full = os.path.join(folder, name)
        if os.path.abspath(full) == target:
            continue
        if os.path.splitext(name)[1].lower() not in _SIBLING_EXTS:
            continue
        checked += 1
        try:
            info = pyogrio.read_info(_gdal_path(full))
            crs_info, _ = _parse_crs(info.get("crs"))
        except Exception:
            try:
                import rasterio

                with rasterio.open(_gdal_path(full)) as ds:
                    crs_info, _ = _parse_crs(ds.crs.to_wkt() if ds.crs else None)
            except Exception:
                continue
        if crs_info.defined and crs_info.epsg and crs_info.epsg not in found:
            found.append(crs_info.epsg)
    return found


def suggest_crs(report, *, recent: Optional[Sequence[int]] = None,
                use_siblings: bool = True) -> List[CrsCandidate]:
    """Rank plausible CRSs for a file that doesn't declare one.

    ``recent`` is the user's recently-chosen EPSG codes, most recent first.
    """
    from pyproj import CRS
    from pyproj.aoi import AreaOfInterest
    from pyproj.database import query_utm_crs_info

    out: List[CrsCandidate] = []
    seen = set()

    def add(epsg, confidence, reason):
        if epsg is None or epsg in seen:
            return
        name = _crs_name(epsg)
        if not name:
            return
        seen.add(epsg)
        out.append(CrsCandidate(epsg, name, confidence, reason))

    layer = report.layers[0] if report.layers else None
    bounds = layer.native_bounds if layer else None
    if not bounds or any(b is None for b in bounds):
        return out

    xmin, ymin, xmax, ymax = bounds

    # --- degrees: nearly unambiguous -------------------------------------- #
    if _looks_lonlat(bounds):
        add(4326, "high",
            "The coordinates are within ±180 / ±90, so they're almost certainly "
            "longitude/latitude degrees.")
        add(4269, "low", "NAD83 lon/lat — same numbers, a North American datum.")
        return out

    # --- projected: use context to pin the zone --------------------------- #
    context: List[int] = []
    if use_siblings:
        try:
            context = context_crs_list(report.path)
        except Exception:
            context = []
    for code in (recent or []):
        if code not in context:
            context.append(code)

    approx_lat = latitude_from_northing((ymin + ymax) / 2.0) if _looks_utm(bounds) else None

    # Neighbouring files are the strongest signal we have.
    for epsg in context:
        try:
            crs = CRS.from_epsg(epsg)
        except Exception:
            continue
        if not crs.is_projected:
            continue
        where = "a file in the same folder" if epsg in (context[:len(context)]) else "recent use"
        reason = f"Used by {where}"
        if approx_lat is not None and crs.area_of_use:
            s, n = crs.area_of_use.bounds[1], crs.area_of_use.bounds[3]
            if s - 2 <= approx_lat <= n + 2:
                add(epsg, "high",
                    f"{reason}, and the northings imply latitude ≈ {approx_lat:.1f}°, "
                    f"which falls inside this CRS's valid area.")
                continue
            add(epsg, "low",
                f"{reason}, but the northings imply latitude ≈ {approx_lat:.1f}°, "
                f"outside this CRS's area ({s:.0f}° to {n:.0f}°).")
            continue
        add(epsg, "medium", f"{reason}.")

    # With a latitude and a neighbouring longitude we can name actual UTM zones.
    if approx_lat is not None:
        lon_hint = _longitude_hint(context, approx_lat)
        if lon_hint is not None:
            aoi = AreaOfInterest(west_lon_degree=lon_hint - 3, south_lat_degree=approx_lat - 1,
                                 east_lon_degree=lon_hint + 3, north_lat_degree=approx_lat + 1)
            for datum in ("WGS 84", "NAD83"):
                try:
                    for info in query_utm_crs_info(datum_name=datum, area_of_interest=aoi):
                        add(int(info.code), "medium",
                            f"A UTM zone covering latitude ≈ {approx_lat:.1f}°, near where "
                            f"the other files in this folder sit.")
                except Exception:
                    pass
        elif not out:
            out.append(CrsCandidate(
                0, f"Unknown UTM zone at latitude ≈ {approx_lat:.1f}°", "low",
                "The northings pin the latitude, but the easting alone can't identify the "
                "zone — every UTM zone would look equally valid. Pick the zone your data "
                "was collected in, or open a correctly-tagged file from the same project.",
            ))

    return out


def _longitude_hint(context_epsgs: Sequence[int], approx_lat: float) -> Optional[float]:
    """A representative longitude from any context CRS's area of use."""
    from pyproj import CRS

    for epsg in context_epsgs:
        try:
            aou = CRS.from_epsg(epsg).area_of_use
        except Exception:
            continue
        if aou:
            return (aou.bounds[0] + aou.bounds[2]) / 2.0
    return None


def search_crs(text: str, limit: int = 40) -> List[CrsCandidate]:
    """Free-text search across EPSG definitions, e.g. 'NAD83 UTM 11' or '26911'."""
    from pyproj.database import query_crs_info

    query = (text or "").strip()
    if not query:
        return []

    if query.upper().startswith("EPSG:"):
        query = query[5:].strip()
    if query.isdigit():
        name = _crs_name(int(query))
        if name:
            return [CrsCandidate(int(query), name, "high", "Matched by EPSG code.")]

    words = [w for w in query.lower().replace(":", " ").split() if w]
    results: List[CrsCandidate] = []
    try:
        entries = query_crs_info(auth_name="EPSG",
                                 pj_types=["PROJECTED_CRS", "GEOGRAPHIC_2D_CRS"])
    except Exception:
        return results
    for info in entries:
        haystack = info.name.lower()
        if all(w in haystack for w in words):
            results.append(CrsCandidate(int(info.code), info.name, "medium", "Name match."))
            if len(results) >= limit:
                break
    return results
