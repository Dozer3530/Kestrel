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

from PySide6.QtCore import (
    Qt, QObject, QPointF, QRectF, QRunnable, QSettings, QThreadPool, QUrl, Signal, Slot,
)
from PySide6.QtGui import (
    QBrush, QColor, QDesktopServices, QFont, QGuiApplication, QIcon, QPainter,
    QPalette, QPen, QPixmap, QPolygonF,
)
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QFileDialog, QFrame, QGridLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QAbstractItemView, QHeaderView, QInputDialog, QMainWindow, QMenu, QMessageBox,
    QProgressDialog, QPushButton, QScrollArea, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from kestrel import repair
from kestrel.crsguess import search_crs, suggest_crs
from kestrel.inspector import inspect_path
from kestrel.textreport import format_report_text
from mapview import MapCard

# Diagnostic title -> the repair that addresses it.
FIXABLE = {
    "No CRS defined": repair.ASSIGN_CRS,
    "Missing .prj (no CRS)": repair.ASSIGN_CRS,
    "Possible CRS / coordinate mismatch": repair.ASSIGN_CRS,
    "Eastings don't fit this UTM zone": repair.ASSIGN_CRS,
    "UTM zone is the wrong hemisphere": repair.ASSIGN_CRS,
    "Data outside the CRS's valid area": repair.ASSIGN_CRS,
    "Invalid geometry": repair.FIX_GEOMETRY,
    "Coordinates found": repair.TABLE_TO_POINTS,
}
FIX_LABEL = {
    repair.ASSIGN_CRS: "Set the CRS…",
    repair.FIX_GEOMETRY: "Fix geometry…",
    repair.TABLE_TO_POINTS: "Make a point layer…",
}

_CONFIDENCE_COLOR = {"high": "#1e8449", "medium": "#b9770e", "low": "#7f8c8d"}


def _settings():
    return QSettings("Kestrel", "Kestrel")


def _recent_crs():
    raw = _settings().value("recent_crs", "")
    out = []
    for part in str(raw).split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out


def _remember_crs(epsg):
    recent = [e for e in _recent_crs() if e != epsg]
    recent.insert(0, int(epsg))
    _settings().setValue("recent_crs", ",".join(str(e) for e in recent[:8]))

APP_NAME = "Kestrel"
TAGLINE = "A sharp eye on your geospatial data"

SEV_COLOR = {"error": "#c0392b", "warning": "#d68910", "info": "#2471a3"}
SEV_LABEL = {"error": "ERROR", "warning": "WARNING", "info": "INFO"}

FILE_FILTER = (
    "Geospatial files (*.zip *.shp *.gpkg *.geojson *.json *.kml *.kmz *.gml *.gpx "
    "*.csv *.xlsx *.lyrx *.mapx *.tif *.tiff *.img *.vrt *.jp2 *.asc);;"
    "ArcGIS layer files (*.lyrx *.mapx);;All files (*.*)"
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
            "Drop a file here to check it — or a folder to check everything in it\n"
            "shapefile · GeoPackage · File Geodatabase · GeoJSON · DXF · CSV · LAS · GeoTIFF · …\n\n"
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
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.toLocalFile()]
        if paths:
            self.on_path(paths[0])          # folders (.gdb) are handled downstream


# --------------------------------------------------------------------------- #
# CRS picker
# --------------------------------------------------------------------------- #
class CrsPicker(QDialog):
    """Choose a CRS: ranked suggestions first, then a code box, then a search."""

    def __init__(self, report, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Choose a coordinate system")
        self.resize(620, 540)
        self.chosen = None

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(8)

        intro = QLabel(
            "Kestrel can't know for certain which CRS this file is in — these are the most "
            "likely candidates based on its coordinates and the other files beside it.")
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #566573;")
        root.addWidget(intro)

        root.addWidget(self._heading("Suggestions"))
        self.suggestions = QListWidget()
        self.suggestions.setMinimumHeight(150)
        try:
            candidates = suggest_crs(report, recent=_recent_crs())
        except Exception:
            candidates = []
        if not candidates:
            self.suggestions.addItem("No suggestions — enter a code or search below.")
            self.suggestions.setEnabled(False)
        for cand in candidates:
            if not cand.epsg:                     # explanatory, not selectable
                item = QListWidgetItem(f"ⓘ  {cand.name}\n     {cand.reason}")
                item.setFlags(Qt.NoItemFlags)
            else:
                item = QListWidgetItem(
                    f"{cand.label}\n     {cand.confidence.upper()} · {cand.reason}")
                item.setData(Qt.UserRole, cand.epsg)
                item.setForeground(QColor(_CONFIDENCE_COLOR.get(cand.confidence, "#1c2833")))
            self.suggestions.addItem(item)
        self.suggestions.itemSelectionChanged.connect(self._from_suggestion)
        self.suggestions.itemDoubleClicked.connect(lambda *_: self._accept_if_valid())
        root.addWidget(self.suggestions)

        root.addWidget(self._heading("Or enter / search for one"))
        row = QHBoxLayout()
        self.query = QLineEdit()
        self.query.setPlaceholderText("EPSG code or name — e.g. 26911, or 'NAD83 UTM 11'")
        self.query.returnPressed.connect(self._search)
        find = QPushButton("Search")
        find.clicked.connect(self._search)
        row.addWidget(self.query, 1)
        row.addWidget(find)
        root.addLayout(row)

        self.results = QListWidget()
        self.results.setMinimumHeight(110)
        self.results.itemSelectionChanged.connect(self._from_result)
        self.results.itemDoubleClicked.connect(lambda *_: self._accept_if_valid())
        root.addWidget(self.results)

        self.chosen_label = QLabel("Nothing selected yet.")
        self.chosen_label.setWordWrap(True)
        self.chosen_label.setStyleSheet("font-weight: 600; color: #1a5276;")
        root.addWidget(self.chosen_label)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self._accept_if_valid)
        self.buttons.rejected.connect(self.reject)
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(False)
        root.addWidget(self.buttons)

        if candidates and candidates[0].epsg:
            self.suggestions.setCurrentRow(0)

    @staticmethod
    def _heading(text):
        label = QLabel(text)
        label.setStyleSheet("font-weight: 700; color: #1a5276; margin-top: 4px;")
        return label

    def _set_chosen(self, epsg, name):
        self.chosen = epsg
        self.chosen_label.setText(f"Selected:  EPSG:{epsg} — {name}")
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(True)

    def _from_suggestion(self):
        item = self.suggestions.currentItem()
        epsg = item.data(Qt.UserRole) if item else None
        if epsg:
            self.results.clearSelection()
            self._set_chosen(epsg, item.text().split("—", 1)[-1].split("\n")[0].strip())

    def _from_result(self):
        item = self.results.currentItem()
        epsg = item.data(Qt.UserRole) if item else None
        if epsg:
            self.suggestions.clearSelection()
            self._set_chosen(epsg, item.text().split("—", 1)[-1].strip())

    def _search(self):
        self.results.clear()
        try:
            hits = search_crs(self.query.text())
        except Exception:
            hits = []
        if not hits:
            self.results.addItem("No matches.")
            return
        for cand in hits:
            item = QListWidgetItem(cand.label)
            item.setData(Qt.UserRole, cand.epsg)
            self.results.addItem(item)

    def _accept_if_valid(self):
        if self.chosen:
            self.accept()


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

        self.fix_btn = QPushButton("Fix / Convert  ▾")
        self.fix_btn.setEnabled(False)
        self.fix_btn.setToolTip("Repairs always write a new file — your original is never changed.")
        fix_menu = QMenu(self)
        fix_menu.addAction("Set the CRS…", lambda: self.run_repair(repair.ASSIGN_CRS))
        fix_menu.addAction("Reproject to…", lambda: self.run_repair(repair.REPROJECT))
        fix_menu.addAction("Fix invalid geometry",
                           lambda: self.run_repair(repair.FIX_GEOMETRY))
        fix_menu.addAction("Make a point layer from this table…",
                           lambda: self.run_repair(repair.TABLE_TO_POINTS))
        fix_menu.addSeparator()
        for label, fmt in (("Convert to GeoPackage", "gpkg"),
                           ("Convert to Shapefile", "shp"),
                           ("Convert to GeoJSON", "geojson")):
            fix_menu.addAction(label, lambda f=fmt: self.run_repair(repair.CONVERT, f))
        self.fix_btn.setMenu(fix_menu)

        self.out_btn = QPushButton("Output folder")
        self.out_btn.clicked.connect(self.choose_output_folder)
        saved_out = _settings().value("output_folder", "")
        if saved_out:
            self.out_btn.setToolTip(f"Repaired files go to: {saved_out}")

        self.copy_btn = QPushButton("Copy report")
        self.copy_btn.clicked.connect(self.copy_report)
        self.copy_btn.setEnabled(False)
        self.folder_btn = QPushButton("Open folder")
        self.folder_btn.clicked.connect(self.open_folder)
        self.folder_btn.setEnabled(False)
        folder_scan_btn = QPushButton("Audit folder…")
        folder_scan_btn.setToolTip("Check every dataset in a folder at once")
        folder_scan_btn.clicked.connect(self.browse_folder)

        url_btn = QPushButton("Open URL…")
        url_btn.setToolTip("Inspect an ArcGIS REST service (FeatureServer / MapServer)")
        url_btn.clicked.connect(self.open_url)

        top.addWidget(browse)
        top.addWidget(folder_scan_btn)
        top.addWidget(url_btn)
        top.addWidget(self.fix_btn)
        top.addStretch(1)
        top.addWidget(self.out_btn)
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

    def open_url(self):
        url, ok = QInputDialog.getText(
            self, "Open an ArcGIS service",
            "Paste a FeatureServer or MapServer URL:\n"
            "(add /0 for one specific layer)")
        if ok and url.strip():
            self.load_path(url.strip())

    # --- batch ------------------------------------------------------------ #
    def browse_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Audit a folder of data", "")
        if folder:
            self.run_batch(folder)

    def run_batch(self, folder: str):
        """Inspect every dataset in a folder and show a sortable summary."""
        from kestrel.batch import scan_folder, to_csv, to_html

        dialog = QProgressDialog("Looking for data…", "Cancel", 0, 0, self)
        dialog.setWindowTitle("Auditing folder")
        dialog.setMinimumDuration(0)
        dialog.setWindowModality(Qt.WindowModal)
        dialog.setValue(0)

        def found(n):
            dialog.setLabelText(f"Looking for data… {n} found")
            QApplication.processEvents()
            return not dialog.wasCanceled()

        def progress(done, total, name):
            dialog.setMaximum(total)
            dialog.setValue(done)
            dialog.setLabelText(f"Checking {done} of {total}\n{name}")
            QApplication.processEvents()
            return not dialog.wasCanceled()

        try:
            result = scan_folder(folder, progress=progress, on_found=found)
        except Exception:
            dialog.close()
            QMessageBox.critical(self, "Folder audit failed", traceback.format_exc())
            return
        dialog.close()

        if not result.rows:
            QMessageBox.information(
                self, "Nothing to check",
                f"No geospatial data found in:\n{folder}")
            return

        self.batch_result = result
        self.current_report = None
        self.copy_btn.setEnabled(False)
        self.fix_btn.setEnabled(False)
        self.folder_btn.setEnabled(True)
        counts = result.counts
        self.status.setText(
            f"{len(result.rows)} dataset(s) in {folder}   ·   "
            f"{counts['error']} error, {counts['warning']} warning, {counts['ok']} clean")
        self._render_batch(result)

    def _render_batch(self, result):
        self.clear_results()
        order = {"error": 0, "warning": 1, "ok": 2}
        rows = sorted(result.rows, key=lambda r: (order.get(r.worst, 9), r.name))

        box = QGroupBox(f"Folder audit — {len(rows)} dataset(s)")
        box.setStyleSheet(
            "QGroupBox { font-weight: 700; border: 1px solid #d5dbdf; border-radius: 8px;"
            " margin-top: 10px; background: #ffffff; }"
            " QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px;"
            " color: #1a5276; }")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(12, 14, 12, 12)

        bar = QHBoxLayout()
        csv_btn = QPushButton("Export CSV…")
        csv_btn.clicked.connect(lambda: self._export_batch(to_csvfile=True))
        html_btn = QPushButton("Export HTML…")
        html_btn.clicked.connect(lambda: self._export_batch(to_csvfile=False))
        bar.addWidget(csv_btn)
        bar.addWidget(html_btn)
        bar.addStretch(1)
        hint = QLabel("Double-click a row to open that file")
        hint.setStyleSheet("color: #7f8c8d; font-size: 11px;")
        bar.addWidget(hint)
        lay.addLayout(bar)

        table = QTableWidget(len(rows), 5)
        table.setHorizontalHeaderLabels(["File", "Type", "CRS", "Size", "Issues"])
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSortingEnabled(False)
        table.verticalHeader().setVisible(False)
        for i, row in enumerate(rows):
            colour = QColor(SEV_COLOR.get(row.worst, "#1e8449")
                            if row.worst != "ok" else "#1e8449")
            for col, text in enumerate((row.name, row.kind, row.crs, row.features,
                                        row.issues)):
                item = QTableWidgetItem(str(text))
                if col == 0:
                    item.setData(Qt.UserRole, row.path)
                if row.worst in ("error", "warning"):
                    item.setForeground(colour)
                table.setItem(i, col, item)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        table.resizeColumnsToContents()
        table.setSortingEnabled(True)
        table.itemDoubleClicked.connect(self._open_batch_row)
        table.setMinimumHeight(420)
        lay.addWidget(table)
        self.results_layout.addWidget(box)

    def _open_batch_row(self, item):
        path = item.tableWidget().item(item.row(), 0).data(Qt.UserRole)
        if path:
            self.load_path(path)

    def _export_batch(self, to_csvfile: bool):
        from kestrel.batch import to_csv, to_html

        if not getattr(self, "batch_result", None):
            return
        kind = ("CSV (*.csv)" if to_csvfile else "HTML (*.html)")
        default = os.path.join(os.path.dirname(self.batch_result.folder),
                               "kestrel_audit." + ("csv" if to_csvfile else "html"))
        path, _ = QFileDialog.getSaveFileName(self, "Save the audit", default, kind)
        if not path:
            return
        try:
            written = (to_csv if to_csvfile else to_html)(self.batch_result, path)
        except Exception:
            QMessageBox.critical(self, "Export failed", traceback.format_exc())
            return
        self.status.setText(f"Saved {written}")
        QDesktopServices.openUrl(QUrl.fromLocalFile(written))

    def load_path(self, path: str):
        if not path:
            return
        # A folder that isn't a geodatabase means "audit everything in here".
        if os.path.isdir(path) and not path.lower().rstrip("\\/").endswith(".gdb"):
            self.run_batch(path)
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
        self.fix_btn.setEnabled(not report.error)
        self.render(report)

    @Slot(str)
    def show_error(self, message: str):
        self.clear_results()
        self.status.setText("Failed to inspect file.")
        self._card("Error", [("Details", message)], accent=SEV_COLOR["error"])

    # --- rendering -------------------------------------------------------- #
    def clear_results(self):
        # setParent(None) detaches immediately; deleteLater() alone only frees the widget
        # once the event loop next runs, so the previous file's cards stayed painted
        # underneath the new ones (inspection is synchronous, so that loop hasn't run yet).
        while self.results_layout.count():
            item = self.results_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
            else:
                sub = item.layout()
                if sub is not None:
                    while sub.count():
                        child = sub.takeAt(0).widget()
                        if child is not None:
                            child.setParent(None)
                            child.deleteLater()

    def render(self, report):
        self.clear_results()
        has_problem = any(d.severity in ("error", "warning") for d in report.diagnostics)

        if has_problem:
            self._diagnostics_card(report.diagnostics)

        if report.error:
            self._card("Could not read file", [("Error", report.error)], accent=SEV_COLOR["error"])

        if report.is_service:
            head = [("Service", report.service_title or report.file_name)]
            if report.portal_access:
                head.append(("Sharing", report.portal_access))
            if report.service_capabilities:
                head.append(("Allows", report.service_capabilities))
            self._card("ArcGIS REST service", head, accent="#7d3c98")
            for layer in report.layers:
                prefix = f"{layer.name} — " if len(report.layers) > 1 else ""
                self._crs_card(prefix, layer.crs)
                self._location_card(prefix, layer.location)
                self._map_card(prefix, layer.location, layer.preview)
                pairs = [("Endpoint", layer.source_path),
                         ("Geometry", layer.geometry_type),
                         ("Features", f"{layer.feature_count}"
                          + (" (sample)" if layer.sampled else ""))]
                if layer.fields:
                    pairs.append((f"Fields ({len(layer.fields)})",
                                  ", ".join(n for n, _ in layer.fields)))
                self._card(prefix + "Details", pairs)

        elif report.is_layer_file:
            for layer in report.layers:
                prefix = f"{layer.name} — " if len(report.layers) > 1 else ""
                pairs = [("Layer", layer.name)]
                if layer.visible is False:
                    pairs.append(("Visible", "No — turned off in the map"))
                if layer.source_path:
                    pairs.append(("Points at", layer.source_path))
                    pairs.append(("Source", (layer.source_kind or "?")
                                  + (" — MISSING" if layer.source_missing else "")))
                elif layer.source_kind:
                    pairs.append(("Points at", f"a {layer.source_kind} (not a file)"))
                if layer.definition_query:
                    pairs.append(("Definition query", layer.definition_query))
                self._card(prefix + "ArcGIS layer",
                           pairs,
                           accent=SEV_COLOR["error"] if layer.source_missing else "#7d3c98")
                if layer.crs.defined:
                    self._crs_card(prefix, layer.crs)
                    self._location_card(prefix, layer.location)
                    self._map_card(prefix, layer.location, layer.preview)
                    self._card(prefix + "Data behind the layer", [
                        ("Geometry", layer.geometry_type),
                        ("Features", layer.feature_count),
                    ])

        elif report.is_vector:
            if len(report.layers) > 1:
                self._card("Dataset",
                           [("Layers", ", ".join(l.name for l in report.layers))],
                           accent="#7d3c98")
            for layer in report.layers:
                prefix = f"{layer.name} — " if len(report.layers) > 1 else ""
                self._crs_card(prefix, layer.crs)
                self._location_card(prefix, layer.location)
                self._map_card(prefix, layer.location, layer.preview)
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
            self._map_card("", r.location)
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

    def _map_card(self, prefix, loc, preview=None):
        if not loc.available:
            return
        box = QGroupBox(prefix + "On the map")
        box.setStyleSheet(
            "QGroupBox { font-weight: 600; border: 1px solid #d5dbdf; border-radius: 8px;"
            " margin-top: 10px; background: #ffffff; }"
            " QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px;"
            " color: #117a65; }"
        )
        lay = QVBoxLayout(box)
        lay.setContentsMargins(12, 14, 12, 12)
        lay.addWidget(MapCard(loc, ASSETS_DIR,
                              satellite=_settings().value("satellite", True, type=bool),
                              on_toggle=lambda on: _settings().setValue("satellite", on),
                              preview=preview))
        self.results_layout.addWidget(box)

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
            operation = FIXABLE.get(d.title)
            if operation and self.current_report is not None:
                btn = QPushButton(FIX_LABEL.get(operation, "Fix…"))
                btn.setCursor(Qt.PointingHandCursor)
                btn.setStyleSheet(
                    "QPushButton { background: #c0622e; color: white; border: none;"
                    " padding: 6px 14px; border-radius: 6px; font-weight: 600; }"
                    " QPushButton:hover { background: #a4501f; }")
                btn.clicked.connect(lambda _=False, op=operation: self.run_repair(op))
                row = QHBoxLayout()
                row.addWidget(btn)
                row.addStretch(1)
                il.addLayout(row)
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

    # --- repairs ---------------------------------------------------------- #
    def output_folder(self, ask_if_unset=True):
        """Where fixed files are written. Chosen once, then remembered."""
        folder = _settings().value("output_folder", "")
        if folder and os.path.isdir(folder):
            return folder
        if not ask_if_unset:
            return ""
        QMessageBox.information(
            self, "Choose an output folder",
            "Kestrel never changes your original files — every fix is written as a new file.\n\n"
            "Pick a folder for the repaired copies. It's remembered from now on "
            "(you can change it any time with the Output folder button).")
        return self.choose_output_folder()

    def choose_output_folder(self):
        start = _settings().value("output_folder", "") or os.path.expanduser("~")
        folder = QFileDialog.getExistingDirectory(self, "Folder for repaired files", start)
        if folder:
            _settings().setValue("output_folder", folder)
            self.out_btn.setToolTip(f"Repaired files go to: {folder}")
        return folder

    def run_repair(self, operation, target_format="gpkg"):
        report = self.current_report
        if report is None:
            return

        epsg = None
        if operation in (repair.ASSIGN_CRS, repair.REPROJECT, repair.TABLE_TO_POINTS):
            picker = CrsPicker(report, self)
            if picker.exec() != QDialog.Accepted or not picker.chosen:
                return
            epsg = picker.chosen

        out_dir = self.output_folder()
        if not out_dir:
            return

        kwargs = {"epsg": epsg, "target_format": target_format}
        plan = repair.plan_repair(report, operation, out_dir, **kwargs)
        if not plan.ok:
            QMessageBox.warning(self, "Can't do that yet", plan.blocker or "Repair not possible.")
            return

        detail = [plan.description, "", f"Source:  {plan.source}",
                  f"Writes:  {plan.target}", "", "Your original file is not modified."]
        if plan.warnings:
            detail += ["", "Note:"] + [f"  • {w}" for w in plan.warnings]
        confirm = QMessageBox(self)
        confirm.setWindowTitle("Preview the fix")
        confirm.setIcon(QMessageBox.Question)
        confirm.setText("Apply this fix?")
        confirm.setInformativeText("\n".join(detail))
        confirm.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
        confirm.button(QMessageBox.Ok).setText("Write the file")
        if confirm.exec() != QMessageBox.Ok:
            return

        QGuiApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()
        try:
            result = repair.apply_repair(report, plan, **kwargs)
        except Exception:
            QGuiApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Fix failed", traceback.format_exc())
            return
        QGuiApplication.restoreOverrideCursor()

        if not result.ok:
            QMessageBox.critical(self, "Fix failed",
                                 result.message + "\n\nYour original file is untouched.")
            return

        if epsg:
            _remember_crs(epsg)
        lines = [result.message]
        if result.verification:
            lines.append(f"\nChecked the result: {result.verification}")
        if result.warnings:
            lines += ["", "Note:"] + [f"  • {w}" for w in result.warnings]

        done = QMessageBox(self)
        done.setWindowTitle("Fixed")
        done.setIcon(QMessageBox.Information)
        done.setText("Done — the repaired file is ready.")
        done.setInformativeText("\n".join(lines) + f"\n\n{result.target}")
        open_btn = done.addButton("Open the fixed file", QMessageBox.AcceptRole)
        done.addButton("Show in folder", QMessageBox.ActionRole)
        done.addButton(QMessageBox.Close)
        done.exec()
        clicked = done.clickedButton()
        if clicked is open_btn:
            self.load_path(result.target)
        elif clicked is not None and clicked.text() == "Show in folder":
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(result.target)))


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
    import json
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

        # Repair path: a full read -> write -> re-inspect round trip. This is the part
        # that actually touches the user's data, so the frozen build must prove it works.
        from kestrel import repair as _repair
        out_dir = os.path.join(tempfile.gettempdir(), "kestrel_selftest_out")
        plan = _repair.plan_repair(lrep, _repair.CONVERT, out_dir, target_format="gpkg")
        assert plan.ok, "repair plan blocked: %s" % plan.blocker
        rres = _repair.apply_repair(lrep, plan, target_format="gpkg")
        assert rres.ok, "repair failed: %s" % rres.message
        lines.append("repair OK (convert -> %s)" % rres.verification)

        # CRS suggestion engine (needs the pyproj EPSG database to be bundled).
        from kestrel.crsguess import search_crs as _search, suggest_crs as _suggest
        cands = _suggest(lrep, use_siblings=False)
        hits = _search("26911")
        assert hits and hits[0].epsg == 26911, "EPSG database not reachable"
        lines.append("crs suggestions OK (%d candidate(s), search resolves EPSG:26911)"
                     % len(cands))

        # Spreadsheet readers must actually be bundled, not silently missing — a frozen
        # build without these fails only when a user happens to open a spreadsheet.
        import openpyxl
        import xlrd
        lines.append("spreadsheet readers OK (openpyxl %s, xlrd %s)"
                     % (openpyxl.__version__, xlrd.__version__))

        # ArcGIS service + layer-file readers.
        from kestrel.arcgis import LAYER_EXTS
        from kestrel.esri import is_service_url
        assert is_service_url("https://x/arcgis/rest/services/A/FeatureServer/0")
        lines.append("arcgis readers OK (layer files: %s)"
                     % ", ".join(sorted(LAYER_EXTS)))

        # The mini-map asset has to ship or the map card renders empty.
        import gui as _self
        assert _self._asset("world.json"), "assets/world.json is missing from the build"
        world = json.load(open(_self._asset("world.json"), encoding="utf-8"))
        lines.append("world.json present (%s)" % ", ".join(sorted(world)))

        # Satellite imagery needs QtNetwork; without it the toggle would fail silently.
        from PySide6.QtNetwork import QNetworkAccessManager   # noqa: F401
        lines.append("QtNetwork present (satellite imagery available)")
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


_CONTEXT_EXTS = [".shp", ".gpkg", ".geojson", ".kml", ".kmz", ".gml", ".gpx",
                 ".tif", ".tiff", ".vrt", ".img", ".fgb", ".lyrx", ".mapx"]


def _register_context_menu(remove=False):
    """Add/remove a per-user 'Inspect with Kestrel' right-click entry for geo files.

    Uses HKCU\\Software\\Classes\\SystemFileAssociations (no admin, doesn't change which
    app owns the file type) and just calls the exe with the file — same as drag/Browse.
    """
    import winreg

    exe = sys.executable
    if getattr(sys, "frozen", False):
        command = f'"{exe}" "%1"'
    else:
        command = f'"{exe}" "{os.path.abspath(__file__)}" "%1"'
    icon = f"{exe},0"

    for ext in _CONTEXT_EXTS:
        base = r"Software\Classes\SystemFileAssociations\%s\shell\Kestrel" % ext
        if remove:
            for sub in (base + r"\command", base):
                try:
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
                except OSError:
                    pass
        else:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "Inspect with Kestrel")
                winreg.SetValueEx(k, "Icon", 0, winreg.REG_SZ, icon)
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + r"\command") as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, command)
    print(("Removed" if remove else "Registered") + " 'Inspect with Kestrel' context menu.")


def main():
    if "--selftest" in sys.argv:
        _selftest()
    if "--diag" in sys.argv:
        _diag(sys.argv[sys.argv.index("--diag") + 1])
    if "--register" in sys.argv:
        _register_context_menu(remove=False)
        return
    if "--unregister" in sys.argv:
        _register_context_menu(remove=True)
        return
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
