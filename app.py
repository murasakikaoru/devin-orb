"""Devin Orb — floating usage ball for Devin (PySide6 edition).

A small always-on-top floating orb (glass clock ball + quota ring) that opens
a usage panel with a spring animation on click. Double-click (optional setting)
pins the panel on top so it stays visible.

Run:  python app.py      (or pythonw app.py for no console)
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import config
import core

APP_DIR = config._config_dir()
LOG_PATH = os.path.join(APP_DIR, "orb.log")


def log(msg: str) -> None:
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


# --- single instance ----------------------------------------------------------

def ensure_single_instance() -> bool:
    if sys.platform != "win32":
        return True
    import ctypes
    ctypes.windll.kernel32.CreateMutexW(None, False, "DevinOrb-SingleInstance")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


# --- application state --------------------------------------------------------

class State:
    def __init__(self):
        self.cfg = config.load()
        self.auth = None
        self.auth_error = False
        self.data = None          # normalized status dict
        self.last_error = None    # 'auth' | 'net' | None
        self.last_fetch = 0.0
        self.panel_pinned = bool(self.cfg.get("panel_pinned"))
        self.last_blur_hide = 0.0
        self.panel_shown_at = 0.0
        self.lock = threading.Lock()

    def ring_percent_left(self):
        """Percent *remaining* driving the orb ring, per ring_source."""
        d = self.data or {}
        daily, weekly = d.get("daily_left"), d.get("weekly_left")
        src = self.cfg.get("ring_source", "auto")
        if src == "daily":
            v = daily if daily is not None else weekly
        elif src == "weekly":
            v = weekly if weekly is not None else daily
        else:
            vals = [x for x in (daily, weekly) if x is not None]
            v = min(vals) if vals else None
        return v


S = State()


def fetch(force=False):
    """Refresh account data. Returns True on success."""
    now = time.time()
    if not force and now - S.last_fetch < 10:
        return True
    S.last_fetch = now
    if S.auth is None and not S.auth_error:
        try:
            S.auth = core.find_credentials()
        except core.NoCredentials:
            S.auth_error = True
            S.last_error = "auth"
            return False
    if S.auth_error or S.auth is None:
        return False
    try:
        S.data = core.fetch_status(S.auth)
        S.data["source"] = S.auth.get("source")
        S.last_error = None
        return True
    except core.ApiError as e:
        if e.status in (401, 403):
            S.auth = None
            S.auth_error = False     # re-resolve creds next time
        S.last_error = "net"
        return False
    except Exception:
        S.last_error = "net"
        return False


def ring_mode() -> str:
    return config.effective_ring_mode(S.cfg)


def theme() -> str:
    return config.effective_theme(S.cfg)


def state_payload() -> dict:
    d = dict(S.data or {})
    return {
        "settings": {k: S.cfg[k] for k in
                     ("dblclick_pin", "ring_mode", "ring_source", "theme",
                      "start_on_login", "devin_hook", "refresh_secs")},
        "lang": S.cfg.get("language", "zh"),
        "pinned": S.panel_pinned,
        "plan": d.get("plan"),
        "daily_left": d.get("daily_left"),
        "weekly_left": d.get("weekly_left"),
        "daily_reset": d.get("daily_reset"),
        "weekly_reset": d.get("weekly_reset"),
        "overage_usd": d.get("overage_usd"),
        "period_start": d.get("period_start"),
        "period_end": d.get("period_end"),
        "fetched_at": d.get("fetched_at"),
        "ringShown": ring_mode(),
        "error": S.last_error,
    }


# --- Qt imports (deferred so --help-ish failures stay cheap) -------------------

from PySide6.QtCore import (QEasingCurve, QObject, QPointF, QRectF, Qt,
                            QTimer, QPropertyAnimation, Signal, Slot)
from PySide6.QtGui import (QColor, QConicalGradient, QGuiApplication, QPainter,
                           QPen, QRadialGradient)
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget


# --- the orb ------------------------------------------------------------------

ORB_WIN = 84           # native window size (logical px)
BALL_R = 26            # ball radius
RING_R = 33            # quota ring radius
ANIM_MS = 33           # ~30fps repaint


class OrbWindow(QWidget):
    """Fully custom-painted floating ball: glass sphere, live clock, quota ring."""

    openRequested = Signal()        # single click (after dblclick guard)
    pinRequested = Signal()         # double click
    contextRequested = Signal()     # right click

    def __init__(self):
        super().__init__(None,
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setFixedSize(ORB_WIN, ORB_WIN)
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)

        self._theme = "dark"
        self._pct = None            # shown ring percent (0..100) or None
        self._stale = False
        self._hover = 0.0           # 0..1 animated hover factor
        self._press = 0.0           # 0..1 animated press factor
        self._ripples = []          # birth times
        self._t0 = time.monotonic()
        self._drag_off = None
        self._moved = False
        self._click_guard = False   # suppress click right after double-click

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(ANIM_MS)

    # -- public state -------------------------------------------------------

    def set_theme(self, t: str):
        if t != self._theme:
            self._theme = t
            self.update()

    def set_ring(self, pct_left, mode, stale):
        shown = None if pct_left is None else (
            100 - pct_left if mode == "used" else pct_left)
        if shown != self._pct or stale != self._stale:
            self._pct = shown
            self._stale = stale
            self.update()

    # -- animation / interaction --------------------------------------------

    def _tick(self):
        self._ripples = [t for t in self._ripples
                         if time.monotonic() - t < 0.55]
        # ease hover/press toward targets
        self._hover += (self._hover_t - self._hover) * 0.18
        self._press += (self._press_t - self._press) * 0.30
        self.update()

    _hover_t = 0.0
    _press_t = 0.0

    def enterEvent(self, e):
        self._hover_t = 1.0

    def leaveEvent(self, e):
        self._hover_t = 0.0
        self._press_t = 0.0

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press_t = 1.0
            self._drag_off = e.globalPosition().toPoint() - self.pos()
            self._moved = False

    def mouseMoveEvent(self, e):
        if self._drag_off is None:
            return
        gp = e.globalPosition().toPoint()
        if not self._moved and (gp - self.pos() - self._drag_off).manhattanLength() > 4:
            self._moved = True
            self._press_t = 0.0
        if self._moved:
            self.move(gp - self._drag_off)

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        self._press_t = 0.0
        was_drag = self._moved
        self._drag_off = None
        self._moved = False
        if was_drag:
            config.update(orb_x=self.x(), orb_y=self.y())
        elif not self._click_guard:
            self._ripples.append(time.monotonic())
            # guard so a double-click doesn't fire open+pin
            QTimer.singleShot(QApplication.doubleClickInterval(),
                              self._emit_open)

    def _emit_open(self):
        if not self._click_guard:
            self.openRequested.emit()

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._click_guard = True
            QTimer.singleShot(600, self._clear_guard)
            self.pinRequested.emit()

    def _clear_guard(self):
        self._click_guard = False

    def contextMenuEvent(self, e):
        self.contextRequested.emit()

    # -- painting -------------------------------------------------------------

    def _colors(self):
        dark = self._theme == "dark"
        # Devin / Cognition brand: pure black & white minimalism
        return {
            "accent": QColor("#FAFAFA") if dark else QColor("#0A0A0A"),
            "accent_hot": QColor("#FFFFFF") if dark else QColor("#3F3F46"),
            "ink": QColor("#FAFAFA") if dark else QColor("#0A0A0A"),
            "tick": QColor(255, 255, 255, 110) if dark else QColor(10, 10, 10, 90),
            "face": QColor(255, 255, 255, 12) if dark else QColor(10, 10, 10, 10),
            "track": QColor(255, 255, 255, 28) if dark else QColor(10, 10, 10, 24),
            "warn": QColor("#fbbf24"),
            "bad": QColor("#ef4444"),
            "stale": QColor("#8a8f98"),
        }

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)

        t = time.monotonic() - self._t0
        c = self.width() / 2
        breathe = 1.0 + 0.018 * math.sin(t * 2 * math.pi / 3.6)
        scale = breathe * (1 + 0.05 * self._hover - 0.10 * self._press)

        col = self._colors()
        dark = self._theme == "dark"

        # ambient glow under the ball (pulses with breathing)
        glow_a = 40 + int(28 * (0.5 + 0.5 * math.sin(t * 2 * math.pi / 3.6))) \
            + int(50 * self._hover)
        glow = QRadialGradient(QPointF(c, c + 6), BALL_R * 1.9)
        glow.setColorAt(0.0, QColor(col["accent"].red(), col["accent"].green(),
                                    col["accent"].blue(), min(glow_a, 110)))
        glow.setColorAt(0.55, QColor(col["accent"].red(), col["accent"].green(),
                                     col["accent"].blue(), int(glow_a * 0.35)))
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setBrush(glow)
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPointF(c, c + 6), BALL_R * 1.9, BALL_R * 1.9)

        # drop shadow
        sh = QRadialGradient(QPointF(c, c + 4), BALL_R * 1.35)
        sh.setColorAt(0.0, QColor(0, 0, 0, 90 + int(60 * self._hover)))
        sh.setColorAt(0.72, QColor(0, 0, 0, 40))
        sh.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setBrush(sh)
        p.drawEllipse(QPointF(c, c + 5), BALL_R * 1.28, BALL_R * 1.18)

        p.save()
        p.translate(c, c)
        p.scale(scale, scale)

        # ---- glass sphere ----------------------------------------------------
        if dark:
            # black pearl / obsidian glass
            ball = QRadialGradient(QPointF(-8, -10), BALL_R * 2.1)
            ball.setColorAt(0.0, QColor("#52525b"))
            ball.setColorAt(0.45, QColor("#232327"))
            ball.setColorAt(0.8, QColor("#0e0e11"))
            ball.setColorAt(1.0, QColor("#050506"))
        else:
            # white ceramic
            ball = QRadialGradient(QPointF(-8, -10), BALL_R * 2.1)
            ball.setColorAt(0.0, QColor("#ffffff"))
            ball.setColorAt(0.45, QColor("#f4f4f5"))
            ball.setColorAt(0.8, QColor("#e2e2e6"))
            ball.setColorAt(1.0, QColor("#c9c9cf"))
        p.setBrush(ball)
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPointF(0, 0), BALL_R, BALL_R)

        # specular highlight (top-left)
        spec = QRadialGradient(QPointF(-9, -12), 15)
        spec.setColorAt(0.0, QColor(255, 255, 255, 235 if dark else 255))
        spec.setColorAt(0.5, QColor(255, 255, 255, 90 if dark else 150))
        spec.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(spec)
        p.drawEllipse(QPointF(-9, -12), 15, 11)

        # bottom inner shadow
        ish = QRadialGradient(QPointF(4, 14), BALL_R * 1.4)
        ish.setColorAt(0.0, QColor(0, 0, 0, 70 if dark else 40))
        ish.setColorAt(0.7, QColor(0, 0, 0, 0))
        p.setBrush(ish)
        p.drawEllipse(QPointF(0, 0), BALL_R, BALL_R)

        # rim light
        rim = QPen(QColor(255, 255, 255, 60 if dark else 110), 1.0)
        p.setPen(rim)
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(0, 0), BALL_R - 0.6, BALL_R - 0.6)

        # ---- clock face ------------------------------------------------------
        fr = 18.5
        p.setBrush(col["face"])
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPointF(0, 0), fr, fr)

        # ticks: 12 hour marks
        p.save()
        for i in range(12):
            major = i % 3 == 0
            pen = QPen(col["tick"], 1.9 if major else 1.1)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            a = math.radians(i * 30)
            r2 = fr - 2.2
            r1 = r2 - (3.6 if major else 2.2)
            p.drawLine(QPointF(r1 * math.sin(a), -r1 * math.cos(a)),
                       QPointF(r2 * math.sin(a), -r2 * math.cos(a)))
        p.restore()

        now = time.localtime()
        sec = time.time() % 60
        minute = now.tm_min + sec / 60
        hour = (now.tm_hour % 12) + minute / 60

        # hour hand
        pen = QPen(col["ink"], 2.8, Qt.SolidLine, Qt.RoundCap)
        p.setPen(pen)
        a = math.radians(hour * 30)
        p.drawLine(QPointF(0, 0), QPointF(9.5 * math.sin(a), -9.5 * math.cos(a)))
        # minute hand
        pen = QPen(col["ink"], 2.0, Qt.SolidLine, Qt.RoundCap)
        p.setPen(pen)
        a = math.radians(minute * 6)
        p.drawLine(QPointF(0, 0), QPointF(13.5 * math.sin(a), -13.5 * math.cos(a)))
        # second hand (smooth sweep) + tail
        pen = QPen(col["accent"], 1.1, Qt.SolidLine, Qt.RoundCap)
        p.setPen(pen)
        a = math.radians(sec * 6)
        p.drawLine(QPointF(-4.5 * math.sin(a), 4.5 * math.cos(a)),
                   QPointF(15.5 * math.sin(a), -15.5 * math.cos(a)))
        # hub
        p.setPen(Qt.NoPen)
        p.setBrush(col["accent"])
        p.drawEllipse(QPointF(0, 0), 2.3, 2.3)
        p.setBrush(col["ink"])
        p.drawEllipse(QPointF(0, 0), 0.9, 0.9)

        p.restore()

        # ---- quota ring --------------------------------------------------------
        ring_pen = QPen(col["track"], 2.6)
        ring_pen.setCapStyle(Qt.RoundCap)
        p.setPen(ring_pen)
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(c, c), RING_R, RING_R)

        if self._pct is not None:
            pct = max(0.0, min(100.0, self._pct))
            arc_color = (col["stale"] if self._stale else
                         col["accent"] if pct >= 50 else
                         col["warn"] if pct >= 20 else col["bad"])
            if self._stale:
                arc_color.setAlpha(int(140 + 100 * (0.5 + 0.5 * math.sin(t * 4))))
            con = QConicalGradient(QPointF(c, c), 90)
            con.setColorAt(0.0, col["accent_hot"] if not self._stale else arc_color)
            con.setColorAt(min(pct / 100.0, 0.999), arc_color)
            con.setColorAt(min(pct / 100.0 + 0.001, 1.0), arc_color)
            pen = QPen(con if not self._stale else arc_color, 2.6)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            rect = QRectF(c - RING_R, c - RING_R, RING_R * 2, RING_R * 2)
            p.drawArc(rect, 90 * 16, int(-pct * 3.6 * 16))

        # error dot
        if self._stale:
            pulse = 0.5 + 0.5 * math.sin(t * 4.4)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(248, 113, 113, int(160 + 90 * pulse)))
            p.drawEllipse(QPointF(c + RING_R * 0.72, c - RING_R * 0.72), 4.2, 4.2)

        # click ripples
        for t0 in self._ripples:
            age = time.monotonic() - t0
            k = age / 0.55
            rp = QRadialGradient(QPointF(c, c), BALL_R + k * 26)
            alpha = int(120 * (1 - k))
            rp.setColorAt(0.0, QColor(0, 0, 0, 0))
            rp.setColorAt(0.85, QColor(col["accent"].red(), col["accent"].green(),
                                       col["accent"].blue(), alpha))
            rp.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.setBrush(rp)
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(c, c), BALL_R + k * 26, BALL_R + k * 26)

        p.end()


# --- the panel ---------------------------------------------------------------

PANEL_W, PANEL_H = 360, 500
APP = None
ORB = None
PANEL = None


class PanelBridge(QObject):
    """Object exposed to JS as `bridge` via QWebChannel."""

    @Slot()
    def panel_ready(self):
        push_panel()

    @Slot()
    def panel_blur(self):
        if S.panel_pinned:
            return
        if time.time() - S.panel_shown_at < 0.6:
            return
        hide_panel(from_blur=True)

    @Slot()
    def toggle_pin(self):
        S.panel_pinned = not S.panel_pinned
        config.update(panel_pinned=S.panel_pinned)
        push_panel()

    @Slot()
    def refresh_now(self):
        threading.Thread(target=lambda: (fetch(force=True), push_orb(), push_panel()),
                         daemon=True).start()

    @Slot(str)
    def set_language(self, lang):
        config.update(language=lang)
        push_panel()

    @Slot(str, str)
    def set_setting(self, key, value):
        if key not in ("dblclick_pin", "ring_mode", "ring_source", "theme",
                       "start_on_login", "devin_hook", "refresh_secs"):
            return
        v = value
        if value in ("true", "false"):
            v = value == "true"
        if key == "refresh_secs":
            v = int(value)
        S.cfg = config.update(**{key: v})
        if key == "start_on_login":
            config.set_autostart(bool(v), os.path.join(BASE_DIR, "app.py"))
        if key == "devin_hook":
            script = os.path.join(BASE_DIR, "app.py")
            ok = (config.install_devin_hook(script) if v
                  else config.remove_devin_hook())
            log(f"devin_hook {'installed' if v else 'removed'} ok={ok}")
        push_orb()
        push_panel()

    @Slot()
    def quit_app(self):
        QApplication.quit()


class PanelWindow(QWidget):
    def __init__(self):
        super().__init__(None,
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(PANEL_W, PANEL_H)

        self.web = QWebEngineView(self)
        self.web.setContextMenuPolicy(Qt.NoContextMenu)
        self.web.setAttribute(Qt.WA_TranslucentBackground)
        self.web.page().setBackgroundColor(Qt.transparent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.web)

        self.bridge = PanelBridge(self)
        ch = QWebChannel(self.web.page())
        ch.registerObject("bridge", self.bridge)
        self.web.page().setWebChannel(ch)
        self.web.load(f"file:///{os.path.join(BASE_DIR, 'web', 'panel.html').replace(os.sep, '/')}")

        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(140)
        self._fade.setEasingCurve(QEasingCurve.OutCubic)

    def event(self, e):
        # real deactivate -> close (equivalent of the old blur-hide)
        if e.type() == e.Type.WindowDeactivate and self.isVisible():
            if not S.panel_pinned and time.time() - S.panel_shown_at > 0.6:
                hide_panel(from_blur=True)
        return super().event(e)


def panel_anchor_position() -> tuple[int, int, str]:
    scr = QGuiApplication.primaryScreen().availableGeometry()
    sw, sh = scr.width(), scr.height()
    ox, oy = ORB.x(), ORB.y()
    cx, cy = ox + ORB_WIN / 2, oy + ORB_WIN / 2
    gap = 10
    if cx > scr.x() + sw / 2:
        px = ox - PANEL_W - gap
        ax = "100%"
    else:
        px = ox + ORB_WIN + gap
        ax = "0%"
    py = cy - PANEL_H / 2
    frac = 0.5
    top, bot = scr.y() + 8, scr.y() + sh - 8
    if py < top:
        frac = min(0.95, max(0.05, (cy - top) / PANEL_H))
        py = top
    elif py + PANEL_H > bot:
        frac = min(0.95, max(0.05, (cy - (bot - PANEL_H)) / PANEL_H))
        py = bot - PANEL_H
    px = max(scr.x() + 8, min(px, scr.x() + sw - PANEL_W - 8))
    return int(px), int(py), f"{ax} {frac * 100:.0f}%"


def js_call(expr: str):
    try:
        PANEL.web.page().runJavaScript(expr)
    except Exception as e:
        log(f"js err: {e!r}")


def show_panel(pinned=None):
    if pinned is not None:
        S.panel_pinned = pinned
    px, py, anchor = panel_anchor_position()
    S.panel_shown_at = time.time()
    PANEL.move(px, py)
    PANEL.setWindowOpacity(0.0)
    PANEL.show()
    js_call(f"setAnchor({json.dumps(anchor)}); playEnter()")
    PANEL._fade.stop()
    PANEL._fade.setStartValue(0.0)
    PANEL._fade.setEndValue(1.0)
    PANEL._fade.start()
    PANEL.activateWindow()
    PANEL.raise_()
    push_panel()


def hide_panel(animated=True, from_blur=False):
    if not PANEL.isVisible():
        return
    if from_blur:
        S.last_blur_hide = time.time()
    if animated:
        js_call("playExit()")
        PANEL._fade.stop()
        PANEL._fade.setStartValue(1.0)
        PANEL._fade.setEndValue(0.0)
        PANEL._fade.start()
        QTimer.singleShot(200, PANEL.hide)
    else:
        PANEL.hide()


def toggle_panel():
    if PANEL.isVisible():
        S.panel_pinned = False
        config.update(panel_pinned=False)
        hide_panel()
    else:
        show_panel()


def on_orb_click():
    # reopen-guard: a blur-hide caused by this same click means "close"
    if time.time() - S.last_blur_hide < 0.4 and not PANEL.isVisible():
        return
    toggle_panel()


def on_orb_dblclick():
    if S.cfg.get("dblclick_pin", True):
        show_panel(pinned=True)
        push_panel()
    else:
        show_panel()


def on_orb_context():
    show_panel()
    QTimer.singleShot(60, lambda: js_call(
        "document.getElementById('settings').classList.add('open')"))


# --- pushes ------------------------------------------------------------------

def push_orb():
    if not ORB:
        return
    ORB.set_theme(theme())
    ORB.set_ring(S.ring_percent_left(), ring_mode(), S.last_error is not None)


def push_panel():
    if not PANEL:
        return
    js_call(f"setTheme({json.dumps(theme())})")
    js_call(f"updateState({json.dumps(state_payload())})")


# --- background loops ----------------------------------------------------------

class Bus(QObject):
    """Thread-safe wake-up into the GUI thread."""
    poke = Signal()


BUS = Bus()


def refresh_loop():
    while True:
        try:
            fetch()
            BUS.poke.emit()
        except Exception as e:
            log(f"refresh err: {e!r}")
        time.sleep(max(15, int(S.cfg.get("refresh_secs", 120))))


def theme_watch_loop():
    last = (theme(), ring_mode())
    while True:
        time.sleep(2.0)
        try:
            cur = (theme(), ring_mode())
            if cur != last:
                last = cur
                BUS.poke.emit()
        except Exception:
            pass


# --- main -----------------------------------------------------------------------

def main():
    global APP, ORB, PANEL
    if not ensure_single_instance():
        log("another instance running; exiting")
        return

    QApplication.setApplicationName("Devin Orb")
    APP = QApplication(sys.argv)
    APP.setQuitOnLastWindowClosed(False)

    ORB = OrbWindow()
    PANEL = PanelWindow()

    ORB.openRequested.connect(on_orb_click)
    ORB.pinRequested.connect(on_orb_dblclick)
    ORB.contextRequested.connect(on_orb_context)
    BUS.poke.connect(lambda: (push_orb(), push_panel()))

    # position: saved or default (right edge, upper third)
    scr = QGuiApplication.primaryScreen().availableGeometry()
    ox = S.cfg.get("orb_x")
    oy = S.cfg.get("orb_y")
    if ox is None or oy is None:
        ox, oy = scr.right() - ORB_WIN - 30, scr.top() + scr.height() // 3
    ox = max(scr.left(), min(int(ox), scr.right() - ORB_WIN))
    oy = max(scr.top(), min(int(oy), scr.bottom() - ORB_WIN))
    ORB.move(ox, oy)
    ORB.show()

    # first run: inject the SessionStart hook so the orb also launches with
    # Devin sessions (removable via the settings toggle).
    if S.cfg.get("devin_hook", True) and not config.devin_hook_installed():
        log(f"devin_hook install ok={config.install_devin_hook(os.path.join(BASE_DIR, 'app.py'))}")

    threading.Thread(target=refresh_loop, daemon=True).start()
    threading.Thread(target=theme_watch_loop, daemon=True).start()

    APP.exec()
    config.update(orb_x=ORB.x(), orb_y=ORB.y())


if __name__ == "__main__":
    main()
