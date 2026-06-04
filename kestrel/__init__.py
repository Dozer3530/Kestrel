"""Kestrel — a sharp eye on your geospatial data.

A quick desktop sanity-checker: drop in a shapefile (zipped or plain), GeoPackage,
GeoJSON, GeoTIFF, etc. and get the basics — most importantly the CRS / UTM zone and the
real-world location — plus warnings explaining why a layer might not be drawing correctly
in QGIS.
"""

from .inspector import inspect_path, analyze_crs
from .textreport import format_report_text

__version__ = "0.1.0"

__all__ = ["inspect_path", "analyze_crs", "format_report_text", "__version__"]
