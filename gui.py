"""Kestrel — a sharp eye on your geospatial data (PySide6 desktop app).

Drop a geospatial file onto the window (or Browse) and instantly see its CRS /
UTM zone, real-world location, basic details, and any reasons it might not draw
correctly in QGIS.

    py gui.py        (or double-click run.bat)

If you drop a logo into ./assets (logo.png and/or icon.ico) it is picked up
automatically for the header and the window/taskbar icon.
"""

import math
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import Qt, QObject, QRunnable, QThreadPool, QUrl, Signal, Slot
from PySide6.QtGui import (
    QColor, QDesktopServices, QFont, QGuiApplication, QIcon, QPalette, QPixmap,
)
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QMainWindow, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from kestrel.inspector import inspect_path
from kestrel.textreport import format_report_text

APP_NAME = "Kestrel"
TAGLINE = "A sharp eye on your geospatial data"

SEV_COLOR = {"error": "#c0392b", "warning": "#d68910", "info": "#2471a3"}
SEV_LABEL = {"error": "ERROR", "warning": "WARNING", "info": "INFO"}

FILE_FILTER = (
    "Geospatial files (*.zip *.shp *.gpkg *.geojson *.json *.kml *.gml *.gpx "
    "*.tif *.tiff *.img *.vrt *.jp2 *.asc);;All files (*.*)"
)

def _base_dir():
    """Base directory for bundled resources — handles PyInstaller's frozen layout."""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


ASSETS_DIR = os.path.join(_base_dir(), "assets")


def _asset(*names):
    """Return the first existing asset path among ``names`` (or None)."""
    for name in names:
        path = os.path.join(ASSETS_DIR, name)
        if os.path.exists(path):
            return path
    return None


def _icon_path():
    return _asset("icon.ico", "kestrel.ico", "logo.png", "kestrel.png", "logo.jpg")


def _logo_path():
    return _asset("logo.png", "kestrel.png", "logo.jpg", "logo.jpeg", "logo.svg", "icon.ico")


def _has_nan(values) -> bool:
    for v in values:
        try:
            if math.isnan(float(v)) or math.isinf(float(v)):
                return True
        except (TypeError, ValueError):
            return True
    return False


def _fmt_bounds(b) -> str:
    xmin, ymin, xmax, ymax = b
    return f"{xmin:.3f}, {ymin:.3f}  ->  {xmax:.3f}, {ymax:.3f}"


# --------------------------------------------------------------------------- #
# Background worker (keeps the UI responsive on large files)
# --------------------------------------------------------------------------- #
class _WorkerSignals(QObject):
    finished = Signal(object)
    failed = Signal(str)


class InspectWorker(QRunnable):
    def __init__(self, path: str):
        super().__init__()
        self.path = path
        self.signals = _WorkerSignals()

    @Slot()
    def run(self):
        try:
            self.signals.finished.emit(inspect_path(self.path))
        except Exception:
            self.signals.failed.emit(traceback.format_exc())


# --------------------------------------------------------------------------- #
# Drop area
# --------------------------------------------------------------------------- #
class DropArea(QFrame):
    def __init__(self, on_path):
        super().__init__()
        self.on_path = on_path
        self.setAcceptDrops(True)
        self.setObjectName("drop")
        self.setMinimumHeight(110)
        layout = QVBoxLayout(self)
        self.label = QLabel(
            "Drop a file here to check it\n"
            "shapefile (zipped or plain) · GeoPackage · GeoJSON · GeoTIFF · …\n\n"
            "…or click Browse"
        )
        self.label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.label)
        self._style(False)

    def _style(self, active: bool):
        border = "#2980b9" if active else "#9aa4ad"
        bg = "#eaf2fb" if active else "#fbfcfd"
        self.setStyleSheet(
            f"#drop {{ border: 2px dashed {border}; border-radius: 10px; background: {bg}; }}"
            f" QLabel {{ color: #566573; font-size: 13px; border: none; background: transparent; }}"
        )

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._style(True)

    def dragLeaveEvent(self, event):
        self._style(False)

    def dropEvent(self, event):
        self._style(False)
        urls = event.mimeData().urls()
        if urls:
            self.on_path(urls[0].toLocalFile())


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(740, 800)
        icon = _icon_path()
        if icon:
            self.setWindowIcon(QIcon(icon))
        self.pool = QThreadPool.globalInstance()
        self.current_report = None

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 12, 14, 14)
        root.setSpacing(10)

        root.addLayout(self._build_header())

        top = QHBoxLayout()
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.browse)
        self.copy_btn = QPushButton("Copy report")
        self.copy_btn.clicked.connect(self.copy_report)
        self.copy_btn.setEnabled(False)
        self.folder_btn = QPushButton("Open folder")
        self.folder_btn.clicked.connect(self.open_folder)
        self.folder_btn.setEnabled(False)
        top.addWidget(browse)
        top.addStretch(1)
        top.addWidget(self.copy_btn)
        top.addWidget(self.folder_btn)
        root.addLayout(top)

        self.drop = DropArea(self.load_path)
        root.addWidget(self.drop)

        self.status = QLabel("Drop a file anywhere on this window — or click Browse — to get started.")
        self.status.setStyleSheet("color: #566573;")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.results_host = QWidget()
        self.results_layout = QVBoxLayout(self.results_host)
        self.results_layout.setAlignment(Qt.AlignTop)
        self.results_layout.setSpacing(10)
        self.scroll.setWidget(self.results_host)
        root.addWidget(self.scroll, 1)

    def _build_header(self):
        header = QHBoxLayout()
        header.setSpacing(12)
        logo = _logo_path()
        if logo:
            pixmap = QPixmap(logo)
            if not pixmap.isNull():
                badge = QLabel()
                badge.setPixmap(pixmap.scaledToHeight(54, Qt.SmoothTransformation))
                header.addWidget(badge, 0, Qt.AlignVCenter)

        text = QVBoxLayout()
        text.setSpacing(0)
        title = QLabel(APP_NAME)
        title.setStyleSheet("font-size: 24px; font-weight: 800; color: #1a5276;")
        subtitle = QLabel(TAGLINE)
        subtitle.setStyleSheet("color: #7f8c8d; font-size: 12px;")
        text.addWidget(title)
        text.addWidget(subtitle)
        header.addLayout(text)
        header.addStretch(1)
        return header

    # --- actions ---------------------------------------------------------- #
    def browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose a geospatial file", "", FILE_FILTER)
        if path:
            self.load_path(path)

    def load_path(self, path: str):
        if not path:
            return
        self.clear_results()
        self.status.setText(f"Inspecting:  {path}")
        self.copy_btn.setEnabled(False)
        self.folder_btn.setEnabled(False)
        # Inspect on the main thread. It's fast (metadata + a small geometry sample), and it
        # avoids calling GDAL/GEOS from a background thread — a known crash risk in frozen
        # builds. A wait cursor keeps it feeling responsive.
        QGuiApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()
        try:
            report = inspect_path(path)
        except Exception:
            QGuiApplication.restoreOverrideCursor()
            self.show_error(traceback.format_exc())
            return
        QGuiApplication.restoreOverrideCursor()
        self.show_report(report)

    @Slot(object)
    def show_report(self, report):
        self.current_report = report
        driver = f"  ·  {report.driver}" if report.driver else ""
        self.status.setText(f"{report.file_name}   ·   {report.kind}{driver}")
        self.copy_btn.setEnabled(True)
        self.folder_btn.setEnabled(True)
        self.render(report)

    @Slot(str)
    def show_error(self, message: str):
        self.clear_results()
        self.status.setText("Failed to inspect file.")
        self._card("Error", [("Details", message)], accent=SEV_COLOR["error"])

    # --- rendering -------------------------------------------------------- #
    def clear_results(self):
        while self.results_layout.count():
            item = self.results_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def render(self, report):
        self.clear_results()
        has_problem = any(d.severity in ("error", "warning") for d in report.diagnostics)

        if has_problem:
            self._diagnostics_card(report.diagnostics)

        if report.error:
            self._card("Could not read file", [("Error", report.error)], accent=SEV_COLOR["error"])

        if report.is_vector:
            if len(report.layers) > 1:
                self._card("Dataset",
                           [("Layers", ", ".join(l.name for l in report.layers))],
                           accent="#7d3c98")
            for layer in report.layers:
                prefix = f"{layer.name} — " if len(report.layers) > 1 else ""
                self._crs_card(prefix, layer.crs)
                self._location_card(prefix, layer.location)
                pairs = [("Geometry", layer.geometry_type), ("Features", layer.feature_count)]
                if layer.native_bounds and not _has_nan(layer.native_bounds):
                    pairs.append(("Native extent", _fmt_bounds(layer.native_bounds)))
                if layer.fields:
                    pairs.append((f"Fields ({len(layer.fields)})",
                                  ", ".join(n for n, _ in layer.fields)))
                self._card(prefix + "Details", pairs)

        elif report.is_raster and report.raster:
            r = report.raster
            self._crs_card("", r.crs)
            self._location_card("", r.location)
            pairs = [
                ("Dimensions", f"{r.width} x {r.height} px"),
                ("Bands", r.band_count),
                ("Data type", r.dtype),
            ]
            if r.nodata is not None:
                pairs.append(("NoData", r.nodata))
            if r.res_x:
                pairs.append(("Pixel size", f"{r.res_x} x {r.res_y}"))
            if r.native_bounds and not _has_nan(r.native_bounds):
                pairs.append(("Native extent", _fmt_bounds(r.native_bounds)))
            self._card("Raster details", pairs)

        if not has_problem:
            if report.diagnostics:
                self._diagnostics_card(report.diagnostics)
            elif not report.error:
                self._card("Diagnostics", [("Status", "All good — nothing looks off.")], accent="#1e8449")

    def _card(self, title, pairs, accent="#2980b9"):
        box = QGroupBox(title)
        box.setStyleSheet(
            "QGroupBox { font-weight: 600; border: 1px solid #d5dbdf; border-radius: 8px;"
            " margin-top: 10px; background: #ffffff; }"
            " QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px;"
            f" color: {accent}; }}"
        )
        grid = QGridLayout(box)
        grid.setColumnStretch(1, 1)
        grid.setContentsMargins(12, 14, 12, 12)
        for row, (key, value) in enumerate(pairs):
            klabel = QLabel(str(key))
            klabel.setStyleSheet("color: #7f8c8d; border: none;")
            vlabel = QLabel("—" if value is None else str(value))
            vlabel.setStyleSheet("color: #1c2833; font-weight: 500; border: none;")
            vlabel.setWordWrap(True)
            vlabel.setTextInteractionFlags(Qt.TextSelectableByMouse)
            grid.addWidget(klabel, row, 0, Qt.AlignTop)
            grid.addWidget(vlabel, row, 1)
        self.results_layout.addWidget(box)
        return box

    def _crs_card(self, prefix, crs):
        title = prefix + "Coordinate System (CRS)"
        if not crs.defined:
            self._card(title,
                       [("Status", "Missing — QGIS won't know where to put this layer")],
                       accent=SEV_COLOR["error"])
            return
        pairs = [
            ("Name", crs.name),
            ("EPSG", f"EPSG:{crs.epsg}" if crs.epsg else "(none)"),
        ]
        if crs.utm_zone:
            pairs.append(("UTM zone", crs.utm_zone))
        pairs.append(("Type", "Projected" if crs.is_projected
                      else ("Geographic" if crs.is_geographic else "?")))
        pairs.append(("Units", crs.unit or "?"))
        if crs.datum:
            pairs.append(("Datum", crs.datum))
        if crs.area_of_use:
            pairs.append(("Valid for", crs.area_of_use))
        box = self._card(title, pairs, accent="#1a5276")
        # Make the CRS card stand out a touch — it's the headline answer.
        box.setStyleSheet(box.styleSheet().replace("font-weight: 600;", "font-weight: 700;"))

    def _location_card(self, prefix, loc):
        title = prefix + "Location (WGS84 lon/lat)"
        if not loc.available:
            self._card(title, [("Status", f"Unavailable — {loc.note}")], accent="#7f8c8d")
            return
        self._card(title, [
            ("Center lat/lon", f"{loc.center_lat:.5f}, {loc.center_lon:.5f}"),
            ("Lat/Lon bbox",
             f"W {loc.west:.4f}   S {loc.south:.4f}   E {loc.east:.4f}   N {loc.north:.4f}"),
        ], accent="#117a65")

    def _diagnostics_card(self, diagnostics):
        box = QGroupBox(f"Diagnostics ({len(diagnostics)})")
        box.setStyleSheet(
            "QGroupBox { font-weight: 700; border: 1px solid #d5dbdf; border-radius: 8px;"
            " margin-top: 10px; background: #ffffff; }"
            " QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; color: #283747; }"
        )
        layout = QVBoxLayout(box)
        layout.setContentsMargins(12, 14, 12, 12)
        layout.setSpacing(8)
        for d in diagnostics:
            color = SEV_COLOR.get(d.severity, "#7f8c8d")
            item = QFrame()
            item.setStyleSheet(
                f"QFrame {{ border: none; border-left: 4px solid {color};"
                " background: #f7f9fa; border-radius: 4px; }"
            )
            il = QVBoxLayout(item)
            il.setContentsMargins(10, 8, 10, 8)
            il.setSpacing(3)
            head = QLabel(f"[{SEV_LABEL.get(d.severity, '?')}]  {d.title}")
            head.setStyleSheet(f"color: {color}; font-weight: 700; border: none; background: transparent;")
            detail = QLabel(d.detail)
            detail.setWordWrap(True)
            detail.setStyleSheet("color: #1c2833; border: none; background: transparent;")
            il.addWidget(head)
            il.addWidget(detail)
            if d.suggested_fix:
                fix = QLabel("Fix:  " + d.suggested_fix)
                fix.setWordWrap(True)
                fix.setStyleSheet("color: #566573; font-style: italic; border: none; background: transparent;")
                il.addWidget(fix)
            layout.addWidget(item)
        self.results_layout.addWidget(box)

    # --- clipboard / folder ---------------------------------------------- #
    def copy_report(self):
        if self.current_report:
            QGuiApplication.clipboard().setText(format_report_text(self.current_report))
            self.status.setText("Report copied to clipboard.")

    def open_folder(self):
        if self.current_report and self.current_report.path:
            folder = os.path.dirname(os.path.abspath(self.current_report.path))
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))


def apply_theme(app):
    """Force a consistent light theme so the app looks the same regardless of the
    OS dark/light setting (the cards and drop zone are designed for a light backdrop)."""
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor("#eef1f4"))
    pal.setColor(QPalette.WindowText, QColor("#1c2833"))
    pal.setColor(QPalette.Base, QColor("#ffffff"))
    pal.setColor(QPalette.AlternateBase, QColor("#f4f6f8"))
    pal.setColor(QPalette.Text, QColor("#1c2833"))
    pal.setColor(QPalette.Button, QColor("#e3e8ec"))
    pal.setColor(QPalette.ButtonText, QColor("#1c2833"))
    pal.setColor(QPalette.ToolTipBase, QColor("#ffffff"))
    pal.setColor(QPalette.ToolTipText, QColor("#1c2833"))
    pal.setColor(QPalette.Highlight, QColor("#2980b9"))
    pal.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    app.setPalette(pal)


def _selftest():
    """Validate the bundled geospatial stack (used to verify a built exe).

    Writes a PASS/FAIL report to %TEMP%\\kestrel_selftest.txt and exits.
    """
    import tempfile
    import traceback

    log = os.path.join(tempfile.gettempdir(), "kestrel_selftest.txt")
    lines = []
    ok = True
    try:
        import pyogrio
        lines.append("pyogrio %s  (GDAL %s)"
                     % (pyogrio.__version__, getattr(pyogrio, "__gdal_version__", "?")))
        import rasterio
        lines.append("rasterio %s" % rasterio.__version__)
        import shapely
        lines.append("shapely %s" % shapely.__version__)
        from pyproj import CRS, Transformer
        crs = CRS.from_epsg(32611)            # needs proj.db
        lines.append("CRS: %s | UTM %s | area_of_use: %s"
                     % (crs.name, crs.utm_zone, bool(crs.area_of_use)))
        assert crs.utm_zone == "11N"
        assert crs.area_of_use is not None
        lon, lat = Transformer.from_crs(32611, 4326, always_xy=True).transform(500000, 5650000)
        lines.append("transform 500000,5650000 -> lat %.4f lon %.4f" % (lat, lon))
        assert 50 < lat < 52 and -118 < lon < -116

        from kestrel.inspector import inspect_path

        # Vector read path (pyogrio reading a GeoJSON).
        gj = os.path.join(tempfile.gettempdir(), "kestrel_selftest.geojson")
        with open(gj, "w", encoding="utf-8") as fh:
            fh.write('{"type":"FeatureCollection","features":[{"type":"Feature",'
                     '"properties":{},"geometry":{"type":"Point","coordinates":[-114.0,51.0]}}]}')
        vrep = inspect_path(gj)
        assert vrep.is_vector and vrep.layers and vrep.layers[0].feature_count == 1
        lines.append("vector read OK (geojson, %d feature)" % vrep.layers[0].feature_count)

        # Geometry-validity path: a LineString triggers pyogrio.raw + shapely/GEOS is_valid,
        # which points-only files skip. This is the bit that can crash a frozen build.
        lgj = os.path.join(tempfile.gettempdir(), "kestrel_selftest_line.geojson")
        with open(lgj, "w", encoding="utf-8") as fh:
            fh.write('{"type":"FeatureCollection","features":[{"type":"Feature",'
                     '"properties":{},"geometry":{"type":"LineString",'
                     '"coordinates":[[-114.0,51.0],[-113.9,51.1],[-113.8,51.0]]}}]}')
        lrep = inspect_path(lgj)
        assert lrep.is_vector and lrep.layers
        lines.append("geometry-validity OK (line, invalid=%s)"
                     % lrep.layers[0].invalid_geometry_count)

        # Raster read path (rasterio writes + inspects a tiny GeoTIFF).
        import numpy as np
        from rasterio.transform import from_origin
        tif = os.path.join(tempfile.gettempdir(), "kestrel_selftest.tif")
        with rasterio.open(tif, "w", driver="GTiff", height=4, width=4, count=1,
                           dtype="uint8", crs="EPSG:32611",
                           transform=from_origin(500000, 5660000, 30, 30)) as dst:
            dst.write(np.arange(16, dtype="uint8").reshape(4, 4), 1)
        rrep = inspect_path(tif)
        assert rrep.is_raster and rrep.raster.crs.utm_zone == "11N"
        lines.append("raster read OK (geotiff, %dx%d, UTM %s)"
                     % (rrep.raster.width, rrep.raster.height, rrep.raster.crs.utm_zone))
    except Exception:
        ok = False
        lines.append("ERROR:\n" + traceback.format_exc())

    report = "SELFTEST %s\n%s\n" % ("PASS" if ok else "FAIL", "\n".join(lines))
    try:
        with open(log, "w", encoding="utf-8") as fh:
            fh.write(report)
    except Exception:
        pass
    try:
        print(report)
    except Exception:
        pass
    sys.exit(0 if ok else 2)


def _diag(path):
    r"""Debug: inspect a file on the main thread, then on a worker thread; log each step
    to %TEMP%\kestrel_diag.txt. A hard crash leaves the last successful step behind."""
    import tempfile
    import threading
    import traceback

    log = os.path.join(tempfile.gettempdir(), "kestrel_diag.txt")

    def w(msg):
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(msg + "\n")

    open(log, "w", encoding="utf-8").close()
    w("DIAG file: " + path)

    w("step 1: main-thread inspect ...")
    try:
        r = inspect_path(path)
        w("  main OK: error=%s layers=%s"
          % (r.error, [(l.geometry_type, l.invalid_geometry_count) for l in r.layers]))
    except Exception:
        w("  main EXC:\n" + traceback.format_exc())

    w("step 2: worker-thread inspect ...")
    out = {}

    def job():
        try:
            out["r"] = inspect_path(path)
        except Exception:
            out["exc"] = traceback.format_exc()

    t = threading.Thread(target=job)
    t.start()
    t.join()
    if "r" in out:
        w("  thread OK: error=%s" % out["r"].error)
    elif "exc" in out:
        w("  thread EXC:\n" + out["exc"])
    else:
        w("  thread: no result")

    w("DIAG done")
    sys.exit(0)


def main():
    if "--selftest" in sys.argv:
        _selftest()
    if "--diag" in sys.argv:
        _diag(sys.argv[sys.argv.index("--diag") + 1])
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    apply_theme(app)
    icon = _icon_path()
    if icon:
        app.setWindowIcon(QIcon(icon))
    default_font = QFont()
    default_font.setPointSize(10)
    app.setFont(default_font)
    window = MainWindow()
    window.show()
    # Open a file passed on the command line (file association / "Open with" / drag-onto-exe).
    file_args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if file_args:
        window.load_path(file_args[0])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
