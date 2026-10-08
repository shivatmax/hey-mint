"""The notch's settings menu: a small black list that rolls down from the button it was opened with.

The open notch's gear (and the small row's settings button) used to pop up a system menu in the middle of the
screen. This one hangs right under the button, unrolls (the list grows downward, its rows slide in one after
another) and rolls back up when you pick something, click elsewhere, press Esc or the notch folds. It shows the
same items as the menu it is given (an NSMenu from the HUD's menu factory: titles, check marks, shortcuts,
separators, disabled rows) and runs the same actions. "Settings…" also folds the open notch: it was opened only
to get there.
"""

from __future__ import annotations

import logging
import time

import AppKit
import objc
import Quartz
from PyObjCTools import AppHelper

log = logging.getLogger("mint.ui.notch_menu")

W = 228                 # the list's width
ROW = 30                # one item
SEP = 9                 # a separator
PAD = 6                 # inside the list, above and below
RADIUS = 14
GAP = 6                 # between the button and the list
INK = (1.0, 1.0, 1.0)


def _cg(rgb, alpha=1.0):
    return Quartz.CGColorCreateGenericRGB(rgb[0], rgb[1], rgb[2], alpha)


def _ns(rgb, alpha=1.0):
    return AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(rgb[0], rgb[1], rgb[2], alpha)


def _key_text(item) -> str:
    """⌘, for an item with a key equivalent ("" otherwise)."""
    key = str(item.keyEquivalent() or "")
    if not key:
        return ""
    mask = int(item.keyEquivalentModifierMask())
    mods = "".join(sym for flag, sym in ((AppKit.NSEventModifierFlagControl, "⌃"),
                                         (AppKit.NSEventModifierFlagOption, "⌥"),
                                         (AppKit.NSEventModifierFlagShift, "⇧"),
                                         (AppKit.NSEventModifierFlagCommand, "⌘")) if mask & flag)
    return mods + key.upper()


def entries(menu) -> list[dict]:
    """The menu's items as plain rows: {"title", "on", "enabled", "key", "item"} or {"sep": True}."""
    rows = []
    for item in menu.itemArray() if menu is not None else []:
        if item.isSeparatorItem():
            if rows and not rows[-1].get("sep"):
                rows.append({"sep": True})
            continue
        if item.isHidden():
            continue
        rows.append({"title": str(item.title()), "on": int(item.state()) == AppKit.NSControlStateValueOn,
                     "enabled": bool(item.isEnabled()), "key": _key_text(item), "item": item})
    while rows and rows[-1].get("sep"):
        rows.pop()
    return rows


def height_of(rows: list[dict]) -> float:
    return 2 * PAD + sum(SEP if r.get("sep") else ROW for r in rows)


class MintNotchMenuPanel(AppKit.NSPanel):
    def canBecomeKeyWindow(self):
        return False


class MintNotchMenuList(AppKit.NSView):
    def isFlipped(self):
        return True

    def acceptsFirstMouse_(self, event):
        return True

    def mouseDown_(self, event):
        point = self.convertPoint_fromView_(event.locationInWindow(), None)
        self.owner.click_at(point.y)


class MintNotchMenuTicker(AppKit.NSObject):
    def initWithOwner_(self, owner):
        self = objc.super(MintNotchMenuTicker, self).init()
        if self is not None:
            self.owner = owner
        return self

    def tick_(self, timer):
        try:
            self.owner.tick()
        except Exception:
            log.debug("notch menu tick", exc_info=True)


class NotchMenu:
    def __init__(self) -> None:
        self.panel = None
        self.rows: list[dict] = []
        self.row_views: list = []
        self.hot = -1
        self.timer = None
        self.monitors: list = []
        self.on_close = None
        self.on_settings = None
        self.opened_at = 0.0

    # --- showing ---------------------------------------------------------------------------------------

    def is_open(self) -> bool:
        return self.panel is not None and self.panel.isVisible()

    def show(self, menu, anchor, level: int, on_close=None, on_settings=None) -> None:
        """`anchor`: the button's rect in screen points (the list hangs under it, right edges lined up).
        `on_settings`: called after "Settings…" runs (the notch folds). `on_close`: when the list is gone."""
        self.close(animated=False)
        self.rows = entries(menu)
        if not self.rows:
            return
        self.on_close, self.on_settings = on_close, on_settings
        h = height_of(self.rows)
        screen = AppKit.NSScreen.mainScreen().frame()
        x = anchor.origin.x + anchor.size.width - W + 4          # right edge under the button's right edge
        x = min(max(x, screen.origin.x + 8), screen.origin.x + screen.size.width - W - 8)
        y = anchor.origin.y - GAP - h
        panel = MintNotchMenuPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            AppKit.NSMakeRect(x, y, W, h), AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel,
            AppKit.NSBackingStoreBuffered, False)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(AppKit.NSColor.clearColor())
        panel.setHasShadow_(True)
        panel.setLevel_(level + 1)
        panel.setHidesOnDeactivate_(False)
        panel.setReleasedWhenClosed_(False)
        panel.setCollectionBehavior_(AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                                     | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary)
        panel.setAppearance_(AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameDarkAqua))
        try:
            from mint.ui.effects import SHARING
            panel.setSharingType_(SHARING)
        except Exception:
            pass
        view = MintNotchMenuList.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, W, h))
        view.owner = self
        view.setWantsLayer_(True)
        layer = view.layer()
        layer.setBackgroundColor_(_cg((0.05, 0.05, 0.06), 0.98))
        layer.setCornerRadius_(RADIUS)
        layer.setBorderColor_(_cg(INK, 0.12))
        layer.setBorderWidth_(0.5)
        layer.setMasksToBounds_(True)
        panel.setContentView_(view)
        self.panel, self.view = panel, view
        self._build_rows(h)
        panel.orderFrontRegardless()
        self._unroll(h)
        self.hot = -1
        self.opened_at = time.monotonic()
        self.timer = AppKit.NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            1 / 60, MintNotchMenuTicker.alloc().initWithOwner_(self), "tick:", None, True)
        AppKit.NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, AppKit.NSRunLoopCommonModes)
        self._watch()

    def _build_rows(self, h: float) -> None:
        self.row_views = []
        y = PAD
        for row in self.rows:
            if row.get("sep"):
                line = Quartz.CALayer.layer()
                line.setFrame_(Quartz.CGRectMake(12, y + SEP / 2, W - 24, 0.5))
                line.setBackgroundColor_(_cg(INK, 0.12))
                self.view.layer().addSublayer_(line)
                self.row_views.append((y, SEP, None, line))
                y += SEP
                continue
            cell = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(PAD, y, W - 2 * PAD, ROW))
            cell.setWantsLayer_(True)
            cell.layer().setCornerRadius_(8)
            alpha = 0.95 if row["enabled"] else 0.4
            check = AppKit.NSTextField.labelWithString_("✓" if row["on"] else "")
            check.setFont_(AppKit.NSFont.systemFontOfSize_weight_(12, AppKit.NSFontWeightBold))
            check.setTextColor_(_ns(INK, alpha))
            check.setFrame_(AppKit.NSMakeRect(8, (ROW - 16) / 2, 14, 16))
            cell.addSubview_(check)
            title = AppKit.NSTextField.labelWithString_(row["title"])
            title.setFont_(AppKit.NSFont.systemFontOfSize_weight_(13, AppKit.NSFontWeightMedium))
            title.setTextColor_(_ns(INK, alpha))
            title.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
            title.setFrame_(AppKit.NSMakeRect(26, (ROW - 17) / 2, W - 2 * PAD - 26 - 50, 17))
            cell.addSubview_(title)
            if row["key"]:
                key = AppKit.NSTextField.labelWithString_(row["key"])
                key.setFont_(AppKit.NSFont.systemFontOfSize_weight_(12, AppKit.NSFontWeightRegular))
                key.setTextColor_(_ns(INK, 0.45))
                key.setAlignment_(AppKit.NSTextAlignmentRight)
                key.setFrame_(AppKit.NSMakeRect(W - 2 * PAD - 54, (ROW - 16) / 2, 46, 16))
                cell.addSubview_(key)
            self.view.addSubview_(cell)
            self.row_views.append((y, ROW, cell, None))
            y += ROW

    def _unroll(self, h: float) -> None:
        """The list rolls down from the button: its height grows from the top, rows slide in one by one."""
        mask = Quartz.CALayer.layer()
        mask.setBackgroundColor_(_cg(INK))
        top = 0.0 if self.view.layer().isGeometryFlipped() else 1.0     # where "the top" is in this layer
        mask.setAnchorPoint_(Quartz.CGPointMake(0.5, top))
        mask.setFrame_(Quartz.CGRectMake(0, 0, W, h))
        self.view.layer().setMask_(mask)
        grow = Quartz.CASpringAnimation.animationWithKeyPath_("bounds.size.height")
        grow.setFromValue_(0.0)
        grow.setToValue_(h)
        grow.setDamping_(22)
        grow.setStiffness_(320)
        grow.setDuration_(min(0.5, grow.settlingDuration()))
        mask.addAnimation_forKey_(grow, "unroll")
        now = Quartz.CACurrentMediaTime()
        n = 0
        for y, _size, cell, line in self.row_views:
            layer = cell.layer() if cell is not None else line
            fade = Quartz.CABasicAnimation.animationWithKeyPath_("opacity")
            fade.setFromValue_(0.0)
            fade.setToValue_(1.0)
            slide = Quartz.CABasicAnimation.animationWithKeyPath_("transform.translation.y")
            slide.setFromValue_(-6.0)
            slide.setToValue_(0.0)
            group = Quartz.CAAnimationGroup.animation()
            group.setAnimations_([fade, slide])
            group.setDuration_(0.2)
            group.setBeginTime_(now + 0.03 + 0.022 * n)
            group.setFillMode_(Quartz.kCAFillModeBackwards)
            group.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithName_(
                Quartz.kCAMediaTimingFunctionEaseOut))
            layer.addAnimation_forKey_(group, "in")
            n += 1

    # --- using it ----------------------------------------------------------------------------------------

    def _index_at(self, y: float) -> int:
        for i, (top, size, cell, _line) in enumerate(self.row_views):
            if cell is not None and top <= y < top + size and self.rows[i].get("enabled"):
                return i
        return -1

    def tick(self) -> None:
        if not self.is_open():
            return
        mouse = AppKit.NSEvent.mouseLocation()
        frame = self.panel.frame()
        inside = AppKit.NSPointInRect(mouse, frame)
        hot = self._index_at(frame.origin.y + frame.size.height - mouse.y) if inside else -1
        if hot != self.hot:
            for i, (_top, _size, cell, _line) in enumerate(self.row_views):
                if cell is not None:
                    cell.layer().setBackgroundColor_(_cg(INK, 0.13) if i == hot else _cg(INK, 0.0))
            self.hot = hot

    def click_at(self, y: float) -> None:
        i = self._index_at(y)
        if i < 0:
            return
        row = self.rows[i]
        item = row["item"]
        settings = str(item.representedObject() or "") == "open_settings" or row["title"].startswith("Settings")
        self.close()
        try:
            if item.action() is not None:
                AppKit.NSApp().sendAction_to_from_(item.action(), item.target(), item)
        except Exception:
            log.exception("notch menu item failed")
        if settings and self.on_settings is not None:
            AppHelper.callLater(0.05, self.on_settings)

    def _watch(self) -> None:
        """A click anywhere else, or Esc, rolls it back up."""
        def outside(event):
            if self.is_open() and time.monotonic() - self.opened_at > 0.15:
                if not AppKit.NSPointInRect(AppKit.NSEvent.mouseLocation(), self.panel.frame()):
                    self.close()
            return event

        def key(event):
            if self.is_open() and int(event.keyCode()) == 53:
                self.close()
                return None
            return event
        mask = AppKit.NSEventMaskLeftMouseDown | AppKit.NSEventMaskRightMouseDown
        self.monitors = [
            AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(mask, outside),
            AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(mask, outside),
            AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(AppKit.NSEventMaskKeyDown, key)]

    def close(self, animated: bool = True) -> None:
        for monitor in self.monitors:
            if monitor is not None:
                AppKit.NSEvent.removeMonitor_(monitor)
        self.monitors = []
        if self.timer is not None:
            self.timer.invalidate()
            self.timer = None
        panel, self.panel = self.panel, None
        if panel is None:
            return
        on_close, self.on_close = self.on_close, None
        if not animated:
            panel.orderOut_(None)
        else:
            mask = panel.contentView().layer().mask()
            if mask is not None:
                roll = Quartz.CABasicAnimation.animationWithKeyPath_("bounds.size.height")
                roll.setFromValue_(mask.bounds().size.height)
                roll.setToValue_(0.0)
                roll.setDuration_(0.14)
                roll.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithName_(
                    Quartz.kCAMediaTimingFunctionEaseIn))
                roll.setFillMode_(Quartz.kCAFillModeForwards)
                roll.setRemovedOnCompletion_(False)
                mask.addAnimation_forKey_(roll, "rollup")
            AppHelper.callLater(0.15, lambda: panel.orderOut_(None))
        if on_close is not None:
            try:
                on_close()
            except Exception:
                log.debug("notch menu on_close", exc_info=True)


menu = NotchMenu()
