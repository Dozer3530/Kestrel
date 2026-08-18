"""Audit a whole folder of geospatial data at once.

Single-file inspection answers "what's wrong with this?". Batch answers the question
that actually costs people time: *"which of these 400 files is going to be a problem?"* —
before a delivery goes out, or after one arrives.
"""

from __future__ import annotations

import csv
import html
import os
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .arcgis import LAYER_EXTS
from .inspector import RASTER_EXTS, VECTOR_EXTS, inspect_path
from .pointcloud import LAS_EXTS
from .tabular import TABLE_EXTS

# Everything worth opening. Shapefile sidecars are skipped so one dataset is one row.
# Plain images can be georeferenced by a world file, so they're openable — but a folder
# scan shouldn't turn every screenshot and logo into a row.
_NOT_WORTH_SCANNING = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".json", ".pdf"}
SCAN_EXTS = ((VECTOR_EXTS | RASTER_EXTS | TABLE_EXTS | LAYER_EXTS | LAS_EXTS
              | {".zip", ".kmz"}) - _NOT_WORTH_SCANNING)
_SIDECARS = {".shx", ".dbf", ".prj", ".cpg", ".sbn", ".sbx", ".qix", ".aux", ".xml",
             ".ovr", ".idx", ".qpj", ".atx", ".lock"}
MAX_FILES = 2000


@dataclass
class BatchRow:
    """One dataset in the scan."""

    path: str
    name: str
    report: object = None
    error: Optional[str] = None

    @property
    def kind(self) -> str:
        return getattr(self.report, "kind", "?") if self.report else "?"

    @property
    def crs(self) -> str:
        if self.error or not self.report:
            return "—"
        for layer in getattr(self.report, "layers", []) or []:
            if layer.crs.defined:
                return f"EPSG:{layer.crs.epsg}" if layer.crs.epsg else layer.crs.name
        raster = getattr(self.report, "raster", None)
        if raster is not None and raster.crs.defined:
            return f"EPSG:{raster.crs.epsg}" if raster.crs.epsg else raster.crs.name
        return "none"

    @property
    def features(self) -> str:
        if not self.report:
            return "—"
        raster = getattr(self.report, "raster", None)
        if raster is not None:
            return f"{raster.width}x{raster.height}"
        counts = [l.feature_count for l in (self.report.layers or [])
                  if l.feature_count is not None]
        return str(sum(counts)) if counts else "—"

    @property
    def worst(self) -> str:
        """'error', 'warning' or 'ok' — for sorting the bad ones to the top.

        Info-level notes deliberately don't count against a file. They're context, not
        problems, and one of them (the WGS 84 / NAD83 note) fires on nearly every North
        American dataset — which would leave a folder reporting zero clean files.
        """
        if self.error:
            return "error"
        diags = getattr(self.report, "diagnostics", []) or []
        for level in ("error", "warning"):
            if any(d.severity == level for d in diags):
                return level
        return "ok"

    @property
    def notes(self) -> int:
        """How many info-level notes this file carries (shown, but not counted against it)."""
        diags = getattr(self.report, "diagnostics", []) or []
        return sum(1 for d in diags if d.severity == "info")

    @property
    def issues(self) -> str:
        if self.error:
            return self.error[:90]
        diags = getattr(self.report, "diagnostics", []) or []
        bad = [d.title for d in diags if d.severity in ("error", "warning")]
        if not bad:
            return "ok"
        return "; ".join(dict.fromkeys(bad))


@dataclass
class BatchResult:
    folder: str
    rows: List[BatchRow] = field(default_factory=list)
    skipped: int = 0
    truncated: bool = False

    @property
    def counts(self) -> dict:
        out = {"error": 0, "warning": 0, "ok": 0}
        for row in self.rows:
            out[row.worst] = out.get(row.worst, 0) + 1
        return out


def find_datasets(folder: str, recursive: bool = True,
                  on_found: Optional[Callable[[int], bool]] = None) -> List[str]:
    """Every openable dataset in a folder, one entry per dataset.

    ``on_found(count)`` is called as the walk progresses — walking a big network share
    can take a while on its own, and silence there looks like a hang.
    """
    found: List[str] = []
    for root, dirs, files in os.walk(folder):
        if on_found is not None and on_found(len(found)) is False:
            break
        # a .gdb is a dataset, not a folder to descend into
        for d in list(dirs):
            if d.lower().endswith(".gdb"):
                found.append(os.path.join(root, d))
                dirs.remove(d)
        for name in sorted(files):
            ext = os.path.splitext(name)[1].lower()
            if ext in _SIDECARS or ext not in SCAN_EXTS:
                continue
            found.append(os.path.join(root, name))
            if len(found) >= MAX_FILES:
                return found
        if not recursive:
            break
    return found


def scan_folder(folder: str, recursive: bool = True,
                progress: Optional[Callable[[int, int, str], bool]] = None,
                on_found: Optional[Callable[[int], bool]] = None) -> BatchResult:
    """Inspect every dataset under ``folder``.

    ``progress(done, total, name)`` may return False to cancel; ``on_found`` reports
    progress during the initial folder walk.
    """
    paths = find_datasets(folder, recursive, on_found)
    result = BatchResult(folder=folder, truncated=len(paths) >= MAX_FILES)
    total = len(paths)

    for i, path in enumerate(paths, 1):
        name = os.path.relpath(path, folder)
        if progress is not None and progress(i, total, name) is False:
            break
        row = BatchRow(path=path, name=name)
        try:
            row.report = inspect_path(path)
            if row.report.error:
                row.error = row.report.error
        except Exception as exc:                      # never let one file stop the scan
            row.error = f"{type(exc).__name__}: {exc}"
        result.rows.append(row)
    return result


def to_csv(result: BatchResult, path: str) -> str:
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(["File", "Type", "CRS", "Features/Size", "Status", "Issues"])
        for row in result.rows:
            writer.writerow([row.name, row.kind, row.crs, row.features,
                             row.worst, row.issues])
    return path


_STATUS_COLOUR = {"error": "#c0392b", "warning": "#d68910",
                  "info": "#2471a3", "ok": "#1e8449"}


def to_html(result: BatchResult, path: str) -> str:
    counts = result.counts
    rows = sorted(result.rows,
                  key=lambda r: ("error", "warning", "info", "ok").index(r.worst))
    parts = [
        "<!doctype html><meta charset='utf-8'>",
        f"<title>Kestrel audit — {html.escape(os.path.basename(result.folder))}</title>",
        "<style>body{font-family:Segoe UI,system-ui,sans-serif;margin:28px;color:#1c2833}"
        "h1{color:#1a5276;margin-bottom:2px}.sub{color:#7f8c8d;margin-top:0}"
        "table{border-collapse:collapse;width:100%;margin-top:18px;font-size:14px}"
        "th{background:#eef2f5;text-align:left;padding:8px;border-bottom:2px solid #d5dbdf}"
        "td{padding:7px 8px;border-bottom:1px solid #eceff1;vertical-align:top}"
        "tr:hover{background:#fafbfc}.tag{color:#fff;padding:2px 8px;border-radius:10px;"
        "font-size:12px;font-weight:600}.n{color:#7f8c8d}</style>",
        f"<h1>Kestrel audit</h1><p class='sub'>{html.escape(result.folder)} — "
        f"{len(result.rows)} dataset(s): "
        f"<b style='color:#c0392b'>{counts['error']} error</b>, "
        f"<b style='color:#d68910'>{counts['warning']} warning</b>, "
        f"<b style='color:#1e8449'>{counts['ok']} clean</b></p>",
        "<table><tr><th>File</th><th>Type</th><th>CRS</th><th>Size</th>"
        "<th>Status</th><th>Issues</th></tr>",
    ]
    for row in rows:
        colour = _STATUS_COLOUR.get(row.worst, "#7f8c8d")
        parts.append(
            f"<tr><td>{html.escape(row.name)}</td><td class='n'>{html.escape(row.kind)}</td>"
            f"<td>{html.escape(row.crs)}</td><td class='n'>{html.escape(row.features)}</td>"
            f"<td><span class='tag' style='background:{colour}'>{row.worst}</span></td>"
            f"<td>{html.escape(row.issues)}</td></tr>")
    parts.append("</table>")
    if result.truncated:
        parts.append(f"<p class='n'>Stopped at {MAX_FILES} datasets.</p>")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(parts))
    return path
