"""Kestrel — command-line entry point.

    py cli.py path\\to\\data.gpkg              inspect one file
    py cli.py --json path\\to\\data.gpkg       machine-readable output
    py cli.py --batch path\\to\\folder         audit a whole folder
    py cli.py --batch folder --csv out.csv    ...and export the results
    py cli.py --batch folder --html out.html

Exit code is 1 when something can't be read or has an error-level diagnostic,
otherwise 0 — handy for scripting and CI alongside QGIS work.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kestrel.inspector import inspect_path
from kestrel.textreport import format_report_text

USAGE = """Kestrel — a sharp eye on your geospatial data

  py cli.py <file|folder|url>            inspect one dataset
  py cli.py --json <file>                report as JSON
  py cli.py --batch <folder>             audit every dataset in a folder
      --csv <out.csv>                    also write a CSV summary
      --html <out.html>                  also write an HTML report
      --no-recurse                       don't descend into sub-folders

Reads shapefiles (zipped or plain), GeoPackage, File Geodatabase, GeoJSON, KML/KMZ,
DXF, CSV/Excel, LAS/LAZ point clouds, rasters, ArcGIS layer files and REST services.
"""


def _as_dict(report):
    """A JSON-friendly view of a report, for pipelines."""
    def crs(c):
        return {"defined": c.defined, "epsg": c.epsg, "name": c.name,
                "projected": c.is_projected, "utm_zone": c.utm_zone, "unit": c.unit,
                "datum": c.datum}

    def loc(l):
        if not l or not l.available:
            return None
        return {"center_lat": l.center_lat, "center_lon": l.center_lon,
                "west": l.west, "south": l.south, "east": l.east, "north": l.north}

    out = {
        "path": report.path,
        "name": report.file_name,
        "kind": report.kind,
        "driver": report.driver,
        "size_bytes": report.size_bytes,
        "error": report.error,
        "layers": [],
        "diagnostics": [{"severity": d.severity, "title": d.title, "detail": d.detail,
                         "fix": d.suggested_fix} for d in report.diagnostics],
    }
    for layer in report.layers or []:
        out["layers"].append({
            "name": layer.name,
            "geometry_type": layer.geometry_type,
            "features": layer.feature_count,
            "crs": crs(layer.crs),
            "location": loc(layer.location),
            "native_bounds": list(layer.native_bounds) if layer.native_bounds else None,
            "fields": [n for n, _ in (layer.fields or [])],
            "source_path": layer.source_path,
            "source_missing": layer.source_missing,
            "definition_query": layer.definition_query,
        })
    if report.raster is not None:
        r = report.raster
        out["raster"] = {"width": r.width, "height": r.height, "bands": r.band_count,
                         "dtype": r.dtype, "nodata": r.nodata,
                         "res_x": r.res_x, "res_y": r.res_y,
                         "tiled": r.tiled, "overviews": r.overview_levels,
                         "compression": r.compression,
                         "crs": crs(r.crs), "location": loc(r.location)}
    return out


def _run_batch(argv) -> int:
    from kestrel.batch import scan_folder, to_csv, to_html

    folder = None
    csv_out = html_out = None
    recursive = "--no-recurse" not in argv
    it = iter(range(len(argv)))
    for i in it:
        arg = argv[i]
        if arg == "--batch" and i + 1 < len(argv):
            folder = argv[i + 1]
        elif arg == "--csv" and i + 1 < len(argv):
            csv_out = argv[i + 1]
        elif arg == "--html" and i + 1 < len(argv):
            html_out = argv[i + 1]
    if not folder or not os.path.isdir(folder):
        print(f"not a folder: {folder}")
        return 2

    def progress(done, total, name):
        print(f"  [{done}/{total}] {name}", flush=True)
        return True

    print(f"Scanning {folder} ...")
    result = scan_folder(folder, recursive=recursive, progress=progress)
    counts = result.counts

    print()
    print("=" * 74)
    print(f"  {len(result.rows)} dataset(s) in {folder}")
    print("=" * 74)
    order = {"error": 0, "warning": 1, "info": 2, "ok": 3}
    for row in sorted(result.rows, key=lambda r: order.get(r.worst, 9)):
        flag = {"error": "[ERROR]", "warning": "[WARN] ", "info": "[info] ",
                "ok": "[ ok  ]"}.get(row.worst, "       ")
        print(f"{flag} {row.name}")
        print(f"         {row.kind:10s} {row.crs:22s} {row.features}")
        if row.issues != "ok":
            print(f"         {row.issues}")
    print("-" * 74)
    print(f"{counts['error']} error, {counts['warning']} warning, "
          f"{counts['info']} info, {counts['ok']} clean")
    if result.truncated:
        print("(stopped at the scan limit)")

    if csv_out:
        print("wrote", to_csv(result, csv_out))
    if html_out:
        print("wrote", to_html(result, html_out))
    return 1 if counts["error"] else 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0 if argv else 2

    if "--batch" in argv:
        return _run_batch(argv)

    as_json = "--json" in argv
    targets = [a for a in argv if not a.startswith("-")]
    if not targets:
        print(USAGE)
        return 2

    report = inspect_path(targets[0])
    if as_json:
        print(json.dumps(_as_dict(report), indent=2, default=str))
    else:
        print(format_report_text(report))

    if report.error or any(d.severity == "error" for d in report.diagnostics):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
