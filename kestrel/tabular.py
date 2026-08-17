"""Read tabular files (CSV / Excel) that carry point coordinates in columns.

These formats don't store a CRS, so Kestrel's job is to find the coordinate columns,
work out the extent, and make a sensible guess at the CRS (lat/lon vs. projected).
"""

from __future__ import annotations

import codecs
import contextlib
import csv
import math
from typing import List, Optional

from .models import CrsInfo, InspectionReport, LayerInfo, LocationInfo

CSV_EXTS = {".csv", ".tsv"}
EXCEL_EXTS = {".xlsx", ".xlsm"}
XLS_EXTS = {".xls"}                 # the old binary format; GDAL has no driver for it
TABLE_EXTS = CSV_EXTS | EXCEL_EXTS | XLS_EXTS

# Candidate coordinate column names, highest priority first (matched case-insensitively).
_X_NAMES = ["longitude", "long", "lon", "lng", "x", "easting", "east",
            "x_coord", "xcoord", "coord_x", "point_x", "pointx", "gps_longitude", "gps_long"]
_Y_NAMES = ["latitude", "lat", "y", "northing", "north",
            "y_coord", "ycoord", "coord_y", "point_y", "pointy", "gps_latitude", "gps_lat"]


def _norm(s) -> str:
    return str(s).strip().lower() if s is not None else ""


def _pick(header_norm: List[str], candidates: List[str]) -> Optional[int]:
    for cand in candidates:
        if cand in header_norm:
            return header_norm.index(cand)
    return None


def _finite(v) -> bool:
    return not (math.isnan(v) or math.isinf(v))


def inspect_table(path, file_name, size, ext) -> InspectionReport:
    try:
        if ext in XLS_EXTS:
            return _inspect_xls(path, file_name, size)
        if ext in EXCEL_EXTS:
            return _inspect_excel(path, file_name, size)
        return _inspect_csv(path, file_name, size)
    except ImportError:
        return InspectionReport(
            path=path, file_name=file_name, size_bytes=size, kind="vector", driver="XLS",
            error="reading the old .xls format needs the xlrd package "
                  "(py -m pip install xlrd), or re-save the file as .xlsx or .csv")
    except Exception as exc:
        driver = {True: "XLS"}.get(ext in XLS_EXTS, "XLSX" if ext in EXCEL_EXTS else "CSV")
        return InspectionReport(path=path, file_name=file_name, size_bytes=size,
                                kind="vector", driver=driver, error=str(exc))


def _build_report(path, file_name, size, driver, layer_name,
                  header, xi, yi, rows_iter) -> InspectionReport:
    total = valid = 0
    xmin = ymin = math.inf
    xmax = ymax = -math.inf
    have_coords = xi is not None and yi is not None

    for row in rows_iter:
        total += 1
        if not have_coords:
            continue
        try:
            x = float(row[xi])
            y = float(row[yi])
        except (ValueError, TypeError, IndexError):
            continue
        if not (_finite(x) and _finite(y)):
            continue
        valid += 1
        xmin, xmax = min(xmin, x), max(xmax, x)
        ymin, ymax = min(ymin, y), max(ymax, y)

    fields = [(str(h), "") for h in header]
    bounds = None
    coord_columns = None
    crs_guess = None
    geometry_type = None
    location = LocationInfo(available=False, note="tabular files carry no CRS")

    if have_coords and valid > 0:
        bounds = (xmin, ymin, xmax, ymax)
        coord_columns = (str(header[xi]), str(header[yi]))
        geometry_type = "Point"
        looks_lonlat = (-180.0 <= xmin and xmax <= 180.0
                        and -90.0 <= ymin and ymax <= 90.0)
        if looks_lonlat:
            crs_guess = "look like WGS 84 longitude/latitude (EPSG:4326)"
            location = LocationInfo(
                available=True, west=xmin, south=ymin, east=xmax, north=ymax,
                center_lat=(ymin + ymax) / 2.0, center_lon=(xmin + xmax) / 2.0,
            )
        else:
            crs_guess = "look like projected coordinates (probably metres) — the CRS is unknown"
            location = LocationInfo(
                available=False,
                note="can't locate without a CRS (coordinates look projected)",
            )

    layer = LayerInfo(
        name=layer_name, geometry_type=geometry_type, feature_count=total,
        fields=fields, native_bounds=bounds, crs=CrsInfo(defined=False),
        location=location, coord_columns=coord_columns, crs_guess=crs_guess,
    )
    return InspectionReport(path=path, file_name=file_name, size_bytes=size,
                            kind="vector", driver=driver, layers=[layer])


def detect_encoding(path) -> str:
    """Best-effort text encoding for a delimited file.

    Exports from ArcGIS/Excel are often UTF-16 or cp1252; reading those as UTF-8
    turns every header into mojibake, which used to defeat coordinate detection
    entirely (and produced advice telling people to rename correct columns).
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(4)
    except OSError:
        return "utf-8-sig"
    if head.startswith(codecs.BOM_UTF16_LE) or head.startswith(codecs.BOM_UTF16_BE):
        return "utf-16"
    if head.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if head[:2] in (b"\xff\xfe", b"\xfe\xff") or (len(head) >= 2 and head[1:2] == b"\x00"):
        return "utf-16"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            fh.read(65536)
        return "utf-8"
    except (UnicodeDecodeError, OSError):
        return "cp1252"


@contextlib.contextmanager
def _open_text_table(path):
    """Yield ``(csv_reader, header)`` with encoding and delimiter worked out."""
    enc = detect_encoding(path)
    with open(path, "r", newline="", encoding=enc, errors="replace") as fh:
        sample = fh.read(8192)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(fh, dialect)
        try:
            header = next(reader)
        except StopIteration:
            header = []
        yield reader, header


def _inspect_csv(path, file_name, size) -> InspectionReport:
    with _open_text_table(path) as (reader, header):
        header_norm = [_norm(h) for h in header]
        xi = _pick(header_norm, _X_NAMES)
        yi = _pick(header_norm, _Y_NAMES)
        return _build_report(path, file_name, size, "CSV", "(table)",
                             header, xi, yi, reader)


def _inspect_xls(path, file_name, size) -> InspectionReport:
    """Excel 97-2003 (.xls). GDAL ships no XLS driver, so this goes through xlrd."""
    import xlrd

    book = xlrd.open_workbook(path)
    try:
        sheet = book.sheet_by_index(0)
        header = [str(v).strip() for v in sheet.row_values(0)] if sheet.nrows else []
        header_norm = [_norm(h) for h in header]
        xi = _pick(header_norm, _X_NAMES)
        yi = _pick(header_norm, _Y_NAMES)
        rows = (sheet.row_values(r) for r in range(1, sheet.nrows))
        return _build_report(path, file_name, size, "XLS", sheet.name or "(sheet)",
                             header, xi, yi, rows)
    finally:
        book.release_resources()


def _inspect_excel(path, file_name, size) -> InspectionReport:
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.active
        rows = ws.iter_rows(values_only=True)
        try:
            header = list(next(rows))
        except StopIteration:
            header = []
        header_norm = [_norm(h) for h in header]
        xi = _pick(header_norm, _X_NAMES)
        yi = _pick(header_norm, _Y_NAMES)
        return _build_report(path, file_name, size, "XLSX", ws.title or "(sheet)",
                             header, xi, yi, rows)
    finally:
        wb.close()
