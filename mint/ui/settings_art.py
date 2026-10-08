"""Settings' pop-up menus: the control itself and the little pictures in its items.

The control (popup()): a real NSPopUpButton - keyboard, VoiceOver and the native NSMenu all stay - whose cell
draws it in the window's own style instead of the system bezel: a rounded field with a faint fill and a hairline
border (the cards' family), the chosen item's picture and title, and a quiet chevron.up.chevron.down. It lights
up a little under the pointer and while its menu is open, and dims when disabled; colours are resolved while
drawing, so light and dark both look right.

Pictures (NSImages for NSMenuItem.setImage_, all drawn when shown, so they follow the appearance):
  symbol(name, rgb)      an SF Symbol, tinted, centred in a fixed box so a menu's titles line up
  orb(theme)             Mint's orb in a theme's colours, with its eyes
  where(notch)           a screen with the orb floating on it, or with the notch island at the top
  position(where)        a screen with a dot where the orb sits
  flag(language)         a language's flag (emoji drawn to an image); a globe for "automatic"
  avatar(name)           a voice's initial in a circle of its own stable colour
  device_symbol(...)     which SF Symbol fits an audio device (mic, speaker, headphones, AirPods, display…)
  voice_title(...)       a voice's menu title: name, a softly coloured female/male tag, its character word

UsageMeter: one slim bar for a usage limit (label, percent used coloured green/amber/red, "resets in …").

Main thread only (AppKit).
"""

from __future__ import annotations

import functools
import hashlib
import logging
import time

import AppKit
import objc

from mint.ui import gfx

log = logging.getLogger("mint.settings")

BOX = (20.0, 18.0)            # a symbol or flag in a menu
SCREEN = (30.0, 20.0)         # the little screen diagrams
INK = (0.04, 0.06, 0.12)      # the orb's face
GREEN, AMBER, RED = (0.2, 0.74, 0.42), (0.98, 0.62, 0.1), (0.95, 0.3, 0.27)


# --- drawing helpers ---------------------------------------------------------------------------------

def _ns(rgb, alpha: float = 1.0):
    return AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(rgb[0], rgb[1], rgb[2], alpha)


def _image(w: float, h: float, draw, template: bool = False, name: str = ""):
    """An NSImage drawn by `draw(w, h)` (y up) whenever it is shown - at the screen's scale, in the current
    appearance. A drawing error leaves it blank rather than breaking the menu."""
    def handler(rect):
        try:
            draw(w, h)
        except Exception:
            log.debug("settings art %s failed", name, exc_info=True)
        return True
    image = AppKit.NSImage.imageWithSize_flipped_drawingHandler_(AppKit.NSMakeSize(w, h), False, handler)
    image.setCacheMode_(AppKit.NSImageCacheNever)          # redrawn for dark / light, never a stale copy
    image.setTemplate_(template)
    if name:
        image.setAccessibilityDescription_(name)
    return image


def _label_color(alpha: float):
    return AppKit.NSColor.labelColor().colorWithAlphaComponent_(alpha)


def _round_rect(x, y, w, h, r):
    return AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(AppKit.NSMakeRect(x, y, w, h), r, r)


def _oval(x, y, w, h):
    return AppKit.NSBezierPath.bezierPathWithOvalInRect_(AppKit.NSMakeRect(x, y, w, h))


def _text(text: str, size: float, color, weight=None, x=None, y=None, w=0.0, h=0.0) -> None:
    """Draw `text` centred in (x, y, w, h)."""
    font = AppKit.NSFont.systemFontOfSize_weight_(size, weight if weight is not None else AppKit.NSFontWeightRegular)
    attrs = {AppKit.NSFontAttributeName: font, AppKit.NSForegroundColorAttributeName: color}
    string = AppKit.NSAttributedString.alloc().initWithString_attributes_(text, attrs)
    size_ = string.size()
    string.drawAtPoint_(AppKit.NSMakePoint(x + (w - size_.width) / 2, y + (h - size_.height) / 2))


# --- pictures ----------------------------------------------------------------------------------------

_TINTED: dict = {}


def _symbol_image(name: str, point: float, weight, color=None):
    """An SF Symbol at `point`; with `color`, painted solid in it (monochrome: every layer the one colour, cut-outs
    kept - hierarchical / palette colouring washed out filled shapes or filled in the play triangle). A dynamic
    colour is resolved for the appearance being drawn."""
    image = (AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
             or AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_("circle", None))
    glyph = image.imageWithSymbolConfiguration_(
        AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_(point, weight))
    if color is None:
        return glyph
    rgb = color.colorUsingColorSpace_(AppKit.NSColorSpace.sRGBColorSpace()) or color
    key = (name, point, weight, round(rgb.redComponent(), 3), round(rgb.greenComponent(), 3),
           round(rgb.blueComponent(), 3), round(rgb.alphaComponent(), 3))
    if key in _TINTED:
        return _TINTED[key]
    size = glyph.size()
    scale = 3
    rep = AppKit.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(  # noqa: E501
        None, max(1, int(round(size.width * scale))), max(1, int(round(size.height * scale))), 8, 4, True, False,
        AppKit.NSCalibratedRGBColorSpace, 0, 0)
    rep.setSize_(size)
    AppKit.NSGraphicsContext.saveGraphicsState()
    try:
        AppKit.NSGraphicsContext.setCurrentContext_(AppKit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))
        rect = AppKit.NSMakeRect(0, 0, size.width, size.height)
        glyph.drawInRect_(rect)
        rgb.set()
        AppKit.NSRectFillUsingOperation(rect, AppKit.NSCompositingOperationSourceAtop)
    finally:
        AppKit.NSGraphicsContext.restoreGraphicsState()
    out = AppKit.NSImage.alloc().initWithSize_(size)
    out.addRepresentation_(rep)
    if len(_TINTED) > 400:
        _TINTED.clear()
    _TINTED[key] = out
    return out


@functools.lru_cache(maxsize=256)
def symbol(name: str, rgb: tuple | None = None, box: tuple = BOX, point: float = 13.0):
    """An SF Symbol centred in a `box`-sized image, tinted `rgb` (or a template the menu tints, when None)."""
    weight = AppKit.NSFontWeightMedium

    def draw(w, h):
        glyph = _symbol_image(name, point, weight, None if rgb is None else _ns(rgb))
        size = glyph.size()
        scale = min(1.0, w / max(1.0, size.width), h / max(1.0, size.height))
        gw, gh = size.width * scale, size.height * scale
        glyph.drawInRect_(AppKit.NSMakeRect((w - gw) / 2, (h - gh) / 2, gw, gh))
    return _image(box[0], box[1], draw, template=rgb is None, name=name)


def _orb_at(cx: float, cy: float, d: float, pal: dict, face: bool = True) -> None:
    """Mint's orb, centred at (cx, cy), `d` across, in palette `pal`: its swirl as a soft diagonal blend, a glass
    highlight, a faint rim and (when it is big enough) the two eyes."""
    path = _oval(cx - d / 2, cy - d / 2, d, d)
    blend = AppKit.NSGradient.alloc().initWithColors_([_ns(gfx.light(pal["awake"])), _ns(pal["awake"]),
                                                       _ns(pal["thinking"]), _ns(pal["speaking"])])
    blend.drawInBezierPath_angle_(path, -55)
    gloss = AppKit.NSGradient.alloc().initWithStartingColor_endingColor_(
        AppKit.NSColor.colorWithWhite_alpha_(1.0, 0.55), AppKit.NSColor.colorWithWhite_alpha_(1.0, 0.0))
    gloss.drawInBezierPath_relativeCenterPosition_(path, AppKit.NSMakePoint(-0.4, 0.5))
    mono = min(pal["awake"]) > 0.7                 # Mono is nearly white: give it a firmer edge
    AppKit.NSColor.colorWithWhite_alpha_(0.0, 0.32 if mono else 0.16).setStroke()
    path.setLineWidth_(max(0.5, d / 36))
    path.stroke()
    if face and d >= 12:
        _ns(INK, 0.88).setFill()
        ew, eh = d * 0.115, d * 0.21
        for dx in (-0.15, 0.15):
            _round_rect(cx + dx * d - ew / 2, cy - eh / 2 + d * 0.03, ew, eh, ew / 2).fill()


@functools.lru_cache(maxsize=32)
def orb(theme: str, d: float = 18.0):
    """Mint's orb in `theme`'s colours (gfx.THEMES), `d` points across - what the user will get."""
    pal = gfx.THEMES.get(theme) or gfx.THEMES["mint"]
    return _image(d + 2, d + 2, lambda w, h: _orb_at(w / 2, h / 2, d, pal), name=f"{theme} orb")


def _screen(w: float, h: float):
    """A small screen outline with a faint fill; -> its inner rect (x, y, w, h)."""
    x, y, sw, sh = 1.0, 1.5, w - 2.0, h - 3.0
    path = _round_rect(x, y, sw, sh, 3.0)
    _label_color(0.07).setFill()
    path.fill()
    _label_color(0.5).setStroke()
    path.setLineWidth_(1.0)
    path.stroke()
    return x, y, sw, sh


@functools.lru_cache(maxsize=4)
def where(notch: bool):
    """Where Mint lives: a screen with the orb floating near a corner, or with the notch island at the top."""
    def draw(w, h):
        x, y, sw, sh = _screen(w, h)
        if notch:
            pill_w, pill_h = sw * 0.36, 4.6
            pill = _round_rect(x + (sw - pill_w) / 2, y + sh - pill_h, pill_w, pill_h + 1.5, pill_h / 2)
            AppKit.NSColor.colorWithWhite_alpha_(0.04, 1.0).setFill()      # a notch is black in both looks
            pill.fill()
            _label_color(0.35).setStroke()                               # ...with an edge to see it on dark
            pill.setLineWidth_(0.6)
            pill.stroke()
            _ns(gfx.palette()["awake"]).setFill()
            _oval(x + sw / 2 + pill_w / 2 - 4.2, y + sh - pill_h / 2 - 1.4, 2.8, 2.8).fill()
        else:
            _orb_at(x + sw - 6.5, y + sh - 6.5, 7.0, gfx.palette(), face=False)
    return _image(SCREEN[0], SCREEN[1], draw, name="notch" if notch else "floating orb")


SPOTS = {"top-right": (0.82, 0.78), "top-left": (0.18, 0.78), "top-center": (0.5, 0.78),
         "bottom-right": (0.82, 0.24), "bottom-left": (0.18, 0.24), "custom": (0.6, 0.5)}


@functools.lru_cache(maxsize=8)
def position(spot: str):
    """The orb's place: a screen with a dot there ("custom": a dot off to one side, with the path it was
    dragged along)."""
    fx, fy = SPOTS.get(spot, SPOTS["top-right"])

    def draw(w, h):
        x, y, sw, sh = _screen(w, h)
        cx, cy = x + sw * fx, y + sh * fy
        if spot == "custom":
            trail = AppKit.NSBezierPath.bezierPath()
            trail.moveToPoint_(AppKit.NSMakePoint(x + sw * 0.2, y + sh * 0.3))
            trail.curveToPoint_controlPoint1_controlPoint2_(
                AppKit.NSMakePoint(cx - 2.5, cy), AppKit.NSMakePoint(x + sw * 0.3, y + sh * 0.75),
                AppKit.NSMakePoint(cx - 6, cy + 3))
            trail.setLineDash_count_phase_([1.4, 1.6], 2, 0)
            trail.setLineWidth_(1.0)
            _label_color(0.45).setStroke()
            trail.stroke()
        _orb_at(cx, cy, 5.6, gfx.palette(), face=False)
    return _image(SCREEN[0], SCREEN[1], draw, name=spot)


FLAGS = {"English": "🇬🇧", "Hindi": "🇮🇳", "Hinglish": "🇮🇳", "Bengali": "🇮🇳", "Marathi": "🇮🇳", "Tamil": "🇮🇳",
         "Telugu": "🇮🇳", "Gujarati": "🇮🇳", "Kannada": "🇮🇳", "Punjabi": "🇮🇳", "Urdu": "🇵🇰", "Spanish": "🇪🇸",
         "French": "🇫🇷", "German": "🇩🇪", "Italian": "🇮🇹", "Portuguese": "🇧🇷", "Dutch": "🇳🇱", "Russian": "🇷🇺",
         "Turkish": "🇹🇷", "Arabic": "🇸🇦", "Japanese": "🇯🇵", "Korean": "🇰🇷", "Chinese (Mandarin)": "🇨🇳",
         "Indonesian": "🇮🇩", "Vietnamese": "🇻🇳", "Thai": "🇹🇭"}
# The language's own name - a second line in the menu, so the Indian languages under one flag still read apart.
NATIVE = {"Hindi": "हिन्दी", "Hinglish": "Hindi + English", "Bengali": "বাংলা", "Marathi": "मराठी", "Tamil": "தமிழ்",
          "Telugu": "తెలుగు", "Gujarati": "ગુજરાતી", "Kannada": "ಕನ್ನಡ", "Punjabi": "ਪੰਜਾਬੀ", "Urdu": "اردو",
          "Spanish": "Español", "French": "Français", "German": "Deutsch", "Italian": "Italiano",
          "Portuguese": "Português", "Dutch": "Nederlands", "Russian": "Русский", "Turkish": "Türkçe",
          "Arabic": "العربية", "Japanese": "日本語", "Korean": "한국어", "Chinese (Mandarin)": "中文 (普通话)",
          "Indonesian": "Bahasa Indonesia", "Vietnamese": "Tiếng Việt", "Thai": "ไทย"}


@functools.lru_cache(maxsize=64)
def flag(language: str):
    """A language's flag (the emoji, drawn); a globe for "auto" or one without a flag."""
    emoji = FLAGS.get(language)
    if not emoji:
        return symbol("globe", (0.2, 0.6, 1.0))

    def draw(w, h):
        font = AppKit.NSFont.fontWithName_size_("Apple Color Emoji", 14.0) or AppKit.NSFont.systemFontOfSize_(14)
        string = AppKit.NSAttributedString.alloc().initWithString_attributes_(emoji, {AppKit.NSFontAttributeName: font})
        bounds = string.boundingRectWithSize_options_(AppKit.NSMakeSize(100, 100),
                                                      AppKit.NSStringDrawingUsesLineFragmentOrigin)
        string.drawAtPoint_(AppKit.NSMakePoint((w - bounds.size.width) / 2, (h - bounds.size.height) / 2 + 0.5))
    return _image(BOX[0], BOX[1], draw, name=language)


AVATAR_COLORS = [(0.95, 0.42, 0.42), (0.98, 0.6, 0.22), (0.9, 0.72, 0.1), (0.36, 0.76, 0.38), (0.15, 0.72, 0.62),
                 (0.2, 0.62, 0.95), (0.38, 0.47, 0.96), (0.6, 0.42, 0.95), (0.88, 0.4, 0.78), (0.55, 0.6, 0.68)]


def avatar_color(name: str) -> tuple:
    """A voice's own colour: the same every time (from its name), spread over a soft palette."""
    return AVATAR_COLORS[hashlib.md5(name.encode()).digest()[0] % len(AVATAR_COLORS)]


@functools.lru_cache(maxsize=64)
def avatar(name: str, d: float = 20.0):
    """A voice's initial in a circle of its colour, with a soft top light."""
    rgb = avatar_color(name)

    def draw(w, h):
        path = _oval((w - d) / 2, (h - d) / 2, d, d)
        AppKit.NSGradient.alloc().initWithStartingColor_endingColor_(
            _ns(gfx.light(rgb)), _ns(gfx.mix(rgb, (0, 0, 0), 0.12))).drawInBezierPath_angle_(path, -90)
        _text((name or "?")[0].upper(), d * 0.5, AppKit.NSColor.whiteColor(), AppKit.NSFontWeightBold,
              x=(w - d) / 2, y=(h - d) / 2 + 0.5, w=d, h=d)
    return _image(d + 2, d + 2, draw, name=name)


GENDER_RGB = {"female": (0.91, 0.36, 0.6), "male": (0.25, 0.52, 0.95)}


def gender_tag(gender: str):
    """"female" / "male" in white on a soft solid capsule of its colour."""
    font = AppKit.NSFont.systemFontOfSize_weight_(10, AppKit.NSFontWeightSemibold)
    width = AppKit.NSAttributedString.alloc().initWithString_attributes_(
        gender, {AppKit.NSFontAttributeName: font}).size().width + 12
    rgb = GENDER_RGB.get(gender, (0.5, 0.5, 0.5))

    def draw(w, h):
        _ns(rgb, 0.92).set()
        pill = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            AppKit.NSMakeRect(0.75, 0.75, w - 1.5, h - 1.5), (h - 1.5) / 2, (h - 1.5) / 2)
        pill.fill()
        AppKit.NSColor.colorWithWhite_alpha_(1.0, 0.55).set()     # a light rim: it still stands out on the blue
        pill.setLineWidth_(1.0)
        pill.stroke()
        _text(gender, 10, AppKit.NSColor.whiteColor(), AppKit.NSFontWeightSemibold, x=0, y=0.5, w=w, h=h)
    return _image(round(width), 16, draw, name=f"tag-{gender}")


def voice_title(name: str, gender: str, tone: str):
    """A voice's menu title: its name, a softly coloured "female" / "male" tag and its character word, in columns
    (tab stops) so a long menu reads like a table."""
    style = AppKit.NSMutableParagraphStyle.alloc().init()
    style.setTabStops_([AppKit.NSTextTab.alloc().initWithTextAlignment_location_options_(
        AppKit.NSTextAlignmentLeft, at, {}) for at in (112.0, 166.0)])
    style.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
    font = AppKit.NSFont.systemFontOfSize_(13)
    out = AppKit.NSMutableAttributedString.alloc().initWithString_attributes_(
        name, {AppKit.NSFontAttributeName: font, AppKit.NSParagraphStyleAttributeName: style,
               AppKit.NSForegroundColorAttributeName: AppKit.NSColor.labelColor()})
    if gender:
        # A solid little capsule with white words: it stays readable on the blue highlight of a chosen row too
        # (coloured text on the highlight would vanish - menus don't recolour an attributed title).
        out.appendAttributedString_(AppKit.NSAttributedString.alloc().initWithString_attributes_(
            "\t", {AppKit.NSFontAttributeName: font, AppKit.NSParagraphStyleAttributeName: style}))
        attachment = AppKit.NSTextAttachment.alloc().init()
        tag = gender_tag(gender)
        attachment.setImage_(tag)
        attachment.setBounds_(AppKit.NSMakeRect(0, -3, tag.size().width, tag.size().height))
        piece = AppKit.NSMutableAttributedString.alloc().initWithAttributedString_(
            AppKit.NSAttributedString.attributedStringWithAttachment_(attachment))
        piece.addAttribute_value_range_(AppKit.NSParagraphStyleAttributeName, style, AppKit.NSMakeRange(0, piece.length()))
        out.appendAttributedString_(piece)
    if tone:
        out.appendAttributedString_(AppKit.NSAttributedString.alloc().initWithString_attributes_(
            ("\t" if gender else "\t\t") + tone, {
                AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(12),
                AppKit.NSForegroundColorAttributeName: AppKit.NSColor.secondaryLabelColor(),
                AppKit.NSParagraphStyleAttributeName: style}))
    return out


DEVICE_RGB = {"mic.fill": (1.0, 0.36, 0.55), "speaker.wave.2.fill": (1.0, 0.5, 0.25),
              "hifispeaker.fill": (1.0, 0.5, 0.25), "headphones": (0.2, 0.55, 1.0), "airpods": (0.2, 0.55, 1.0),
              "airpodspro": (0.2, 0.55, 1.0), "airpodsmax": (0.2, 0.55, 1.0), "beats.headphones": (0.2, 0.55, 1.0),
              "display": (0.15, 0.7, 0.62), "tv": (0.15, 0.7, 0.62), "iphone": (0.45, 0.5, 0.62),
              "ipad": (0.45, 0.5, 0.62), "airplayaudio": (0.2, 0.6, 1.0), "waveform": (0.6, 0.42, 0.95),
              "square.stack.3d.down.right.fill": (0.6, 0.42, 0.95), "gearshape.fill": (0.5, 0.54, 0.62),
              "cable.connector": (0.45, 0.5, 0.62)}


def device_symbol(name: str, transport: str = "", output: bool = False) -> str:
    """The SF Symbol for an audio device, from its name and transport: AirPods (Pro / Max), headphones, an iPhone,
    a display, AirPlay, a virtual device (Teams, BlackHole…), a USB or built-in mic or speaker. "" = System
    default."""
    n = f" {name.lower()} "
    t = (transport or "").lower()
    if not name:
        return "gearshape.fill"
    if "airpods max" in n:
        return "airpodsmax"
    if "airpods pro" in n:
        return "airpodspro"
    if "airpods" in n:
        return "airpods"
    if "beats" in n:
        return "beats.headphones"
    if any(k in n for k in ("headphone", "headset", "buds", "earphone", " wh-", " wf-", "bose", "jabra", "sennheiser")):
        return "headphones"
    if "iphone" in n or t == "continuity":
        return "iphone"
    if "ipad" in n:
        return "ipad"
    if t in ("hdmi", "displayport") or any(k in n for k in ("display", "monitor", " tv ", " lg ", " dell ")):
        return "display"
    if t == "airplay":
        return "airplayaudio"
    if t == "aggregate" or "multi-output" in n or "aggregate" in n:
        return "square.stack.3d.down.right.fill"
    if t == "virtual" or any(k in n for k in ("teams", "zoom", "blackhole", "loopback", "soundflower", "virtual")):
        return "waveform"
    if t == "bluetooth" and output and not any(k in n for k in ("speaker", "jbl", "boom", "soundlink")):
        return "headphones"
    if output:
        return "speaker.wave.2.fill" if t in ("built-in", "") else "hifispeaker.fill"
    return "mic.fill"


def device_spec(name: str, transport: str = "", output: bool = False) -> tuple:
    """(symbol, rgb) for an audio device - a pop-up option's icon (no AppKit: fine off the main thread)."""
    sym = device_symbol(name, transport, output)
    return sym, DEVICE_RGB.get(sym, (0.45, 0.5, 0.62))


def device_icon(name: str, transport: str = "", output: bool = False):
    return symbol(*device_spec(name, transport, output))


# --- the pop-up control ------------------------------------------------------------------------------

PAD_X = 9.0
ICON_GAP = 7.0
CHEVRON_W = 16.0


def resolve(icon):
    """An option's picture: an NSImage, a callable returning one, an SF Symbol name, or (symbol, rgb)."""
    if icon is None or icon == "":
        return None
    try:
        if callable(icon) and not isinstance(icon, AppKit.NSImage):
            icon = icon()
        if isinstance(icon, str):
            return symbol(icon, None)
        if isinstance(icon, tuple):
            return symbol(icon[0], tuple(icon[1]) if len(icon) > 1 and icon[1] else None)
        return icon
    except Exception:
        log.debug("settings art: icon %r failed", icon, exc_info=True)
        return None


class MintSettingsPopUpCell(AppKit.NSPopUpButtonCell):
    """Draws the closed pop-up like the window's own fields; the menu stays the native NSMenu."""

    @objc.python_method
    def _content(self, frame):
        """-> (image rect or None, title rect, chevron rect) inside `frame`."""
        item = self.selectedItem() if not self.pullsDown() else self.itemAtIndex_(0) if self.numberOfItems() else None
        image = item.image() if item is not None else None
        x = frame.origin.x + PAD_X
        image_rect = None
        if image is not None:
            size = image.size()
            ih = min(size.height, frame.size.height - 6)
            iw = size.width * (ih / max(1.0, size.height))
            image_rect = AppKit.NSMakeRect(x, frame.origin.y + (frame.size.height - ih) / 2, iw, ih)
            x += iw + ICON_GAP
        chevron = AppKit.NSMakeRect(frame.origin.x + frame.size.width - PAD_X - CHEVRON_W + 3, frame.origin.y,
                                    CHEVRON_W - 3, frame.size.height)
        title = AppKit.NSMakeRect(x, frame.origin.y, max(0.0, chevron.origin.x - 4 - x), frame.size.height)
        return item, image_rect, title, chevron

    def titleRectForBounds_(self, bounds):
        return self._content(bounds)[2]

    def imageRectForBounds_(self, bounds):
        rect = self._content(bounds)[1]
        return rect if rect is not None else AppKit.NSZeroRect

    def drawWithFrame_inView_(self, frame, view):
        try:
            self._draw(frame, view)
        except Exception:
            log.debug("settings pop-up drawing failed", exc_info=True)

    @objc.python_method
    def _draw(self, frame, view):
        enabled = self.isEnabled()
        hover = bool(getattr(view, "hover", False)) and enabled
        pressed = (self.isHighlighted() or bool(getattr(view, "open", False))) and enabled
        dark = "Dark" in str(view.effectiveAppearance().bestMatchFromAppearancesWithNames_(
            [AppKit.NSAppearanceNameAqua, AppKit.NSAppearanceNameDarkAqua]) or "")
        body = AppKit.NSInsetRect(frame, 0.5, 1.5)
        path = _round_rect(body.origin.x, body.origin.y, body.size.width, body.size.height, 7.0)
        fill = (0.13 if pressed else 0.1 if hover else 0.065) if dark else (0.085 if pressed else 0.06 if hover else 0.035)
        _label_color(fill).setFill()
        path.fill()
        _label_color((0.2 if hover or pressed else 0.13) if dark else (0.2 if hover or pressed else 0.14)).setStroke()
        path.setLineWidth_(1.0 if AppKit.NSScreen.mainScreen() is None
                           or AppKit.NSScreen.mainScreen().backingScaleFactor() < 2 else 0.5)
        path.stroke()

        item, image_rect, title_rect, chevron = self._content(frame)
        alpha = 1.0 if enabled else 0.45
        if image_rect is not None and item is not None:
            image = item.image()
            if image.isTemplate():
                image = _tinted(image, AppKit.NSColor.secondaryLabelColor())
            image.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
                image_rect, AppKit.NSZeroRect, AppKit.NSCompositingOperationSourceOver, alpha, True, None)
        title = self._title_string(item)
        if title is not None:
            h = title.size().height
            title.drawWithRect_options_(
                AppKit.NSMakeRect(title_rect.origin.x, title_rect.origin.y + (title_rect.size.height - h) / 2,
                                  title_rect.size.width, h),
                AppKit.NSStringDrawingUsesLineFragmentOrigin | AppKit.NSStringDrawingTruncatesLastVisibleLine)
        glyph = _symbol_image("chevron.down" if self.pullsDown() else "chevron.up.chevron.down", 9.0,
                              AppKit.NSFontWeightSemibold,
                              AppKit.NSColor.secondaryLabelColor().colorWithAlphaComponent_(alpha))
        size = glyph.size()
        glyph.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
            AppKit.NSMakeRect(chevron.origin.x + (chevron.size.width - size.width) / 2,
                              chevron.origin.y + (chevron.size.height - size.height) / 2, size.width, size.height),
            AppKit.NSZeroRect, AppKit.NSCompositingOperationSourceOver, 1.0, True, None)

    @objc.python_method
    def _title_string(self, item):
        return closed_title(item, self.font(), self.isEnabled(), str(self.title() or ""))

    def drawFocusRingMaskWithFrame_inView_(self, frame, view):
        body = AppKit.NSInsetRect(frame, 0.5, 1.5)
        _round_rect(body.origin.x, body.origin.y, body.size.width, body.size.height, 7.0).fill()

    def focusRingMaskBoundsForFrame_inView_(self, frame, view):
        return AppKit.NSInsetRect(frame, 0.5, 1.5)


def closed_title(item, font=None, enabled: bool = True, fallback: str = ""):
    """What the closed control shows for `item`, in dynamic label colours (resolved as it is drawn, so dark mode
    gets light text). An attributed title in columns (a voice: name, female/male, character word) shows only its
    first two columns, side by side - the character word is for the menu."""
    font = font or AppKit.NSFont.systemFontOfSize_(13)
    color = AppKit.NSColor.labelColor() if enabled else AppKit.NSColor.tertiaryLabelColor()
    style = AppKit.NSMutableParagraphStyle.alloc().init()
    style.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
    attributed = item.attributedTitle() if item is not None else None
    if attributed is not None and attributed.length():
        text = str(attributed.string())
        tabs = [i for i, ch in enumerate(text) if ch == "\t"]
        out = attributed.mutableCopy()
        if len(tabs) > 1:
            out.deleteCharactersInRange_(AppKit.NSMakeRange(tabs[1], len(text) - tabs[1]))
        if tabs:
            out.replaceCharactersInRange_withString_(AppKit.NSMakeRange(tabs[0], 1), "  ")
        whole = AppKit.NSMakeRange(0, out.length())
        out.addAttribute_value_range_(AppKit.NSParagraphStyleAttributeName, style, whole)
        plain = []
        out.enumerateAttribute_inRange_options_usingBlock_(
            AppKit.NSForegroundColorAttributeName, whole, 0,
            lambda value, rng, stop: plain.append(rng) if value is None or not enabled else None)
        for rng in plain:
            out.addAttribute_value_range_(AppKit.NSForegroundColorAttributeName, color, rng)
        return out
    text = str(item.title()) if item is not None else fallback
    return AppKit.NSAttributedString.alloc().initWithString_attributes_(text, {
        AppKit.NSFontAttributeName: font, AppKit.NSForegroundColorAttributeName: color,
        AppKit.NSParagraphStyleAttributeName: style})


def fit_width(button, w: float = 0.0, most: float = 420.0) -> float:
    """How wide `button` (a MintSettingsPopUp) must be so every item's closed title fits whole - at least `w`, at
    most `most`."""
    widest = 0.0
    font = button.font()
    items = [button.itemAtIndex_(0)] if button.pullsDown() and button.numberOfItems() else list(button.itemArray())
    for item in items:
        if item is None or item.isSeparatorItem():
            continue
        need = PAD_X + closed_title(item, font).size().width + 6 + CHEVRON_W + PAD_X
        image = item.image()
        if image is not None:
            size = image.size()
            ih = min(size.height, 20.0)
            need += size.width * (ih / max(1.0, size.height)) + ICON_GAP
        widest = max(widest, need)
    return min(max(float(w), widest), max(float(w), most))


def fit(button, w: float = 0.0, most: float = 420.0) -> None:
    """Widen `button` to fit_width, leftwards (its right edge stays)."""
    frame = button.frame()
    want = fit_width(button, w or frame.size.width, most)
    if want > frame.size.width + 0.5:
        right = frame.origin.x + frame.size.width
        button.setFrame_(AppKit.NSMakeRect(right - want, frame.origin.y, want, frame.size.height))


def _tinted(image, color):
    """A template image drawn in `color` (for the closed control; menus tint templates themselves)."""
    size = image.size()

    def draw(w, h):
        image.drawInRect_(AppKit.NSMakeRect(0, 0, w, h))
        color.set()
        AppKit.NSRectFillUsingOperation(AppKit.NSMakeRect(0, 0, w, h), AppKit.NSCompositingOperationSourceAtop)
    return _image(size.width, size.height, draw)


class MintSettingsPopUp(AppKit.NSPopUpButton):
    """NSPopUpButton with MintSettingsPopUpCell, a hover state and a pressed look while its menu is open."""

    @classmethod
    def cellClass(cls):
        return MintSettingsPopUpCell

    def updateTrackingAreas(self):
        objc.super(MintSettingsPopUp, self).updateTrackingAreas()
        for area in list(self.trackingAreas()):
            if area.owner() is self:
                self.removeTrackingArea_(area)
        self.addTrackingArea_(AppKit.NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(), AppKit.NSTrackingMouseEnteredAndExited | AppKit.NSTrackingActiveInKeyWindow
            | AppKit.NSTrackingInVisibleRect, self, None))

    def mouseEntered_(self, event):
        self.hover = True
        self.setNeedsDisplay_(True)

    def mouseExited_(self, event):
        self.hover = False
        self.setNeedsDisplay_(True)

    def willOpenMenu_withEvent_(self, menu, event):
        self.open = True
        self.setNeedsDisplay_(True)

    def didCloseMenu_withEvent_(self, menu, event):
        self.open = False
        self.setNeedsDisplay_(True)


def popup(frame, pulls_down: bool = False):
    """A styled pop-up (MintSettingsPopUp) - use it like NSPopUpButton."""
    button = MintSettingsPopUp.alloc().initWithFrame_pullsDown_(frame, pulls_down)
    if not isinstance(button.cell(), MintSettingsPopUpCell):       # cellClass not honoured: put it in by hand
        cell = MintSettingsPopUpCell.alloc().initTextCell_pullsDown_("", pulls_down)
        button.setCell_(cell)
    button.setFont_(AppKit.NSFont.systemFontOfSize_(13))
    button.cell().setArrowPosition_(AppKit.NSPopUpNoArrow)
    button.hover = False
    button.open = False
    return button


def _item_symbol(name: str, rgb: tuple, item, box: tuple = BOX, point: float = 13.0):
    """symbol(name, rgb) for a menu item: in its colour, but white while the item is highlighted (a blue symbol
    on the blue highlight would vanish)."""
    weight = AppKit.NSFontWeightMedium

    def draw(w, h):
        color = (AppKit.NSColor.selectedMenuItemTextColor() if item.isHighlighted() else _ns(rgb))
        glyph = _symbol_image(name, point, weight, color)
        size = glyph.size()
        scale = min(1.0, w / max(1.0, size.width), h / max(1.0, size.height))
        gw, gh = size.width * scale, size.height * scale
        glyph.drawInRect_(AppKit.NSMakeRect((w - gw) / 2, (h - gh) / 2, gw, gh))
    return _image(box[0], box[1], draw, name=name)


def add_item(button, title: str, icon=None, subtitle: str = "", attributed=None):
    """Add an item with its picture (see resolve), an optional grey second line (macOS 14.4+) or an attributed
    title. -> the NSMenuItem."""
    # Not addItemWithTitle_: that drops an earlier item with the same title (two devices of one name).
    item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
    button.menu().addItem_(item)
    if isinstance(icon, tuple) and len(icon) > 1 and icon[1]:
        image = _item_symbol(icon[0], tuple(icon[1]), item)
    else:
        image = resolve(icon)
    if image is not None:
        item.setImage_(image)
    if subtitle and hasattr(item, "setSubtitle_"):
        item.setSubtitle_(subtitle)
    if attributed is not None:
        item.setAttributedTitle_(attributed)
    return item


# --- buttons that fit their title --------------------------------------------------------------------

class MintSettingsButton(AppKit.NSButton):
    """A push button at least `min_w` wide that grows to fit its title (and image) - again whenever the title
    changes (Connect… -> Disconnect), keeping its right edge where it was."""

    def setTitle_(self, title):
        objc.super(MintSettingsButton, self).setTitle_(plain_title(title))
        fit_button(self)

    def setAttributedTitle_(self, title):
        text = str(title.string()) if title is not None else ""
        if text != plain_title(text):
            title = title.mutableCopy()
            title.replaceCharactersInRange_withString_(AppKit.NSMakeRange(0, title.length()), plain_title(text))
        objc.super(MintSettingsButton, self).setAttributedTitle_(title)
        fit_button(self)

    def setImage_(self, image):
        objc.super(MintSettingsButton, self).setImage_(image)
        fit_button(self)


def plain_title(title) -> str:
    """A button says what it does, without a trailing "…" (or "..."): the user wants no dots on buttons, even the
    macOS "opens a dialog" kind."""
    text = str(title or "")
    stripped = text.rstrip()
    for tail in ("…", "..."):
        if stripped.endswith(tail):
            return stripped[: -len(tail)].rstrip()
    return text


def _needed(button) -> float:
    return float(button.cell().cellSize().width) + 6        # a little air beyond the bezel's own padding


def button_width(title: str, w: float = 0.0, image=None) -> float:
    """How wide a rounded push button with `title` must be: at least `w`."""
    probe = AppKit.NSButton.buttonWithTitle_target_action_(title, None, None)
    probe.setBezelStyle_(AppKit.NSBezelStyleRounded)
    if image is not None:
        probe.setImage_(image)
        probe.setImagePosition_(AppKit.NSImageLeading)
    return max(float(w), _needed(probe))


def fit_button(button) -> None:
    min_w = getattr(button, "min_w", None)
    if min_w is None:
        return
    frame = button.frame()
    want = max(float(min_w), _needed(button))
    if abs(want - frame.size.width) < 0.5:
        return
    right = frame.origin.x + frame.size.width
    button.setFrame_(AppKit.NSMakeRect(right - want, frame.origin.y, want, frame.size.height))


def push_button(title: str, frame, min_w: float | None = None):
    """A MintSettingsButton (rounded bezel) in `frame`, widened to fit `title` - leftwards, so the right edge
    stays put."""
    button = MintSettingsButton.buttonWithTitle_target_action_(plain_title(title), None, None)
    button.setBezelStyle_(AppKit.NSBezelStyleRounded)
    button.setFrame_(frame)
    button.min_w = frame.size.width if min_w is None else min_w
    fit_button(button)
    return button


# --- usage limits ------------------------------------------------------------------------------------

def tone_rgb(used: float) -> tuple:
    """Green while there is plenty left, amber from 60 % used, red from 85 %."""
    return GREEN if used < 60 else AMBER if used < 85 else RED


def until(epoch: float, now: float | None = None) -> str:
    """"resets in 2 h 10 min" / "in 3 days" / "has reset"."""
    left = float(epoch or 0) - (time.time() if now is None else now)
    if not epoch:
        return ""
    if left <= 0:
        return "has reset"
    minutes = int(left // 60)
    if minutes < 60:
        return f"resets in {max(1, minutes)} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"resets in {hours} h {minutes} min" if minutes and hours < 10 else f"resets in {hours} h"
    days = round(left / 86400)
    return f"resets in {days} day{'s' if days != 1 else ''}"


def ago(epoch: float, now: float | None = None) -> str:
    seconds = max(0, (time.time() if now is None else now) - float(epoch or 0))
    if not epoch:
        return ""
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} days ago"


class UsageMeter(AppKit.NSView):
    """One usage limit: "5 hours" and the percent used (coloured by how much) over a slim bar, "resets in …" under
    it. Set .label, .used (0-100, or None: not known) and .resets (epoch seconds) before it shows."""

    def isFlipped(self):
        return True

    def drawRect_(self, rect):
        try:
            self._draw()
        except Exception:
            log.debug("usage meter drawing failed", exc_info=True)

    @objc.python_method
    def _draw(self):
        w = self.bounds().size.width
        used = getattr(self, "used", None)
        label = getattr(self, "label", "")
        small = AppKit.NSFont.systemFontOfSize_(11)
        AppKit.NSAttributedString.alloc().initWithString_attributes_(label, {
            AppKit.NSFontAttributeName: small,
            AppKit.NSForegroundColorAttributeName: AppKit.NSColor.secondaryLabelColor()}).drawAtPoint_(
            AppKit.NSMakePoint(0, 0))
        words = "—" if used is None else f"{round(used)}%"
        color = AppKit.NSColor.tertiaryLabelColor() if used is None else _ns(tone_rgb(used))
        pct = AppKit.NSAttributedString.alloc().initWithString_attributes_(words, {
            AppKit.NSFontAttributeName: AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(
                12, AppKit.NSFontWeightSemibold), AppKit.NSForegroundColorAttributeName: color})
        pct.drawAtPoint_(AppKit.NSMakePoint(w - pct.size().width, -1))
        track = _round_rect(0, 18, w, 5, 2.5)
        _label_color(0.1).setFill()
        track.fill()
        if used is not None and used > 0:
            _ns(tone_rgb(used)).setFill()
            _round_rect(0, 18, max(5.0, w * min(100.0, used) / 100), 5, 2.5).fill()
        note = getattr(self, "note", "")
        if note:
            AppKit.NSAttributedString.alloc().initWithString_attributes_(note, {
                AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(10.5),
                AppKit.NSForegroundColorAttributeName: AppKit.NSColor.tertiaryLabelColor()}).drawAtPoint_(
                AppKit.NSMakePoint(0, 26))


def usage_meter(frame, label: str, used, resets=0.0, now: float | None = None):
    """A UsageMeter for one limit, with VoiceOver words ("Claude 5 hours: 7% used, resets in 2 h")."""
    meter = UsageMeter.alloc().initWithFrame_(frame)
    meter.label, meter.used = label, used
    meter.note = until(resets, now) if used is not None else "not seen yet"
    meter.setAccessibilityElement_(True)
    meter.setAccessibilityRole_(AppKit.NSAccessibilityLevelIndicatorRole)
    meter.setAccessibilityLabel_(label)
    meter.setAccessibilityValue_("not known" if used is None else f"{round(used)}% used" +
                                 (f", {meter.note}" if meter.note else ""))
    return meter
