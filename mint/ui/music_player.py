"""The mini player: a small black card by the orb while music plays (Spotify or Apple Music).

    ┌───────────────────────────────────────────┐
    │ ┌───────┐  Blinding Lights            ( × ) │     the × is the shared close button (gfx)
    │ │  art  │  ▮▮▮ The Weeknd                   │     bars dance while it plays
    │ │     ◉ │  1:02 ━━━━━━━━○───────── 3:20     │     click or drag the bar to seek
    │ └───────┘       ⏮    ⏯    ⏭      🔈 ━━━   │     the volume slides in while the pointer is on it
    └───────────────────────────────────────────┘

* It shows when the user asks to play something (music.py calls show) and whenever music starts
  (pref music_player_auto, default on). It hides a few seconds after the music pauses or stops, when
  its × is clicked (then it stays away until the music stops and starts again, or the user asks), and
  steps aside while the island or the chat is out (they grow from the same orb).
* Clicking the artwork opens the player app. Buttons work on the first click; nothing takes focus.
* Orb mode: it sits just above the orb (below it when the orb is at the top), its corner on the orb's
  side, and springs out of the orb. Notch mode: it hangs centred under the notch, like the image card.

For the notch (notch.py, another session's file) this module also offers:

    view, update = player_view(width, height)   # the same player, drawn on a clear background
    info = compact_info(height)                 # the closed notch: artwork + playing bars views

Both are kept up to date by this module's own timer (twice a second, cheap: it reads music.cached());
update(info) repaints at once with a music.now_playing()/cached() dict. Main thread only, except
show()/hide(), which can be called from any thread.
"""

from __future__ import annotations

import logging
import subprocess
import time

import AppKit
import objc
import Quartz
from PyObjCTools import AppHelper

from mint.ui import gfx

log = logging.getLogger("mint.ui.music_player")

W, H = 320, 110            # the card
PAD = 14
RADIUS = 22
GAP = 12                   # between the orb and the card
ORB_R = 22                 # the orb's radius (hud.D / 2)
AUTO_HIDE = 6.0            # seconds after a pause or stop
SURFACE = (0.035, 0.035, 0.045)
INK = (1.0, 1.0, 1.0)
DIM = (0.62, 0.63, 0.68)
BUNDLES = {"Spotify": "com.spotify.client", "Music": "com.apple.Music"}


def _cg(rgb, alpha=1.0):
    return Quartz.CGColorCreateGenericRGB(rgb[0], rgb[1], rgb[2], alpha)


def _ns(rgb, alpha=1.0):
    return AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(rgb[0], rgb[1], rgb[2], alpha)


def _font(size, weight=AppKit.NSFontWeightRegular, rounded=False, digits=False):
    if digits:
        return AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(size, weight)
    font = AppKit.NSFont.systemFontOfSize_weight_(size, weight)
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
    field.setAllowsDefaultTighteningForTruncation_(True)
    return field


def _clock(seconds) -> str:
    seconds = max(0, int(seconds or 0))
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}" if seconds >= 3600 \
        else f"{seconds // 60}:{seconds % 60:02d}"


def _music():
    from mint.tools import music
    return music


def _in_background(job, *args) -> None:
    import threading

    def run():
        try:
            job(*args)
        except Exception:
            log.exception("music player action")
        _music().poke()
    threading.Thread(target=run, daemon=True, name="music-action").start()


_images: dict[str, object] = {}


def _image(path: str):
    if not path:
        return None
    if path not in _images:
        if len(_images) > 40:
            _images.clear()
        _images[path] = AppKit.NSImage.alloc().initWithContentsOfFile_(path)
    return _images[path]


# --- small views -------------------------------------------------------------------------------------

class _MusicPlayerPanel(AppKit.NSPanel):
    def canBecomeKeyWindow(self):
        return False

    def canBecomeMainWindow(self):
        return False


class _MusicPlayerButton(AppKit.NSButton):
    """Takes the first click, even when Mint isn't the active app."""

    def acceptsFirstMouse_(self, event):
        return True


class _MusicPlayerSlider(AppKit.NSSlider):
    def acceptsFirstMouse_(self, event):
        return True


class _MusicPlayerArt(AppKit.NSImageView):
    """The cover: a click opens the player app."""

    def acceptsFirstMouse_(self, event):
        return True

    def mouseDown_(self, event):
        pass

    def mouseUp_(self, event):
        owner = getattr(self, "owner", None)
        if owner is not None:
            owner.open_app()

    def resetCursorRects(self):
        self.addCursorRect_cursor_(self.bounds(), AppKit.NSCursor.pointingHandCursor())


class _MusicPlayerBar(AppKit.NSView):
    """The thin progress bar; click or drag on it to seek."""

    def acceptsFirstMouse_(self, event):
        return True

    @objc.python_method
    def _fraction(self, event):
        x = self.convertPoint_fromView_(event.locationInWindow(), None).x
        width = max(1.0, self.bounds().size.width)
        return min(1.0, max(0.0, x / width))

    def mouseDown_(self, event):
        owner = getattr(self, "owner", None)
        if owner is not None:
            owner.scrub(self._fraction(event), False)

    def mouseDragged_(self, event):
        owner = getattr(self, "owner", None)
        if owner is not None:
            owner.scrub(self._fraction(event), False)

    def mouseUp_(self, event):
        owner = getattr(self, "owner", None)
        if owner is not None:
            owner.scrub(self._fraction(event), True)


class _MusicPlayerBars(AppKit.NSView):
    """Three little bars that dance while music plays (Apple's now-playing sign), mint coloured."""

    def initWithFrame_(self, frame):
        self = objc.super(_MusicPlayerBars, self).initWithFrame_(frame)
        if self is None:
            return None
        self.setWantsLayer_(True)
        self._bars = []
        self._playing = None
        w, h = frame.size.width, frame.size.height
        n, gap = 3, max(1.5, w * 0.14)
        bar_w = (w - gap * (n - 1)) / n
        for i in range(n):
            bar = Quartz.CALayer.layer()
            bar.setAnchorPoint_(Quartz.CGPointMake(0.5, 0.0))
            bar.setBounds_(Quartz.CGRectMake(0, 0, bar_w, h))
            bar.setPosition_(Quartz.CGPointMake(i * (bar_w + gap) + bar_w / 2, 0))
            bar.setCornerRadius_(bar_w / 2)
            bar.setBackgroundColor_(_cg(tuple(gfx.accent())))
            self.layer().addSublayer_(bar)
            self._bars.append(bar)
        self.setPlaying_(False)
        return self

    def setPlaying_(self, playing):
        playing = bool(playing)
        if playing is self._playing:
            return
        self._playing = playing
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        for i, bar in enumerate(self._bars):
            bar.removeAllAnimations()
            bar.setBackgroundColor_(_cg(tuple(gfx.accent()), 1.0 if playing else 0.55))
            bar.setTransform_(Quartz.CATransform3DMakeScale(1.0, (0.35, 0.7, 0.5)[i % 3] if not playing else 1.0, 1.0))
            if playing:
                dance = Quartz.CABasicAnimation.animationWithKeyPath_("transform.scale.y")
                dance.setFromValue_((0.25, 0.4, 0.3)[i % 3])
                dance.setToValue_(1.0)
                dance.setDuration_((0.42, 0.33, 0.51)[i % 3])
                dance.setAutoreverses_(True)
                dance.setRepeatCount_(1e9)
                dance.setTimeOffset_(i * 0.17)
                dance.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithName_(
                    Quartz.kCAMediaTimingFunctionEaseInEaseOut))
                bar.addAnimation_forKey_(dance, "dance")
        Quartz.CATransaction.commit()


class _MusicPlayerTarget(AppKit.NSObject):
    def initWithOwner_(self, owner):
        self = objc.super(_MusicPlayerTarget, self).init()
        if self is None:
            return None
        self.owner = owner
        return self

    def previous_(self, sender):
        self.owner.act("previous")

    def toggle_(self, sender):
        self.owner.act("toggle")

    def next_(self, sender):
        self.owner.act("next")

    def volume_(self, sender):
        self.owner.volume(int(sender.intValue()))

    def close_(self, sender):
        self.owner.close_clicked()

    def tick_(self, timer):
        try:
            self.owner.tick()
        except Exception:
            log.debug("music player tick", exc_info=True)


# --- the player itself (the card's content, and the notch's) --------------------------------------

class PlayerView:
    """Artwork, title, artist, progress with times, prev / play-pause / next and a hover volume,
    laid out in `width` x `height` (made for about 320 x 110; wider and taller works)."""

    def __init__(self, width: float, height: float, close_room: float = 0.0, on_close=None) -> None:
        self.w, self.h = float(width), float(height)
        self.close_room = close_room               # room kept on the title row for a ×
        self.on_close = on_close
        self.info: dict = {}
        self.hover = False
        self._sig = None
        self._scrub_until = 0.0
        self._scrub_at = None
        self._volume_until = 0.0
        self._volume_pending = None
        self._laid_out = False
        self.in_window = False
        self.target = _MusicPlayerTarget.alloc().initWithOwner_(self)
        self.view = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, self.w, self.h))
        self.view.setWantsLayer_(True)
        self._build()

    # building
    def _symbol_button(self, symbol, action, size, symbol_size, tip):
        button = _MusicPlayerButton.buttonWithImage_target_action_(gfx.symbol(symbol, symbol_size, "bold"),
                                                                   self.target, action)
        button.setBordered_(False)
        button.setFrame_(AppKit.NSMakeRect(0, 0, size, size))
        button.setWantsLayer_(True)
        button.layer().setCornerRadius_(size / 2)
        button.setContentTintColor_(_ns(INK, 0.92))
        button.setToolTip_(tip)
        self.view.addSubview_(button)
        return button

    def _build(self) -> None:
        mint = tuple(gfx.accent())
        art = self.h - 2 * PAD
        self.art_size = art
        self.art = _MusicPlayerArt.alloc().initWithFrame_(AppKit.NSMakeRect(PAD, PAD, art, art))
        self.art.owner = self
        self.art.setImageScaling_(AppKit.NSImageScaleProportionallyUpOrDown)
        self.art.setWantsLayer_(True)
        self.art.layer().setCornerRadius_(max(8.0, art * 0.14))
        self.art.layer().setMasksToBounds_(True)
        self.art.layer().setBackgroundColor_(_cg((1, 1, 1), 0.07))
        self.art.setToolTip_("Open the player")
        self.view.addSubview_(self.art)
        self.note = AppKit.NSImageView.imageViewWithImage_(gfx.symbol("music.note", art * 0.32, "medium"))
        self.note.setContentTintColor_(_ns(INK, 0.28))
        self.note.setFrame_(AppKit.NSMakeRect(PAD, PAD, art, art))
        self.view.addSubview_(self.note)
        self.badge = AppKit.NSImageView.alloc().initWithFrame_(AppKit.NSMakeRect(PAD + art - 17, PAD - 3, 20, 20))
        self.badge.setImageScaling_(AppKit.NSImageScaleProportionallyUpOrDown)
        shadow = AppKit.NSShadow.alloc().init()
        shadow.setShadowBlurRadius_(3)
        shadow.setShadowColor_(_ns((0, 0, 0), 0.6))
        self.badge.setShadow_(shadow)
        self.view.addSubview_(self.badge)

        self.title = _label(15, AppKit.NSFontWeightBold, rounded=True)
        self.view.addSubview_(self.title)
        self.artist = _label(12, AppKit.NSFontWeightMedium, DIM)
        self.view.addSubview_(self.artist)
        self.bars = _MusicPlayerBars.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 11, 10))
        self.view.addSubview_(self.bars)

        self.elapsed = _label(10, AppKit.NSFontWeightMedium, DIM, digits=True)
        self.view.addSubview_(self.elapsed)
        self.remaining = _label(10, AppKit.NSFontWeightMedium, DIM, digits=True)
        self.remaining.setAlignment_(AppKit.NSTextAlignmentRight)
        self.view.addSubview_(self.remaining)
        self.bar = _MusicPlayerBar.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 100, 12))
        self.bar.owner = self
        self.bar.setWantsLayer_(True)
        self.track = Quartz.CALayer.layer()
        self.track.setBackgroundColor_(_cg((1, 1, 1), 0.16))
        self.track.setCornerRadius_(1.5)
        self.bar.layer().addSublayer_(self.track)
        self.fill = Quartz.CALayer.layer()
        self.fill.setBackgroundColor_(_cg(mint))
        self.fill.setCornerRadius_(1.5)
        self.bar.layer().addSublayer_(self.fill)
        self.knob = Quartz.CALayer.layer()
        self.knob.setBounds_(Quartz.CGRectMake(0, 0, 9, 9))
        self.knob.setCornerRadius_(4.5)
        self.knob.setBackgroundColor_(_cg(INK))
        self.knob.setOpacity_(0.0)
        self.bar.layer().addSublayer_(self.knob)
        self.view.addSubview_(self.bar)

        self.prev_button = self._symbol_button("backward.fill", "previous:", 28, 12, "Previous")
        self.play_button = self._symbol_button("play.fill", "toggle:", 32, 15, "Play / pause")
        self.next_button = self._symbol_button("forward.fill", "next:", 28, 12, "Next")
        self.speaker = AppKit.NSImageView.imageViewWithImage_(gfx.symbol("speaker.wave.2.fill", 10, "semibold"))
        self.speaker.setContentTintColor_(_ns(DIM))
        self.view.addSubview_(self.speaker)
        self.slider = _MusicPlayerSlider.sliderWithValue_minValue_maxValue_target_action_(
            50, 0, 100, self.target, "volume:")
        self.slider.setControlSize_(AppKit.NSControlSizeMini)
        self.slider.setContinuous_(True)
        try:
            self.slider.setTrackFillColor_(_ns(mint))
        except Exception:
            pass
        self.view.addSubview_(self.slider)
        for view in (self.speaker, self.slider):
            view.setAlphaValue_(0.0)
        self._layout()

    def _layout(self) -> None:
        w, h, art = self.w, self.h, self.art_size
        x0 = PAD + art + 14
        rw = w - x0 - PAD
        top = h - PAD + 2
        self.title.setFrame_(AppKit.NSMakeRect(x0, top - 20, rw - self.close_room, 20))
        self._artist_frame = (x0, top - 38, rw, 16)
        self.bars.setFrameOrigin_(AppKit.NSMakePoint(x0, top - 34))
        row = PAD + 32
        self.elapsed.setFrame_(AppKit.NSMakeRect(x0, row - 2, 34, 13))
        self.remaining.setFrame_(AppKit.NSMakeRect(x0 + rw - 40, row - 2, 40, 13))
        self.bar.setFrame_(AppKit.NSMakeRect(x0 + 36, row - 2, rw - 36 - 42, 13))
        self._layout_controls()
        self._paint_progress()

    def _layout_controls(self) -> None:
        x0 = PAD + self.art_size + 14
        rw = self.w - x0 - PAD
        group = 28 + 8 + 32 + 8 + 28
        volume_w = 14 + 4 + 64
        left = x0 + (rw - group) / 2 if not self.hover else x0 - 2
        y = PAD - 4
        for button, x in ((self.prev_button, left), (self.play_button, left + 36), (self.next_button, left + 76)):
            size = button.frame().size.width
            mover = button.animator() if self._laid_out else button
            mover.setFrameOrigin_(AppKit.NSMakePoint(x, y + (32 - size) / 2))
        self._laid_out = True
        vx = x0 + rw - volume_w
        self.speaker.setFrame_(AppKit.NSMakeRect(vx, y + 10, 14, 12))
        self.slider.setFrame_(AppKit.NSMakeRect(vx + 18, y + 8, 64, 16))

    # painting
    def _paint_progress(self) -> None:
        info = self.info
        duration = float(info.get("duration") or 0)
        position = float(info.get("position") or 0)
        if self._scrub_at is not None and duration:
            position = self._scrub_at * duration
        fraction = min(1.0, position / duration) if duration else 0.0
        bw = self.bar.frame().size.width
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        self.track.setFrame_(Quartz.CGRectMake(0, 5, bw, 3))
        self.fill.setFrame_(Quartz.CGRectMake(0, 5, max(3.0, bw * fraction) if duration else 0, 3))
        self.knob.setPosition_(Quartz.CGPointMake(bw * fraction, 6.5))
        self.knob.setOpacity_(1.0 if (self.hover and duration) else 0.0)
        Quartz.CATransaction.commit()
        self.elapsed.setStringValue_(_clock(position) if duration else "")
        self.remaining.setStringValue_(f"-{_clock(duration - position)}" if duration else "")

    def update(self, info: dict | None = None) -> None:
        """Repaint from a music.now_playing()/cached() dict (None: the cached one)."""
        if info is None:
            info = _music().cached()
        self.info = dict(info or {})
        info = self.info
        playing = info.get("state") == "playing"
        sig = (info.get("app"), info.get("title"), info.get("artist"), info.get("artwork"), info.get("state"),
               bool(info.get("running", info.get("app"))))
        if sig != self._sig:
            self._sig = sig
            title = info.get("title") or ""
            if not info.get("app"):
                title, artist = "Nothing playing", "Say “play some music”"
            elif not title:
                title, artist = f"{info.get('app')}", "Nothing playing"
            else:
                artist = info.get("artist") or info.get("album") or ""
            self.title.setStringValue_(title)
            self.title.setToolTip_(f"{title} — {info['album']}" if info.get("album") else title)
            self.artist.setStringValue_(artist)
            image = _image(info.get("artwork") or "")
            if image is not self.art.image():
                fade = Quartz.CATransition.animation()
                fade.setType_(Quartz.kCATransitionFade)
                fade.setDuration_(0.3)
                self.art.layer().addAnimation_forKey_(fade, "fade")
                self.art.setImage_(image)
            self.note.setHidden_(image is not None)
            bundle = BUNDLES.get(info.get("app") or "")
            self.badge.setImage_(gfx.app_icon("bundle", bundle) if bundle else None)
            self.badge.setHidden_(not bundle)
            self.play_button.setImage_(gfx.symbol("pause.fill" if playing else "play.fill", 15, "bold"))
            self.bars.setPlaying_(playing)
            has_track = bool(info.get("title"))
            self.bars.setHidden_(not has_track)
            x, y, w, h = self._artist_frame
            shift = 17 if has_track else 0
            self.artist.setFrame_(AppKit.NSMakeRect(x + shift, y, w - shift, h))
            for button in (self.prev_button, self.next_button, self.play_button):
                button.setEnabled_(bool(info.get("app")))
                button.setAlphaValue_(1.0 if info.get("app") else 0.35)
        if info.get("volume") is not None and time.monotonic() > self._volume_until:
            self.slider.setIntValue_(int(info["volume"]))
        self.slider.setEnabled_(info.get("volume") is not None)
        self._paint_progress()

    def set_hover(self, hover: bool) -> None:
        hover = bool(hover)
        if hover == self.hover:
            return
        self.hover = hover
        AppKit.NSAnimationContext.beginGrouping()
        AppKit.NSAnimationContext.currentContext().setDuration_(0.18)
        self._layout_controls()
        for view in (self.speaker, self.slider):
            view.animator().setAlphaValue_(1.0 if hover and self.info.get("volume") is not None else 0.0)
        AppKit.NSAnimationContext.endGrouping()
        self._paint_progress()

    # actions (the music work runs off the main thread)
    def act(self, what: str) -> None:
        music = _music()
        app = self.info.get("app") or ""
        if what == "toggle":
            playing = self.info.get("state") == "playing"
            self.info["state"] = "paused" if playing else "playing"      # answer the click at once
            self._sig = None
            self.update(self.info)
            _in_background(music.pause if playing else music.resume, app)
        elif what == "next":
            _in_background(music.next_track, app)
        elif what == "previous":
            _in_background(music.previous_track, app)

    def volume(self, level: int) -> None:
        """The slider moved: send the newest level at most ~7 times a second while it is dragged."""
        self._volume_until = time.monotonic() + 1.5
        if not self.info.get("app"):
            return
        first = self._volume_pending is None
        self._volume_pending = int(level)
        if first:
            AppHelper.callLater(0.15, self._send_volume)

    def _send_volume(self) -> None:
        level, self._volume_pending = self._volume_pending, None
        if level is not None:
            self.info["volume"] = level
            _in_background(_music().set_volume, str(level), self.info.get("app") or "")

    def scrub(self, fraction: float, done: bool) -> None:
        duration = float(self.info.get("duration") or 0)
        if not duration or not self.info.get("app"):
            return
        self._scrub_at = None if done else fraction
        if done:
            self.info["position"] = fraction * duration
            _in_background(_music().seek, str(round(fraction * duration, 1)), self.info.get("app") or "")
        self._paint_progress()

    def open_app(self) -> None:
        app = self.info.get("app") or _music().pick_app()
        bundle = BUNDLES.get(app)
        if bundle:
            subprocess.Popen(["open", "-b", bundle])

    def close_clicked(self) -> None:
        if self.on_close is not None:
            self.on_close()


# --- the card by the orb -------------------------------------------------------------------------

class MusicPlayer:
    def __init__(self) -> None:
        self.panel = None
        self.player: PlayerView | None = None
        self.opened = False            # wanted on screen (it may be stepped aside for the island)
        self.suspended = False         # stepped aside while the island or the chat is out
        self.dismissed = False         # the × was clicked: away until the music stops and starts again
        self.hide_at = 0.0
        self.hud = None
        self.timer = None

    def _build(self) -> None:
        panel = _MusicPlayerPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            AppKit.NSMakeRect(0, 0, W, H),
            AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel,
            AppKit.NSBackingStoreBuffered, False)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(AppKit.NSColor.clearColor())
        panel.setHasShadow_(True)
        panel.setLevel_(AppKit.NSStatusWindowLevel)
        panel.setHidesOnDeactivate_(False)
        panel.setReleasedWhenClosed_(False)
        panel.setBecomesKeyOnlyIfNeeded_(True)
        panel.setCollectionBehavior_(AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                                     | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
                                     | AppKit.NSWindowCollectionBehaviorIgnoresCycle)
        panel.setAppearance_(AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameDarkAqua))
        try:
            from mint.ui.effects import SHARING
            panel.setSharingType_(SHARING)
        except Exception:
            pass
        root = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, W, H))
        root.setWantsLayer_(True)
        layer = root.layer()
        layer.setBackgroundColor_(_cg(SURFACE))
        layer.setCornerRadius_(RADIUS)
        layer.setBorderColor_(_cg((1, 1, 1), 0.09))
        layer.setBorderWidth_(0.5)
        layer.setMasksToBounds_(True)
        panel.setContentView_(root)
        self.player = PlayerView(W, H, close_room=gfx.CLOSE + 6, on_close=self.close_clicked)
        root.addSubview_(self.player.view)
        self.x_button = gfx.close_button(self.player.target, "close:", "Close")
        self.x_button.setFrameOrigin_(AppKit.NSMakePoint(W - 10 - gfx.CLOSE, H - 10 - gfx.CLOSE))
        root.addSubview_(self.x_button)
        self.card_target = _MusicPlayerTarget.alloc().initWithOwner_(self)
        self.panel, self.root = panel, root

    def _ensure_timer(self) -> None:
        if self.timer is None:
            self.timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                1 / 10, self.card_target, "tick:", None, True)

    # where
    def _hud(self):
        if self.hud is None:
            try:
                from mint.ui.island import island
                self.hud = island.hud
            except Exception:
                self.hud = None
        return self.hud

    def _frame(self):
        """The card's frame and the point it grows from (the orb, or the notch)."""
        screen = AppKit.NSScreen.mainScreen().visibleFrame()
        left, bottom = screen.origin.x, screen.origin.y
        right, top = left + screen.size.width, bottom + screen.size.height
        try:
            from mint.ui import notch
            if notch.active() and getattr(notch.notch, "top", None):
                n = notch.notch
                x, y = n.cx - W / 2, (n.top - getattr(n, "nh", 32)) - 10 - H
                return AppKit.NSMakeRect(x, y, W, H), (n.cx, n.top - getattr(n, "nh", 32) / 2)
        except Exception:
            pass
        hud = self._hud()
        center = hud.orb_center() if hud is not None else None
        if center is None:
            center = (right - 46, bottom + 46)
        cx, cy = center
        on_right = cx > left + screen.size.width / 2
        upper = cy > bottom + screen.size.height / 2
        x = cx + ORB_R - W if on_right else cx - ORB_R
        if abs(cx - (left + screen.size.width / 2)) < 80:
            x = cx - W / 2                               # top-centre orb
        y = cy - ORB_R - GAP - H if upper else cy + ORB_R + GAP
        x = min(max(x, left + 8), right - W - 8)
        y = min(max(y, bottom + 8), top - H - 8)
        return AppKit.NSMakeRect(x, y, W, H), (cx, cy)

    def _morph(self, opening: bool) -> None:
        frame, (cx, cy) = self._frame()
        layer = self.root.layer()
        ax = min(1.0, max(0.0, (cx - frame.origin.x) / W))
        ay = 1.0 if cy > frame.origin.y + H / 2 else 0.0
        layer.setAnchorPoint_(Quartz.CGPointMake(ax, ay))
        layer.setPosition_(Quartz.CGPointMake(ax * W, ay * H))
        grow = Quartz.CASpringAnimation.animationWithKeyPath_("transform.scale")
        grow.setDamping_(18)
        grow.setStiffness_(240)
        grow.setMass_(1)
        grow.setFromValue_(0.15 if opening else 1.0)
        grow.setToValue_(1.0 if opening else 0.15)
        grow.setDuration_(0.5 if opening else 0.24)
        grow.setFillMode_(Quartz.kCAFillModeForwards)
        grow.setRemovedOnCompletion_(opening)
        layer.addAnimation_forKey_(grow, "morph")
        AppKit.NSAnimationContext.beginGrouping()
        AppKit.NSAnimationContext.currentContext().setDuration_(0.16 if opening else 0.2)
        self.panel.animator().setAlphaValue_(1.0 if opening else 0.0)
        AppKit.NSAnimationContext.endGrouping()

    def _blocked(self) -> bool:
        """The island or the chat is out of the orb right now: the card steps aside."""
        hud = self._hud()
        if hud is None:
            return False
        try:
            return bool(getattr(hud, "island_active", False)) or bool(hud.console_open())
        except Exception:
            return False

    # showing
    def _appear(self) -> None:
        self.player.update(_music().cached())
        frame, _ = self._frame()
        self.panel.setFrame_display_(frame, False)
        self.panel.setAlphaValue_(0.0)
        self.panel.orderFrontRegardless()
        self._morph(True)

    def _vanish(self) -> None:
        self._morph(False)

        def done():
            if self.opened and not self.suspended:
                return                                  # shown again meanwhile
            self.panel.orderOut_(None)
            self.root.layer().removeAnimationForKey_("morph")
        AppHelper.callLater(0.26, done)

    def show(self, reason: str = "asked") -> None:
        if self.panel is None:
            self._build()
        self._ensure_timer()
        if reason == "asked":
            self.dismissed = False
        elif self.dismissed:
            return
        self.hide_at = 0.0
        if reason == "asked":
            self.hide_at = time.monotonic() + AUTO_HIDE + 4       # nothing may be playing yet
        _music().want_fast(3.0)
        if self.opened:
            if not self.suspended:
                self.player.update(_music().cached())
            return
        self.opened = True
        self.suspended = self._blocked()
        if not self.suspended:
            self._appear()

    def hide(self) -> None:
        if not self.opened:
            return
        self.opened = False
        if not self.suspended:
            self._vanish()
        self.suspended = False

    def close_clicked(self) -> None:
        self.dismissed = True
        self.hide()

    def is_open(self) -> bool:
        return self.opened and not self.suspended

    # every tenth of a second
    def tick(self) -> None:
        if not self.opened or self.panel is None:
            return
        info = _music().cached()
        blocked = self._blocked()
        if blocked != self.suspended:
            self.suspended = blocked
            if blocked:
                self._vanish()
            else:
                self._appear()
        if self.suspended:
            return
        _music().want_fast(2.5)
        mouse = AppKit.NSEvent.mouseLocation()
        frame = self.panel.frame()
        on_card = AppKit.NSPointInRect(mouse, frame)
        self.player.set_hover(on_card)
        gfx.track_close(self.x_button, on_card, mouse)
        self.player.update(info)
        wanted, _ = self._frame()
        if abs(wanted.origin.x - frame.origin.x) > 1 or abs(wanted.origin.y - frame.origin.y) > 1:
            self.panel.setFrame_display_animate_(wanted, True, False)          # the orb moved
        now = time.monotonic()
        if info.get("state") == "playing":
            self.hide_at = 0.0
        elif not self.hide_at:
            self.hide_at = now + AUTO_HIDE
        elif now > self.hide_at and not on_card:
            self.hide()

    # from the music watcher (main thread)
    def changed(self, before: dict, after: dict) -> None:
        started = after.get("state") == "playing" and before.get("state") != "playing"
        if after.get("state") != "playing" and before.get("state") == "playing":
            self.dismissed = False                      # stopped: the next start may show the card again
        if started and _auto():
            self.show(reason="auto")
        if self.opened and not self.suspended and self.player is not None:
            self.player.update(after)


def _auto() -> bool:
    try:
        from mint.core import prefs
        return prefs.get("music_player_auto") is not False
    except Exception:
        return True


card = MusicPlayer()


# --- the notch's pieces (another module places them) ---------------------------------------------

_views: list[PlayerView] = []
_compact: dict = {}


class _Driver:
    """Keeps the notch's views fresh twice a second (and the card's timer when there is no card yet)."""

    def __init__(self) -> None:
        self.target = _MusicPlayerTarget.alloc().initWithOwner_(self)
        self.timer = None
        self.ticks = 0

    def start(self) -> None:
        if self.timer is None:
            self.timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                0.5, self.target, "tick:", None, True)

    def tick(self) -> None:
        self.ticks += 1
        info = None
        for player in list(_views):
            window = player.view.window()
            if window is None:
                if player.in_window:                    # taken out of the notch: forget it
                    _views.remove(player)
                continue
            player.in_window = True
            if window.isVisible() and not player.view.isHiddenOrHasHiddenAncestor():
                info = info or _music().cached()
                _music().want_fast(1.5)
                player.update(info)
        if _compact:
            info = info or _music().cached()
            _paint_compact(info)


_driver_instance: list = []


def _driver():
    if not _driver_instance:
        _driver_instance.append(_Driver())
    return _driver_instance[0].target


def player_view(width: float, height: float):
    """For the notch: (NSView, update). The view is the whole player on a clear background, `width` x
    `height` points (about 320-380 x 100-120). It refreshes itself twice a second while it is in a
    visible window; update(info) repaints at once with a music.cached()/now_playing() dict."""
    attach()
    player = PlayerView(width, height)
    _views.append(player)
    player.update(_music().cached())
    return player.view, player.update


def _paint_compact(info: dict) -> None:
    playing = info.get("state") == "playing"
    image = _image(info.get("artwork") or "")
    art = _compact.get("art_view")
    if art is not None and art.image() is not image:
        art.setImage_(image)
    bars = _compact.get("bars_view")
    if bars is not None:
        bars.setPlaying_(playing)
    _compact.update(playing=playing, state=info.get("state") or "stopped", title=info.get("title") or "",
                    artist=info.get("artist") or "", app=info.get("app") or "", artwork=info.get("artwork") or "",
                    image=image, show=bool(info.get("title")) and info.get("state") in ("playing", "paused"))


def compact_info(height: float = 22) -> dict:
    """For the notch's closed state: {show, playing, state, title, artist, app, artwork (path), image
    (NSImage or None), art_view (a rounded NSImageView, height x height), bars_view (the dancing bars,
    about 0.8 x 0.55 of height)}. The two views are made once and kept current by this module; add them
    to the notch's wings (artwork on the left, bars on the right, as the iPhone does) while `show`."""
    attach()
    size = float(height)
    if _compact.get("size") != size:
        art = AppKit.NSImageView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, size, size))
        art.setImageScaling_(AppKit.NSImageScaleProportionallyUpOrDown)
        art.setWantsLayer_(True)
        art.layer().setCornerRadius_(max(4.0, size * 0.22))
        art.layer().setMasksToBounds_(True)
        art.layer().setBackgroundColor_(_cg((1, 1, 1), 0.08))
        bars = _MusicPlayerBars.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, round(size * 0.8), round(size * 0.55)))
        _compact.update(size=size, art_view=art, bars_view=bars)
    _paint_compact(_music().cached())
    return dict(_compact)


# --- wiring -------------------------------------------------------------------------------------

_attached = [False]


def attach() -> None:
    """Main thread, once: listen to the music watcher and start the refresh timer."""
    if _attached[0]:
        return
    _attached[0] = True
    music = _music()
    music.start_service()
    music.on_change(lambda before, after: AppHelper.callAfter(card.changed, before, after))
    _driver()
    _driver_instance[0].start()


def show(reason: str = "asked") -> None:
    """Any thread: bring the card out (reason 'asked' also undoes a ×; 'auto' respects it)."""
    def run():
        attach()
        card.show(reason)
    AppHelper.callAfter(run)


def hide() -> None:
    AppHelper.callAfter(card.hide)


def is_open() -> bool:
    return card.is_open()
