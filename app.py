"""Devin Orb — floating usage ball for Devin (PySide6 edition).

A small always-on-top floating orb (glass clock ball + quota ring) that opens
a usage panel with a spring animation on click. Double-click (optional setting)
pins the panel on top so it stays visible.

Run:  python app.py      (or pythonw app.py for no console)
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import math
import os
import sys
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import config
import core
import notify

APP_DIR = config._config_dir()
LOG_PATH = os.path.join(APP_DIR, "orb.log")


def log(msg: str) -> None:
    try:
        # rotate once the log grows past ~256 KB: keep the tail
        if os.path.getsize(LOG_PATH) > 256 * 1024:
            with open(LOG_PATH, "rb") as f:
                f.seek(-128 * 1024, os.SEEK_END)
                tail = f.read().split(b"\n", 1)[-1]
            with open(LOG_PATH, "wb") as f:
                f.write(tail)
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

from PySide6.QtCore import (QEasingCurve, QObject, QPoint, QPointF, QRect,
                            QRectF, Qt, QTimer, QPropertyAnimation, Signal,
                            Property)
from PySide6.QtGui import (QColor, QConicalGradient, QGuiApplication, QIcon,
                           QIntValidator, QLinearGradient, QPainter, QPen,
                           QPixmap, QRadialGradient)
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel,
                               QLayout, QLineEdit, QMenu, QPushButton,
                               QSizePolicy, QSystemTrayIcon, QVBoxLayout,
                               QWidget)


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
    moved = Signal()                # position changed (panel follows)

    SNAP_PX = 60                    # release within this of an edge -> dock
    IDLE_MS = 2600                  # idle at edge before auto-retracting

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

        # edge docking: 'L' | 'R' | None; _docked_in = retracted half-circle
        self._dock_edge = None
        self._docked_in = False
        self._suppress_click = False
        self._bounce_t0 = 0.0        # notification bounce birth time
        self._slide = QPropertyAnimation(self, b"pos", self)
        self._slide.setDuration(280)
        self._slide.setEasingCurve(QEasingCurve.InOutCubic)
        self._slide.valueChanged.connect(lambda _v: self.moved.emit())
        self._idle = QTimer(self)
        self._idle.setSingleShot(True)
        self._idle.timeout.connect(self._retract)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(ANIM_MS)

    # -- edge docking -----------------------------------------------------------

    def _avail(self):
        c = self.geometry().center()
        scr = QGuiApplication.screenAt(c)
        if scr is None:
            # center sits outside every screen (edge-retracted orb, or a
            # resolution change left us off-screen) — fall back to the
            # NEAREST screen, not blindly to the primary one
            def _dist(s):
                a = s.availableGeometry()
                dx = max(a.left() - c.x(), 0, c.x() - a.right())
                dy = max(a.top() - c.y(), 0, c.y() - a.bottom())
                return dx * dx + dy * dy
            scr = min(QGuiApplication.screens(), key=_dist)
        return scr.availableGeometry()

    def ensure_on_screen(self):
        """Pull the orb back inside the nearest screen if fully outside."""
        g = self.geometry()
        if any(s.availableGeometry().intersects(g)
               for s in QGuiApplication.screens()):
            return True
        a = self._avail()
        self.move(int(min(max(g.x(), a.left()), a.right() - ORB_WIN + 1)),
                  int(min(max(g.y(), a.top()), a.bottom() - ORB_WIN + 1)))
        self.moved.emit()
        return False

    def on_screen_changed(self):
        """Resolution/monitor topology changed — recover & re-dock."""
        self.ensure_on_screen()
        self._dock_edge = self._detect_edge()
        if self._dock_edge:
            self._slide_to(self._in_x() if self._docked_in
                           else self._flush_x())
        self._arm_idle()

    def _detect_edge(self):
        """'L'/'R' if the window hugs a screen edge, else None."""
        a = self._avail()
        if abs(self.x() - a.left()) <= self.SNAP_PX:
            return 'L'
        if abs(self.x() + ORB_WIN - (a.right() + 1)) <= self.SNAP_PX:
            return 'R'
        return None

    def _flush_x(self):
        a = self._avail()
        return (a.right() - ORB_WIN + 1) if self._dock_edge == 'R' else a.left()

    def _in_x(self):
        """Center exactly on the edge -> only the inner half-circle shows."""
        a = self._avail()
        return (a.right() - ORB_WIN // 2 + 1) if self._dock_edge == 'R' \
            else a.left() - ORB_WIN // 2

    def _slide_to(self, x):
        self._slide.stop()
        self._slide.setStartValue(self.pos())
        self._slide.setEndValue(QPoint(int(x), self.y()))
        self._slide.start()

    def _arm_idle(self):
        if self._dock_edge and not self._docked_in \
                and self._drag_off is None:
            self._idle.start(self.IDLE_MS)

    def _retract(self):
        if not self._dock_edge or self._drag_off is not None:
            return
        self._docked_in = True
        self._slide_to(self._in_x())

    def _expand(self):
        self._idle.stop()
        if self._docked_in:
            self._docked_in = False
            self._slide_to(self._flush_x())

    def dock_check(self):
        """Startup: adopt docked state if the saved position hugs an edge."""
        self._dock_edge = self._detect_edge()
        self._arm_idle()

    def bounce(self):
        """Notification arrived — springy double-pop + pull out of the edge."""
        self._bounce_t0 = time.monotonic()
        self._expand()
        self._arm_idle()

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
        self._expand()              # hover pulls a retracted orb back out

    def leaveEvent(self, e):
        self._hover_t = 0.0
        self._press_t = 0.0
        self._arm_idle()            # re-arm the retract timer

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press_t = 1.0
            if self._docked_in:
                # first contact just pulls the orb out; it doesn't open
                self._suppress_click = True
                self._expand()
            self._idle.stop()
            self._drag_off = e.globalPosition().toPoint() - self.pos()
            self._moved = False

    def mouseMoveEvent(self, e):
        if self._drag_off is None:
            # window may have slid under a stationary cursor during retract —
            # no enterEvent fires then, so treat any move as contact too
            if self._docked_in:
                self._expand()
            return
        gp = e.globalPosition().toPoint()
        if not self._moved and (gp - self.pos() - self._drag_off).manhattanLength() > 4:
            self._moved = True
            self._press_t = 0.0
        if self._moved:
            self.move(gp - self._drag_off)
            self.moved.emit()       # panel tracks the orb live

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        self._press_t = 0.0
        was_drag = self._moved
        self._drag_off = None
        self._moved = False
        if was_drag:
            self._dock_edge = self._detect_edge()
            if self._dock_edge:
                self._slide_to(self._flush_x())
                self._arm_idle()
                config.update(orb_x=self._flush_x(), orb_y=self.y())
            else:
                config.update(orb_x=self.x(), orb_y=self.y())
        elif not self._click_guard and not self._suppress_click:
            self._ripples.append(time.monotonic())
            # guard so a double-click doesn't fire open+pin
            QTimer.singleShot(QApplication.doubleClickInterval(),
                              self._emit_open)
        self._suppress_click = False
        self._arm_idle()

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
        self._expand()
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
        dy = 0.0
        bt = time.monotonic() - self._bounce_t0
        if bt < 1.3:
            # decaying spring: scale pop + little hop
            k = math.exp(-3.4 * bt)
            scale *= 1.0 + 0.16 * k * math.sin(bt * 15)
            dy = -7.0 * k * abs(math.sin(bt * 8))

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
        p.translate(c, c + dy)
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

        # Cognition-minimal face: only the four cardinal ticks
        pen = QPen(col["tick"], 1.2)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        for i in (0, 3, 6, 9):
            a = math.radians(i * 30)
            r2 = fr - 2.4
            r1 = r2 - 3.0
            p.drawLine(QPointF(r1 * math.sin(a), -r1 * math.cos(a)),
                       QPointF(r2 * math.sin(a), -r2 * math.cos(a)))

        now = time.localtime()
        sec = time.time() % 60
        minute = now.tm_min + sec / 60
        hour = (now.tm_hour % 12) + minute / 60

        # hour hand — short, confident
        pen = QPen(col["ink"], 2.4, Qt.SolidLine, Qt.RoundCap)
        p.setPen(pen)
        a = math.radians(hour * 30)
        p.drawLine(QPointF(0, 0), QPointF(8.5 * math.sin(a), -8.5 * math.cos(a)))
        # minute hand — thin and long
        pen = QPen(col["ink"], 1.6, Qt.SolidLine, Qt.RoundCap)
        p.setPen(pen)
        a = math.radians(minute * 6)
        p.drawLine(QPointF(0, 0), QPointF(13.0 * math.sin(a), -13.0 * math.cos(a)))
        # second hand — hairline sweep, barely-there tail
        pen = QPen(col["accent"], 0.9, Qt.SolidLine, Qt.RoundCap)
        p.setPen(pen)
        a = math.radians(sec * 6)
        p.drawLine(QPointF(-3.5 * math.sin(a), 3.5 * math.cos(a)),
                   QPointF(15.0 * math.sin(a), -15.0 * math.cos(a)))
        # hub — small pin
        p.setPen(Qt.NoPen)
        p.setBrush(col["accent"])
        p.drawEllipse(QPointF(0, 0), 1.8, 1.8)
        p.setBrush(col["ink"])
        p.drawEllipse(QPointF(0, 0), 0.7, 0.7)

        p.restore()

        # ---- quota ring --------------------------------------------------------
        cy = c + dy
        ring_pen = QPen(col["track"], 2.6)
        ring_pen.setCapStyle(Qt.RoundCap)
        p.setPen(ring_pen)
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(c, cy), RING_R, RING_R)

        if self._pct is not None:
            pct = max(0.0, min(100.0, self._pct))
            arc_color = (col["stale"] if self._stale else
                         col["accent"] if pct >= 50 else
                         col["warn"] if pct >= 20 else col["bad"])
            if self._stale:
                arc_color.setAlpha(int(140 + 100 * (0.5 + 0.5 * math.sin(t * 4))))
            con = QConicalGradient(QPointF(c, cy), 90)
            con.setColorAt(0.0, col["accent_hot"] if not self._stale else arc_color)
            con.setColorAt(min(pct / 100.0, 0.999), arc_color)
            con.setColorAt(min(pct / 100.0 + 0.001, 1.0), arc_color)
            pen = QPen(con if not self._stale else arc_color, 2.6)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            rect = QRectF(c - RING_R, cy - RING_R, RING_R * 2, RING_R * 2)
            p.drawArc(rect, 90 * 16, int(-pct * 3.6 * 16))

        # error dot
        if self._stale:
            pulse = 0.5 + 0.5 * math.sin(t * 4.4)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(248, 113, 113, int(160 + 90 * pulse)))
            p.drawEllipse(QPointF(c + RING_R * 0.72, cy - RING_R * 0.72), 4.2, 4.2)

        # click ripples
        for t0 in self._ripples:
            age = time.monotonic() - t0
            k = age / 0.55
            rp = QRadialGradient(QPointF(c, cy), BALL_R + k * 26)
            alpha = int(120 * (1 - k))
            rp.setColorAt(0.0, QColor(0, 0, 0, 0))
            rp.setColorAt(0.85, QColor(col["accent"].red(), col["accent"].green(),
                                       col["accent"].blue(), alpha))
            rp.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.setBrush(rp)
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(c, cy), BALL_R + k * 26, BALL_R + k * 26)

        p.end()


# --- the panel (native Qt widgets — no WebEngine) -----------------------------

PAD = 9                    # transparent margin around the card (shadow room)
PANEL_W = 372 + 2 * PAD    # total window width
APP = None
ORB = None
PANEL = None

I18N = {
    "zh": dict(app="Devin", daily="每日配额", weekly="每周配额",
               balance="额外余额", left="剩余", used="已用",
               updated="更新于", ago="前",
               sPin="双击悬浮球 → 面板置顶", sRing="配额环显示",
               sSrc="配额环数据源", sTheme="主题", sRefresh="自动刷新",
               sLogin="开机自启", sDevin="随 Devin 启动",
               quit="退出悬浮球", on="开", off="关",
               tOpen="打开面板", tSettings="设置", tQuit="退出",
               auto="自动", dailyS="每日", weeklyS="每周",
               follow="跟随OU", light="浅", dark="深",
               custom="自定义", customLbl="自定义间隔", ok="确定",
               customPh="分钟",
               errAuth="未登录 Devin — 请先登录 Devin Desktop",
               errNet="获取数据失败，点 ⟳ 重试",
               periodFmt=lambda a, b: f"计费周期 {a} → {b}"),
    "en": dict(app="Devin", daily="Daily quota", weekly="Weekly quota",
               balance="Extra balance", left="left", used="used",
               updated="Updated", ago="ago",
               sPin="Double-click orb pins panel", sRing="Ring shows",
               sSrc="Ring source", sTheme="Theme", sRefresh="Auto refresh",
               sLogin="Start on login", sDevin="Launch with Devin",
               quit="Quit orb", on="On", off="Off",
               tOpen="Open panel", tSettings="Settings", tQuit="Quit",
               auto="Auto", dailyS="Daily", weeklyS="Weekly",
               follow="OU", light="Light", dark="Dark",
               custom="Custom", customLbl="Custom interval", ok="OK",
               customPh="min",
               errAuth="Not signed in to Devin — sign in to Devin Desktop first",
               errNet="Failed to fetch. Press ⟳ to retry",
               periodFmt=lambda a, b: f"Billing cycle {a} → {b}"),
}

# Devin / Cognition monochrome palette
PAL = {
    "dark": dict(
        cardbg="qlineargradient(x1:0,y1:0,x2:1,y2:1, "
               "stop:0 rgba(28,28,33,238), stop:1 rgba(16,16,19,243))",
        border="rgba(255,255,255,33)", text="#f2f2f4", dim="#9ba0a8",
        track=QColor(255, 255, 255, 26), btn="rgba(255,255,255,20)",
        btnhov="rgba(255,255,255,41)", accent="#fafafa",
        fill=("#e4e4e7", "#fafafa")),
    "light": dict(
        cardbg="qlineargradient(x1:0,y1:0,x2:1,y2:1, "
               "stop:0 rgba(255,255,255,240), stop:1 rgba(243,245,249,243))",
        border="rgba(15,23,42,26)", text="#1f2430", dim="#6b7280",
        track=QColor(32, 36, 46, 26), btn="rgba(32,36,46,18)",
        btnhov="rgba(32,36,46,36)", accent="#0a0a0a",
        fill=("#3f3f46", "#0a0a0a")),
}

BAR_LEVELS = {
    "warn": ("#f59e0b", "#fbbf24"),
    "bad": ("#ef4444", "#f87171"),
}

CARD_QSS = """
#card {{ background: {cardbg}; border:1px solid {border}; border-radius:18px; }}
QLabel {{ color:{text}; background:transparent; font-family:"Segoe UI","Microsoft YaHei"; }}
QLabel#dim  {{ color:{dim}; font-size:12.5px; }}
QLabel#tiny {{ color:{dim}; font-size:10.5px; }}
QLabel#tinyr{{ color:{dim}; font-size:10.5px; }}
QLabel#title{{ font-size:17px; font-weight:700; }}
QLabel#badge{{ font-size:10px; font-weight:700; color:{dim}; padding:2px 7px;
    border-radius:6px; background:{btn}; border:1px solid {border}; }}
QLabel#qval {{ font-size:12.5px; font-weight:600; }}
QLabel#amt  {{ font-size:24px; font-weight:800; color:{accent}; }}
QLabel#err  {{ color:#f87171; font-size:11px; }}
QPushButton#icon {{ min-width:26px;max-width:26px;min-height:26px;max-height:26px;
    border-radius:8px; border:none; background:{btn}; color:{dim}; font-size:13px; }}
QPushButton#icon:hover {{ background:{btnhov}; color:{text}; }}
QPushButton#icon:checked {{ color:{accent}; background:{btnhov}; }}
QFrame#balance {{ background:{btn}; border:1px solid {border}; border-radius:12px; }}
QFrame#segbox {{ background:{trackqss}; border-radius:8px; }}
QPushButton#seg {{ border:none; background:transparent; color:{dim};
    font-size:11px; padding:4px 9px; border-radius:6px; }}
QPushButton#seg:checked {{ background:{btnhov}; color:{text}; font-weight:600; }}
QWidget#setrow {{ border-top:1px solid {border}; }}
QPushButton#quit {{ margin-top:8px; padding:7px; border-radius:9px;
    border:1px solid {border}; background:{btn}; color:{dim}; font-size:12px; }}
QPushButton#quit:hover {{ color:#f87171; background:rgba(248,113,113,31); }}
QLineEdit#edit {{ background:{btn}; border:1px solid {border}; border-radius:7px;
    padding:4px 8px; color:{text}; font-size:12px; }}
QLineEdit#edit:focus {{ border:1px solid {dim}; }}
QPushButton#okbtn {{ background:{btnhov}; color:{text}; border:none;
    border-radius:7px; padding:5px 12px; font-size:11px; font-weight:600; }}
"""


def fmt_reset(unix, lang):
    if not unix:
        return ""
    s = max(0, int(unix - time.time()))
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    rel = f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{m}m"
    return f"{rel} 后重置" if lang == "zh" else f"Resets in {rel}"


def fmt_updated(ts, lang):
    if not ts:
        return ""
    s = max(0, int(time.time() - ts))
    rel = f"{s}s" if s < 60 else f"{s // 60}m"
    i = I18N[lang]
    return f"{i['updated']} {rel} {i['ago']}" if lang == "zh" \
        else f"{i['updated']} {rel} {i['ago']}"


def fmt_date(s):
    return (s or "")[:10] or "—"


class QuotaBar(QWidget):
    """Animated rounded progress bar; fills toward remaining/used percent."""

    def __init__(self):
        super().__init__()
        self._pct = 0.0
        self.level = "ok"
        self.pal = PAL["dark"]
        self.setFixedHeight(10)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._anim = QPropertyAnimation(self, b"pct", self)
        self._anim.setDuration(650)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)

    def _get(self):
        return self._pct

    def _set(self, v):
        self._pct = v
        self.update()

    pct = Property(float, _get, _set)

    def show_pct(self, v, level):
        self.level = level
        self._anim.stop()
        self._anim.setStartValue(self._pct)
        self._anim.setEndValue(float(v))
        self._anim.start()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        h = self.height()
        r = QRectF(0, 0.5, self.width(), h - 1)
        p.setPen(Qt.NoPen)
        p.setBrush(self.pal["track"])
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        if self._pct > 0:
            w = max(r.height(), r.width() * self._pct / 100.0)
            fr = QRectF(0, 0.5, w, r.height())
            c1, c2 = BAR_LEVELS.get(self.level, self.pal["fill"])
            g = QLinearGradient(fr.topLeft(), fr.topRight())
            g.setColorAt(0, QColor(c1))
            g.setColorAt(1, QColor(c2))
            p.setBrush(g)
            p.drawRoundedRect(fr, fr.height() / 2, fr.height() / 2)
        p.end()


class SegControl(QFrame):
    """Segmented switch; emits the chosen value string."""

    valueChanged = Signal(str)

    def __init__(self, options):
        super().__init__(objectName="segbox")
        self.opts = options               # [(value, i18n-key)]
        self.btns = {}
        h = QHBoxLayout(self)
        h.setContentsMargins(2, 2, 2, 2)
        h.setSpacing(2)
        for v, _k in options:
            b = QPushButton("", objectName="seg")
            b.setCheckable(True)
            b.setAutoExclusive(True)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _c=False, vv=v: self.valueChanged.emit(vv))
            self.btns[v] = b
            h.addWidget(b)

    def set_labels(self, lang):
        for v, k in self.opts:
            self.btns[v].setText(I18N[lang].get(k, k))

    def set_value(self, v):
        b = self.btns.get(str(v))
        if b:
            b.setChecked(True)


class QuotaRow(QWidget):
    def __init__(self):
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(5)
        top = QHBoxLayout()
        self.name = QLabel("", objectName="dim")
        self.val = QLabel("", objectName="qval")
        top.addWidget(self.name)
        top.addStretch(1)
        top.addWidget(self.val)
        self.bar = QuotaBar()
        self.reset = QLabel("", objectName="tinyr")
        self.reset.setAlignment(Qt.AlignRight)
        v.addLayout(top)
        v.addWidget(self.bar)
        v.addWidget(self.reset)

    def show_data(self, left, reset_unix, used_mode, lang):
        if left is None:
            self.hide()
            return
        self.show()
        shown = 100 - left if used_mode else left
        shown = max(0.0, min(100.0, shown))
        i = I18N[lang]
        self.val.setText(f"{shown:.0f}% {i['used' if used_mode else 'left']}")
        lvl = "ok" if shown >= 50 else "warn" if shown >= 20 else "bad"
        self.bar.show_pct(shown, lvl)
        self.reset.setText(fmt_reset(reset_unix, lang))


class IconBtn(QPushButton):
    def __init__(self, text):
        super().__init__(text, objectName="icon")
        self.setCursor(Qt.PointingHandCursor)


class PanelWindow(QWidget):
    """Native glass card panel. Height animates when the settings drawer opens,
    so it never clips or overflows the screen."""

    def __init__(self):
        super().__init__(None,
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)

        self._theme_name = ""
        self._state = {}
        self._set_open = False
        self._closing = False
        self._side = 1.0                 # which side the orb is on

        outer = QVBoxLayout(self)
        outer.setContentsMargins(PAD, PAD, PAD, PAD)
        outer.setSpacing(0)
        # window always hugs the card's real content height — the settings
        # drawer grows/shrinks the whole window, never clips off-screen.
        outer.setSizeConstraint(QLayout.SetFixedSize)

        self.card = QFrame(objectName="card")
        self.card.setFixedWidth(PANEL_W - 2 * PAD)
        outer.addWidget(self.card)
        lay = QVBoxLayout(self.card)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.setSpacing(0)

        # header
        head = QHBoxLayout()
        head.setSpacing(6)
        self.lbl_title = QLabel("Devin", objectName="title")
        self.badge = QLabel("", objectName="badge")
        self.badge.hide()
        head.addWidget(self.lbl_title)
        head.addWidget(self.badge)
        head.addStretch(1)
        self.b_lang = IconBtn("文")
        self.b_pin = IconBtn("📌")
        self.b_pin.setCheckable(True)
        self.b_ref = IconBtn("⟳")
        self.b_gear = IconBtn("⚙")
        self.b_gear.setCheckable(True)
        for b in (self.b_lang, self.b_pin, self.b_ref, self.b_gear):
            head.addWidget(b)
        lay.addLayout(head)
        lay.addSpacing(8)

        self.q_daily = QuotaRow()
        self.q_weekly = QuotaRow()
        lay.addWidget(self.q_daily)
        lay.addSpacing(10)
        lay.addWidget(self.q_weekly)
        lay.addSpacing(8)

        bal = QFrame(objectName="balance")
        bh = QHBoxLayout(bal)
        bh.setContentsMargins(14, 10, 14, 10)
        self.lbl_bal = QLabel("", objectName="dim")
        self.amt = QLabel("—", objectName="amt")
        bh.addWidget(self.lbl_bal)
        bh.addStretch(1)
        bh.addWidget(self.amt)
        lay.addWidget(bal)
        lay.addSpacing(4)

        meta = QHBoxLayout()
        self.period = QLabel("", objectName="tiny")
        self.updated = QLabel("", objectName="tiny")
        meta.addWidget(self.period)
        meta.addStretch(1)
        meta.addWidget(self.updated)
        lay.addLayout(meta)
        self.err = QLabel("", objectName="err")
        self.err.hide()
        lay.addWidget(self.err)

        # settings drawer
        self.settings = QWidget()
        sl = QVBoxLayout(self.settings)
        sl.setContentsMargins(2, 8, 2, 0)
        sl.setSpacing(0)
        self._rows = []

        def seg_row(text_key, setting_key, options, first=False,
                    handler=None):
            row = QWidget(objectName="" if first else "setrow")
            h = QHBoxLayout(row)
            h.setContentsMargins(2, 7, 2, 7)
            lbl = QLabel("", objectName="dim")
            lbl.setStyleSheet("font-size:12px")
            seg = SegControl(options)
            seg.valueChanged.connect(
                handler if handler else
                (lambda v, k=setting_key: apply_setting(k, v)))
            h.addWidget(lbl)
            h.addStretch(1)
            h.addWidget(seg)
            sl.addWidget(row)
            self._rows.append((text_key, lbl, seg))
            return seg

        self.seg_dbl = seg_row("sPin", "dblclick_pin",
                               [("true", "on"), ("false", "off")], first=True)
        self.seg_ring = seg_row("sRing", "ring_mode",
                                [("left", "left"), ("used", "used")])
        self.seg_src = seg_row("sSrc", "ring_source",
                               [("auto", "auto"), ("daily", "dailyS"),
                                ("weekly", "weeklyS")])
        self.seg_theme = seg_row("sTheme", "theme",
                                 [("follow", "follow"), ("light", "light"),
                                  ("dark", "dark")])
        self.seg_ref = seg_row("sRefresh", "refresh_secs",
                               [("300", "5m"), ("600", "10m"),
                                ("3000", "50m"), ("custom", "custom")],
                               handler=self._on_refresh_seg)

        # inline custom-interval editor (minutes), hidden until 自定义 is on
        self.custom_row = QWidget(objectName="setrow")
        ch = QHBoxLayout(self.custom_row)
        ch.setContentsMargins(2, 7, 2, 7)
        self.custom_lbl = QLabel("", objectName="dim")
        self.custom_lbl.setStyleSheet("font-size:12px")
        self.custom_edit = QLineEdit(objectName="edit")
        self.custom_edit.setFixedWidth(72)
        self.custom_edit.setValidator(QIntValidator(1, 1440, self))
        self.custom_ok = QPushButton("", objectName="okbtn")
        self.custom_ok.setCursor(Qt.PointingHandCursor)
        self.custom_ok.clicked.connect(self._apply_custom_refresh)
        self.custom_edit.returnPressed.connect(self._apply_custom_refresh)
        ch.addWidget(self.custom_lbl)
        ch.addStretch(1)
        ch.addWidget(self.custom_edit)
        ch.addSpacing(4)
        ch.addWidget(self.custom_ok)
        self.custom_row.hide()
        sl.addWidget(self.custom_row)
        self.seg_login = seg_row("sLogin", "start_on_login",
                                 [("true", "on"), ("false", "off")])
        self.seg_devin = seg_row("sDevin", "devin_hook",
                                 [("true", "on"), ("false", "off")])

        self.b_quit = QPushButton("", objectName="quit")
        self.b_quit.setCursor(Qt.PointingHandCursor)
        sl.addWidget(self.b_quit)
        self.settings.setVisible(False)   # collapsed: contributes zero height
        lay.addWidget(self.settings)

        # signals
        self.b_lang.clicked.connect(
            lambda: apply_setting("language",
                                  "en" if S.cfg.get("language") == "zh" else "zh"))
        self.b_pin.clicked.connect(self._on_pin)
        self.b_ref.clicked.connect(
            lambda: threading.Thread(
                target=lambda: (fetch(force=True), BUS.poke.emit()),
                daemon=True).start())
        self.b_gear.clicked.connect(self.toggle_settings)
        self.b_quit.clicked.connect(QApplication.quit)

        # animations
        self._anim_pop = QPropertyAnimation(self, b"pos", self)
        self._anim_pop.finished.connect(self._on_pop_finished)
        self._anim_move = QPropertyAnimation(self, b"pos", self)
        self._anim_set = QPropertyAnimation(self.settings, b"maximumHeight", self)
        self._anim_set.finished.connect(self._on_set_finished)
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setEasingCurve(QEasingCurve.OutCubic)

        # countdown label refresh
        self._tick = QTimer(self)
        self._tick.timeout.connect(
            lambda: self.apply_state(self._state) if self.isVisible() else None)
        self._tick.start(30000)

        self.adjustSize()

    def _on_refresh_seg(self, v):
        if v == "custom":
            # toggle the inline editor; the seg button stays lit
            self.custom_row.setVisible(not self.custom_row.isVisible())
            if self.custom_row.isVisible():
                self.custom_edit.setFocus()
        else:
            self.custom_row.hide()
            apply_setting("refresh_secs", v)

    def _apply_custom_refresh(self):
        try:
            mins = int(self.custom_edit.text())
        except ValueError:
            return
        mins = max(1, min(1440, mins))
        S.cfg = config.update(refresh_secs=mins * 60)
        self.custom_row.hide()
        push_orb()
        push_panel()

    # -- theme / state -----------------------------------------------------------

    def apply_theme(self):
        p = PAL[self._theme_name]
        qss = CARD_QSS.format(
            cardbg=p["cardbg"], border=p["border"], text=p["text"],
            dim=p["dim"], btn=p["btn"], btnhov=p["btnhov"],
            accent=p["accent"],
            trackqss=f"rgba({p['track'].red()},{p['track'].green()},"
                     f"{p['track'].blue()},{p['track'].alpha()})")
        self.card.setStyleSheet(qss)
        self.q_daily.bar.pal = p
        self.q_weekly.bar.pal = p
        self.update()

    def apply_state(self, s):
        if not s:
            return
        self._state = s
        lang = s.get("lang", "zh")
        i = I18N[lang]
        if theme() != self._theme_name:
            self._theme_name = theme()
            self.apply_theme()
        self.b_pin.setChecked(bool(s.get("pinned")))
        plan = s.get("plan") or ""
        self.badge.setText(plan)
        self.badge.setVisible(bool(plan))
        # labels / i18n
        self.lbl_title.setText(i["app"])
        self.q_daily.name.setText(i["daily"])
        self.q_weekly.name.setText(i["weekly"])
        self.lbl_bal.setText(i["balance"])
        self.b_quit.setText(i["quit"])
        for tk, lbl, seg in self._rows:
            lbl.setText(i[tk])
            seg.set_labels(lang)
        # settings values
        st = s.get("settings", {})
        self.seg_dbl.set_value("true" if st.get("dblclick_pin") else "false")
        rm = st.get("ring_mode", "follow")
        self.seg_ring.set_value(rm if rm in ("left", "used") else "left")
        self.seg_src.set_value(st.get("ring_source", "auto"))
        self.seg_theme.set_value(st.get("theme", "follow"))
        rf = st.get("refresh_secs", 300)
        is_custom = str(rf) not in ("300", "600", "3000")
        self.seg_ref.set_value("custom" if is_custom else str(rf))
        self.custom_lbl.setText(i["customLbl"])
        self.custom_edit.setPlaceholderText(i["customPh"])
        self.custom_ok.setText(i["ok"])
        if is_custom and not self.custom_edit.hasFocus():
            self.custom_edit.setText(str(max(1, round(rf / 60))))
        self.seg_login.set_value("true" if st.get("start_on_login") else "false")
        self.seg_devin.set_value("true" if st.get("devin_hook") else "false")
        # data
        err = s.get("error")
        self.err.setVisible(bool(err))
        if err:
            self.err.setText(i["errAuth"] if err == "auth" else i["errNet"])
        used = s.get("ringShown") == "used"
        self.q_daily.show_data(s.get("daily_left"), s.get("daily_reset"), used, lang)
        self.q_weekly.show_data(s.get("weekly_left"), s.get("weekly_reset"), used, lang)
        ov = s.get("overage_usd")
        self.amt.setText("—" if ov is None else f"${ov:.2f}")
        self.period.setText(i["periodFmt"](fmt_date(s.get("period_start")),
                                           fmt_date(s.get("period_end"))))
        self.updated.setText(fmt_updated(s.get("fetched_at"), lang))

    def _settings_height(self):
        """Natural height of the settings drawer contents."""
        h = self.settings.layout().totalSizeHint().height()
        return h if h > 0 else 250

    # -- interactions ------------------------------------------------------------

    def _on_pin(self):
        S.panel_pinned = self.b_pin.isChecked()
        config.update(panel_pinned=S.panel_pinned)

    def toggle_settings(self):
        self.set_settings_open(not self._set_open)

    def set_settings_open(self, open_, animate=True):
        if open_ == self._set_open:
            return
        self._set_open = open_
        self.b_gear.setChecked(open_)
        if self._anim_pop.state() == QPropertyAnimation.Running:
            animate = False
        h_set = self._settings_height()
        g = self.geometry()
        scr = QGuiApplication.screenAt(g.center()) \
            or QGuiApplication.primaryScreen()
        a = scr.availableGeometry()
        if open_:
            # grow downward; shift up if the bottom would leave the screen
            ny = g.y()
            if g.y() + g.height() + h_set > a.bottom() - 8:
                ny = max(a.top() + 8,
                         a.bottom() - 8 - g.height() - h_set)
            self.settings.setMaximumHeight(0)
            self.settings.setVisible(True)
            if animate and self.isVisible():
                self._anim_set.stop()
                self._anim_set.setDuration(300)
                self._anim_set.setEasingCurve(QEasingCurve.InOutCubic)
                self._anim_set.setStartValue(0)
                self._anim_set.setEndValue(h_set)
                self._anim_set.start()
                if ny != g.y():
                    self._anim_move.stop()
                    self._anim_move.setDuration(300)
                    self._anim_move.setEasingCurve(QEasingCurve.InOutCubic)
                    self._anim_move.setStartValue(g.topLeft())
                    self._anim_move.setEndValue(QPoint(g.x(), ny))
                    self._anim_move.start()
            else:
                self.settings.setMaximumHeight(16777215)
                if ny != g.y():
                    self.move(g.x(), ny)
        else:
            # collapse AND re-anchor vertically, so the compact card hugs
            # the orb again instead of staying where the tall panel ended
            cur = self.settings.height() if self.settings.isVisible() else 0
            h_small = g.height() - cur
            oc = ORB.geometry().center()
            scr = QGuiApplication.screenAt(oc) \
                or QGuiApplication.primaryScreen()
            a = scr.availableGeometry()
            py = oc.y() - h_small / 2
            py = int(max(a.top() + 8, min(py, a.bottom() - h_small - 8)))
            if animate and self.isVisible():
                self._anim_set.stop()
                self._anim_set.setDuration(260)
                self._anim_set.setEasingCurve(QEasingCurve.InOutCubic)
                self._anim_set.setStartValue(self.settings.height())
                self._anim_set.setEndValue(0)
                self._anim_set.start()
                if py != g.y():
                    self._anim_move.stop()
                    self._anim_move.setDuration(260)
                    self._anim_move.setEasingCurve(QEasingCurve.InOutCubic)
                    self._anim_move.setStartValue(g.topLeft())
                    self._anim_move.setEndValue(QPoint(g.x(), py))
                    self._anim_move.start()
            else:
                self.settings.setVisible(False)
                if py != g.y():
                    self.move(g.x(), py)

    def _on_set_finished(self):
        if self._set_open:
            # let the drawer auto-adapt to its content after the animation
            self.settings.setMaximumHeight(16777215)
        elif self.settings.maximumHeight() == 0:
            self.settings.setVisible(False)

    def pop_show(self, target: QRect, side: float):
        """Slide in from the orb's direction + fade, with a soft overshoot."""
        self._side = side
        self._closing = False
        self._anim_move.stop()
        self.resize(target.size())
        start = QPoint(target.x() + int(30 * (1 if side >= 1.0 else -1)),
                       target.y() + 10)
        self.move(start)
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()
        self.activateWindow()
        self._anim_pop.stop()
        self._anim_pop.setDuration(380)
        self._anim_pop.setEasingCurve(QEasingCurve.OutBack)
        self._anim_pop.setStartValue(start)
        self._anim_pop.setEndValue(target.topLeft())
        self._anim_pop.start()
        self._fade.stop()
        self._fade.setDuration(150)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()

    def pop_hide(self):
        if self._closing:
            return
        self._closing = True
        self._anim_move.stop()
        self._anim_set.stop()
        g = self.geometry()
        end = QPoint(g.x() + int(24 * (1 if self._side >= 1.0 else -1)),
                     g.y() + 6)
        self._anim_pop.stop()
        self._anim_pop.setDuration(150)
        self._anim_pop.setEasingCurve(QEasingCurve.InCubic)
        self._anim_pop.setStartValue(g.topLeft())
        self._anim_pop.setEndValue(end)
        self._anim_pop.start()
        self._fade.stop()
        self._fade.setDuration(140)
        self._fade.setStartValue(1.0)
        self._fade.setEndValue(0.0)
        self._fade.start()
        QTimer.singleShot(210, self._finish_hide)

    def _on_pop_finished(self):
        if self._closing:
            self._finish_hide()

    def _finish_hide(self):
        if not self._closing:
            return
        self._closing = False
        self.hide()
        self.setWindowOpacity(1.0)

    # -- window events -------------------------------------------------------------

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            S.panel_pinned = False
            config.update(panel_pinned=False)
            hide_panel()
            return
        super().keyPressEvent(e)

    def event(self, e):
        if e.type() == e.Type.WindowDeactivate and self.isVisible():
            if not S.panel_pinned and time.time() - S.panel_shown_at > 0.6:
                hide_panel(from_blur=True)
        return super().event(e)


# --- notification bubble -------------------------------------------------------

BUB = None
BUB_W = 268
BUB_TAIL = 9


def raise_devin_window():
    """Bring the Devin Desktop main window to the foreground."""
    try:
        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        found = []
        CMP = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p,
                                 ctypes.c_void_p)

        def _cb(hwnd, _):
            if not u32.IsWindowVisible(hwnd):
                return True
            pid = wt.DWORD(0)
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            h = k32.OpenProcess(0x1000, False, pid.value)
            if not h:
                return True
            buf = ctypes.create_unicode_buffer(260)
            n = wt.DWORD(260)
            ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n))
            k32.CloseHandle(h)
            if ok and buf.value.rsplit(chr(92), 1)[-1].lower() == "devin.exe":
                r = wt.RECT()
                u32.GetWindowRect(hwnd, ctypes.byref(r))
                if r.right - r.left > 200 and r.bottom - r.top > 200:
                    found.append((hwnd, (r.right - r.left) *
                                 (r.bottom - r.top)))
            return True

        u32.EnumWindows(CMP(_cb), None)
        if not found:
            return
        hwnd = max(found, key=lambda x: x[1])[0]
        if u32.IsIconic(hwnd):
            u32.ShowWindow(hwnd, 9)  # SW_RESTORE
        # foreground lock workaround: join input queues so Windows lets us
        # steal foreground, then detach
        cur_tid = k32.GetCurrentThreadId()
        fg = u32.GetForegroundWindow()
        fg_tid = u32.GetWindowThreadProcessId(fg, None) if fg else 0
        attached = False
        if fg_tid and fg_tid != cur_tid:
            attached = bool(u32.AttachThreadInput(cur_tid, fg_tid, True))
        u32.BringWindowToTop(hwnd)
        u32.SetForegroundWindow(hwnd)
        # SwitchToThisWindow ignores the foreground lock entirely
        u32.SwitchToThisWindow(hwnd, True)
        if attached:
            u32.AttachThreadInput(cur_tid, fg_tid, False)
    except Exception:
        pass


class NotifyBubble(QWidget):
    """Small speech-bubble that pops above the orb when Devin sends a toast."""

    LIFE_MS = 9000

    def __init__(self):
        super().__init__(None,
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedWidth(BUB_W)
        self.setCursor(Qt.PointingHandCursor)
        self._tail_x = BUB_W / 2
        self._tail_top = False        # tail on top (bubble shown under orb)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, BUB_TAIL)
        outer.setSpacing(0)
        outer.setSizeConstraint(QLayout.SetFixedSize)

        self.card = QFrame(objectName="card")
        lay = QVBoxLayout(self.card)
        lay.setContentsMargins(13, 10, 13, 10)
        lay.setSpacing(3)
        self.title = QLabel("", objectName="btitle")
        self.body = QLabel("", objectName="bbody")
        self.body.setWordWrap(True)
        lay.addWidget(self.title)
        lay.addWidget(self.body)
        outer.addWidget(self.card)

        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._slide = QPropertyAnimation(self, b"pos", self)
        self._life = QTimer(self)
        self._life.setSingleShot(True)
        self._life.timeout.connect(self.dismiss)
        self.adjustSize()

    def apply_theme(self, dark):
        p = PAL["dark" if dark else "light"]
        self.card.setStyleSheet(CARD_QSS.format(
            cardbg=p["cardbg"], border=p["border"], text=p["text"],
            dim=p["dim"], btn=p["btn"], btnhov=p["btnhov"],
            accent=p["accent"],
            trackqss=f"rgba({p['track'].red()},{p['track'].green()},"
                     f"{p['track'].blue()},{p['track'].alpha()})") +
            "\nQLabel#btitle { font-size:12.5px; font-weight:700; }"
            "\nQLabel#bbody  { font-size:11.5px; color:" + p["dim"] + "; }")
        self._tailc = QColor(22, 22, 26, 242) if dark \
            else QColor(246, 248, 251, 242)
        self.update()

    def show_notify(self, title, body):
        self.title.setText(title or "Devin")
        body = (body or "").strip()
        if len(body) > 160:
            body = body[:160].rstrip() + "…"
        self.body.setText(body)
        self.apply_theme(theme() == "dark")
        self.adjustSize()

        # anchor: bubble bottom-center sits above the orb's top-center
        og = ORB.geometry()
        oc = og.center()
        scr = QGuiApplication.screenAt(oc) or QGuiApplication.primaryScreen()
        a = scr.availableGeometry()
        w, h = self.width(), self.height()
        bx = int(oc.x() - w / 2)
        bx = max(a.left() + 8, min(bx, a.right() - w - 8))
        self._tail_x = min(max(oc.x() - bx, 24), w - 24)
        by = og.top() - h - 6
        self._tail_top = False
        if by < a.top() + 6:            # no room above -> under the orb
            by = og.bottom() + 6
            self._tail_top = True
        lay = self.layout()
        lay.setContentsMargins(0, BUB_TAIL if self._tail_top else 0,
                               0, 0 if self._tail_top else BUB_TAIL)
        self.adjustSize()
        h = self.height()
        if self._tail_top:
            by = og.bottom() + 6

        # entrance: fade + slide out of the orb
        start_y = by + (10 if not self._tail_top else -10)
        self.move(bx, start_y)
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()
        self._slide.stop()
        self._slide.setDuration(260)
        self._slide.setEasingCurve(QEasingCurve.OutBack)
        self._slide.setStartValue(QPoint(bx, start_y))
        self._slide.setEndValue(QPoint(bx, by))
        self._slide.start()
        self._fade.stop()
        self._fade.setDuration(180)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()
        self._life.start(self.LIFE_MS)

    def dismiss(self):
        if not self.isVisible():
            return
        self._life.stop()
        self._fade.stop()
        self._fade.setDuration(160)
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(0.0)
        self._fade.start()
        QTimer.singleShot(180, self.hide)

    def mousePressEvent(self, e):
        self.dismiss()
        threading.Thread(target=raise_devin_window, daemon=True).start()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w = self.width()
        tx = self._tail_x
        p.setPen(Qt.NoPen)
        p.setBrush(getattr(self, "_tailc", QColor(22, 22, 26, 242)))
        if self._tail_top:
            p.drawPolygon([QPointF(tx - 7, BUB_TAIL + 1), QPointF(tx + 7, BUB_TAIL + 1),
                           QPointF(tx, 0)])
        else:
            y0 = self.height() - BUB_TAIL - 1
            p.drawPolygon([QPointF(tx - 7, y0), QPointF(tx + 7, y0),
                           QPointF(tx, self.height())])
        p.end()


def on_devin_notify(title, body):
    """GUI-thread entry: bounce the orb and pop the bubble."""
    ORB.bounce()
    BUB.show_notify(title, body)


def panel_anchor_position() -> tuple:
    """Panel x,y anchored beside the orb; returns (px, py, side)."""
    oc = ORB.geometry().center()
    scr = QGuiApplication.screenAt(oc) or QGuiApplication.primaryScreen()
    a = scr.availableGeometry()
    gap = 10
    w, h = PANEL.width(), PANEL.height()
    if oc.x() > a.center().x():
        px = ORB.x() - w - gap
        side = 1.0
    else:
        px = ORB.x() + ORB_WIN + gap
        side = 0.0
    py = oc.y() - h / 2
    py = max(a.top() + 8, min(py, a.bottom() - h - 8))
    px = max(a.left() + 8, min(px, a.right() - w - 8))
    return int(px), int(py), side


def show_panel(pinned=None, open_settings=False):
    if pinned is not None:
        S.panel_pinned = pinned
        config.update(panel_pinned=pinned)
    px, py, side = panel_anchor_position()
    S.panel_shown_at = time.time()
    PANEL.pop_show(QRect(px, py, PANEL_W, PANEL.height()), side)
    if open_settings:
        PANEL.set_settings_open(True, animate=False)
    push_panel()


def hide_panel(animated=True, from_blur=False):
    if not PANEL.isVisible():
        return
    if from_blur:
        S.last_blur_hide = time.time()
    if animated:
        PANEL.pop_hide()
    else:
        PANEL.hide()


def toggle_panel():
    if PANEL.isVisible():
        S.panel_pinned = False
        config.update(panel_pinned=False)
        hide_panel()
    else:
        ORB.ensure_on_screen()   # orb may have been left off-screen
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
    show_panel(open_settings=True)


def on_orb_moved():
    """Panel stays glued to the orb while it drags or slides."""
    if PANEL and PANEL.isVisible() and not PANEL._closing:
        px, py, side = panel_anchor_position()
        PANEL._side = side
        PANEL.move(px, py)


def apply_setting(key, value):
    if key not in ("dblclick_pin", "ring_mode", "ring_source", "theme",
                   "start_on_login", "devin_hook", "refresh_secs", "language"):
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
    if key == "language" and TRAY:
        _rebuild_tray_menu()
    push_orb()
    push_panel()


# --- pushes ------------------------------------------------------------------

def push_orb():
    if not ORB:
        return
    ORB.set_theme(theme())
    ORB.set_ring(S.ring_percent_left(), ring_mode(), S.last_error is not None)


def push_panel():
    if PANEL:
        PANEL.apply_state(state_payload())


# --- background loops ----------------------------------------------------------

class Bus(QObject):
    """Thread-safe wake-up into the GUI thread."""
    poke = Signal()
    notify = Signal(str, str)


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


# --- system tray ----------------------------------------------------------------

def _tray_icon() -> QIcon:
    """Paint the orb face as the tray icon (no file dependency)."""
    sz = 64
    pm = QPixmap(sz, sz)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    m = sz * 0.06
    # obsidian ball
    g = QRadialGradient(sz * 0.35, sz * 0.3, sz * 0.65)
    g.setColorAt(0, QColor(60, 60, 68))
    g.setColorAt(1, QColor(10, 10, 12))
    p.setPen(Qt.NoPen)
    p.setBrush(g)
    p.drawEllipse(QRectF(m, m, sz - 2 * m, sz - 2 * m))
    # quota ring
    p.setPen(QPen(QColor(52, 211, 153), sz * 0.05))
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(QRectF(m * 1.6, m * 1.6, sz - 3.2 * m, sz - 3.2 * m))
    # white hands at 10:10
    cx = cy = sz / 2
    p.setPen(QPen(Qt.white, sz * 0.045, Qt.SolidLine, Qt.RoundCap))
    p.drawLine(QPointF(cx, cy), QPointF(cx - sz * 0.12, cy - sz * 0.11))
    p.drawLine(QPointF(cx, cy), QPointF(cx + sz * 0.02, cy - sz * 0.23))
    p.end()
    return QIcon(pm)


def _rebuild_tray_menu():
    i = I18N[S.cfg.get("language", "zh")]
    m = QMenu()
    m.setStyleSheet(
        "QMenu { background:rgba(24,24,28,245); color:#e8e8ec;"
        " border:1px solid rgba(255,255,255,30); border-radius:10px;"
        " padding:6px; }"
        "QMenu::item { padding:7px 26px 7px 16px; border-radius:6px; }"
        "QMenu::item:selected { background:rgba(255,255,255,18); }"
        "QMenu::separator { height:1px; margin:5px 10px;"
        " background:rgba(255,255,255,20); }")
    m.addAction(i["tOpen"], lambda: toggle_panel())
    m.addAction(i["tSettings"], lambda: show_panel(open_settings=True))
    m.addSeparator()
    m.addAction(i["tQuit"], QApplication.quit)
    TRAY.setContextMenu(m)


def on_tray_activated(reason):
    if reason == QSystemTrayIcon.Trigger:
        toggle_panel()


# --- main -----------------------------------------------------------------------

def main():
    global APP, ORB, PANEL, BUB, TRAY
    if not ensure_single_instance():
        log("another instance running; exiting")
        return

    QApplication.setApplicationName("Devin Orb")
    APP = QApplication(sys.argv)
    APP.setQuitOnLastWindowClosed(False)

    ORB = OrbWindow()
    PANEL = PanelWindow()
    BUB = NotifyBubble()

    # system tray icon: left click toggles the panel, right click menu
    TRAY = QSystemTrayIcon(_tray_icon())
    TRAY.setToolTip("Devin Orb")
    TRAY.activated.connect(on_tray_activated)
    _rebuild_tray_menu()
    TRAY.show()

    ORB.openRequested.connect(on_orb_click)
    ORB.pinRequested.connect(on_orb_dblclick)
    ORB.contextRequested.connect(on_orb_context)
    ORB.moved.connect(on_orb_moved)
    BUS.notify.connect(lambda t, b: on_devin_notify(t, b))
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
    ORB.dock_check()

    # display topology changes (resolution switches, monitor (un)plugs,
    # fullscreen games): pull the orb back on-screen and re-dock
    def _watch_screen(s):
        s.geometryChanged.connect(
            lambda *_: ORB.on_screen_changed())
    for s in QGuiApplication.screens():
        _watch_screen(s)
    APP.screenAdded.connect(_watch_screen)
    APP.screenRemoved.connect(
        lambda *_: ORB.on_screen_changed())

    # first run: inject the SessionStart hook so the orb also launches with
    # Devin sessions (removable via the settings toggle).
    if S.cfg.get("devin_hook", True):
        # install_devin_hook is idempotent and rewrites the stored command
        # when the launcher changes (source vs packaged exe)
        log(f"devin_hook install ok={config.install_devin_hook(os.path.join(BASE_DIR, 'app.py'))}")

    threading.Thread(target=refresh_loop, daemon=True).start()
    threading.Thread(target=theme_watch_loop, daemon=True).start()
    notify.start_listener(lambda t, b: BUS.notify.emit(t, b), log)

    APP.exec()
    config.update(orb_x=ORB.x(), orb_y=ORB.y())


if __name__ == "__main__":
    main()
