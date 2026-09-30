"""The shelf, for the notch: a tray to park files in, then drag them out, AirDrop or share them.

    ┌──────────┐  ┌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌┐
    │   (◎)    │  ╎  ┌────┐     ┌────┐     ┌────┐     ┌────┐          ╎
    │ AirDrop  │  ╎  │ 🖼 │     │ 📄 │     │ 📁 │     │ 🎞 │   ...    ╎    a horizontal row; scrolls
    │ 2 files  │  ╎  └────┘     └────┘     └────┘     └────┘          ╎
    └──────────┘  ╎  photo.png  notes.txt  Project    clip.mov        ╎
                  └╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌┘

Modelled on boring.notch's shelf (GPL-3.0, like Mint): the square share tile on the left (black
gradient, 3 pt dashed border, 55 pt circle, the service's icon, its name), the dashed tray on the
right (radius 16), 105 pt items with 56 pt QuickLook thumbnails (radius 12) and two-line names cut in
the middle, the accent selection (0.15 fill, 0.8 stroke), the drag preview (thumbnail + name on an
accent pill), drag-out after 3 pt. Re-implemented in AppKit.

* Drop files on the tray (or on the notch: accepts_drag / add_paths) to park them. Only references
  are kept - the path and a bookmark (so a moved or renamed file is followed) in
  ~/Library/Application Support/Mint/shelf.json. Files are never copied or moved by the shelf; a drag
  out is the user's own drop (Finder copies or moves as it always does).
* Click selects (⌘ toggles, ⇧ extends), a click on empty tray clears; double-click opens; drag the
  item (or the selection) into Finder or any app. The × on a hovered item removes it.
* Right-click: AirDrop, Share…, Open, Show in Finder, Copy, Remove from Shelf - for the selection when
  the item is in it. Right-click on the empty tray: Clear Shelf.
* The share tile: click = AirDrop the selection (else every file; an empty shelf opens a file
  picker); drop files on it = AirDrop them straight away without parking them. AirDrop always opens
  macOS's own sheet - nothing is sent until the user picks a device.

The API the notch uses (notch.py, another session's file):

    view, update = view(width, height)   # made for 560 x 150; the share tile hides below ~360 wide
    available()                          # there are files, or a file drag is over the notch
    accepts_drag(pasteboard)             # the pasteboard holds file URLs (a drag the shelf can take)
    add_paths(paths) -> int              # park files (any thread), returns how many were new
    set_drag_over(bool)                  # the notch reports a file drag over its (closed) shape
    on_change(fn)                        # fn() on the main thread when files or the drag state change
    paths(), remove_paths(paths), clear()

Views repaint when the shelf changes; while visible they check every 5 s (background) that the
files still exist. Main thread for views; file checks, bookmarks, saving and thumbnails run in the
background.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from pathlib import Path

import AppKit
import objc
import Quartz
from PyObjCTools import AppHelper

from mint.ui import gfx

log = logging.getLogger("mint.ui.notch_shelf")

STORE = Path.home() / "Library" / "Application Support" / "Mint" / "shelf.json"
ITEM_W, ICON, NAME_H = 105.0, 56.0, 30.0       # boring.notch's ShelfItemView
PAD_V, PAD_H, SPACING = 10.0, 5.0, 8.0
TILE_W = ITEM_W + 2 * PAD_H
TILE_H = PAD_V + ICON + 2 + NAME_H + PAD_V
GAP = 12.0                                     # between the share tile and the tray
DRAG_START = 3.0                               # points of movement before a drag begins
CHECK_EVERY = 5.0
INK = (1.0, 1.0, 1.0)
GREY = (0.56, 0.56, 0.58)
AIRDROP = "com.apple.share.AirDrop.send"


def _cg(rgb, alpha=1.0):
    return Quartz.CGColorCreateGenericRGB(rgb[0], rgb[1], rgb[2], alpha)


def _ns(rgb, alpha=1.0):
    return AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(rgb[0], rgb[1], rgb[2], alpha)


def _accent() -> tuple:
    return tuple(gfx.accent())


def _rounded(size, weight):
    font = AppKit.NSFont.systemFontOfSize_weight_(size, weight)
    descriptor = font.fontDescriptor().fontDescriptorWithDesign_(AppKit.NSFontDescriptorSystemDesignRounded)
    if descriptor is not None:
        font = AppKit.NSFont.fontWithDescriptor_size_(descriptor, size) or font
    return font


def _label(font, rgb=INK, alpha=1.0, centred=True):
    field = AppKit.NSTextField.labelWithString_("")
    field.setFont_(font)
    field.setTextColor_(_ns(rgb, alpha))
    field.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
    if centred:
        field.setAlignment_(AppKit.NSTextAlignmentCenter)
    return field


# --- the shelf's contents (any thread) -----------------------------------------------------------------

_lock = threading.RLock()
_items: list[dict] = []           # {"path", "bookmark" (base64 or ""), "added"}
_loaded = [False]
_drag_over = [False]
_listeners: list = []


def _load() -> None:
    if _loaded[0]:
        return
    _loaded[0] = True
    try:
        data = json.loads(STORE.read_text())
        rows = data.get("items", []) if isinstance(data, dict) else []
    except FileNotFoundError:
        rows = []
    except Exception:
        log.warning("shelf.json unreadable; starting empty", exc_info=True)
        rows = []
    seen = set()
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("path"), str) and row["path"] not in seen:
            seen.add(row["path"])
            _items.append({"path": row["path"], "bookmark": str(row.get("bookmark") or ""),
                           "added": float(row.get("added") or 0)})
    if _items:
        threading.Thread(target=_validate, daemon=True, name="shelf-check").start()


def _save() -> None:
    """Write shelf.json in the background (atomic replace)."""
    with _lock:
        rows = [dict(item) for item in _items]

    def write():
        try:
            STORE.parent.mkdir(parents=True, exist_ok=True)
            tmp = STORE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"version": 1, "items": rows}, indent=1))
            os.replace(tmp, STORE)
        except Exception:
            log.exception("saving the shelf")
    threading.Thread(target=write, daemon=True, name="shelf-save").start()


def _changed() -> None:
    def tell():
        for fn in list(_listeners):
            try:
                fn()
            except Exception:
                log.exception("shelf listener")
        for item in list(_views):
            item.reload()
    if threading.current_thread() is threading.main_thread():
        tell()
    else:
        AppHelper.callAfter(tell)


def _bookmark(path: str) -> str:
    url = AppKit.NSURL.fileURLWithPath_(path)
    data, error = url.bookmarkDataWithOptions_includingResourceValuesForKeys_relativeToURL_error_(0, None, None, None)
    return base64.b64encode(bytes(data)).decode() if data is not None else ""


def _resolve(bookmark: str) -> str | None:
    if not bookmark:
        return None
    try:
        raw = base64.b64decode(bookmark)
        data = AppKit.NSData.dataWithBytes_length_(raw, len(raw))
        url, stale, error = AppKit.NSURL.URLByResolvingBookmarkData_options_relativeToURL_bookmarkDataIsStale_error_(
            data, AppKit.NSURLBookmarkResolutionWithoutUI | AppKit.NSURLBookmarkResolutionWithoutMounting,
            None, None, None)
        return str(url.path()) if url is not None else None
    except Exception:
        return None


def _validate() -> None:
    """Background: follow moved/renamed files through their bookmarks, make missing bookmarks, drop files
    that are gone."""
    with _lock:
        snapshot = [dict(item) for item in _items]
    updates, gone = {}, set()
    for item in snapshot:
        path = item["path"]
        if os.path.exists(path):
            if not item["bookmark"]:
                try:
                    updates[path] = {"bookmark": _bookmark(path)}
                except Exception:
                    pass
            continue
        moved = _resolve(item["bookmark"])
        if moved and os.path.exists(moved):
            updates[path] = {"path": moved, "bookmark": _bookmark(moved)}
        else:
            gone.add(path)
    if not updates and not gone:
        return
    with _lock:
        for item in _items:
            item.update(updates.get(item["path"], {}))
        _items[:] = [item for item in _items if item["path"] not in gone]
        seen, unique = set(), []
        for item in _items:
            if item["path"] not in seen:
                seen.add(item["path"])
                unique.append(item)
        _items[:] = unique
    _save()
    _changed()


def paths() -> list[str]:
    with _lock:
        _load()
        return [item["path"] for item in _items]


def add_paths(paths_in) -> int:
    """Park files on the shelf (any thread). Returns how many were new. Only references are stored."""
    fresh = []
    with _lock:
        _load()
        have = {item["path"] for item in _items}
        for raw in paths_in or []:
            path = os.path.abspath(os.path.expanduser(str(raw)))
            if path in have or not os.path.exists(path):
                continue
            have.add(path)
            fresh.append(path)
            _items.append({"path": path, "bookmark": "", "added": time.time()})
    if fresh:
        _save()
        _changed()
        threading.Thread(target=_validate, daemon=True, name="shelf-bookmarks").start()
    return len(fresh)


def remove_paths(paths_in) -> int:
    """Take files off the shelf (the files themselves are not touched)."""
    drop = {str(p) for p in paths_in or []}
    with _lock:
        _load()
        before = len(_items)
        _items[:] = [item for item in _items if item["path"] not in drop]
        removed = before - len(_items)
    if removed:
        _save()
        _changed()
    return removed


def clear() -> None:
    remove_paths(paths())


def available() -> bool:
    """The notch shows the shelf tab: there are files, or a file drag is over the notch."""
    with _lock:
        _load()
        return bool(_items) or _drag_over[0]


def set_drag_over(active: bool) -> None:
    """The notch says a file drag is over it (or left)."""
    active = bool(active)
    if _drag_over[0] != active:
        _drag_over[0] = active
        _changed()


def on_change(callback):
    """callback() on the main thread when the files or the drag-over state change. Returns an unsubscribe."""
    _listeners.append(callback)

    def unsubscribe():
        if callback in _listeners:
            _listeners.remove(callback)
    return unsubscribe


def _file_urls(pasteboard) -> list:
    if pasteboard is None:
        return []
    options = {AppKit.NSPasteboardURLReadingFileURLsOnlyKey: True}
    urls = pasteboard.readObjectsForClasses_options_([AppKit.NSURL], options) or []
    return [url for url in urls if url.isFileURL()]


def accepts_drag(pasteboard) -> bool:
    """The pasteboard (a drag's, or any) holds file URLs the shelf can take."""
    try:
        options = {AppKit.NSPasteboardURLReadingFileURLsOnlyKey: True}
        return bool(pasteboard is not None and pasteboard.canReadObjectForClasses_options_([AppKit.NSURL], options))
    except Exception:
        return False


def paths_from(pasteboard) -> list[str]:
    """The file paths on a pasteboard (for the notch's own drop handling: add_paths(paths_from(pb)))."""
    return [str(url.path()) for url in _file_urls(pasteboard)]


# --- sharing ----------------------------------------------------------------------------------------------

def airdrop_service():
    return AppKit.NSSharingService.sharingServiceNamed_(AIRDROP)


def can_airdrop(paths_in) -> bool:
    """AirDrop is available for these files (asks the service; sends nothing)."""
    service = airdrop_service()
    urls = [AppKit.NSURL.fileURLWithPath_(p) for p in paths_in or []]
    return bool(service is not None and urls and service.canPerformWithItems_(urls))


def airdrop(paths_in) -> bool:
    """Open macOS's AirDrop sheet for these files (main thread). The user picks the device."""
    service = airdrop_service()
    urls = [AppKit.NSURL.fileURLWithPath_(p) for p in paths_in or [] if os.path.exists(p)]
    if service is None or not urls or not service.canPerformWithItems_(urls):
        AppKit.NSBeep()
        return False
    AppKit.NSApp.activateIgnoringOtherApps_(True)
    service.performWithItems_(urls)
    return True


def share(paths_in, anchor) -> None:
    """The system share menu (NSSharingServicePicker) from `anchor` (an NSView)."""
    urls = [AppKit.NSURL.fileURLWithPath_(p) for p in paths_in or [] if os.path.exists(p)]
    if not urls or anchor is None:
        return
    picker = AppKit.NSSharingServicePicker.alloc().initWithItems_(urls)
    _keep.append(picker)
    del _keep[:-4]
    picker.showRelativeToRect_ofView_preferredEdge_(anchor.bounds(), anchor, AppKit.NSMinYEdge)


_keep: list = []


def open_paths(paths_in) -> None:
    workspace = AppKit.NSWorkspace.sharedWorkspace()
    for path in paths_in or []:
        if os.path.exists(path):
            workspace.openURL_(AppKit.NSURL.fileURLWithPath_(path))


def reveal(paths_in) -> None:
    urls = [AppKit.NSURL.fileURLWithPath_(p) for p in paths_in or [] if os.path.exists(p)]
    if urls:
        AppKit.NSWorkspace.sharedWorkspace().activateFileViewerSelectingURLs_(urls)


def copy_to_pasteboard(paths_in) -> None:
    urls = [AppKit.NSURL.fileURLWithPath_(p) for p in paths_in or [] if os.path.exists(p)]
    if urls:
        board = AppKit.NSPasteboard.generalPasteboard()
        board.clearContents()
        board.writeObjects_(urls)


# --- thumbnails (QuickLook, in the background) -------------------------------------------------------

_ql: list = []
_thumbs: dict = {}                   # (path, mtime) -> NSImage


def _generator():
    if not _ql:
        try:
            objc.loadBundle("QuickLookThumbnailing", {},
                            bundle_path="/System/Library/Frameworks/QuickLookThumbnailing.framework")
            objc.registerMetaDataForSelector(
                b"QLThumbnailGenerator", b"generateBestRepresentationForRequest:completionHandler:",
                {"arguments": {3: {"callable": {"retval": {"type": b"v"},
                                                "arguments": {0: {"type": b"^v"}, 1: {"type": b"@"},
                                                              2: {"type": b"@"}}}}}})
            _ql.append((objc.lookUpClass("QLThumbnailGenerator"), objc.lookUpClass("QLThumbnailGenerationRequest")))
        except Exception:
            log.debug("QuickLookThumbnailing", exc_info=True)
            _ql.append(None)
    return _ql[0]


def _mtime(path: str) -> float:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0


def thumbnail(path: str, done) -> None:
    """done(NSImage) on the main thread with a QuickLook thumbnail (or the file's icon)."""
    key = (path, _mtime(path))
    if key in _thumbs:
        done(_thumbs[key])
        return
    classes = _generator()
    if classes is None:
        return
    generator, request_class = classes

    def finished(rep, error):
        image = rep.NSImage() if rep is not None else None
        if image is None:
            return

        def give():
            if len(_thumbs) > 200:
                _thumbs.clear()
            _thumbs[key] = image
            done(image)
        AppHelper.callAfter(give)
    try:
        request = request_class.alloc().initWithFileAtURL_size_scale_representationTypes_(
            AppKit.NSURL.fileURLWithPath_(path), (ICON, ICON), 2.0, 0xFFFFFFFF)
        generator.sharedGenerator().generateBestRepresentationForRequest_completionHandler_(request, finished)
    except Exception:
        log.debug("thumbnail %s", path, exc_info=True)


def _two_lines(name: str, font, width: float) -> str:
    """The name cut in the middle ("A very long na…ame.png") so it fits two lines of `width`."""
    attrs = {AppKit.NSFontAttributeName: font}
    line = AppKit.NSLayoutManager.alloc().init().defaultLineHeightForFont_(font)

    def fits(text):
        box = AppKit.NSAttributedString.alloc().initWithString_attributes_(text, attrs).boundingRectWithSize_options_(
            AppKit.NSMakeSize(width, 1000), AppKit.NSStringDrawingUsesLineFragmentOrigin)
        return box.size.height <= 2 * line + 0.5
    if fits(name):
        return name
    lo, hi = 1, len(name)
    while lo < hi:
        keep = (lo + hi + 1) // 2
        head = (keep + 1) // 2
        text = name[:head] + "…" + name[len(name) - (keep - head):]
        if fits(text):
            lo = keep
        else:
            hi = keep - 1
    head = (lo + 1) // 2
    return name[:head] + "…" + name[len(name) - (lo - head):]


def _fit(size, box: float) -> tuple:
    """(x, y, w, h) of an image of `size` fitted into a box x box square, centred."""
    w, h = max(1.0, size.width), max(1.0, size.height)
    scale = box / max(w, h)
    fw, fh = w * scale, h * scale
    return (box - fw) / 2, (box - fh) / 2, fw, fh


def _drag_image(image, name: str):
    """boring.notch's drag preview: the 56 pt thumbnail over the name on an accent pill, 105 wide."""
    font = AppKit.NSFont.systemFontOfSize_weight_(12, AppKit.NSFontWeightMedium)
    label = _two_lines(name, font, ITEM_W - 16)
    attrs = {AppKit.NSFontAttributeName: font, AppKit.NSForegroundColorAttributeName: AppKit.NSColor.whiteColor()}
    text = AppKit.NSAttributedString.alloc().initWithString_attributes_(label, attrs)
    box = text.boundingRectWithSize_options_(AppKit.NSMakeSize(ITEM_W - 16, 1000),
                                             AppKit.NSStringDrawingUsesLineFragmentOrigin)
    tw, th = min(ITEM_W - 16, box.size.width), box.size.height
    total_h = ICON + 4 + th + 4
    picture = AppKit.NSImage.alloc().initWithSize_(AppKit.NSMakeSize(ITEM_W, total_h))
    picture.lockFocus()
    try:
        if image is not None:
            fx, fy, fw, fh = _fit(image.size(), ICON)
            rect = AppKit.NSMakeRect((ITEM_W - ICON) / 2 + fx, total_h - ICON + fy, fw, fh)
            radius = min(12.0, fw / 4, fh / 4)
            AppKit.NSGraphicsContext.saveGraphicsState()
            AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, radius, radius).addClip()
            image.drawInRect_(rect)
            AppKit.NSGraphicsContext.restoreGraphicsState()
        pill = AppKit.NSMakeRect((ITEM_W - tw) / 2 - 8, 0, tw + 16, th + 4)
        _ns(_accent()).setFill()
        AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(pill, 4, 4).fill()
        style = AppKit.NSMutableParagraphStyle.alloc().init()
        style.setAlignment_(AppKit.NSTextAlignmentCenter)
        dark = sum(c * w for c, w in zip(_accent(), (0.2126, 0.7152, 0.0722))) > 0.6
        attrs = {AppKit.NSFontAttributeName: font, AppKit.NSParagraphStyleAttributeName: style,
                 AppKit.NSForegroundColorAttributeName: _ns((0, 0, 0) if dark else INK)}
        AppKit.NSAttributedString.alloc().initWithString_attributes_(label, attrs).drawWithRect_options_(
            AppKit.NSMakeRect((ITEM_W - tw) / 2, 2, tw, th), AppKit.NSStringDrawingUsesLineFragmentOrigin)
    finally:
        picture.unlockFocus()
    return picture


# --- views --------------------------------------------------------------------------------------------------

class _NotchShelfRoot(AppKit.NSView):
    """The whole shelf: takes file drops anywhere on it."""

    def acceptsFirstMouse_(self, event):
        return True

    def draggingEntered_(self, sender):
        return self.owner.drag_entered(sender, None)

    def draggingUpdated_(self, sender):
        return self.owner.drag_entered(sender, None)

    def draggingExited_(self, sender):
        self.owner.drag_exited(None)

    def prepareForDragOperation_(self, sender):
        return True

    def performDragOperation_(self, sender):
        return self.owner.drop(sender, None)

    def concludeDragOperation_(self, sender):
        self.owner.drag_exited(None)


class _NotchShelfShare(AppKit.NSView):
    """The share tile: click to AirDrop, drop files to AirDrop them."""

    def acceptsFirstMouse_(self, event):
        return True

    def mouseDown_(self, event):
        pass

    def mouseUp_(self, event):
        if AppKit.NSPointInRect(self.convertPoint_fromView_(event.locationInWindow(), None), self.bounds()):
            self.owner.share_tile_clicked()

    def resetCursorRects(self):
        self.addCursorRect_cursor_(self.bounds(), AppKit.NSCursor.pointingHandCursor())

    def draggingEntered_(self, sender):
        return self.owner.drag_entered(sender, "share")

    def draggingUpdated_(self, sender):
        return self.owner.drag_entered(sender, "share")

    def draggingExited_(self, sender):
        self.owner.drag_exited("share")

    def prepareForDragOperation_(self, sender):
        return True

    def performDragOperation_(self, sender):
        return self.owner.drop(sender, "share")

    def concludeDragOperation_(self, sender):
        self.owner.drag_exited("share")


class _NotchShelfTray(AppKit.NSView):
    """The dashed tray: a click on its empty part clears the selection; right-click offers Clear Shelf."""

    def acceptsFirstMouse_(self, event):
        return True

    def mouseDown_(self, event):
        self.owner.select_none()

    def menuForEvent_(self, event):
        return self.owner.tray_menu()


class _NotchShelfDocument(AppKit.NSView):
    def isFlipped(self):
        return False

    def acceptsFirstMouse_(self, event):
        return True

    def mouseDown_(self, event):
        self.owner.select_none()

    def menuForEvent_(self, event):
        return self.owner.tray_menu()


class _NotchShelfScroll(AppKit.NSScrollView):
    """Scrolls sideways, also for a plain (vertical) mouse wheel."""

    def scrollWheel_(self, event):
        dx, dy = event.scrollingDeltaX(), event.scrollingDeltaY()
        if abs(dy) > abs(dx):
            clip = self.contentView()
            step = dy if event.hasPreciseScrollingDeltas() else dy * 12
            doc_w = self.documentView().frame().size.width
            x = min(max(0.0, clip.bounds().origin.x - step), max(0.0, doc_w - clip.bounds().size.width))
            clip.scrollToPoint_(AppKit.NSMakePoint(x, 0))
            self.reflectScrolledClipView_(clip)
            return
        objc.super(_NotchShelfScroll, self).scrollWheel_(event)


class _NotchShelfTile(AppKit.NSView):
    """One file: select, open, drag out (NSDraggingSource), right-click menu, hover ×."""

    def acceptsFirstMouse_(self, event):
        return True

    def hitTest_(self, point):
        # Clicks on the thumbnail belong to the tile (an NSImageView refuses the first click into the
        # non-activating notch, so a drag from the picture never began); the × keeps its own.
        found = objc.super(_NotchShelfTile, self).hitTest_(point)
        if found is None or isinstance(found, AppKit.NSButton):
            return found
        return self

    def updateTrackingAreas(self):
        for area in list(self.trackingAreas()):
            self.removeTrackingArea_(area)
        options = (AppKit.NSTrackingMouseEnteredAndExited | AppKit.NSTrackingActiveAlways
                   | AppKit.NSTrackingInVisibleRect)
        self.addTrackingArea_(AppKit.NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(), options, self, None))
        objc.super(_NotchShelfTile, self).updateTrackingAreas()

    def mouseEntered_(self, event):
        self.owner.hover(self, True)

    def mouseExited_(self, event):
        self.owner.hover(self, False)

    def mouseDown_(self, event):
        self.down_event = event
        self.down_at = event.locationInWindow()
        if event.clickCount() == 2:
            self.owner.open_tile(self)
            return
        self.owner.click(self, int(event.modifierFlags()))

    def mouseDragged_(self, event):
        start = getattr(self, "down_at", None)
        if start is None:
            return
        where = event.locationInWindow()
        if abs(where.x - start.x) + abs(where.y - start.y) <= DRAG_START:
            return
        self.down_at = None
        self.owner.drag_out(self, event)

    def mouseUp_(self, event):
        self.down_at = None

    def menuForEvent_(self, event):
        return self.owner.tile_menu(self)

    # NSDraggingSource
    def draggingSession_sourceOperationMaskForDraggingContext_(self, session, context):
        if context == AppKit.NSDraggingContextOutsideApplication:
            return AppKit.NSDragOperationCopy | AppKit.NSDragOperationMove | AppKit.NSDragOperationGeneric
        return AppKit.NSDragOperationCopy

    def draggingSession_endedAtPoint_operation_(self, session, point, operation):
        self.owner.drag_ended()

    def ignoreModifierKeysForDraggingSession_(self, session):
        return False


class _NotchShelfTarget(AppKit.NSObject):
    def initWithOwner_(self, owner):
        self = objc.super(_NotchShelfTarget, self).init()
        if self is None:
            return None
        self.owner = owner
        return self

    def menu_(self, sender):
        self.owner.menu_action(str(sender.representedObject() or ""))

    def remove_(self, sender):
        self.owner.remove_hovered()

    def scrolled_(self, note):
        if self.owner is not None:
            self.owner.edges()

    def tick_(self, timer):
        try:
            _tick()
        except Exception:
            log.debug("shelf tick", exc_info=True)


def _dashed(frame_w, frame_h, radius):
    layer = Quartz.CAShapeLayer.layer()
    layer.setFrame_(Quartz.CGRectMake(0, 0, frame_w, frame_h))
    path = Quartz.CGPathCreateWithRoundedRect(Quartz.CGRectMake(1.5, 1.5, frame_w - 3, frame_h - 3),
                                              radius, radius, None)
    layer.setPath_(path)
    layer.setFillColor_(None)
    layer.setLineWidth_(3)
    layer.setLineCap_(Quartz.kCALineCapRound)
    layer.setLineDashPattern_([10, 10])
    layer.setStrokeColor_(_cg(INK, 0.1))
    return layer


class ShelfView:
    def __init__(self, width: float, height: float) -> None:
        self.w, self.h = float(width), float(height)
        self.selected: set[str] = set()
        self.anchor: str | None = None          # the last plainly clicked path (for ⇧-click)
        self.tiles: list = []                   # (path, tile, parts)
        self.hovered = None
        self.in_window = False
        self.checked = 0.0
        self.dragging = False
        self.target_zone = None                 # "tray" / "share" while a drag hovers
        self._paths: list[str] = []
        self.target = _NotchShelfTarget.alloc().initWithOwner_(self)
        root = _NotchShelfRoot.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, self.w, self.h))
        root.owner = self
        root.setWantsLayer_(True)
        root.layer().setMasksToBounds_(True)
        root.setAppearance_(AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameDarkAqua))
        root.registerForDraggedTypes_([AppKit.NSPasteboardTypeFileURL])
        self.view = root
        self._build()
        self.reload()

    # building
    def _build(self) -> None:
        w, h = self.w, self.h
        side = min(h, round(w * 0.28)) if w >= 360 else 0.0
        self.share = None
        if side:
            share = _NotchShelfShare.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, side, h))
            share.owner = self
            share.setWantsLayer_(True)
            share.registerForDraggedTypes_([AppKit.NSPasteboardTypeFileURL])
            layer = share.layer()
            layer.setCornerRadius_(12)
            fill = Quartz.CAGradientLayer.layer()
            fill.setFrame_(Quartz.CGRectMake(0, 0, side, h))
            fill.setCornerRadius_(12)
            fill.setColors_([_cg((0, 0, 0), 0.35), _cg((0, 0, 0), 0.20)])
            fill.setStartPoint_(Quartz.CGPointMake(0, 1))
            fill.setEndPoint_(Quartz.CGPointMake(1, 0))
            layer.addSublayer_(fill)
            self.share_border = _dashed(side, h, 12)
            layer.addSublayer_(self.share_border)
            self.circle = Quartz.CALayer.layer()
            self.circle.setBounds_(Quartz.CGRectMake(0, 0, 55, 55))
            self.circle.setCornerRadius_(27.5)
            self.circle.setBackgroundColor_(_cg(INK, 0.09))
            block = 55 + 5 + 18 + 14
            top = (h + block) / 2
            self.circle.setPosition_(Quartz.CGPointMake(side / 2, top - 27.5))
            layer.addSublayer_(self.circle)
            service = airdrop_service()
            icon = AppKit.NSImageView.alloc().initWithFrame_(AppKit.NSMakeRect(side / 2 - 17, top - 27.5 - 17, 34, 34))
            icon.setImageScaling_(AppKit.NSImageScaleProportionallyUpOrDown)
            icon.setImage_(service.image() if service is not None and service.image() is not None
                           else gfx.symbol("square.and.arrow.up", 24, "medium"))
            icon.setWantsLayer_(True)
            self.share_icon = icon
            share.addSubview_(icon)
            name = _label(_rounded(13, AppKit.NSFontWeightBold), INK, 0.8)
            name.setStringValue_(service.title() if service is not None else "Share")
            name.setFrame_(AppKit.NSMakeRect(4, round(top - 55 - 5 - 18), side - 8, 18))
            share.addSubview_(name)
            self.share_hint = _label(AppKit.NSFont.systemFontOfSize_weight_(10, AppKit.NSFontWeightMedium), GREY)
            self.share_hint.setFrame_(AppKit.NSMakeRect(4, round(top - 55 - 5 - 18 - 14), side - 8, 14))
            share.addSubview_(self.share_hint)
            share.setToolTip_("AirDrop the selected files (or all of them). Drop files here to AirDrop them.")
            self.view.addSubview_(share)
            self.share = share
        x0 = side + GAP if side else 0.0
        tray_w = w - x0
        tray = _NotchShelfTray.alloc().initWithFrame_(AppKit.NSMakeRect(x0, 0, tray_w, h))
        tray.owner = self
        tray.setWantsLayer_(True)
        tray.layer().setCornerRadius_(16)
        self.tray_border = _dashed(tray_w, h, 16)
        tray.layer().addSublayer_(self.tray_border)
        self.tray = tray
        self.view.addSubview_(tray)

        # empty: tray.and.arrow.down + "Drop files here"
        self.empty = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, tray_w, h))
        config = AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_scale_(
            20, AppKit.NSFontWeightMedium, AppKit.NSImageSymbolScaleLarge)
        config = config.configurationByApplyingConfiguration_(
            AppKit.NSImageSymbolConfiguration.configurationWithPaletteColors_([_ns(INK), _ns(GREY)]))
        tray_icon = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_("tray.and.arrow.down.fill", None)
        icon = AppKit.NSImageView.imageViewWithImage_(tray_icon.imageWithSymbolConfiguration_(config))
        icon.setFrame_(AppKit.NSMakeRect(0, h / 2 + 2, tray_w, 30))
        self.empty.addSubview_(icon)
        text = _label(_rounded(15, AppKit.NSFontWeightMedium), GREY)
        text.setStringValue_("Drop files here")
        text.setFrame_(AppKit.NSMakeRect(8, h / 2 - 26, tray_w - 16, 20))
        self.empty.addSubview_(text)
        tray.addSubview_(self.empty)

        inset = 8.0
        scroll = _NotchShelfScroll.alloc().initWithFrame_(AppKit.NSMakeRect(inset, 3, tray_w - 2 * inset, h - 6))
        scroll.setDrawsBackground_(False)
        scroll.setHasHorizontalScroller_(False)
        scroll.setHasVerticalScroller_(False)
        scroll.setBorderType_(AppKit.NSNoBorder)
        scroll.setHorizontalScrollElasticity_(AppKit.NSScrollElasticityAllowed)
        scroll.setVerticalScrollElasticity_(AppKit.NSScrollElasticityNone)
        document = _NotchShelfDocument.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, tray_w - 2 * inset, h - 6))
        document.owner = self
        scroll.setDocumentView_(document)
        scroll.setWantsLayer_(True)
        sw = tray_w - 2 * inset
        self.fade = Quartz.CAGradientLayer.layer()             # more to either side: the edges fade
        self.fade.setFrame_(Quartz.CGRectMake(0, 0, sw, h - 6))
        self.fade.setStartPoint_(Quartz.CGPointMake(0, 0.5))
        self.fade.setEndPoint_(Quartz.CGPointMake(1, 0.5))
        edge = min(0.2, 18.0 / sw)
        self.fade.setColors_([_cg((0, 0, 0), 0.0), _cg((0, 0, 0), 1.0), _cg((0, 0, 0), 1.0), _cg((0, 0, 0), 0.0)])
        self.fade.setLocations_([0.0, edge, 1.0 - edge, 1.0])
        self.scroll, self.document = scroll, document
        clip = scroll.contentView()
        clip.setPostsBoundsChangedNotifications_(True)
        AppKit.NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self.target, "scrolled:", AppKit.NSViewBoundsDidChangeNotification, clip)
        tray.addSubview_(scroll)

    # painting
    def edges(self) -> None:
        """Fade the tray's edges only where there is more to scroll to."""
        clip = self.scroll.contentView().bounds()
        doc_w = self.document.frame().size.width
        left = clip.origin.x > 1
        right = clip.origin.x + clip.size.width < doc_w - 1
        if not (left or right):
            self.scroll.layer().setMask_(None)
            return
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        self.fade.setColors_([_cg((0, 0, 0), 0.0 if left else 1.0), _cg((0, 0, 0), 1.0), _cg((0, 0, 0), 1.0),
                              _cg((0, 0, 0), 0.0 if right else 1.0)])
        self.scroll.layer().setMask_(self.fade)
        Quartz.CATransaction.commit()

    def reload(self) -> None:
        """Rebuild the tiles from the shelf's files (main thread)."""
        now = paths()
        if now == self._paths and self.tiles:
            self._paint_state()
            return
        self._paths = now
        self.selected &= set(now)
        for _, tile, _ in self.tiles:
            tile.removeFromSuperview()
        self.tiles = []
        self.hovered = None
        self.empty.setHidden_(bool(now))
        self.scroll.setHidden_(not now)
        doc_h = self.scroll.frame().size.height
        doc_w = max(self.scroll.frame().size.width, len(now) * TILE_W + max(0, len(now) - 1) * SPACING)
        self.document.setFrameSize_(AppKit.NSMakeSize(doc_w, doc_h))
        y = round((doc_h - TILE_H) / 2)
        self.scroll.contentView().scrollToPoint_(AppKit.NSMakePoint(0, 0))
        self.scroll.reflectScrolledClipView_(self.scroll.contentView())
        self.edges()
        for i, path in enumerate(now):
            tile, parts = self._tile(path)
            tile.setFrameOrigin_(AppKit.NSMakePoint(i * (TILE_W + SPACING), y))
            self.document.addSubview_(tile)
            self.tiles.append((path, tile, parts))
        self._paint_state()

    def _tile(self, path: str):
        tile = _NotchShelfTile.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, TILE_W, TILE_H))
        tile.owner = self
        tile.path = path
        tile.setWantsLayer_(True)
        tile.layer().setCornerRadius_(12)
        tile.layer().setCornerCurve_(Quartz.kCACornerCurveContinuous)
        tile.setToolTip_("~" + path[len(str(Path.home())):] if path.startswith(str(Path.home()) + "/") else path)
        holder = AppKit.NSView.alloc().initWithFrame_(
            AppKit.NSMakeRect((TILE_W - ICON) / 2, TILE_H - PAD_V - ICON, ICON, ICON))
        holder.setWantsLayer_(True)
        shadow = AppKit.NSShadow.alloc().init()
        shadow.setShadowColor_(_ns((0, 0, 0), 0.15))
        shadow.setShadowBlurRadius_(3)
        shadow.setShadowOffset_(AppKit.NSMakeSize(0, -2))
        holder.setShadow_(shadow)
        image_view = AppKit.NSImageView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, ICON, ICON))
        image_view.setImageScaling_(AppKit.NSImageScaleProportionallyUpOrDown)
        image_view.setWantsLayer_(True)
        image_view.layer().setCornerRadius_(12)
        image_view.layer().setMasksToBounds_(True)
        image_view.setImage_(AppKit.NSWorkspace.sharedWorkspace().iconForFile_(path))
        holder.addSubview_(image_view)
        tile.addSubview_(holder)
        font = AppKit.NSFont.systemFontOfSize_weight_(12, AppKit.NSFontWeightMedium)
        name = AppKit.NSTextField.wrappingLabelWithString_("")
        name.setFont_(font)
        name.setTextColor_(_ns(INK))
        name.setAlignment_(AppKit.NSTextAlignmentCenter)
        name.setMaximumNumberOfLines_(2)
        name.setSelectable_(False)
        display = AppKit.NSFileManager.defaultManager().displayNameAtPath_(path) or os.path.basename(path)
        name.setStringValue_(_two_lines(str(display), font, ITEM_W - 14))
        name.setFrame_(AppKit.NSMakeRect(PAD_H + 2, PAD_V, ITEM_W - 4, NAME_H))
        tile.addSubview_(name)
        close = gfx.close_button(self.target, "remove:", "Remove from Shelf")
        close.setFrameOrigin_(AppKit.NSMakePoint(TILE_W - gfx.CLOSE - 5, TILE_H - gfx.CLOSE - 5))
        close.setAlphaValue_(1.0)
        close.setHidden_(True)
        tile.addSubview_(close)
        parts = {"image": image_view, "name": name, "close": close, "display": str(display)}

        def got(image, view=image_view):
            fx, fy, fw, fh = _fit(image.size(), ICON)
            view.setFrame_(AppKit.NSMakeRect(round(fx), round(fy), round(fw), round(fh)))
            view.layer().setCornerRadius_(min(12.0, fw / 4, fh / 4))
            view.setImage_(image)
        thumbnail(path, got)
        return tile, parts

    def _paint_state(self) -> None:
        accent = _accent()
        tray_hot = self.target_zone == "tray"
        self.tray_border.setStrokeColor_(_cg(accent, 0.9) if tray_hot else _cg(INK, 0.1))
        if self.share is not None:
            share_hot = self.target_zone == "share"
            self.share_border.setStrokeColor_(_cg(accent, 0.9) if share_hot else _cg(INK, 0.1))
            self.circle.setBackgroundColor_(_cg(INK, 0.11 if share_hot else 0.09))
            scale = 1.06 if share_hot else 1.0
            self.share_icon.layer().setAffineTransform_(Quartz.CGAffineTransformMakeScale(scale, scale))
            chosen = [p for p in self._paths if p in self.selected]
            if share_hot:
                hint = "Drop to send"
            elif chosen:
                hint = f"{len(chosen)} selected" if len(chosen) > 1 else "1 selected"
            elif self._paths:
                hint = f"All {len(self._paths)} files" if len(self._paths) > 1 else "1 file"
            else:
                hint = "Drop or click"
            self.share_hint.setStringValue_(hint)
        for path, tile, parts in self.tiles:
            chosen = path in self.selected
            hover = 0.06 if tile is self.hovered else 0.0
            tile.layer().setBackgroundColor_(_cg(accent, 0.15) if chosen else _cg(INK, hover))
            tile.layer().setBorderColor_(_cg(accent, 0.8) if chosen else _cg(INK, 0.0))
            tile.layer().setBorderWidth_(2 if chosen else 1)
            parts["close"].setHidden_(tile is not self.hovered or self.dragging)

    # selection
    def click(self, tile, flags: int) -> None:
        path = tile.path
        if flags & AppKit.NSEventModifierFlagCommand:
            self.selected ^= {path}
            self.anchor = path
        elif flags & AppKit.NSEventModifierFlagShift and self.anchor in self._paths:
            a, b = self._paths.index(self.anchor), self._paths.index(path)
            self.selected = set(self._paths[min(a, b):max(a, b) + 1])
        else:
            if path not in self.selected or len(self.selected) == 1:
                self.selected = {path}
            self.anchor = path
        self._paint_state()

    def select_none(self) -> None:
        if self.selected:
            self.selected = set()
            self._paint_state()

    def _targets(self, tile) -> list[str]:
        """The selection when the tile is in it, else just the tile's file."""
        if tile is not None and tile.path not in self.selected:
            return [tile.path]
        return [p for p in self._paths if p in self.selected] or ([tile.path] if tile is not None else [])

    def hover(self, tile, inside: bool) -> None:
        if inside:
            self.hovered = tile
        elif self.hovered is tile:
            self.hovered = None
        self._paint_state()

    def remove_hovered(self) -> None:
        if self.hovered is not None:
            remove_paths([self.hovered.path])

    def open_tile(self, tile) -> None:
        open_paths(self._targets(tile))

    # the menus
    def _item(self, menu, title: str, key: str, symbol: str | None = None):
        item = menu.addItemWithTitle_action_keyEquivalent_(title, "menu:", "")
        item.setTarget_(self.target)
        item.setRepresentedObject_(key)
        if symbol:
            image = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, None)
            if image is not None:
                item.setImage_(image)
        return item

    def tile_menu(self, tile):
        if tile.path not in self.selected:
            self.selected = {tile.path}
            self.anchor = tile.path
            self._paint_state()
        self._menu_tile = tile
        chosen = self._targets(tile)
        many = len(chosen) > 1
        menu = AppKit.NSMenu.alloc().initWithTitle_("Shelf")
        menu.setAutoenablesItems_(False)
        airdrop_item = self._item(menu, "AirDrop", "airdrop", "wifi")
        service = airdrop_service()
        if service is not None and service.image() is not None:
            picture = service.image().copy()
            picture.setSize_(AppKit.NSMakeSize(16, 16))
            airdrop_item.setImage_(picture)
        airdrop_item.setEnabled_(can_airdrop(chosen))
        self._item(menu, "Share…", "share", "square.and.arrow.up")
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        self._item(menu, f"Open {len(chosen)} Items" if many else "Open", "open", "arrow.up.forward.app")
        self._item(menu, "Show in Finder", "reveal", "folder")
        self._item(menu, "Copy", "copy", "doc.on.doc")
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        self._item(menu, f"Remove {len(chosen)} from Shelf" if many else "Remove from Shelf", "remove", "xmark.circle")
        return menu

    def tray_menu(self):
        self._menu_tile = None
        if not self._paths:
            return None
        menu = AppKit.NSMenu.alloc().initWithTitle_("Shelf")
        menu.setAutoenablesItems_(False)
        self._item(menu, "AirDrop All", "airdrop_all", "wifi")
        self._item(menu, "Clear Shelf", "clear", "trash")
        return menu

    def menu_action(self, key: str) -> None:
        tile = getattr(self, "_menu_tile", None)
        chosen = self._targets(tile) if tile is not None else list(self._paths)
        if key in ("airdrop", "airdrop_all"):
            airdrop(chosen)
        elif key == "share":
            share(chosen, tile or self.tray)
        elif key == "open":
            open_paths(chosen)
        elif key == "reveal":
            reveal(chosen)
        elif key == "copy":
            copy_to_pasteboard(chosen)
        elif key == "remove":
            remove_paths(chosen)
        elif key == "clear":
            clear()

    def share_tile_clicked(self) -> None:
        chosen = [p for p in self._paths if p in self.selected] or list(self._paths)
        if chosen:
            airdrop(chosen)
            return
        panel = AppKit.NSOpenPanel.openPanel()          # an empty shelf: pick files to AirDrop (boring.notch)
        panel.setCanChooseFiles_(True)
        panel.setCanChooseDirectories_(True)
        panel.setAllowsMultipleSelection_(True)
        panel.setPrompt_("AirDrop")
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        if panel.runModal() == AppKit.NSModalResponseOK:
            airdrop([str(url.path()) for url in panel.URLs()])

    # drag out
    def drag_out(self, tile, event) -> None:
        chosen = self._targets(tile)
        items = []
        where = tile.convertPoint_fromView_(event.locationInWindow(), None)
        by_path = {p: parts for p, _, parts in self.tiles}
        for i, path in enumerate(chosen):
            if not os.path.exists(path):
                continue
            parts = by_path.get(path, {})
            picture = _drag_image(parts["image"].image() if parts else None,
                                  parts.get("display") or os.path.basename(path))
            size = picture.size()
            item = AppKit.NSDraggingItem.alloc().initWithPasteboardWriter_(AppKit.NSURL.fileURLWithPath_(path))
            item.setDraggingFrame_contents_(AppKit.NSMakeRect(where.x - size.width / 2 + i * 6,
                                                              where.y - size.height / 2 - i * 6,
                                                              size.width, size.height), picture)
            items.append(item)
        if not items:
            return
        self.dragging = True
        self._paint_state()
        session = tile.beginDraggingSessionWithItems_event_source_(items, event, tile)
        session.setAnimatesToStartingPositionsOnCancelOrFail_(True)
        session.setDraggingFormation_(AppKit.NSDraggingFormationStack if len(items) > 1
                                      else AppKit.NSDraggingFormationNone)

    def drag_ended(self) -> None:
        self.dragging = False
        self._paint_state()
        threading.Thread(target=_validate, daemon=True, name="shelf-check").start()   # a move is followed

    # drag in
    def drag_entered(self, sender, zone):
        source = sender.draggingSource()
        if isinstance(source, _NotchShelfTile) or not accepts_drag(sender.draggingPasteboard()):
            return AppKit.NSDragOperationNone
        zone = zone or "tray"
        if self.target_zone != zone:
            self.target_zone = zone
            self._paint_state()
            set_drag_over(True)
        mask = sender.draggingSourceOperationMask()
        for op in (AppKit.NSDragOperationCopy, AppKit.NSDragOperationLink, AppKit.NSDragOperationGeneric):
            if mask & op:
                return op
        return AppKit.NSDragOperationNone

    def drag_exited(self, zone) -> None:
        if zone == "share" and self.target_zone != "share":
            return
        if self.target_zone is not None:
            self.target_zone = None
            self._paint_state()
            set_drag_over(False)

    def drop(self, sender, zone) -> bool:
        source = sender.draggingSource()
        if isinstance(source, _NotchShelfTile):
            return False
        dropped = paths_from(sender.draggingPasteboard())
        self.target_zone = None
        self._paint_state()
        set_drag_over(False)
        if not dropped:
            return False
        if zone == "share":
            AppHelper.callAfter(airdrop, dropped)          # after the drag finishes, so the sheet can take focus
            return True
        add_paths(dropped)
        return True

    # upkeep
    def tick(self, visible: bool) -> None:
        if visible and self._paths and time.monotonic() - self.checked > CHECK_EVERY:
            self.checked = time.monotonic()
            threading.Thread(target=_validate, daemon=True, name="shelf-check").start()

    def update(self, *_ignored) -> None:
        """Repaint now (and re-check the files)."""
        self._paths = []
        self.reload()
        self.checked = 0.0


# --- keeping them fresh ---------------------------------------------------------------------------------

_views: list = []
_timer: list = []


def _tick() -> None:
    for item in list(_views):
        window = item.view.window()
        if window is None:
            if item.in_window:                  # taken out of the notch: forget it
                _views.remove(item)
            continue
        item.in_window = True
        item.tick(bool(window.isVisible()) and not item.view.isHiddenOrHasHiddenAncestor())


def _attach() -> None:
    if _timer:
        return
    target = _NotchShelfTarget.alloc().initWithOwner_(None)
    timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        1.0, target, "tick:", None, True)
    _timer.extend([target, timer])


def view(width: float, height: float):
    """For the notch: (NSView, update). Main thread. Made for 560 x 150 (the share tile shows from about
    360 wide; below that it is the tray alone). The view takes file drops itself."""
    _attach()
    item = ShelfView(width, height)
    _views.append(item)
    return item.view, item.update
