"""Kestrel — command-line entry point.

Print a sanity report for a geospatial file:

    py cli.py path\\to\\data.gpkg

Exit code is 1 when the file can't be read or has an error-level diagnostic,
otherwise 0 — handy for scripting alongside QGIS work.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kestrel.inspector import inspect_path
from kestrel.textreport import format_report_text


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    # Make sure stray Unicode (e.g. degree signs in CRS descriptions) never crashes
    # the Windows console.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if not argv or argv[0] in ("-h", "--help"):
        print("Kestrel — a sharp eye on your geospatial data")
        print("Usage: py cli.py <path-to-geospatial-file>")
        print("Supports zipped/plain shapefiles, GeoPackage, GeoJSON, GeoTIFF, etc.")
        return 0 if argv else 2

    report = inspect_path(argv[0])
    print(format_report_text(report))

    if report.error or any(d.severity == "error" for d in report.diagnostics):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
