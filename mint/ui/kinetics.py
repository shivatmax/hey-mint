"""One motion vocabulary for Mint's surfaces (plans/grokbot-notch-overhaul.md, phase 0).

Everything here is Core Animation: Python only sets targets, the render server animates at 60/120 Hz.

    spring(layer, key, old, new, preset="gentle")   # CASpringAnimation from a preset
    stagger(layers, fn, step=0.035)                  # fn(layer, delay) per layer
    wipe_in(layer, seconds=0.32, delay=0.0)          # gradient mask sweeps left -> right
    blur_in(layer, seconds=0.32, delay=0.0)          # from 0.96 scale + blur + 0 opacity
    fade(layer, on, seconds=0.2, delay=0.0)
    fly(layer, from_point, to_point, from_scale, to_scale, preset)
    shake(layer, px=5, seconds=0.45)
    pulse(layer, key="glow", seconds=1.4)            # gentle opacity breathing, forever
    reduce_motion()                                  # macOS "Reduce motion" or Animation: Minimal -> short fades
    calm()                                           # Animation: Calm -> no bounce, fewer idle extras

Presets follow the notch spec Mint already used: critically damped, so nothing overshoots past the
notch's edge, with a "bouncy" for playful moments.
"""

from __future__ import annotations

import time

import AppKit
import Quartz

PRESETS = {
    # stiffness, damping, mass
    "snappy": (300.0, 2 * (300.0 ** 0.5), 1.0),     # critically damped, ~0.35 s
    "gentle": (150.0, 2 * (150.0 ** 0.5), 1.0),     # critically damped, ~0.5 s
    "bouncy": (220.0, 14.0, 1.0),                   # a little overshoot, settles ~0.7 s
    "wobbly": (180.0, 9.0, 1.0),                    # pokes and slaps
}

_reduce = {"value": False, "checked": 0.0}


def level() -> str:
    """Settings ▸ Appearance & Sound ▸ Animation: "full", "calm" or "minimal"."""
    try:
        from mint.core import prefs
        value = prefs.get("motion")
    except Exception:
        value = None
    return value if value in ("full", "calm", "minimal") else "full"


def calm() -> bool:
    """Gentle motion: no bouncy overshoot, fewer idle extras (Animation: Calm, or Minimal)."""
    return level() != "full" or reduce_motion()


def reduce_motion() -> bool:
    """Fades only: macOS Accessibility > Display > Reduce motion (cached for 2 s), or Animation: Minimal."""
    if level() == "minimal":
        return True
    now = time.monotonic()
    if now - _reduce["checked"] > 2.0:
        _reduce["checked"] = now
        try:
            _reduce["value"] = bool(AppKit.NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion())
        except Exception:
            _reduce["value"] = False
    return _reduce["value"]


def _value(v):
    if isinstance(v, tuple) and len(v) == 2:
        return AppKit.NSValue.valueWithPoint_(AppKit.NSMakePoint(*v))
    if isinstance(v, tuple) and len(v) == 4:
        return AppKit.NSValue.valueWithRect_(AppKit.NSMakeRect(*v))
    return v


def media_now(layer=None) -> float:
    return Quartz.CACurrentMediaTime()


def spring(layer, key: str, old, new, preset: str = "gentle", delay: float = 0.0, anim_key: str | None = None):
    """Animate `key` from old to new with a spring; the model value is set to `new`.
    Under Reduce motion it becomes a 0.18 s ease (no travel bounce)."""
    if reduce_motion():
        anim = Quartz.CABasicAnimation.animationWithKeyPath_(key)
        anim.setDuration_(0.18)
        anim.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithName_(
            Quartz.kCAMediaTimingFunctionEaseOut))
    else:
        if preset in ("bouncy", "wobbly") and level() == "calm":
            preset = "gentle"                       # Calm: settle without the bounce
        stiffness, damping, mass = PRESETS.get(preset, PRESETS["gentle"])
        anim = Quartz.CASpringAnimation.animationWithKeyPath_(key)
        anim.setStiffness_(stiffness)
        anim.setDamping_(damping)
        anim.setMass_(mass)
        anim.setDuration_(min(anim.settlingDuration(), 1.4))
    anim.setFromValue_(_value(old))
    anim.setToValue_(_value(new))
    if delay:
        anim.setBeginTime_(media_now() + delay)
        anim.setFillMode_(Quartz.kCAFillModeBackwards)
    Quartz.CATransaction.begin()
    Quartz.CATransaction.setDisableActions_(True)
    layer.setValue_forKeyPath_(_value(new), key)
    Quartz.CATransaction.commit()
    layer.addAnimation_forKey_(anim, anim_key or key)
    return anim


def basic(layer, key: str, old, new, seconds: float = 0.2, delay: float = 0.0,
          timing=None, anim_key: str | None = None, keep: bool = True):
    anim = Quartz.CABasicAnimation.animationWithKeyPath_(key)
    anim.setFromValue_(_value(old))
    anim.setToValue_(_value(new))
    anim.setDuration_(seconds)
    anim.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithName_(
        timing or Quartz.kCAMediaTimingFunctionEaseOut))
    if delay:
        anim.setBeginTime_(media_now() + delay)
        anim.setFillMode_(Quartz.kCAFillModeBackwards)
    if keep:
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        layer.setValue_forKeyPath_(_value(new), key)
        Quartz.CATransaction.commit()
    layer.addAnimation_forKey_(anim, anim_key or key)
    return anim


def fade(layer, on: bool, seconds: float = 0.2, delay: float = 0.0) -> None:
    old = layer.presentationLayer().opacity() if layer.presentationLayer() is not None else layer.opacity()
    basic(layer, "opacity", old, 1.0 if on else 0.0, seconds, delay, anim_key="kin-fade")


def stagger(layers, fn, step: float = 0.035, start: float = 0.0) -> None:
    """fn(layer, delay) for each layer, `step` seconds apart."""
    for i, layer in enumerate(layers):
        if layer is not None:
            fn(layer, start + i * step)


def blur_in(layer, seconds: float = 0.32, delay: float = 0.0, scale: float = 0.96, radius: float = 6.0) -> None:
    """Rise in from slightly small, blurred and clear (the Grok Bot card entrance). The blur only
    works where the hosting view has layerUsesCoreImageFilters; otherwise it's scale + fade."""
    basic(layer, "opacity", 0.0, 1.0, seconds * 0.8, delay, anim_key="kin-bi-o")
    if reduce_motion():
        return
    spring(layer, "transform.scale", scale, 1.0, "snappy", delay, anim_key="kin-bi-s")
    try:
        blur = Quartz.CIFilter.filterWithName_("CIGaussianBlur")
        if blur is None:
            return
        blur.setDefaults()
        blur.setName_("kinblur")
        layer.setFilters_([blur])
        anim = Quartz.CABasicAnimation.animationWithKeyPath_("filters.kinblur.inputRadius")
        anim.setFromValue_(radius)
        anim.setToValue_(0.0)
        anim.setDuration_(seconds)
        anim.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithName_(Quartz.kCAMediaTimingFunctionEaseOut))
        if delay:
            anim.setBeginTime_(media_now() + delay)
            anim.setFillMode_(Quartz.kCAFillModeBackwards)
        blur.setValue_forKey_(0.0, "inputRadius")
        layer.addAnimation_forKey_(anim, "kin-bi-b")
    except Exception:
        pass


def wipe_in(layer, seconds: float = 0.32, delay: float = 0.0, soft: float = 0.25) -> None:
    """Reveal a (text) layer left to right with a soft gradient edge."""
    if reduce_motion():
        basic(layer, "opacity", 0.0, 1.0, 0.18, delay, anim_key="kin-wipe-o")
        return
    bounds = layer.bounds()
    mask = Quartz.CAGradientLayer.layer()
    mask.setFrame_(bounds)
    mask.setStartPoint_(Quartz.CGPointMake(0, 0.5))
    mask.setEndPoint_(Quartz.CGPointMake(1, 0.5))
    black = AppKit.NSColor.blackColor().CGColor()
    clear = AppKit.NSColor.clearColor().CGColor()
    mask.setColors_([black, black, clear, clear])
    mask.setLocations_([0.0, 1.0, 1.0 + soft, 2.0])
    layer.setMask_(mask)
    anim = Quartz.CABasicAnimation.animationWithKeyPath_("locations")
    anim.setFromValue_([-soft, -soft, 0.0, 1.0])
    anim.setToValue_([0.0, 1.0, 1.0 + soft, 2.0])
    anim.setDuration_(seconds)
    anim.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithName_(Quartz.kCAMediaTimingFunctionEaseOut))
    if delay:
        anim.setBeginTime_(media_now() + delay)
        anim.setFillMode_(Quartz.kCAFillModeBackwards)
    mask.addAnimation_forKey_(anim, "wipe")

    def unmask():
        if layer.mask() is mask:
            layer.setMask_(None)
    from PyObjCTools import AppHelper
    AppHelper.callLater(delay + seconds + 0.05, unmask)


def fly(layer, from_point, to_point, from_scale: float = 1.0, to_scale: float = 1.0,
        preset: str = "gentle", delay: float = 0.0) -> None:
    """Matched geometry: move a layer between two layouts (position + scale on one spring)."""
    spring(layer, "position", tuple(from_point), tuple(to_point), preset, delay, anim_key="kin-fly-p")
    if from_scale != to_scale or from_scale != 1.0:
        spring(layer, "transform.scale", from_scale, to_scale, preset, delay, anim_key="kin-fly-s")


def shake(layer, px: float = 5.0, seconds: float = 0.45) -> None:
    if reduce_motion():
        return
    anim = Quartz.CAKeyframeAnimation.animationWithKeyPath_("transform.translation.x")
    anim.setValues_([0, -px, px, -px * 0.7, px * 0.7, -px * 0.3, 0])
    anim.setDuration_(seconds)
    layer.addAnimation_forKey_(anim, "kin-shake")


def pulse(layer, key: str = "kin-pulse", seconds: float = 1.4, low: float = 0.35, high: float = 1.0) -> None:
    anim = Quartz.CABasicAnimation.animationWithKeyPath_("opacity")
    anim.setFromValue_(low)
    anim.setToValue_(high)
    anim.setDuration_(seconds / 2)
    anim.setAutoreverses_(True)
    anim.setRepeatCount_(float("inf"))
    anim.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithName_(Quartz.kCAMediaTimingFunctionEaseInEaseOut))
    layer.addAnimation_forKey_(anim, key)


def pop(layer, preset: str = "bouncy", start: float = 0.4, delay: float = 0.0) -> None:
    """Scale in from `start` to 1 with a springy settle (badges, dots, chips)."""
    spring(layer, "transform.scale", start, 1.0, preset, delay, anim_key="kin-pop")
    basic(layer, "opacity", 0.0, 1.0, 0.14, delay, anim_key="kin-pop-o")
