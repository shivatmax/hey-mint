"""The battery, for the notch: a big battery view for the open notch and a small badge for a wing.

    view (about 220-300 x 150)                       badge (a wing, about 64 x 22)
    ┌───────────────────────────────────────┐
    │   ┌──────────────┐╮     74%             │         74% [▮▮▮▯⚡]
    │   │▮▮▮▮▮▮▮▮▮  ⚡ │╯     Charging         │
    │   └──────────────┘      1:05 until full   │
    └───────────────────────────────────────┘

The look follows boring.notch's BatteryView (GPL-3.0, like Mint): a thin white outline at half
opacity, a rounded fill in the state's colour - green while charging or plugged in (or full), red at
20% and below on battery, yellow in Low Power Mode, white otherwise - and a bolt (charging) or a plug
(plugged in, not charging) over it. Re-implemented here with Core Animation.

The API the notch uses (notch.py, another session's file):

    view, update = view(width, height)    # the open notch's battery tab, clear background
    badge = compact_badge(height)         # "74%" + a small battery, for a wing
    available()                           # this Mac has a battery
    unsubscribe = charging_changed(fn)    # fn(info) on the main thread when a charger connects/leaves;
                                          # info["event"] = "Plugged In", "Unplugged", "Full charge"...
    info = status()                       # the newest reading (dict below), never blocks

Readings come from IOKit's power sources (IOPSCopyPowerSourcesInfo through ctypes; `pmset -g batt`
if that fails) on a background thread every 2 s, so a charger is noticed within about 2 s. Views and
badges repaint themselves (main thread, once a second, only when the reading changed); update()
repaints at once. Build views on the main thread.

info = {present, percent, charging, plugged, charged, low_power, time_to_full (minutes or None),
        time_to_empty (minutes or None), calculating, source ("AC Power" / "Battery Power")}
"""

from __future__ import annotations

import ctypes
import logging
import re
import subprocess
import threading
import time

import AppKit
import objc
import Quartz
from PyObjCTools import AppHelper

from mint.ui import gfx

log = logging.getLogger("mint.ui.notch_battery")

POLL = 2.0                 # seconds between readings
INK = (1.0, 1.0, 1.0)
DIM = (0.65, 0.65, 0.65)   # boring.notch's Color(white: 0.65)
GREEN = (0.20, 0.84, 0.29)  # the system green
RED = (1.00, 0.27, 0.23)
YELLOW = (1.00, 0.80, 0.0)
SETTINGS_URL = "x-apple.systempreferences:com.apple.Battery-Settings.extension"


def _cg(rgb, alpha=1.0):
    return Quartz.CGColorCreateGenericRGB(rgb[0], rgb[1], rgb[2], alpha)


def _ns(rgb, alpha=1.0):
    return AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(rgb[0], rgb[1], rgb[2], alpha)


def _font(size, weight=AppKit.NSFontWeightRegular, rounded=False, digits=False):
    font = (AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(size, weight) if digits
            else AppKit.NSFont.systemFontOfSize_weight_(size, weight))
    if rounded:
        descriptor = font.fontDescriptor().fontDescriptorWithDesign_(AppKit.NSFontDescriptorSystemDesignRounded)
        if descriptor is not None:
            font = AppKit.NSFont.fontWithDescriptor_size_(descriptor, size) or font
    return font


def _label(size, weight=AppKit.NSFontWeightRegular, rgb=INK, alpha=1.0, rounded=False, digits=False):
    field = AppKit.NSTextField.labelWithString_("")
    field.setFont_(_font(size, weight, rounded, digits))
    field.setTextColor_(_ns(rgb, alpha))
    field.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
    return field


# --- reading the battery (any thread) -------------------------------------------------------------

_EMPTY = {"present": False, "percent": 0, "charging": False, "plugged": False, "charged": False,
          "low_power": False, "time_to_full": None, "time_to_empty": None, "calculating": False,
          "source": ""}

_iokit: list = []


def _lib():
    if not _iokit:
        iokit = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/IOKit.framework/IOKit")
        cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        iokit.IOPSCopyPowerSourcesInfo.restype = ctypes.c_void_p
        iokit.IOPSCopyPowerSourcesInfo.argtypes = []
        iokit.IOPSCopyPowerSourcesList.restype = ctypes.c_void_p
        iokit.IOPSCopyPowerSourcesList.argtypes = [ctypes.c_void_p]
        iokit.IOPSGetPowerSourceDescription.restype = ctypes.c_void_p
        iokit.IOPSGetPowerSourceDescription.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        cf.CFRelease.restype = None
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        _iokit.extend([iokit, cf])
    return _iokit


def _low_power() -> bool:
    try:
        return bool(AppKit.NSProcessInfo.processInfo().isLowPowerModeEnabled())
    except Exception:
        return False


def _minutes(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _read_iokit() -> dict | None:
    iokit, cf = _lib()
    blob = iokit.IOPSCopyPowerSourcesInfo()
    if not blob:
        return None
    listed = iokit.IOPSCopyPowerSourcesList(blob)
    try:
        if not listed:
            return dict(_EMPTY)
        for source in objc.objc_object(c_void_p=listed):
            described = iokit.IOPSGetPowerSourceDescription(blob, objc.pyobjc_id(source))
            if not described:
                continue
            d = dict(objc.objc_object(c_void_p=described))
            if d.get("Type") != "InternalBattery" or not d.get("Is Present", True):
                continue
            top = float(d.get("Max Capacity") or 100) or 100.0
            percent = int(round(float(d.get("Current Capacity") or 0) * 100.0 / top))
            plugged = d.get("Power Source State") == "AC Power"
            charging = bool(d.get("Is Charging"))
            to_full, to_empty = d.get("Time to Full Charge"), d.get("Time to Empty")
            return {"present": True, "percent": max(0, min(100, percent)), "charging": charging,
                    "plugged": plugged, "charged": bool(d.get("Is Charged")) or (plugged and percent >= 100),
                    "low_power": bool(d.get("LPM Active")) or _low_power(),
                    "time_to_full": _minutes(to_full) if charging else None,
                    "time_to_empty": _minutes(to_empty) if not plugged else None,
                    "calculating": (charging and to_full == -1) or (not plugged and to_empty == -1),
                    "source": str(d.get("Power Source State") or "")}
        return dict(_EMPTY)
    finally:
        if listed:
            cf.CFRelease(listed)
        cf.CFRelease(blob)


def _read_pmset() -> dict:
    """`pmset -g batt`: "Now drawing from 'AC Power'  -InternalBattery-0 (id=…) 74%; charging; 1:05 remaining"."""
    out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5).stdout
    info = dict(_EMPTY)
    match = re.search(r"InternalBattery.*?(\d+)%;\s*([^;]+);\s*([^\n]*)", out)
    if not match:
        return info
    state, rest = match.group(2).strip().lower(), match.group(3)
    clock = re.search(r"(\d+):(\d+) remaining", rest)
    minutes = int(clock.group(1)) * 60 + int(clock.group(2)) if clock else None
    plugged = "'AC Power'" in out
    charging = state == "charging"
    info.update(present=True, percent=int(match.group(1)), charging=charging, plugged=plugged,
                charged=state in ("charged", "finishing charge") or (plugged and int(match.group(1)) >= 100),
                low_power=_low_power(), source="AC Power" if plugged else "Battery Power",
                time_to_full=minutes if charging else None, time_to_empty=minutes if not plugged else None,
                calculating="no estimate" in rest)
    return info


def read() -> dict:
    """A fresh reading (any thread, a millisecond or so)."""
    try:
        info = _read_iokit()
        if info is not None:
            return info
    except Exception:
        log.debug("IOKit power sources", exc_info=True)
    try:
        return _read_pmset()
    except Exception:
        log.debug("pmset", exc_info=True)
        return dict(_EMPTY)


# --- the watcher (a background thread) and its listeners -------------------------------------------

_lock = threading.Lock()
_now: dict = {}
_listeners: list = []
_thread: list = []


def status() -> dict:
    """The newest reading; the first call reads once (quick), later calls never block."""
    with _lock:
        if _now:
            return dict(_now)
    info = read()
    with _lock:
        if not _now:
            _now.update(info)
    return dict(info)


def _power_key(info: dict) -> tuple:
    return (bool(info.get("plugged")), bool(info.get("charging")), bool(info.get("charged")),
            bool(info.get("low_power")))


def event_text(before: dict, after: dict) -> str:
    """What changed, in boring.notch's words: "Plugged In", "Unplugged", "Charging battery", "Full charge",
    "Not charging", "Low Power: On" / "Low Power: Off" ("" when nothing a peek is for)."""
    if bool(before.get("low_power")) != bool(after.get("low_power")):
        return "Low Power: On" if after.get("low_power") else "Low Power: Off"
    if bool(before.get("plugged")) != bool(after.get("plugged")):
        if not after.get("plugged"):
            return "Unplugged"
        return "Charging battery" if after.get("charging") else "Plugged In"
    if bool(before.get("charging")) != bool(after.get("charging")):
        if after.get("charging"):
            return "Charging battery"
        return "Full charge" if after.get("charged") or after.get("percent", 0) >= 100 else "Not charging"
    if after.get("charged") and not before.get("charged"):
        return "Full charge"
    return ""


def _watch() -> None:
    last = None
    while True:
        info = read()
        with _lock:
            _now.clear()
            _now.update(info)
        if last is not None and _power_key(info) != _power_key(last) and _listeners:
            text = event_text(last, info)
            if text:
                AppHelper.callAfter(_tell, dict(info, event=text))
        last = info
        time.sleep(POLL)


def _tell(info: dict) -> None:
    for fn in list(_listeners):
        try:
            fn(info)
        except Exception:
            log.exception("charging_changed listener")


def start() -> None:
    """Start the watcher thread (once; any thread)."""
    if _thread:
        return
    worker = threading.Thread(target=_watch, daemon=True, name="notch-battery")
    _thread.append(worker)
    worker.start()


def charging_changed(callback):
    """callback(info) on the main thread when a charger is connected or removed, charging starts or stops,
    the battery becomes full, or Low Power Mode flips. info is status() plus "event", boring.notch's
    words for it ("Plugged In", "Unplugged", "Charging battery", "Full charge", "Not charging",
    "Low Power: On"/"Low Power: Off"): the notch shows it in a wing for about 3 s, next to
    compact_badge(). Noticed within POLL (2 s). Returns a function that unsubscribes."""
    start()
    _listeners.append(callback)

    def unsubscribe():
        if callback in _listeners:
            _listeners.remove(callback)
    return unsubscribe


_has_battery: list = []


def available() -> bool:
    """This Mac has a battery (asked once)."""
    if not _has_battery:
        _has_battery.append(bool(status().get("present")))
    return _has_battery[0]


def fill_rgb(info: dict) -> tuple:
    """boring.notch's batteryColor."""
    if info.get("low_power"):
        return YELLOW
    if info.get("percent", 0) <= 20 and not info.get("charging") and not info.get("plugged"):
        return RED
    if info.get("charging") or info.get("plugged") or info.get("percent", 0) >= 100:
        return GREEN
    return INK


def _hm(minutes) -> str:
    minutes = int(minutes or 0)
    return f"{minutes // 60}:{minutes % 60:02d}"


def describe(info: dict) -> tuple[str, str]:
    """(state line, time line): ("Charging", "1:05 until full")."""
    if not info.get("present"):
        return "No battery", ""
    if info.get("charging"):
        state = "Charging"
        when = (f"{_hm(info['time_to_full'])} until full" if info.get("time_to_full")
                else "Calculating…" if info.get("calculating") else "")
    elif info.get("plugged"):
        state = "Fully charged" if info.get("charged") else "Plugged in"
        when = "Power adapter" if info.get("charged") else "Charging on hold"
    else:
        state = "Low battery" if info.get("percent", 0) <= 20 else "On battery"
        when = (f"{_hm(info['time_to_empty'])} remaining" if info.get("time_to_empty")
                else "Calculating…" if info.get("calculating") else "")
    return state, when


# --- the battery glyph -------------------------------------------------------------------------------

class _NotchBatGlyph(AppKit.NSView):
    """A battery drawn with layers: outline + cap at half white, a rounded fill, a bolt or plug on top."""

    def initWithFrame_(self, frame):
        self = objc.super(_NotchBatGlyph, self).initWithFrame_(frame)
        if self is None:
            return None
        self.setWantsLayer_(True)
        w, h = frame.size.width, frame.size.height
        cap_w = max(2.0, round(w * 0.06))
        body_w = w - cap_w - max(1.0, w * 0.02)
        line = max(1.0, round(h * 0.07 * 2) / 2)
        self._body_w, self._inset = body_w, line + max(1.0, round(h * 0.07))
        root = self.layer()
        self.body = Quartz.CALayer.layer()
        self.body.setFrame_(Quartz.CGRectMake(0, 0, body_w, h))
        self.body.setCornerRadius_(h * 0.3)
        self.body.setBorderWidth_(line)
        self.body.setBorderColor_(_cg(INK, 0.5))
        root.addSublayer_(self.body)
        self.cap = Quartz.CALayer.layer()
        self.cap.setFrame_(Quartz.CGRectMake(w - cap_w, h * 0.32, cap_w, h * 0.36))
        self.cap.setCornerRadius_(cap_w / 2)
        self.cap.setMaskedCorners_(Quartz.kCALayerMaxXMinYCorner | Quartz.kCALayerMaxXMaxYCorner)
        self.cap.setBackgroundColor_(_cg(INK, 0.5))
        root.addSublayer_(self.cap)
        self.fill = Quartz.CALayer.layer()
        self.fill.setCornerRadius_(max(1.5, h * 0.3 - self._inset))
        root.addSublayer_(self.fill)
        size = h * 0.72
        self.sign = AppKit.NSImageView.alloc().initWithFrame_(
            AppKit.NSMakeRect((body_w - size) / 2, (h - size) / 2, size, size))
        self.sign.setImageScaling_(AppKit.NSImageScaleProportionallyUpOrDown)
        self.sign.setContentTintColor_(_ns(INK))
        shadow = AppKit.NSShadow.alloc().init()
        shadow.setShadowBlurRadius_(max(1.5, h * 0.08))
        shadow.setShadowOffset_(AppKit.NSMakeSize(0, 0))
        shadow.setShadowColor_(_ns((0, 0, 0), 0.85))
        self.sign.setShadow_(shadow)
        self.addSubview_(self.sign)
        self._key = None
        return self

    def isFlipped(self):
        return False

    @objc.python_method
    def paint(self, info: dict) -> None:
        percent = int(info.get("percent") or 0)
        sign = "bolt.fill" if info.get("charging") else "powerplug.fill" if info.get("plugged") else ""
        key = (percent, sign, fill_rgb(info))
        if key == self._key:
            return
        self._key = key
        h, inset = self.bounds().size.height, self._inset
        inner_w = self._body_w - 2 * inset
        width = max(min(inner_w, inner_w * percent / 100.0), min(inner_w, h * 0.18)) if percent else 0
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setAnimationDuration_(0.35)
        self.fill.setFrame_(Quartz.CGRectMake(inset, inset, width, h - 2 * inset))
        self.fill.setBackgroundColor_(_cg(key[2]))
        Quartz.CATransaction.commit()
        if sign:
            self.sign.setImage_(gfx.symbol(sign, round(h * 0.52), "bold"))
        self.sign.setHidden_(not sign)


# --- the notch's big view ----------------------------------------------------------------------------

class _NotchBatClick(AppKit.NSView):
    """The whole view: a click opens Battery settings."""

    def acceptsFirstMouse_(self, event):
        return True

    def mouseDown_(self, event):
        pass

    def mouseUp_(self, event):
        AppKit.NSWorkspace.sharedWorkspace().openURL_(AppKit.NSURL.URLWithString_(SETTINGS_URL))

    def resetCursorRects(self):
        self.addCursorRect_cursor_(self.bounds(), AppKit.NSCursor.pointingHandCursor())


class BatteryView:
    """Big glyph + percent + state + time, centred in width x height (side by side from about 220 wide,
    stacked when narrower)."""

    def __init__(self, width: float, height: float) -> None:
        self.w, self.h = float(width), float(height)
        self.in_window = False
        self._sig = None
        view = _NotchBatClick.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, self.w, self.h))
        view.setWantsLayer_(True)
        view.layer().setMasksToBounds_(True)
        view.setAppearance_(AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameDarkAqua))
        view.setToolTip_("Battery settings")
        self.view = view
        self._build()

    def _build(self) -> None:
        w, h = self.w, self.h
        self.wide = w >= 200 and w >= h * 1.4
        if self.wide:
            glyph_w = min(96.0, w * 0.36, h * 0.66)
        else:
            glyph_w = min(84.0, w * 0.6, h * 0.42)
        glyph_h = round(glyph_w * 0.46)
        self.glyph = _NotchBatGlyph.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, glyph_w, glyph_h))
        self.view.addSubview_(self.glyph)
        big = 30 if self.wide else max(18, min(26, h * 0.2))
        self.percent = _label(big, AppKit.NSFontWeightSemibold, rounded=True, digits=True)
        self.state = _label(13 if self.wide else 12, AppKit.NSFontWeightSemibold)
        self.when = _label(11, AppKit.NSFontWeightRegular, DIM, digits=True)
        self.lpm = _label(10, AppKit.NSFontWeightMedium, YELLOW)
        self.lpm.setStringValue_("Low Power Mode")
        for field in (self.percent, self.state, self.when, self.lpm):
            self.view.addSubview_(field)
            if not self.wide:
                field.setAlignment_(AppKit.NSTextAlignmentCenter)

    def _layout(self, lines: list) -> None:
        """Centre the glyph and the text block (text: [(field, height)])."""
        w, h = self.w, self.h
        gw, gh = self.glyph.frame().size.width, self.glyph.frame().size.height
        text_h = sum(lh for _, lh in lines)
        if self.wide:
            text_w = max([f.intrinsicContentSize().width for f, _ in lines] + [60])
            text_w = min(text_w + 4, w - gw - 18 - 16)
            gap = 18
            x0 = (w - (gw + gap + text_w)) / 2
            self.glyph.setFrameOrigin_(AppKit.NSMakePoint(round(x0), round((h - gh) / 2)))
            y = (h + text_h) / 2
            for field, lh in lines:
                y -= lh
                field.setFrame_(AppKit.NSMakeRect(round(x0 + gw + gap), round(y), text_w, lh))
        else:
            gap = 10
            total = gh + gap + text_h
            top = (h + total) / 2
            self.glyph.setFrameOrigin_(AppKit.NSMakePoint(round((w - gw) / 2), round(top - gh)))
            y = top - gh - gap
            for field, lh in lines:
                y -= lh
                field.setFrame_(AppKit.NSMakeRect(6, round(y), w - 12, lh))

    def update(self, info: dict | None = None) -> None:
        info = dict(info or status())
        state, when = describe(info)
        sig = (info.get("percent"), state, when, info.get("low_power"), fill_rgb(info))
        self.glyph.paint(info)
        if sig == self._sig:
            return
        self._sig = sig
        self.percent.setStringValue_(f"{int(info.get('percent') or 0)}%" if info.get("present") else "—")
        self.state.setStringValue_(state)
        low = info.get("percent", 0) <= 20 and not info.get("plugged")
        colour = GREEN if (info.get("charging") or info.get("charged")) else RED if low else INK
        self.state.setTextColor_(_ns(colour))
        self.when.setStringValue_(when)
        big = self.percent.font().pointSize()
        lines = [(self.percent, round(big * 1.25)), (self.state, 17)]
        if when:
            lines.append((self.when, 15))
        self.when.setHidden_(not when)
        self.lpm.setHidden_(not info.get("low_power"))
        if info.get("low_power"):
            lines.append((self.lpm, 14))
        self._layout(lines)


class _NotchBatBadge(AppKit.NSView):
    pass


class Badge:
    """ "74%" + a small battery (+ bolt), `height` tall; green while charging, red at 20% and below."""

    def __init__(self, height: float) -> None:
        self.h = float(height)
        self.in_window = False
        self._sig = None
        size = max(10.0, min(13.0, self.h * 0.55))
        self.text = _label(size, AppKit.NSFontWeightSemibold, digits=True)
        glyph_h = round(max(9.0, min(12.0, self.h * 0.5)))
        self.glyph = _NotchBatGlyph.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, round(glyph_h * 2.15), glyph_h))
        self.w = 34 + 4 + self.glyph.frame().size.width
        view = _NotchBatBadge.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, self.w, self.h))
        view.setWantsLayer_(True)
        view.layer().setMasksToBounds_(True)
        view.setAppearance_(AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameDarkAqua))
        view.addSubview_(self.text)
        view.addSubview_(self.glyph)
        self.text.setAlignment_(AppKit.NSTextAlignmentRight)
        line = round(size * 1.3)
        self.text.setFrame_(AppKit.NSMakeRect(0, round((self.h - line) / 2), 34, line))
        self.glyph.setFrameOrigin_(AppKit.NSMakePoint(38, round((self.h - glyph_h) / 2)))
        self.view = view

    def update(self, info: dict | None = None) -> None:
        info = dict(info or status())
        self.glyph.paint(info)
        percent = int(info.get("percent") or 0)
        colour = GREEN if info.get("charging") else RED if percent <= 20 and not info.get("plugged") else INK
        sig = (percent, colour)
        if sig == self._sig:
            return
        self._sig = sig
        self.text.setStringValue_(f"{percent}%")
        self.text.setTextColor_(_ns(colour))


# --- keeping them fresh -----------------------------------------------------------------------------

_views: list = []


class _NotchBatTarget(AppKit.NSObject):
    def tick_(self, timer):
        try:
            _tick()
        except Exception:
            log.debug("battery tick", exc_info=True)


def _tick() -> None:
    info = None
    for item in list(_views):
        window = item.view.window()
        if window is None:
            if item.in_window:                      # taken out of the notch: forget it
                _views.remove(item)
            continue
        item.in_window = True
        if window.isVisible() and not item.view.isHiddenOrHasHiddenAncestor():
            info = info or status()
            item.update(info)


_timer: list = []


def _attach() -> None:
    """Main thread, once: the watcher thread and the repaint timer."""
    start()
    if not _timer:
        target = _NotchBatTarget.alloc().init()
        timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0, target, "tick:", None, True)
        _timer.extend([target, timer])


def view(width: float, height: float):
    """For the notch: (NSView, update). Main thread. Refreshes itself once a second while visible (only
    repaints when the reading changed); update(info=None) repaints at once."""
    _attach()
    item = BatteryView(width, height)
    _views.append(item)
    item.update(status())
    return item.view, item.update


def compact_badge(height: float = 22):
    """For a wing: an NSView `height` tall (about 60-66 wide: read its frame). Keeps itself fresh."""
    _attach()
    item = Badge(height)
    _views.append(item)
    item.update(status())
    return item.view
