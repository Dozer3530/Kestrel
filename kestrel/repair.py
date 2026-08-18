"""Safe, non-destructive repairs for the problems Kestrel detects.

Design rules (these are deliberate — GIS field data is often irreplaceable):

* **Never modify the input.** Every operation writes a NEW file; the original is only
  ever opened for reading.
* **Write atomically.** Output goes to a temporary name in the destination folder and is
  renamed into place only after a successful write, so a failure can't leave a readable
  but truncated dataset behind.
* **Verify afterwards.** Every repair re-inspects its own output and reports what the
  result actually looks like, rather than assuming the write did what we asked.
* **Preview before writing.** :func:`plan_repair` describes what *would* happen without
  touching the disk — that's what batch mode confirms against.

No geopandas at runtime: the frozen build deliberately excludes it, so everything here
goes through ``pyogrio.raw`` + shapely + pyproj.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import List, Optional

# Operation identifiers
ASSIGN_CRS = "assign_crs"
REPROJECT = "reproject"
FIX_GEOMETRY = "fix_geometry"
CONVERT = "convert"
TABLE_TO_POINTS = "table_to_points"

# target format -> (driver, extension)
FORMATS = {
    "gpkg": ("GPKG", ".gpkg"),
    "shp": ("ESRI Shapefile", ".shp"),
    "geojson": ("GeoJSON", ".geojson"),
}

# Formats that cannot carry certain things; surfaced as warnings during preview.
_SHP_LIMITS = (
    "Shapefile limits: field names are truncated to 10 characters, text to 254, "
    "date/time becomes text, and each file is capped at 2 GB."
)


@dataclass
class RepairPlan:
    """What a repair *would* do. Produced without touching the disk."""

    operation: str
    source: str
    target: str
    description: str
    ok: bool = True
    warnings: List[str] = field(default_factory=list)
    blocker: Optional[str] = None       # why it can't run, when ok is False


@dataclass
class RepairResult:
    """What a repair actually did."""

    operation: str
    source: str
    target: Optional[str]
    ok: bool
    message: str
    warnings: List[str] = field(default_factory=list)
    verification: Optional[str] = None  # re-inspection of the written file


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def esri_wkt(crs_input) -> str:
    """WKT in the flavour a shapefile ``.prj`` needs.

    pyproj's default ``to_wkt()`` is WKT2, which GDAL reads back as *no CRS* from a
    ``.prj``. Anything writing a sidecar must use this instead.
    """
    from pyproj import CRS

    crs = CRS.from_user_input(crs_input)
    try:
        return crs.to_wkt("WKT1_ESRI")
    except Exception:
        return crs.to_wkt()


def _unique_path(folder: str, stem: str, ext: str) -> str:
    """A path in ``folder`` that doesn't exist yet (never clobbers an earlier repair)."""
    candidate = os.path.join(folder, stem + ext)
    if not os.path.exists(candidate):
        return candidate
    for n in range(2, 1000):
        candidate = os.path.join(folder, f"{stem}_{n}{ext}")
        if not os.path.exists(candidate):
            return candidate
    return os.path.join(folder, f"{stem}_{os.getpid()}{ext}")


def _sidecars(path: str) -> List[str]:
    """All files belonging to a dataset (shapefile sidecars; just the file otherwise)."""
    base, ext = os.path.splitext(path)
    if ext.lower() != ".shp":
        return [path]
    out = []
    for e in (".shp", ".shx", ".dbf", ".prj", ".cpg", ".qix", ".sbn", ".sbx", ".shp.xml"):
        p = base + e
        if os.path.exists(p):
            out.append(p)
    return out or [path]


def _finalize(tmp_path: str, final_path: str) -> None:
    """Move a completed temp dataset (plus sidecars) into place."""
    tmp_base, _ = os.path.splitext(tmp_path)
    final_base, _ = os.path.splitext(final_path)
    for src in _sidecars(tmp_path):
        ext = src[len(tmp_base):]
        shutil.move(src, final_base + ext)


def _cleanup(tmp_path: str) -> None:
    tmp_base, _ = os.path.splitext(tmp_path)
    folder = os.path.dirname(tmp_path) or "."
    stem = os.path.basename(tmp_base)
    try:
        for name in os.listdir(folder):
            if name.startswith(stem):
                try:
                    os.remove(os.path.join(folder, name))
                except OSError:
                    pass
    except OSError:
        pass


def _verify(path: str) -> str:
    """Re-inspect a written file and summarize it — proof the repair landed."""
    from .inspector import inspect_path

    try:
        rep = inspect_path(path)
    except Exception as exc:                      # pragma: no cover - defensive
        return f"could not re-read the output: {exc}"
    if rep.error:
        return f"output could not be read back: {rep.error}"
    if rep.is_raster and rep.raster:
        return f"raster {rep.raster.width}x{rep.raster.height}, CRS {rep.raster.crs.summary}"
    if not rep.layers:
        return "output has no readable layers"
    layer = rep.layers[0]
    bits = [f"{layer.feature_count} feature(s)", f"CRS {layer.crs.summary}"]
    if layer.invalid_geometry_count:
        bits.append(f"{layer.invalid_geometry_count} still-invalid geometries")
    elif layer.invalid_geometry_count == 0:
        bits.append("geometry valid")
    return ", ".join(bits)


def _read_vector(source: str, layer_name: Optional[str]):
    """Read a whole layer through pyogrio.raw. Returns (meta, geometry_wkb, field_data)."""
    from pyogrio.raw import read as raw_read

    from .inspector import _gdal_path

    # GDAL virtual sources (ESRIJSON:https://..., /vsizip/...) must not be path-mangled.
    resolved = source if "://" in str(source) or str(source).startswith(
        ("ESRIJSON:", "/vsi")) else _gdal_path(source)
    kwargs = {"layer": layer_name} if layer_name else {}
    meta, _fids, geom, fields = raw_read(resolved, **kwargs)
    return meta, geom, fields


_TYPE_NAMES = {
    0: "Point", 1: "LineString", 2: "LinearRing", 3: "Polygon",
    4: "MultiPoint", 5: "MultiLineString", 6: "MultiPolygon", 7: "GeometryCollection",
}
_MULTI_OF = {0: 4, 4: 4, 1: 5, 5: 5, 3: 6, 6: 6}


def _geometry_type_for(geoms, fallback):
    """Derive the output geometry type from the real data.

    Trusting the source metadata breaks after a repair (make_valid can turn a Polygon into
    a MultiPolygon), and blanket-promoting to multi breaks plain point shapefiles.
    Returns ``(type_name, promote_to_multi)``.
    """
    import shapely

    if geoms is None:
        return fallback or "Unknown", False
    present = geoms != None                                   # noqa: E711
    if not present.any():
        return fallback or "Unknown", False
    ids = {int(i) for i in shapely.get_type_id(geoms[present])}
    # Keep the Z/M suffix. Without it a LINESTRING Z layer is declared plain LineString
    # and every elevation is silently dropped on write.
    suffix = ""
    try:
        if bool(shapely.has_z(geoms[present]).any()):
            suffix = " Z"
    except Exception:
        pass
    if len(ids) == 1:
        return _TYPE_NAMES.get(ids.pop(), fallback or "Unknown") + suffix, False
    families = {_MULTI_OF.get(i) for i in ids}
    if len(families) == 1 and None not in families:
        return _TYPE_NAMES[families.pop()] + suffix, True     # mixed single/multi -> promote
    return "Unknown", False


def _write_vector(target_tmp: str, meta, geom, fields, driver: str,
                  crs, layer: Optional[str], geometry_type: Optional[str] = None,
                  promote: bool = False):
    from pyogrio.raw import write as raw_write

    kwargs = {}
    if driver == "GPKG":
        kwargs["layer"] = layer or os.path.splitext(os.path.basename(target_tmp))[0]
    if promote:
        kwargs["promote_to_multi"] = True
    raw_write(
        target_tmp,
        geometry=geom,
        field_data=list(fields) if fields is not None else [],
        fields=list(meta.get("fields", [])),
        geometry_type=geometry_type or meta.get("geometry_type") or "Unknown",
        crs=crs,
        driver=driver,
        **kwargs,
    )


def _fmt_key(fmt: str) -> str:
    return (fmt or "gpkg").lower().lstrip(".")


def readable_source(report, layer=None):
    """Where the *data* actually lives, which isn't always ``report.path``.

    A layer file or portal item only points at data, and a service isn't a file at all —
    so repairs have to follow the pointer instead of handing GDAL the .lyrx/.pitemx.
    Returns ``(source, layer_name, stem, note)``; source is None when nothing is readable.
    """
    layer = layer or (report.layers[0] if report.layers else None)

    if report.is_service:
        if layer is None or not layer.source_path:
            return None, None, None, "this service exposes no readable layer"
        query = (f"{layer.source_path}/query?where=1%3D1&outFields=*&f=json"
                 f"&resultRecordCount={_SERVICE_EXPORT_LIMIT}&returnGeometry=true")
        return ("ESRIJSON:" + query, None, _safe_layer_name(layer.name),
                f"Reads up to {_SERVICE_EXPORT_LIMIT} features from the service.")

    if report.is_layer_file:
        if layer is None or not layer.source_path:
            return None, None, None, "this layer records no data source to work from"
        if layer.source_missing:
            return None, None, None, ("the data this layer points at is missing, so there's "
                                      "nothing to convert")
        sub = layer.name if str(layer.source_path).lower().endswith(".gdb") else None
        stem = os.path.splitext(os.path.basename(layer.source_path))[0] or layer.name
        return layer.source_path, sub, _safe_layer_name(stem), None

    return report.path, None, os.path.splitext(os.path.basename(report.path))[0], None


_SERVICE_EXPORT_LIMIT = 5000


def _safe_layer_name(name: str) -> str:
    """A layer name GDAL will accept (no spaces, punctuation or leading digit/dot)."""
    cleaned = "".join(c if (c.isalnum() or c == "_") else "_" for c in str(name or "layer"))
    cleaned = cleaned.strip("_") or "layer"
    if cleaned[0].isdigit():
        cleaned = "layer_" + cleaned
    return cleaned[:60]


# --------------------------------------------------------------------------- #
# preview
# --------------------------------------------------------------------------- #
def plan_repair(report, operation: str, out_dir: str, *, layer=None, epsg=None,
                target_format: str = "gpkg") -> RepairPlan:
    """Describe what a repair would do, without writing anything."""
    src = report.path
    warnings: List[str] = []

    def blocked(msg):
        return RepairPlan(operation, src, "", msg, ok=False, blocker=msg)

    if report.error:
        return blocked(f"file can't be read: {report.error}")
    if not out_dir:
        return blocked("no output folder chosen")

    # Follow layer files / portal items / services to the data they point at.
    data_src, _sub, stem, note = readable_source(report, layer)
    if data_src is None:
        return blocked(note or "there's no readable data behind this")
    if note:
        warnings.append(note)
    stem = stem or os.path.splitext(os.path.basename(src))[0]

    if len(report.layers or []) > 1 and operation != TABLE_TO_POINTS:
        others = ", ".join(l.name for l in report.layers[1:])
        warnings.append(
            f"This dataset has {len(report.layers)} layers and only "
            f"'{report.layers[0].name}' will be written. Not included: {others}.")

    if report.is_pointcloud:
        return blocked("Kestrel can't rewrite LAS/LAZ point clouds — set the CRS with "
                       "lasinfo or PDAL, or re-export from the software that made it")
    if report.is_raster and operation in (ASSIGN_CRS, FIX_GEOMETRY, TABLE_TO_POINTS):
        return blocked("that only applies to vector data — this is a raster")

    if report.is_service and operation == ASSIGN_CRS:
        return blocked("a service already declares its CRS — use Reproject or Convert "
                       "to write a copy in a different one")
    if (report.is_service or report.is_layer_file) and operation == TABLE_TO_POINTS:
        return blocked("that only applies to CSV/Excel tables")

    if operation in (ASSIGN_CRS, REPROJECT):
        if epsg is None:
            return blocked("no CRS chosen")

    # Layer files and services have no meaningful "own" format, so default to GeoPackage.
    native_fmt = (_fmt_key(os.path.splitext(src)[1])
                  if not (report.is_service or report.is_layer_file) else "gpkg")

    if operation == ASSIGN_CRS:
        fmt = native_fmt if native_fmt in FORMATS else "gpkg"
        driver, ext = FORMATS[fmt]
        target = _unique_path(out_dir, stem + "_crs", ext)
        desc = (f"Write a copy tagged EPSG:{epsg}. Coordinates are not changed — this only "
                f"records which CRS the existing numbers are in.")
        if report.layers and report.layers[0].crs.defined:
            warnings.append("This file already declares a CRS; assigning will override it.")
        if driver == "ESRI Shapefile":
            warnings.append(_SHP_LIMITS)
    elif operation == REPROJECT:
        fmt = native_fmt if native_fmt in FORMATS else "gpkg"
        driver, ext = FORMATS[fmt]
        target = _unique_path(out_dir, f"{stem}_epsg{epsg}", ext)
        desc = f"Transform the coordinates into EPSG:{epsg} and write a new file."
        if report.layers and not report.layers[0].crs.defined:
            return blocked("can't reproject a file with no CRS — assign its CRS first")
    elif operation == FIX_GEOMETRY:
        fmt = native_fmt if native_fmt in FORMATS else "gpkg"
        driver, ext = FORMATS[fmt]
        target = _unique_path(out_dir, stem + "_fixed", ext)
        bad = report.layers[0].invalid_geometry_count if report.layers else None
        if not bad:
            return blocked("no invalid geometry was found in the sample")
        desc = f"Repair {bad} invalid geometr(ies) and write a corrected copy."
        warnings.append("Repairing self-intersections can split a shape into multiple parts.")
    elif operation == CONVERT:
        fmt = _fmt_key(target_format)
        if fmt not in FORMATS:
            return blocked(f"unsupported target format: {target_format}")
        driver, ext = FORMATS[fmt]
        target = _unique_path(out_dir, stem, ext)
        desc = f"Write a copy as {driver}."
        if driver == "ESRI Shapefile":
            warnings.append(_SHP_LIMITS)
        if driver == "GeoJSON":
            warnings.append("GeoJSON output is reprojected to EPSG:4326, as the format expects.")
    elif operation == TABLE_TO_POINTS:
        fmt = _fmt_key(target_format)
        driver, ext = FORMATS.get(fmt, FORMATS["gpkg"])
        target = _unique_path(out_dir, stem + "_points", ext)
        if not report.layers or not report.layers[0].coord_columns:
            return blocked("no coordinate columns were detected in this table")
        if epsg is None:
            return blocked("choose the CRS the coordinates are in")
        x, y = report.layers[0].coord_columns
        desc = (f"Build point geometry from '{x}'/'{y}' as EPSG:{epsg} "
                f"and write a real spatial layer.")
    else:
        return blocked(f"unknown operation: {operation}")

    if not os.path.isdir(out_dir):
        warnings.append("The output folder will be created.")
    return RepairPlan(operation, src, target, desc, ok=True, warnings=warnings)


# --------------------------------------------------------------------------- #
# execution
# --------------------------------------------------------------------------- #
def apply_repair(report, plan: RepairPlan, *, layer=None, epsg=None,
                 target_format: str = "gpkg") -> RepairResult:
    """Carry out a previewed repair. Writes a new file; never touches the source."""
    if not plan.ok:
        return RepairResult(plan.operation, plan.source, None, False,
                            plan.blocker or "repair not possible")

    op = plan.operation
    target = plan.target
    out_dir = os.path.dirname(target)
    warnings = list(plan.warnings)

    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as exc:
        return RepairResult(op, plan.source, None, False,
                            f"can't create the output folder: {exc}")

    ext = os.path.splitext(target)[1]
    driver = next((d for d, e in FORMATS.values() if e == ext), "GPKG")
    tmp_fd, tmp_path = tempfile.mkstemp(prefix=".kestrel_", suffix=ext, dir=out_dir)
    os.close(tmp_fd)
    os.remove(tmp_path)                     # we only wanted a reserved unique name

    # Layer name comes from the FINAL target, never the temp file (whose name starts
    # with a dot and would be rejected by GDAL).
    out_layer = _safe_layer_name(os.path.splitext(os.path.basename(target))[0])

    try:
        if op == TABLE_TO_POINTS:
            warnings += _do_table_to_points(report, tmp_path, driver, epsg, out_layer)
        else:
            warnings += _do_vector_op(report, op, tmp_path, driver, epsg, layer, out_layer)

        if not os.path.exists(tmp_path):
            raise RuntimeError("the writer produced no output file")
        _finalize(tmp_path, target)
    except Exception as exc:
        _cleanup(tmp_path)
        return RepairResult(op, plan.source, None, False,
                            f"{type(exc).__name__}: {exc}", warnings)

    return RepairResult(op, plan.source, target, True,
                        f"Wrote {os.path.basename(target)}", warnings, _verify(target))


def _do_vector_op(report, op, tmp_path, driver, epsg, layer, out_layer) -> List[str]:
    import numpy as np
    import shapely

    warnings: List[str] = []
    data_src, sub, _stem, _note = readable_source(report)
    if data_src is None:
        raise RuntimeError("there's no readable data behind this file")

    layer_name = sub if sub is not None else layer
    if layer_name is None and not (report.is_service or report.is_layer_file):
        layer_name = report.layers[0].name if report.layers else None
    if layer_name in (None, "(default)"):
        layer_name = None

    meta, geom, fields = _read_vector(data_src, layer_name)
    src_crs = meta.get("crs")
    out_crs = src_crs
    geoms = shapely.from_wkb(geom) if geom is not None else None

    if op == ASSIGN_CRS:
        out_crs = f"EPSG:{epsg}"                       # coordinates untouched

    elif op == REPROJECT:
        if not src_crs:
            raise RuntimeError("source has no CRS to reproject from")
        geoms, note = _reproject(geoms, src_crs, f"EPSG:{epsg}")
        warnings += note
        out_crs = f"EPSG:{epsg}"

    elif op == FIX_GEOMETRY:
        geoms, note = _make_valid(geoms, driver)
        warnings += note

    elif op == CONVERT:
        if driver == "GeoJSON" and src_crs:
            crs_obj = _as_crs(src_crs)
            if crs_obj is not None and crs_obj.to_epsg() != 4326:
                geoms, note = _reproject(geoms, src_crs, "EPSG:4326")
                warnings += note
            out_crs = "EPSG:4326"
        if driver == "ESRI Shapefile":
            long_names = [f for f in meta.get("fields", []) if len(str(f)) > 10]
            if long_names:
                warnings.append(
                    "Field names longer than 10 characters will be shortened: "
                    + ", ".join(str(f) for f in long_names[:6])
                    + ("…" if len(long_names) > 6 else ""))

    out_geom = shapely.to_wkb(geoms) if geoms is not None else geom
    gtype, promote = _geometry_type_for(geoms, meta.get("geometry_type"))
    _write_vector(tmp_path, meta, out_geom, fields, driver, out_crs, out_layer,
                  geometry_type=gtype, promote=promote)
    return warnings


def _as_crs(crs_input):
    from pyproj import CRS

    try:
        return CRS.from_user_input(crs_input)
    except Exception:
        return None


def _reproject(geoms, src_crs, dst_crs):
    """Transform coordinates, preserving Z, and refuse to write non-finite results."""
    import numpy as np
    import shapely
    from pyproj import Transformer

    warnings: List[str] = []
    if geoms is None:
        return geoms, warnings

    transformer = Transformer.from_crs(_as_crs(src_crs), _as_crs(dst_crs), always_xy=True)
    geoms = shapely.from_wkb(shapely.to_wkb(geoms))          # copy, don't mutate input
    has_z = bool(np.any(shapely.has_z(geoms[geoms != None])))  # noqa: E711
    coords = shapely.get_coordinates(geoms, include_z=has_z)
    if coords.size:
        x, y = transformer.transform(coords[:, 0], coords[:, 1])
        bad = ~(np.isfinite(x) & np.isfinite(y))
        if bad.any():
            raise RuntimeError(
                f"{int(bad.sum())} coordinate(s) fall outside the target CRS's valid area — "
                "reprojection would produce invalid points, so nothing was written")
        coords[:, 0], coords[:, 1] = x, y
        shapely.set_coordinates(geoms, coords)
    return geoms, warnings


def _make_valid(geoms, driver):
    """Repair invalid geometry, keeping the original dimension where we can."""
    import numpy as np
    import shapely

    warnings: List[str] = []
    if geoms is None:
        return geoms, warnings

    present = geoms != None                                   # noqa: E711
    invalid = np.zeros(len(geoms), dtype=bool)
    invalid[present] = ~shapely.is_valid(geoms[present])
    n_bad = int(invalid.sum())
    if not n_bad:
        return geoms, warnings

    try:                       # 'structure' keeps polygons as polygons (shapely >= 2.1)
        fixed = shapely.make_valid(geoms[invalid], method="structure", keep_collapsed=False)
    except TypeError:
        fixed = shapely.make_valid(geoms[invalid])
        warnings.append("Used the default geometry repair; some shapes may change type.")

    geoms = geoms.copy()
    geoms[invalid] = fixed
    still_bad = int((~shapely.is_valid(geoms[present])).sum())
    warnings.append(f"Repaired {n_bad} invalid geometr(ies)."
                    + (f" {still_bad} could not be fixed." if still_bad else ""))
    if driver == "ESRI Shapefile":
        kinds = set(shapely.get_type_id(geoms[present]).tolist())
        if 7 in kinds:
            warnings.append("Repair produced mixed-type shapes, which shapefiles can't hold; "
                            "GeoPackage output is recommended here.")
    return geoms, warnings


def _do_table_to_points(report, tmp_path, driver, epsg, out_layer) -> List[str]:
    """Turn a CSV/Excel coordinate table into a real point layer."""
    import numpy as np
    import shapely

    layer = report.layers[0]
    xcol, ycol = layer.coord_columns
    rows = _read_table_rows(report.path, xcol, ycol)
    if not rows:
        raise RuntimeError("no rows with usable coordinates were found")

    xs = np.array([r[0] for r in rows], dtype="float64")
    ys = np.array([r[1] for r in rows], dtype="float64")
    geoms = shapely.to_wkb(shapely.points(xs, ys))
    skipped = layer.feature_count - len(rows) if layer.feature_count else 0

    from pyogrio.raw import write as raw_write
    raw_write(
        tmp_path,
        geometry=geoms,
        field_data=[np.arange(1, len(rows) + 1, dtype="int64")],
        fields=["row_id"],
        geometry_type="Point",
        crs=f"EPSG:{epsg}",
        driver=driver,
        **({"layer": out_layer} if driver == "GPKG" else {}),
    )
    notes = [f"Built {len(rows)} point(s) from '{xcol}'/'{ycol}'."]
    if skipped > 0:
        notes.append(f"{skipped} row(s) had no usable coordinates and were skipped.")
    notes.append("Attribute columns are not carried across yet — only row_id.")
    return notes


def _read_table_rows(path, xcol, ycol):
    """Re-read a CSV/Excel table and yield the (x, y) pairs that parse."""
    ext = os.path.splitext(path)[1].lower()
    out = []
    if ext in (".xlsx", ".xlsm"):
        import openpyxl

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            ws = wb.active
            rows = ws.iter_rows(values_only=True)
            header = [str(h).strip().lower() if h is not None else "" for h in next(rows)]
            xi, yi = header.index(xcol.strip().lower()), header.index(ycol.strip().lower())
            for row in rows:
                try:
                    out.append((float(row[xi]), float(row[yi])))
                except (TypeError, ValueError, IndexError):
                    continue
        finally:
            wb.close()
        return out

    if ext == ".xls":
        import xlrd

        book = xlrd.open_workbook(path)
        try:
            sheet = book.sheet_by_index(0)
            header = [str(v).strip().lower() for v in sheet.row_values(0)]
            xi, yi = header.index(xcol.strip().lower()), header.index(ycol.strip().lower())
            for r in range(1, sheet.nrows):
                row = sheet.row_values(r)
                try:
                    out.append((float(row[xi]), float(row[yi])))
                except (TypeError, ValueError, IndexError):
                    continue
        finally:
            book.release_resources()
        return out

    from .tabular import _open_text_table

    with _open_text_table(path) as (reader, header):
        norm = [h.strip().lower() for h in header]
        xi, yi = norm.index(xcol.strip().lower()), norm.index(ycol.strip().lower())
        for row in reader:
            try:
                out.append((float(row[xi]), float(row[yi])))
            except (TypeError, ValueError, IndexError):
                continue
    return out
