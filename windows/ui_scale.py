"""Adaptive UI scaling.

The panels were designed for a 1200 x 900 window.  ``UiScale`` turns the main
window's size into one scale factor (1.0 at that reference size, clamped to
``MIN_FACTOR`` .. ``MAX_FACTOR``) and re-renders everything registered with it
when the factor changes: style sheets (every ``NNpx`` / ``NNpt`` length is
scaled), fonts, fixed and minimum widget sizes, layout margins and spacings,
the application font, and anything else through ``bind``.  The controls
therefore grow on a large screen and shrink on a small one instead of staying
one size.  Resizes are debounced, and a change of less than ``THRESHOLD``
(2 %) re-styles nothing.

The module-level helpers (``style``, ``font``, ``fixed_width``, ``px`` ...) work
on the one application-wide instance returned by ``instance()``, which the main
window attaches to itself.  Helpers may be used before that (they apply the
current factor, 1.0 until a window is attached) and every registration is
re-applied whenever the factor changes.  A registration never keeps its widget
alive: once the widget is deleted the registration silently drops out.

``ScalingCanvas`` does the same for a matplotlib figure: its DPI follows the
canvas size relative to a reference canvas size, so text, markers and line
widths grow and shrink with the plot while the layout fractions stay put.

The window is sized from its screen (``fit_to_screen``), never to a fixed
minimum: ``attach`` gives it a small explicit minimum so that it can shrink
freely and its content shrinks with it.  A dialog shown before the main window
exists (the startup check) takes the factor that window will get from
``screen_factor()``.

Usage::

    from windows import ui_scale

    ui_scale.instance().set_factor(ui_scale.screen_factor())   # before any window
    ui_scale.fit_to_screen(self)                   # in the main window
    self.ui_scale = ui_scale.instance()
    self.ui_scale.attach(self)

    ui_scale.style(label, "color: #39FF14; font: bold 12px Arial;")
    ui_scale.font(title, 16, bold=True)
    ui_scale.fixed_width(entry, 80)
    canvas = ui_scale.ScalingCanvas(fig, ref_size=(1100, 700))
"""

from __future__ import annotations

import logging
import math
import re
import weakref
from functools import partial

from PyQt6 import sip
from PyQt6.QtCore import QEvent, QObject, QPoint, QSize, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QGuiApplication, QResizeEvent

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg

log = logging.getLogger(__name__)

#: Window size (logical pixels) the panels were designed for: factor 1.0.
REFERENCE = (1200, 900)

#: The window-size factor never leaves this range.
MIN_FACTOR = 0.6
MAX_FACTOR = 2.5

#: Default explicit minimum size of the attached window (logical pixels):
#: small enough for an 800 x 480 display.  Below 720 x 540 the factor stays at
#: MIN_FACTOR, so panels lay out at that scale and their side panels scroll.
MIN_WINDOW = (640, 400)

#: A resize is applied once the window has kept its size for this long (ms).
DEBOUNCE_MS = 100

#: Relative factor change below which a resize re-styles nothing.
THRESHOLD = 0.02

# A CSS length in px or pt ("12px", "1.5px", ".5pt") that is not part of a
# word, a number or a colour.  A leading minus sign stays outside the match.
_LENGTH = re.compile(r"(?<![\w.#])(\d+(?:\.\d*)?|\.\d+)(px|pt)\b")

# A url(...) reference, whose file name is never scaled ("url(16px.png)").
_URL = re.compile(r"url\([^)]*\)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def clamp(value: float, low: float, high: float) -> float:
    """``value`` limited to ``low`` .. ``high``."""
    return max(low, min(high, value))


def scale_length(value: float, factor: float) -> int:
    """``value`` (pixels) times ``factor``, rounded half up.

    Zero stays zero and any other length keeps at least 1 pixel (with its
    sign), so a 1px border never disappears on a small window.
    """
    if value == 0:
        return 0
    sign = -1 if value < 0 else 1
    return sign * max(1, int(math.floor(abs(value) * factor + 0.5)))


def scale_css(text: str, factor: float) -> str:
    """``text`` (a Qt style sheet) with every px / pt length scaled by
    ``factor`` (but nothing inside a ``url(...)``)."""
    def repl(match: re.Match) -> str:
        return f"{scale_length(float(match.group(1)), factor)}{match.group(2)}"
    parts, start = [], 0
    for url in _URL.finditer(text):
        parts.append(_LENGTH.sub(repl, text[start:url.start()]))
        parts.append(url.group(0))
        start = url.end()
    parts.append(_LENGTH.sub(repl, text[start:]))
    return "".join(parts)


def factor_for_size(width: float, height: float, reference=REFERENCE,
                    low: float = MIN_FACTOR, high: float = MAX_FACTOR) -> float:
    """Scale factor of a ``width`` x ``height`` window: the smaller of the two
    ratios to ``reference``, so text never outgrows the short side."""
    return clamp(min(width / reference[0], height / reference[1]), low, high)


# ---------------------------------------------------------------------------
# Binding appliers (module level so a binding never holds its widget)
# ---------------------------------------------------------------------------

def _apply_style(template: str, widget, factor: float) -> None:
    widget.setStyleSheet(scale_css(template, factor))


def _apply_font(size_pt: float, bold: bool, family: str, italic: bool,
                widget, factor: float) -> None:
    qfont = QFont(family)
    qfont.setPointSizeF(max(1.0, size_pt * factor))
    qfont.setBold(bold)
    qfont.setItalic(italic)
    widget.setFont(qfont)


def _apply_call(method: str, lengths: tuple, obj, factor: float) -> None:
    getattr(obj, method)(*(scale_length(n, factor) for n in lengths))


# ---------------------------------------------------------------------------
# Registrations
# ---------------------------------------------------------------------------

class _Binding:
    """One registered object and its bindings ``{key: fn(obj, factor)}``.

    An object created from Python keeps the same wrapper for as long as its
    C++ side lives, so it is held weakly.  An object Qt created itself (a
    table's header view, say) gets a fresh wrapper every time Python asks for
    it and loses the old one as soon as Python lets go, so it is held strongly
    instead and dropped when Qt destroys it (its ``destroyed`` signal).
    """

    __slots__ = ("strong", "weak", "fns")

    def __init__(self, obj):
        if sip.ispycreated(obj):
            self.strong, self.weak = None, weakref.ref(obj)
        else:
            self.strong, self.weak = obj, None
            obj.destroyed.connect(partial(_forget, sip.unwrapinstance(obj)))
        self.fns: dict = {}

    def target(self):
        """The object, or None once it is gone."""
        obj = self.strong if self.strong is not None else self.weak()
        if obj is None or sip.isdeleted(obj):
            return None
        return obj


def _key(obj) -> int:
    """Registry key: the address of the C++ object (stable across wrappers)."""
    return sip.unwrapinstance(obj)


#: Every UiScale, so a destroyed object can be forgotten by all of them.
_scales: "weakref.WeakSet[UiScale]" = weakref.WeakSet()

#: The application font at factor 1.0, saved by the first ``attach``: one
#: per process, so an instance attached after another (or after another was
#: destroyed with its window) never scales an already scaled font.
_app_font_base: QFont | None = None


def _forget(key: int, *_args) -> None:
    for scale in list(_scales):
        scale._bindings.pop(key, None)


# ---------------------------------------------------------------------------
# Scale factor
# ---------------------------------------------------------------------------

class UiScale(QObject):
    """One UI scale factor, derived from a window's size, and the widgets
    re-rendered when it changes.

    ``changed(factor)`` is emitted after every registered binding has been
    re-applied with the new factor.
    """

    changed = pyqtSignal(float)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._factor = 1.0
        self._window_ref = None
        self._bindings: dict[int, _Binding] = {}     # C++ address -> registration
        self._scale_app_font = False                 # set by attach
        _scales.add(self)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(DEBOUNCE_MS)
        self._timer.timeout.connect(self.update_now)

    # ------------------------------------------------------------------
    # Factor
    # ------------------------------------------------------------------

    @property
    def factor(self) -> float:
        """Current scale factor (1.0 at the 1200 x 900 reference window)."""
        return self._factor

    def attached_window(self):
        """The attached window, or None."""
        window = self._window_ref() if self._window_ref is not None else None
        if window is None or sip.isdeleted(window):
            return None
        return window

    def attach(self, window, scale_app_font: bool = True, min_window=MIN_WINDOW) -> None:
        """Follow ``window``'s size and become the application-wide instance.

        Registrations made on a previous application-wide instance (for example
        by panels built before the main window attached) are taken over and
        re-applied at this instance's factor.  With ``scale_app_font`` the
        application font (used by every widget without a font of its own) is
        scaled as well, from its size before the first ``attach``; without it
        the application font is left at (or put back to) that size.  Only the
        application-wide instance touches the application font.  The factor is
        computed from the window's size straight away, so size the window first
        (``fit_to_screen``) to give the panels built next the right factor.

        ``min_window`` (logical pixels, capped to the screen) becomes the
        window's explicit minimum size unless it has one already; None leaves
        it alone.  Without an explicit minimum Qt pins a window to its
        content's minimum size at the current scale, so a window shrunk from a
        large size (un-maximised, say) would stop where the old, larger scale
        needs it instead of shrinking with its content, and content wider than
        the window would push the window, and with it the factor, up.
        """
        global _instance, _app_font_base
        previous = _instance
        stale = False                 # adopted registrations still at the previous factor
        if previous is not None and previous is not self and not sip.isdeleted(previous):
            stale = self._adopt(previous)
        _instance = self

        old = self.attached_window()
        if old is not None and old is not window:
            old.removeEventFilter(self)
        self._window_ref = weakref.ref(window)
        window.installEventFilter(self)
        if min_window is not None and window.minimumSize() == QSize(0, 0):
            w, h = int(min_window[0]), int(min_window[1])
            screen = window.screen() or QGuiApplication.primaryScreen()
            if screen is not None:
                area = screen.availableGeometry()
                w, h = min(w, area.width()), min(h, area.height())
            window.setMinimumSize(w, h)

        app = QGuiApplication.instance()
        if app is not None and _app_font_base is None:
            _app_font_base = QFont(app.font())
        self._scale_app_font = bool(scale_app_font)
        if not self._scale_app_font and app is not None and app.font() != _app_font_base:
            app.setFont(_app_font_base)         # back to its unscaled size
        self._apply_app_font()
        before = self._factor
        self.update_now()
        if stale and self._factor == before:     # no refresh happened: re-apply them now
            self.refresh()

    def update_now(self) -> None:
        """Recompute the factor from the attached window's size right away."""
        self._timer.stop()
        window = self.attached_window()
        if window is None:
            return
        size = window.size()
        if size.width() <= 0 or size.height() <= 0:
            return
        self._set_factor(factor_for_size(size.width(), size.height()), THRESHOLD)

    def set_factor(self, factor: float) -> None:
        """Force a factor (clamped); the next window resize recomputes it."""
        self._set_factor(factor, 0.0)

    def refresh(self) -> None:
        """Re-apply every registered binding with the current factor."""
        window = self.attached_window()
        frozen = window is not None and window.updatesEnabled()
        if frozen:
            window.setUpdatesEnabled(False)
        try:
            self._apply_app_font()
            for key, entry in list(self._bindings.items()):
                obj = entry.target()
                if obj is None:
                    self._bindings.pop(key, None)
                    continue
                for fn in list(entry.fns.values()):
                    if not self._call(key, obj, fn):
                        break
        finally:
            if frozen:
                window.setUpdatesEnabled(True)

    def _set_factor(self, factor: float, threshold: float) -> bool:
        factor = clamp(float(factor), MIN_FACTOR, MAX_FACTOR)
        if factor == self._factor or abs(factor - self._factor) <= threshold * self._factor:
            return False
        self._factor = factor
        self.refresh()
        self.changed.emit(factor)
        return True

    def _apply_app_font(self) -> None:
        """Scale the application font (the application-wide instance only)."""
        app = QGuiApplication.instance()
        base = _app_font_base
        if not self._scale_app_font or base is None or app is None or _instance is not self:
            return
        qfont = QFont(base)
        if base.pointSizeF() > 0:
            qfont.setPointSizeF(max(1.0, base.pointSizeF() * self._factor))
        elif base.pixelSize() > 0:
            qfont.setPixelSize(scale_length(base.pixelSize(), self._factor))
        app.setFont(qfont)

    # ------------------------------------------------------------------
    # Bindings
    # ------------------------------------------------------------------

    def bind(self, obj, fn, key=None) -> None:
        """Call ``fn(obj, factor)`` now and whenever the factor changes.

        ``obj`` (a QObject: widget, layout, header ...) is not kept alive by
        the binding: once it is deleted the binding is dropped silently.
        ``fn`` must not hold a reference to ``obj`` itself (it receives it).
        A new binding with the same ``key`` (default: ``fn`` itself) replaces
        the old one, so re-styling a widget does not stack templates.
        """
        if obj is None or sip.isdeleted(obj):
            return
        k = _key(obj)
        entry = self._bindings.get(k)
        if entry is None or entry.target() is None:     # new, or a dead object's address reused
            entry = _Binding(obj)
            self._bindings[k] = entry
        entry.fns[fn if key is None else key] = fn
        self._call(k, obj, fn)

    def unbind(self, obj, key=None) -> None:
        """Forget ``obj``'s binding ``key`` (all of them when ``key`` is None)."""
        if obj is None or sip.isdeleted(obj):
            return
        k = _key(obj)
        entry = self._bindings.get(k)
        if entry is None:
            return
        if key is None:
            self._bindings.pop(k, None)
        else:
            entry.fns.pop(key, None)

    def binding_count(self) -> int:
        """Number of live objects with at least one binding."""
        return sum(1 for entry in list(self._bindings.values()) if entry.target() is not None)

    def _call(self, k: int, obj, fn) -> bool:
        """Apply one binding; False (and the object dropped) if it is gone."""
        try:
            fn(obj, self._factor)
            return True
        except RuntimeError:                  # wrapped C++ object already deleted
            self._bindings.pop(k, None)
            return False
        except Exception:                     # never let one bad binding break a resize
            log.exception("UI scale binding failed for %r", obj)
            return True

    def _adopt(self, other: "UiScale") -> bool:
        """Take over ``other``'s registrations.  True if one was taken over
        that was applied at another factor than this instance's."""
        adopted = False
        for k, entry in list(other._bindings.items()):
            if entry.target() is None:
                continue
            adopted = True
            mine = self._bindings.get(k)
            if mine is None or mine.target() is None:
                self._bindings[k] = entry
            else:
                for key, fn in entry.fns.items():
                    mine.fns.setdefault(key, fn)
        other._bindings = {}
        return adopted and other._factor != self._factor

    # ------------------------------------------------------------------
    # Window events
    # ------------------------------------------------------------------

    def eventFilter(self, obj, event) -> bool:
        etype = event.type()
        if etype in (QEvent.Type.Resize, QEvent.Type.Show) and obj is self.attached_window():
            if etype == QEvent.Type.Resize and obj.isVisible():
                self._timer.start()           # wait until the user stops dragging
            else:
                self.update_now()             # first sizing: no flash of the wrong size
        return False


_instance: UiScale | None = None


def instance() -> UiScale:
    """The application-wide ``UiScale`` (created on first use)."""
    global _instance
    if _instance is None or sip.isdeleted(_instance):
        _instance = UiScale()
    return _instance


# ---------------------------------------------------------------------------
# Module-level helpers on the application-wide instance
# ---------------------------------------------------------------------------

def factor() -> float:
    """The current scale factor."""
    return instance().factor


def px(n: float) -> int:
    """``n`` design pixels at the current scale (non-zero stays >= 1).

    A snapshot: use it for a length needed at the moment (a pick radius, a
    pen width), and the registering helpers below for anything that must
    follow later changes of the factor.
    """
    return scale_length(n, instance().factor)


def css(text: str) -> str:
    """``text`` with every px / pt length scaled to the current factor."""
    return scale_css(text, instance().factor)


def bind(obj, fn, key=None) -> None:
    """``UiScale.bind`` on the application-wide instance."""
    instance().bind(obj, fn, key)


def style(widget, text: str) -> None:
    """Set ``widget``'s style sheet to ``text``, scaled now and on every change.

    Calling it again on the same widget replaces the template.
    """
    instance().bind(widget, partial(_apply_style, text), key="style")


def font(widget, size_pt: float, bold: bool = False, family: str = "Arial",
         italic: bool = False) -> None:
    """Give ``widget`` a ``size_pt`` font (at factor 1.0) that follows the scale."""
    instance().bind(widget, partial(_apply_font, float(size_pt), bool(bold), family, bool(italic)),
                    key="font")


def fixed_width(widget, n: float) -> None:
    """``widget.setFixedWidth`` in design pixels."""
    instance().bind(widget, partial(_apply_call, "setFixedWidth", (n,)), key="fixed_width")


def min_width(widget, n: float) -> None:
    """``widget.setMinimumWidth`` in design pixels."""
    instance().bind(widget, partial(_apply_call, "setMinimumWidth", (n,)), key="min_width")


def max_width(widget, n: float) -> None:
    """``widget.setMaximumWidth`` in design pixels."""
    instance().bind(widget, partial(_apply_call, "setMaximumWidth", (n,)), key="max_width")


def fixed_height(widget, n: float) -> None:
    """``widget.setFixedHeight`` in design pixels."""
    instance().bind(widget, partial(_apply_call, "setFixedHeight", (n,)), key="fixed_height")


def min_height(widget, n: float) -> None:
    """``widget.setMinimumHeight`` in design pixels."""
    instance().bind(widget, partial(_apply_call, "setMinimumHeight", (n,)), key="min_height")


def max_height(widget, n: float) -> None:
    """``widget.setMaximumHeight`` in design pixels."""
    instance().bind(widget, partial(_apply_call, "setMaximumHeight", (n,)), key="max_height")


def min_size(widget, w: float, h: float) -> None:
    """``widget.setMinimumSize`` in design pixels."""
    instance().bind(widget, partial(_apply_call, "setMinimumSize", (w, h)), key="min_size")


def margins(target, left: float, top: float, right: float, bottom: float) -> None:
    """``setContentsMargins`` of a layout or widget in design pixels."""
    instance().bind(target, partial(_apply_call, "setContentsMargins", (left, top, right, bottom)),
                    key="margins")


def spacing(layout, n: float) -> None:
    """``layout.setSpacing`` in design pixels."""
    instance().bind(layout, partial(_apply_call, "setSpacing", (n,)), key="spacing")


def call(obj, method: str, *lengths: float, key=None) -> None:
    """Call ``obj.<method>(*lengths)`` with the lengths in design pixels, now
    and on every change; e.g. ``call(table.verticalHeader(),
    "setDefaultSectionSize", 26)``.  ``key`` defaults to the method name."""
    instance().bind(obj, partial(_apply_call, method, tuple(lengths)),
                    key=method if key is None else key)


def _fitted_size(area, fraction: float, minimum) -> tuple[int, int]:
    """``fraction`` of the screen ``area`` (a QRect), never below ``minimum``
    unless the screen is smaller."""
    w = min(area.width(), max(int(area.width() * fraction), int(minimum[0])))
    h = min(area.height(), max(int(area.height() * fraction), int(minimum[1])))
    return w, h


def screen_factor(fraction: float = 0.9, screen=None) -> float:
    """The factor of a window that ``fit_to_screen`` sizes on ``screen``
    (default: the primary screen).

    For dialogs shown before the main window exists (the startup check), so
    they match the window that follows:
    ``ui_scale.instance().set_factor(ui_scale.screen_factor())``.
    """
    screen = screen or QGuiApplication.primaryScreen()
    if screen is None:
        return 1.0
    return factor_for_size(*_fitted_size(screen.availableGeometry(), fraction, MIN_WINDOW))


def fit_to_screen(window, fraction: float = 0.9, minimum=MIN_WINDOW) -> QSize:
    """Resize ``window`` to ``fraction`` of its screen's available area (never
    below ``minimum`` unless the screen is smaller) and centre it there.

    Returns the new size.  Call it before ``show()``.  A hidden window gets
    no resize event until it is shown, so the scale factor of a window that
    is already attached is brought up to date here.
    """
    screen = window.screen() if hasattr(window, "screen") else None
    screen = screen or QGuiApplication.primaryScreen()
    if screen is None:
        return window.size()
    area = screen.availableGeometry()
    w, h = _fitted_size(area, fraction, minimum)
    window.resize(w, h)
    window.move(area.center() - QPoint(w // 2, h // 2))
    scale = _instance
    if scale is not None and not sip.isdeleted(scale) and scale.attached_window() is window:
        scale.update_now()
    return QSize(w, h)


# ---------------------------------------------------------------------------
# Matplotlib canvas
# ---------------------------------------------------------------------------

class ScalingCanvas(FigureCanvasQTAgg):
    """A matplotlib canvas whose figure DPI follows the canvas size.

    At ``ref_size`` (logical pixels) the figure renders at its own DPI (the
    "base" DPI it was created with).  Elsewhere the DPI is the base DPI times
    ``scale_factor = clamp(min(w / ref_w, h / ref_h), min_factor, max_factor)``
    (in 1 % steps), so fonts, markers and line widths, which are all given in
    points, keep their size relative to the plot while subplot positions,
    which are figure fractions, do not move.

    High-DPI screens are handled the way matplotlib does it: the factor is
    computed from the logical size and the device pixel ratio multiplies the
    DPI on top (``figure._original_dpi`` holds the scaled logical DPI, which is
    what matplotlib multiplies by the ratio when the window moves to a screen
    with another ``devicePixelRatio``).

    ``scaled(factor)`` is emitted after a rescale, once the figure has its new
    DPI and size.  The size hint is ``ref_size``, not the figure's size.
    """

    scaled = pyqtSignal(float)

    def __init__(self, figure, ref_size=(1000, 700), min_factor: float = 0.5,
                 max_factor: float = 3.0):
        self._ref_size = (max(1, int(ref_size[0])), max(1, int(ref_size[1])))
        self._min_factor = float(min_factor)
        self._max_factor = max(float(max_factor), self._min_factor)
        self._scale_factor = 1.0
        # A Figure records its DPI as _original_dpi when it is created; use it
        # in case another canvas has already multiplied figure.dpi.
        self._base_dpi = float(getattr(figure, "_original_dpi", None) or figure.dpi)
        super().__init__(figure)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def scale_factor(self) -> float:
        """Current factor (1.0 at ``ref_size``)."""
        return self._scale_factor

    @property
    def ref_size(self) -> tuple[int, int]:
        """Canvas size (logical pixels) at which the factor is 1.0."""
        return self._ref_size

    def set_ref_size(self, width: int, height: int) -> None:
        """Change the reference size and rescale for the current size."""
        self._ref_size = (max(1, int(width)), max(1, int(height)))
        self._rescale(self.size())

    def factor_for(self, width: float, height: float) -> float:
        """The factor a ``width`` x ``height`` canvas gets (1 % steps)."""
        raw = min(width / self._ref_size[0], height / self._ref_size[1])
        return round(clamp(raw, self._min_factor, self._max_factor), 2)

    def sizeHint(self) -> QSize:
        return QSize(*self._ref_size)

    # ------------------------------------------------------------------
    # Qt events
    # ------------------------------------------------------------------

    def resizeEvent(self, event) -> None:
        if getattr(self, "_in_resize_event", False) or self.figure is None:
            super().resizeEvent(event)        # matplotlib guards the recursion itself
            return
        changed = self._set_factor_for(event.size())
        super().resizeEvent(event)
        self._cover_widget(event.size())
        if changed:
            self.scaled.emit(self._scale_factor)

    def _cover_widget(self, size: QSize) -> None:
        """matplotlib sizes the figure ``width / dpi`` inches, and ``width / dpi
        * dpi`` can come out a hair short (844.9999999999999), which the Agg
        renderer truncates: the canvas's last pixel column or row would never
        be painted.  Nudge the figure size up by the least amount that covers it."""
        figure = self.figure
        ratio = self._pixel_ratio()
        width, height = size.width() * ratio, size.height() * ratio
        if width <= 0 or height <= 0:
            return
        dpi = float(figure.dpi)
        w_in, h_in = (float(v) for v in figure.get_size_inches())
        if w_in * dpi >= width and h_in * dpi >= height:
            return
        while w_in * dpi < width:
            w_in = math.nextafter(w_in, math.inf)
        while h_in * dpi < height:
            h_in = math.nextafter(h_in, math.inf)
        figure.set_size_inches(w_in, h_in, forward=False)

    def _rescale(self, size: QSize) -> None:
        """Apply the factor for ``size`` now (outside a Qt resize)."""
        if self.figure is None:
            return
        if self._set_factor_for(size):
            # matplotlib recomputes the figure size in a resize event; this is
            # how it handles a device-pixel-ratio change too.
            self.resizeEvent(QResizeEvent(size, size))
            self.scaled.emit(self._scale_factor)

    def _set_factor_for(self, size: QSize) -> bool:
        if size.width() <= 1 or size.height() <= 1:    # hidden or collapsed: keep the last factor
            return False
        factor = self.factor_for(size.width(), size.height())
        if factor == self._scale_factor:
            return False
        self._scale_factor = factor
        logical = self._base_dpi * factor
        figure = self.figure
        figure._original_dpi = logical
        dpi = logical * self._pixel_ratio()
        set_dpi = getattr(figure, "_set_dpi", None)
        if set_dpi is not None:
            set_dpi(dpi, forward=False)
        else:                                             # not expected on matplotlib >= 3.5
            figure.dpi = dpi
        return True

    def _pixel_ratio(self) -> float:
        ratio = getattr(self, "device_pixel_ratio", None)
        if ratio is None:                                 # matplotlib < 3.5
            ratio = getattr(self, "_dpi_ratio", 1)
        return float(ratio or 1)
