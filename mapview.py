"""The map card: a small globe for orientation next to a zoomed view of the data.

Everything is drawn with QPainter over the outlines bundled in ``assets/world.json`` —
no tiles, no network, no API keys. That keeps Kestrel instant and usable on locked-down
machines, which is where a lot of GIS work actually happens.

The trade-off is honest: offline vectors give you coastlines, lakes and state/province
borders, so you can tell *where* the data landed — not what the field looks like.
"""

from __future__ import annotations

import json
import math
import os

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

OCEAN = QColor("#e8f1f7")
LAND = QColor("#dbe5d4")
LAND_EDGE = QColor("#a9bfa0")
LAKE = QColor("#cfe3f0")
BORDER = QColor("#b9b2a4")
GRID = QColor(255, 255, 255, 140)
RUST = QColor("#c0622e")
FRAME = QColor("#cfd8dd")

_WORLD = None


def load_world(assets_dir):
    """Load and cache the bundled outlines, with a bounding box per shape for clipping."""
    global _WORLD
    if _WORLD is not None:
        return _WORLD
    _WORLD = {}
    path = os.path.join(assets_dir, "world.json")
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception:
        return _WORLD
    if isinstance(raw, dict) and "polys" in raw:        # older single-layer format
        raw = {"globe": raw["polys"], "land": raw["polys"]}
    for key, shapes in raw.items():
        if key == "places":               # already point records, not rings
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
    best = None
    cos_lat = max(math.cos(math.radians(lat)), 0.05)
    for plon, plat, name, _rank, _pop in places:
        dx = (plon - lon) * cos_lat
        dy = plat - lat
        d2 = dx * dx + dy * dy
        if best is None or d2 < best[0]:
            best = (d2, name, dx, dy)
    if best is None:
        return None
    _d2, name, dx, dy = best
    km = math.sqrt(dx * dx + dy * dy) * 111.32
    # bearing FROM the place TO the data
    angle = math.degrees(math.atan2(-dx, -dy)) % 360
    points = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
              "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return name, km, points[int((angle + 11.25) % 360 // 22.5)]


def _nice_distance(km):
    """A round number at or below ``km``, for the scale bar."""
    for step in (10000, 5000, 2000, 1000, 500, 200, 100, 50, 20, 10, 5, 2, 1,
                 0.5, 0.2, 0.1):
        if km >= step:
            return step
    return 0.05


class GlobeView(QWidget):
    """An orthographic globe centred on the data — 'where on Earth is this'."""

    def __init__(self, lat, lon, assets_dir):
        super().__init__()
        self.lat, self.lon = lat, lon
        self.assets_dir = assets_dir
        self.setMinimumSize(190, 190)

    def paintEvent(self, _event):
        w, h = self.width(), self.height()
        radius = min(w, h) / 2.0 - 6
        cx, cy = w / 2.0, h / 2.0
        lat0 = math.radians(self.lat)
        lon0 = math.radians(self.lon)
        sin0, cos0 = math.sin(lat0), math.cos(lat0)

        def project(lon_deg, lat_deg):
            la, lo = math.radians(lat_deg), math.radians(lon_deg) - lon0
            cos_c = sin0 * math.sin(la) + cos0 * math.cos(la) * math.cos(lo)
            if cos_c < 0:                     # on the far side of the globe
                return None
            x = math.cos(la) * math.sin(lo)
            y = cos0 * math.sin(la) - sin0 * math.cos(la) * math.cos(lo)
            return QPointF(cx + x * radius, cy - y * radius)

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor("#b7c6d1"), 1))
        p.setBrush(QBrush(OCEAN))
        p.drawEllipse(QPointF(cx, cy), radius, radius)

        p.setBrush(QBrush(LAND))
        p.setPen(QPen(LAND_EDGE, 0))
        for ring, _bbox in load_world(self.assets_dir).get("globe", []):
            pts = [project(c[0], c[1]) for c in ring]
            visible = [pt for pt in pts if pt is not None]
            if len(visible) >= 3 and len(visible) > len(pts) * 0.55:
                p.drawPolygon(QPolygonF(visible))

        # graticule every 30°
        p.setPen(QPen(GRID, 0))
        for lon_line in range(-180, 181, 30):
            pts = [project(lon_line, la) for la in range(-90, 91, 5)]
            pts = [pt for pt in pts if pt is not None]
            if len(pts) > 1:
                p.drawPolyline(QPolygonF(pts))
        for lat_line in range(-60, 61, 30):
            pts = [project(lo, lat_line) for lo in range(-180, 181, 5)]
            pts = [pt for pt in pts if pt is not None]
            if len(pts) > 1:
                p.drawPolyline(QPolygonF(pts))

        here = project(self.lon, self.lat)
        if here is not None:
            p.setPen(QPen(QColor(255, 255, 255), 2))
            p.setBrush(QBrush(RUST))
            p.drawEllipse(here, 5, 5)
        p.end()


class ExtentView(QWidget):
    """A zoomed map of the data's own extent, with context and a scale bar."""

    # Don't zoom closer than this. A 2 km window inland shows nothing but green —
    # the outlines only start being informative at regional scale, where towns,
    # borders and lakes tell you where you actually are.
    MIN_SPAN_DEG = 1.1

    def __init__(self, west, south, east, north, assets_dir):
        super().__init__()
        self.box = (west, south, east, north)
        self.assets_dir = assets_dir
        self.setMinimumHeight(190)

    def _window(self, w, h):
        """The lon/lat window to draw, padded around the data and aspect-corrected."""
        west, south, east, north = self.box
        clat = max(min((south + north) / 2.0, 89.0), -89.0)
        clon = (west + east) / 2.0
        cos_lat = max(math.cos(math.radians(clat)), 0.05)

        span_lat = max(north - south, self.MIN_SPAN_DEG)
        span_lon = max(east - west, self.MIN_SPAN_DEG / cos_lat)
        span_lat *= 2.6                      # padding: show the data in its surroundings
        span_lon *= 2.6

        # match the widget's aspect ratio, remembering 1° lon is shorter than 1° lat
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

        def X(lon):
            return (lon - left) / span_lon * w

        def Y(lat):
            return (top - lat) / span_lat * h

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(0, 0, w, h, OCEAN)

        world = load_world(self.assets_dir)
        view = (left, bottom, right, top)

        def hits(bbox):
            return not (bbox[2] < view[0] or bbox[0] > view[2]
                        or bbox[3] < view[1] or bbox[1] > view[3])

        def draw(layer, pen, brush, closed):
            p.setPen(pen)
            p.setBrush(brush)
            for ring, bbox in world.get(layer, []):
                if not hits(bbox):
                    continue
                poly = QPolygonF([QPointF(X(c[0]), Y(c[1])) for c in ring])
                if closed:
                    p.drawPolygon(poly)
                else:
                    p.drawPolyline(poly)

        draw("land", QPen(LAND_EDGE, 1), QBrush(LAND), True)
        draw("lakes", QPen(QColor("#a9c6d8"), 1), QBrush(LAKE), True)
        draw("admin1", QPen(BORDER, 1, Qt.DashLine), Qt.NoBrush, False)
        draw("admin0", QPen(QColor("#8d8578"), 1.4), Qt.NoBrush, False)

        self._graticule(p, w, h, X, Y, left, right, bottom, top)
        self._places(p, w, h, X, Y, view)

        # the data itself
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

        self._scalebar(p, w, h, span_lon, clat)
        p.setPen(QPen(FRAME, 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(QRectF(0.5, 0.5, w - 1, h - 1))
        p.end()

    def _places(self, p, w, h, X, Y, view):
        """Town and city labels — inland, these are what actually orient you."""
        left, bottom, right, top = view
        shown = 0
        font = QFont()
        font.setPointSize(8)
        p.setFont(font)
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
            if shown >= 14:                  # keep it readable
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
        start = math.floor(left / step) * step
        while start <= right:
            x = X(start)
            p.drawLine(QPointF(x, 0), QPointF(x, h))
            p.drawText(QPointF(x + 3, h - 4), f"{start:g}°")
            start += step
        start = math.floor(bottom / step) * step
        while start <= top:
            y = Y(start)
            p.drawLine(QPointF(0, y), QPointF(w, y))
            p.drawText(QPointF(3, y - 3), f"{start:g}°")
            start += step

    @staticmethod
    def _scalebar(p, w, h, span_lon, clat):
        km_across = span_lon * 111.32 * max(math.cos(math.radians(clat)), 0.05)
        if km_across <= 0:
            return
        target_km = _nice_distance(km_across * 0.3)
        px = target_km / km_across * w
        if px < 20:
            return
        x0, y0 = w - px - 14, h - 16
        p.setPen(QPen(QColor("#2c3e50"), 2))
        p.drawLine(QPointF(x0, y0), QPointF(x0 + px, y0))
        p.drawLine(QPointF(x0, y0 - 4), QPointF(x0, y0 + 4))
        p.drawLine(QPointF(x0 + px, y0 - 4), QPointF(x0 + px, y0 + 4))
        font = QFont()
        font.setPointSize(8)
        font.setBold(True)
        p.setFont(font)
        label = f"{target_km:g} km" if target_km >= 1 else f"{target_km * 1000:g} m"
        p.drawText(QPointF(x0, y0 - 7), label)


class MapCard(QWidget):
    """Globe on the left for orientation, zoomed extent on the right for detail."""

    def __init__(self, loc, assets_dir):
        super().__init__()
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
        detail_col.addWidget(ExtentView(loc.west, loc.south, loc.east, loc.north,
                                        assets_dir))
        span_km = max(
            (loc.north - loc.south) * 111.32,
            (loc.east - loc.west) * 111.32 * max(
                math.cos(math.radians(loc.center_lat)), 0.05),
        )
        extent_note = ("a single spot" if span_km < 0.05
                       else f"{span_km:.1f} km across" if span_km < 10
                       else f"{span_km:.0f} km across")
        near = nearest_place(loc.center_lat, loc.center_lon, assets_dir)
        if near:
            name, km, compass = near
            where = (f"in {name}" if km < 3
                     else f"{km:.0f} km {compass} of {name}")
            caption = f"Data is {extent_note} — {where}"
        else:
            caption = f"Zoomed to the data — {extent_note}"
        detail_col.addWidget(self._caption(caption))
        row.addLayout(detail_col, 1)

    @staticmethod
    def _caption(text):
        label = QLabel(text)
        label.setAlignment(Qt.AlignCenter)
        label.setStyleSheet("color: #7f8c8d; font-size: 11px; border: none;")
        return label
