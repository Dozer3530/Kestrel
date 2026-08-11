"""The map card: a globe for orientation next to a zoomed view of the data.

Two modes for the detail pane:

* **Satellite (default)** — Esri World Imagery tiles, fetched asynchronously and cached
  on disk. There is no API key involved, so there is no secret to leak and no bill to
  run up. With real imagery underneath, the zoom clamp lifts and you can go right in to
  the field. If the network is unavailable it quietly falls back to the offline drawing
  and says so, so this is safe to leave on.
* **Offline** — coastlines, lakes, borders and town names drawn from the outlines
  bundled in ``assets/world.json``. Works with no network at all. Zoom is clamped to a
  regional scale because the bundled vectors have nothing to show closer in.
"""

from __future__ import annotations

import json
import math
import os

from PySide6.QtCore import QPoint, QPointF, QRectF, QStandardPaths, Qt, QUrl
from PySide6.QtGui import (
    QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QPixmap, QPolygonF,
)
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget

OCEAN = QColor("#e8f1f7")
LAND = QColor("#dbe5d4")
LAND_EDGE = QColor("#a9bfa0")
LAKE = QColor("#cfe3f0")
BORDER = QColor("#b9b2a4")
GRID = QColor(255, 255, 255, 140)
RUST = QColor("#c0622e")
FRAME = QColor("#cfd8dd")

TILE_URL = ("https://services.arcgisonline.com/ArcGIS/rest/services/"
            "World_Imagery/MapServer/tile/{z}/{y}/{x}")
TILE_ATTRIBUTION = "Imagery: Esri, Maxar, Earthstar Geographics"
TILE_SIZE = 256
MAX_TILE_ZOOM = 19

_WORLD = None


# --------------------------------------------------------------------------- #
# bundled outlines
# --------------------------------------------------------------------------- #
def load_world(assets_dir):
    """Load and cache the bundled outlines, with a bbox per shape for clipping."""
    global _WORLD
    if _WORLD is not None:
        return _WORLD
    _WORLD = {}
    try:
        with open(os.path.join(assets_dir, "world.json"), encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception:
        return _WORLD
    if isinstance(raw, dict) and "polys" in raw:            # older single-layer format
        raw = {"globe": raw["polys"], "land": raw["polys"]}
    for key, shapes in raw.items():
        if key == "places":                                  # point records, not rings
            _WORLD[key] = shapes
            continue
        prepared = []
        for ring in shapes:
            xs = [c[0] for c in ring]
            ys = [c[1] for c in ring]
            prepared.append((ring, (min(xs), min(ys), max(xs), max(ys))))
        _WORLD[key] = prepared
    return _WORLD


def nearest_place(lat, lon, assets_dir):
    """(name, km, compass) of the closest populated place, or None."""
    places = load_world(assets_dir).get("places", [])
    if not places:
        return None
    cos_lat = max(math.cos(math.radians(lat)), 0.05)
    best = None
    for plon, plat, name, _rank, _pop in places:
        dx = (plon - lon) * cos_lat
        dy = plat - lat
        d2 = dx * dx + dy * dy
        if best is None or d2 < best[0]:
            best = (d2, name, dx, dy)
    _d2, name, dx, dy = best
    km = math.sqrt(dx * dx + dy * dy) * 111.32
    angle = math.degrees(math.atan2(-dx, -dy)) % 360
    points = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
              "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return name, km, points[int((angle + 11.25) % 360 // 22.5)]


def _nice_distance(km):
    for step in (10000, 5000, 2000, 1000, 500, 200, 100, 50, 20, 10, 5, 2, 1,
                 0.5, 0.2, 0.1, 0.05, 0.02):
        if km >= step:
            return step
    return 0.01


# --------------------------------------------------------------------------- #
# Globe
# --------------------------------------------------------------------------- #
class GlobeView(QWidget):
    """An orthographic globe centred on the data — 'where on Earth is this'."""

    def __init__(self, lat, lon, assets_dir):
        super().__init__()
        self.lat, self.lon = lat, lon
        self.assets_dir = assets_dir
        self.setMinimumSize(190, 190)

    def _vec(self, lon_deg, lat_deg):
        """Unit vector in view space: (right, up, towards-viewer)."""
        la = math.radians(lat_deg)
        lo = math.radians(lon_deg) - math.radians(self.lon)
        lat0 = math.radians(self.lat)
        sin0, cos0 = math.sin(lat0), math.cos(lat0)
        cos_la, sin_la = math.cos(la), math.sin(la)
        return (cos_la * math.sin(lo),
                cos0 * sin_la - sin0 * cos_la * math.cos(lo),
                sin0 * sin_la + cos0 * cos_la * math.cos(lo))

    @staticmethod
    def _limb_crossing(a, b):
        """Point on the horizon between a (visible) and b (hidden)."""
        t = a[2] / (a[2] - b[2]) if (a[2] - b[2]) else 0.0
        p = tuple(a[i] + t * (b[i] - a[i]) for i in range(3))
        n = math.sqrt(sum(c * c for c in p)) or 1.0
        return (p[0] / n, p[1] / n, 0.0)

    def _ring_polygon(self, ring):
        """Project a ring for filling, or None if it's entirely on the far side.

        Vertices behind the horizon are pushed radially onto the limb rather than
        dropped. Combined with clipping to the disc, that keeps every polygon closed
        and inside the globe — dropping them and joining what's left is what sliced
        those wedges across the face.
        """
        pts = []
        any_visible = False
        for c in ring:
            x, y, z = self._vec(c[0], c[1])
            if z >= 0:
                any_visible = True
            else:
                n = math.hypot(x, y) or 1.0
                x, y = x / n, y / n
            pts.append((x, y))
        return pts if any_visible else None

    def _visible_runs(self, ring):
        """Split a ring into runs of visible vertices, cut exactly at the horizon.

        Dropping hidden vertices and joining what's left is what produced the stray
        wedges across the disc — the join was a straight chord through the globe.
        Inserting the true horizon crossing makes coastlines stop at the limb.
        """
        verts = [self._vec(c[0], c[1]) for c in ring]
        if not verts:
            return []
        runs, current = [], []
        for i, v in enumerate(verts):
            prev = verts[i - 1]
            if v[2] >= 0:
                if prev[2] < 0 and current == [] and i > 0:
                    current.append(self._limb_crossing(v, prev))
                current.append(v)
            else:
                if current:
                    current.append(self._limb_crossing(current[-1], v))
                    runs.append(current)
                    current = []
        if current:
            runs.append(current)
        return [self._close_on_limb(r) for r in runs]

    @staticmethod
    def _close_on_limb(run):
        """Follow the horizon between a run's endpoints instead of cutting a chord.

        A landmass clipped by the limb should hug the edge of the disc; joining its
        two cut ends with a straight line slices a wedge out of the globe.
        """
        if len(run) < 3:
            return run
        a, b = run[-1], run[0]
        if abs(a[2]) > 1e-6 or abs(b[2]) > 1e-6:
            return run                        # not both on the horizon: nothing to do
        start = math.atan2(a[1], a[0])
        end = math.atan2(b[1], b[0])
        delta = (end - start + math.pi) % (2 * math.pi) - math.pi   # shorter way round
        steps = max(1, int(abs(delta) / math.radians(3)))
        arc = [(math.cos(start + delta * i / steps),
                math.sin(start + delta * i / steps), 0.0)
               for i in range(1, steps)]
        return run + arc

    def paintEvent(self, _event):
        w, h = self.width(), self.height()
        radius = min(w, h) / 2.0 - 6
        cx, cy = w / 2.0, h / 2.0

        def screen(v):
            return QPointF(cx + v[0] * radius, cy - v[1] * radius)

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor("#b7c6d1"), 1))
        p.setBrush(QBrush(OCEAN))
        p.drawEllipse(QPointF(cx, cy), radius, radius)

        disc = QPainterPath()
        disc.addEllipse(QPointF(cx, cy), radius, radius)
        p.save()
        p.setClipPath(disc)
        p.setBrush(QBrush(LAND))
        p.setPen(QPen(LAND_EDGE, 0))
        for ring, _bbox in load_world(self.assets_dir).get("globe", []):
            flat = self._ring_polygon(ring)
            if flat and len(flat) >= 3:
                p.drawPolygon(QPolygonF([
                    QPointF(cx + vx * radius, cy - vy * radius) for vx, vy in flat]))
        p.restore()

        p.setPen(QPen(GRID, 0))
        for lon_line in range(-180, 181, 30):
            for run in self._visible_runs([[lon_line, la] for la in range(-90, 91, 3)]):
                if len(run) > 1:
                    p.drawPolyline(QPolygonF([screen(v) for v in run]))
        for lat_line in range(-60, 61, 30):
            for run in self._visible_runs([[lo, lat_line] for lo in range(-180, 181, 3)]):
                if len(run) > 1:
                    p.drawPolyline(QPolygonF([screen(v) for v in run]))

        here = self._vec(self.lon, self.lat)
        if here[2] >= 0:
            p.setPen(QPen(QColor(255, 255, 255), 2))
            p.setBrush(QBrush(RUST))
            p.drawEllipse(screen(here), 5, 5)
        p.end()


# --------------------------------------------------------------------------- #
# Satellite tiles
# --------------------------------------------------------------------------- #
class TileFetcher:
    """Async Web-Mercator tile fetching with a disk cache. Never blocks the UI."""

    def __init__(self):
        self.net = QNetworkAccessManager()
        self.memory = {}
        self.pending = set()
        self.failures = 0
        base = QStandardPaths.writableLocation(QStandardPaths.CacheLocation) or ""
        self.cache_dir = os.path.join(base or os.path.expanduser("~"), "kestrel_tiles")

    def offline(self):
        return self.failures >= 6            # stop hammering a network that isn't there

    def _disk_path(self, z, x, y):
        return os.path.join(self.cache_dir, str(z), str(x), f"{y}.jpg")

    def get(self, z, x, y, on_ready):
        """Return a QPixmap now, or None and call ``on_ready`` when it arrives."""
        key = (z, x, y)
        if key in self.memory:
            return self.memory[key]
        path = self._disk_path(z, x, y)
        if os.path.exists(path):
            pix = QPixmap(path)
            if not pix.isNull():
                self.memory[key] = pix
                return pix
        if key in self.pending or self.offline():
            return None
        self.pending.add(key)
        request = QNetworkRequest(QUrl(TILE_URL.format(z=z, x=x, y=y)))
        request.setHeader(QNetworkRequest.UserAgentHeader, "Kestrel/0.3 (GIS sanity checker)")
        request.setTransferTimeout(6000)
        reply = self.net.get(request)

        def done():
            self.pending.discard(key)
            data = reply.readAll()
            reply.deleteLater()
            pix = QPixmap()
            if data and pix.loadFromData(data) and not pix.isNull():
                self.memory[key] = pix
                self.failures = 0
                try:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    pix.save(path, "JPG")
                except OSError:
                    pass
                on_ready()
            else:
                self.failures += 1
                on_ready()

        reply.finished.connect(done)
        return None


_FETCHER = None


def tile_fetcher():
    global _FETCHER
    if _FETCHER is None:
        _FETCHER = TileFetcher()
    return _FETCHER


def _lon_to_tile(lon, z):
    return (lon + 180.0) / 360.0 * (2 ** z)


def _lat_to_tile(lat, z):
    lat = max(min(lat, 85.05), -85.05)
    s = math.sin(math.radians(lat))
    return (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * (2 ** z)


def _tile_to_lon(tx, z):
    return tx / (2 ** z) * 360.0 - 180.0


def _tile_to_lat(ty, z):
    n = math.pi - 2 * math.pi * ty / (2 ** z)
    return math.degrees(math.atan(math.sinh(n)))


# --------------------------------------------------------------------------- #
# Detail pane
# --------------------------------------------------------------------------- #
class ExtentView(QWidget):
    """A zoomed map of the data's own extent, over vectors or satellite imagery."""

    # Offline, there is nothing to draw closer than regional scale — a 2 km window
    # inland renders as a blank green box. With imagery there is always detail.
    MIN_SPAN_OFFLINE = 1.1
    MIN_SPAN_SATELLITE = 0.004          # ~450 m

    def __init__(self, west, south, east, north, assets_dir, satellite=False, preview=None):
        super().__init__()
        self.box = (west, south, east, north)
        self.assets_dir = assets_dir
        self.satellite = satellite
        self.preview = preview
        self.setMinimumHeight(200)

    def set_satellite(self, on):
        self.satellite = bool(on)
        self.update()

    def _window(self, w, h):
        west, south, east, north = self.box
        clat = max(min((south + north) / 2.0, 85.0), -85.0)
        clon = (west + east) / 2.0
        cos_lat = max(math.cos(math.radians(clat)), 0.05)

        floor = self.MIN_SPAN_SATELLITE if self.satellite else self.MIN_SPAN_OFFLINE
        pad = 1.8 if self.satellite else 2.6
        span_lat = max(north - south, floor) * pad
        span_lon = max(east - west, floor / cos_lat) * pad

        aspect = max(w, 1) / max(h, 1)
        if span_lon * cos_lat / span_lat < aspect:
            span_lon = span_lat * aspect / cos_lat
        else:
            span_lat = span_lon * cos_lat / aspect
        return clon, clat, span_lon, span_lat

    def paintEvent(self, _event):
        w, h = self.width(), self.height()
        clon, clat, span_lon, span_lat = self._window(w, h)
        left, right = clon - span_lon / 2.0, clon + span_lon / 2.0
        bottom, top = clat - span_lat / 2.0, clat + span_lat / 2.0

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)

        drew_imagery = False
        if self.satellite and not tile_fetcher().offline():
            drew_imagery = self._paint_tiles(p, w, h, left, right, bottom, top)

        def X(lon):
            return (lon - left) / span_lon * w

        def Y(lat):
            return (top - lat) / span_lat * h

        if not drew_imagery:
            p.fillRect(0, 0, w, h, OCEAN)
            self._paint_vectors(p, X, Y, (left, bottom, right, top))
            self._graticule(p, w, h, X, Y, left, right, bottom, top)
            self._places(p, w, h, X, Y, (left, bottom, right, top))
            if self.satellite:
                self._note(p, w, h, "Imagery unavailable — showing offline map")

        self._marker(p, X, Y)
        self._scalebar(p, w, h, span_lon, clat, dark=drew_imagery)
        if drew_imagery:
            self._note(p, w, h, TILE_ATTRIBUTION)
        p.setPen(QPen(FRAME, 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(QRectF(0.5, 0.5, w - 1, h - 1))
        p.end()

    def _paint_tiles(self, p, w, h, left, right, bottom, top):
        """Draw Web-Mercator tiles covering the view. True if anything was drawn."""
        span_lon = right - left
        zoom = int(math.floor(math.log2(max(w, 1) * 360.0 / (TILE_SIZE * span_lon))))
        zoom = max(0, min(MAX_TILE_ZOOM, zoom))

        x0 = _lon_to_tile(left, zoom)
        x1 = _lon_to_tile(right, zoom)
        y0 = _lat_to_tile(top, zoom)
        y1 = _lat_to_tile(bottom, zoom)
        if x1 <= x0 or y1 <= y0:
            return False
        # Mercator: use the tile grid itself for vertical placement so imagery lines up
        px_per_tile_x = w / (x1 - x0)
        px_per_tile_y = h / (y1 - y0)
        if (x1 - x0) * (y1 - y0) > 400:            # sanity cap on tile count
            return False

        fetcher = tile_fetcher()
        drew = False
        for tx in range(int(math.floor(x0)), int(math.ceil(x1))):
            for ty in range(int(math.floor(y0)), int(math.ceil(y1))):
                if tx < 0 or ty < 0 or tx >= 2 ** zoom or ty >= 2 ** zoom:
                    continue
                pix = fetcher.get(zoom, tx, ty, self.update)
                if pix is None:
                    continue
                rect = QRectF((tx - x0) * px_per_tile_x, (ty - y0) * px_per_tile_y,
                              px_per_tile_x + 1, px_per_tile_y + 1)
                p.drawPixmap(rect, pix, QRectF(0, 0, pix.width(), pix.height()))
                drew = True
        if not drew:
            return False
        # the vertical scale differs slightly from the linear window; good enough at
        # these spans, and the marker below is drawn in the same linear frame
        return True

    def _paint_vectors(self, p, X, Y, view):
        world = load_world(self.assets_dir)

        def hits(b):
            return not (b[2] < view[0] or b[0] > view[2] or b[3] < view[1] or b[1] > view[3])

        def draw(layer, pen, brush, closed):
            p.setPen(pen)
            p.setBrush(brush)
            for ring, bbox in world.get(layer, []):
                if not hits(bbox):
                    continue
                poly = QPolygonF([QPointF(X(c[0]), Y(c[1])) for c in ring])
                p.drawPolygon(poly) if closed else p.drawPolyline(poly)

        draw("land", QPen(LAND_EDGE, 1), QBrush(LAND), True)
        draw("lakes", QPen(QColor("#a9c6d8"), 1), QBrush(LAKE), True)
        draw("admin1", QPen(BORDER, 1, Qt.DashLine), Qt.NoBrush, False)
        draw("admin0", QPen(QColor("#8d8578"), 1.4), Qt.NoBrush, False)

    def _marker(self, p, X, Y):
        """Draw the data itself — the real features when we have them, else the extent."""
        if self.preview and self._draw_preview(p, X, Y):
            return

        west, south, east, north = self.box
        x0, x1, y0, y1 = X(west), X(east), Y(north), Y(south)
        p.setPen(QPen(RUST, 2))
        if abs(x1 - x0) < 6 and abs(y1 - y0) < 6:
            cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
            p.setBrush(QBrush(QColor(192, 98, 46, 60)))
            p.drawEllipse(QPointF(cx, cy), 13, 13)
            p.setBrush(QBrush(RUST))
            p.setPen(QPen(QColor(255, 255, 255), 1.5))
            p.drawEllipse(QPointF(cx, cy), 4.5, 4.5)
        else:
            p.setBrush(QBrush(QColor(192, 98, 46, 55)))
            p.drawRect(QRectF(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0)))

    def _draw_preview(self, p, X, Y):
        """Render the actual features. False if there was nothing worth drawing."""
        drew = False
        halo = QColor(255, 255, 255, 170)

        polygons = self.preview.get("polygons") or []
        if polygons:
            p.setPen(QPen(RUST, 2))
            p.setBrush(QBrush(QColor(192, 98, 46, 70)))
            for ring in polygons:
                p.drawPolygon(QPolygonF([QPointF(X(c[0]), Y(c[1])) for c in ring]))
            drew = True

        lines = self.preview.get("lines") or []
        if lines:
            for width, colour in ((3.5, halo), (1.8, RUST)):
                p.setPen(QPen(colour, width))
                p.setBrush(Qt.NoBrush)
                for line in lines:
                    p.drawPolyline(QPolygonF([QPointF(X(c[0]), Y(c[1])) for c in line]))
            drew = True

        points = self.preview.get("points") or []
        if points:
            # size the dots to the crowd: big and clear when there are a few, small
            # enough to still read as a pattern when there are thousands
            r = 4.0 if len(points) <= 60 else (2.6 if len(points) <= 600 else 1.6)
            p.setPen(QPen(halo, 1.2))
            p.setBrush(QBrush(RUST))
            for c in points:
                p.drawEllipse(QPointF(X(c[0]), Y(c[1])), r, r)
            drew = True
        return drew

    def _places(self, p, w, h, X, Y, view):
        left, bottom, right, top = view
        font = QFont()
        font.setPointSize(8)
        p.setFont(font)
        shown = 0
        for lon, lat, name, _rank, _pop in load_world(self.assets_dir).get("places", []):
            if not (left <= lon <= right and bottom <= lat <= top):
                continue
            x, y = X(lon), Y(lat)
            if not (6 < x < w - 6 and 10 < y < h - 6):
                continue
            p.setPen(QPen(QColor("#4a5763"), 1))
            p.setBrush(QBrush(QColor("#5d6d7e")))
            p.drawEllipse(QPointF(x, y), 2.6, 2.6)
            p.setPen(QPen(QColor("#2c3e50")))
            p.drawText(QPointF(x + 5, y + 3), name)
            shown += 1
            if shown >= 14:
                break

    @staticmethod
    def _graticule(p, w, h, X, Y, left, right, bottom, top):
        span = max(right - left, top - bottom)
        step = next((s for s in (30, 10, 5, 2, 1, 0.5, 0.2, 0.1, 0.05, 0.02, 0.01)
                     if span / s >= 3), 0.01)
        p.setPen(QPen(GRID, 1))
        font = QFont()
        font.setPointSize(7)
        p.setFont(font)
        v = math.floor(left / step) * step
        while v <= right:
            x = X(v)
            p.drawLine(QPointF(x, 0), QPointF(x, h))
            p.drawText(QPointF(x + 3, h - 4), f"{v:g}°")
            v += step
        v = math.floor(bottom / step) * step
        while v <= top:
            y = Y(v)
            p.drawLine(QPointF(0, y), QPointF(w, y))
            p.drawText(QPointF(3, y - 3), f"{v:g}°")
            v += step

    @staticmethod
    def _note(p, w, h, text):
        font = QFont()
        font.setPointSize(7)
        p.setFont(font)
        p.setPen(QPen(QColor(255, 255, 255, 210)))
        p.fillRect(QRectF(0, h - 14, w, 14), QColor(0, 0, 0, 90))
        p.drawText(QPointF(6, h - 4), text)

    @staticmethod
    def _scalebar(p, w, h, span_lon, clat, dark=False):
        km_across = span_lon * 111.32 * max(math.cos(math.radians(clat)), 0.05)
        if km_across <= 0:
            return
        target = _nice_distance(km_across * 0.3)
        px = target / km_across * w
        if px < 20:
            return
        x0, y0 = w - px - 14, h - 26
        colour = QColor("#ffffff") if dark else QColor("#2c3e50")
        p.setPen(QPen(colour, 2))
        p.drawLine(QPointF(x0, y0), QPointF(x0 + px, y0))
        p.drawLine(QPointF(x0, y0 - 4), QPointF(x0, y0 + 4))
        p.drawLine(QPointF(x0 + px, y0 - 4), QPointF(x0 + px, y0 + 4))
        font = QFont()
        font.setPointSize(8)
        font.setBold(True)
        p.setFont(font)
        label = f"{target:g} km" if target >= 1 else f"{target * 1000:g} m"
        p.drawText(QPointF(x0, y0 - 7), label)


# --------------------------------------------------------------------------- #
# Card
# --------------------------------------------------------------------------- #
class MapCard(QWidget):
    """Globe on the left for orientation, zoomed extent on the right for detail."""

    def __init__(self, loc, assets_dir, satellite=False, on_toggle=None, preview=None):
        super().__init__()
        self.on_toggle = on_toggle
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)

        globe_col = QVBoxLayout()
        globe_col.setSpacing(2)
        globe_col.addWidget(GlobeView(loc.center_lat, loc.center_lon, assets_dir))
        globe_col.addWidget(self._caption("Where on Earth"))
        row.addLayout(globe_col, 0)

        detail_col = QVBoxLayout()
        detail_col.setSpacing(2)
        self.extent = ExtentView(loc.west, loc.south, loc.east, loc.north,
                                 assets_dir, satellite=satellite, preview=preview)
        detail_col.addWidget(self.extent)

        bar = QHBoxLayout()
        span_km = max(
            (loc.north - loc.south) * 111.32,
            (loc.east - loc.west) * 111.32 * max(
                math.cos(math.radians(loc.center_lat)), 0.05))
        extent_note = ("a single spot" if span_km < 0.05
                       else f"{span_km:.1f} km across" if span_km < 10
                       else f"{span_km:.0f} km across")
        near = nearest_place(loc.center_lat, loc.center_lon, assets_dir)
        if near:
            name, km, compass = near
            where = f"in {name}" if km < 3 else f"{km:.0f} km {compass} of {name}"
            text = f"Data is {extent_note} — {where}"
        else:
            text = f"Zoomed to the data — {extent_note}"
        bar.addWidget(self._caption(text), 1)

        self.toggle = QCheckBox("Satellite")
        self.toggle.setChecked(bool(satellite))
        self.toggle.setToolTip(
            "Show Esri World Imagery under the data (needs an internet connection).\n"
            "Turn it off to use the offline map — Kestrel falls back to it automatically\n"
            "if imagery can't be reached.")
        # The default indicator all but vanishes against the app's light palette, so it
        # gets an explicit box: white when off, filled rust when on.
        self.toggle.setStyleSheet("""
            QCheckBox { color: #566573; font-size: 11px; spacing: 6px; }
            QCheckBox::indicator {
                width: 13px; height: 13px;
                border: 1px solid #8b97a1; border-radius: 3px; background: #ffffff;
            }
            QCheckBox::indicator:hover { border-color: #2980b9; }
            QCheckBox::indicator:checked {
                background: #c0622e; border: 1px solid #8f4720;
            }
            QCheckBox::indicator:checked:hover { background: #a4501f; }
        """)
        self.toggle.toggled.connect(self._toggled)
        bar.addWidget(self.toggle, 0)
        detail_col.addLayout(bar)
        row.addLayout(detail_col, 1)

    def _toggled(self, on):
        self.extent.set_satellite(on)
        if self.on_toggle:
            self.on_toggle(on)

    @staticmethod
    def _caption(text):
        label = QLabel(text)
        label.setAlignment(Qt.AlignCenter)
        label.setStyleSheet("color: #7f8c8d; font-size: 11px; border: none;")
        return label
