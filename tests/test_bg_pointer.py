"""Mint's own pointer (bg_pointer): clicks, drags and wheel turns posted to one app's window through SkyLight, so the
user's cursor never moves. Fails closed - no SkyLight, a stale or Mint-owned window, a drop outside the window - and
then ground uses the real pointer and says so. The window server is faked: no event reaches a real app."""
import types

import pytest
import Quartz
from PIL import Image

try:
    from mint.screen import bg_pointer, effect, ground
except ImportError:
    from mint import bg_pointer, effect, ground

TG, OTHER = 4242, 5151


def _row(number, pid, x, y, w, h, name="App", layer=0, onscreen=True):
    return {"kCGWindowLayer": layer, "kCGWindowAlpha": 1.0, "kCGWindowNumber": number, "kCGWindowOwnerPID": pid,
            "kCGWindowOwnerName": name, "kCGWindowIsOnscreen": onscreen,
            "kCGWindowBounds": {"X": x, "Y": y, "Width": w, "Height": h}}


@pytest.fixture
def world(monkeypatch):
    """A screen with Telegram's window under a Notes window on its right half; a fake SkyLight that records what
    would be posted; pictures of the window that change only when `w.changes` says so."""
    import os
    w = types.SimpleNamespace(
        rows=[_row(900, os.getpid(), 0, 0, 1440, 900, "python"),                 # Mint's click-through glow
              _row(31, OTHER, 700, 0, 740, 900, "Notes"),
              _row(12, TG, 0, 0, 1000, 800, "Telegram"),
              _row(13, TG, 0, 0, 1440, 30, "Telegram", layer=25)],
        posted=[], fields={}, changes=True, pointer=(50.0, 50.0), real=[], front=TG, moves_pointer=False, local={})

    def window_list(option, number):
        if option == Quartz.kCGWindowListOptionIncludingWindow:
            return [r for r in w.rows if r["kCGWindowNumber"] == number]
        return [r for r in w.rows if r.get("kCGWindowIsOnscreen", True)]

    monkeypatch.setattr(Quartz, "CGWindowListCopyWindowInfo", window_list)
    monkeypatch.setattr(bg_pointer, "_click_through", lambda: {900})
    monkeypatch.setattr(bg_pointer, "_off", {"why": ""})
    monkeypatch.setattr(bg_pointer, "_reached", set())
    monkeypatch.setattr(bg_pointer, "_front_pid", lambda: w.front)
    clock = [0.0]                             # a fake clock: no real waiting, and the polls still end
    monkeypatch.setattr(bg_pointer, "time", types.SimpleNamespace(
        sleep=lambda s: clock.__setitem__(0, clock[0] + s), monotonic=lambda: clock[0], time_ns=lambda: 123456789))
    monkeypatch.setattr(effect, "pointer_at", lambda: w.pointer)

    def set_field(ptr, field, value):
        w.fields.setdefault(ptr.value, {})[field] = value

    def window_location(ptr, x, y):
        w.local[ptr.value] = (round(x), round(y))

    spi = types.SimpleNamespace(post=lambda pid, ptr: None, window_location=window_location,
                                set_field=set_field, can_post=True, can_focus=False)
    monkeypatch.setattr(bg_pointer, "_spi", spi)

    def send(event, target):
        p = Quartz.CGEventGetLocation(event)
        ptr = bg_pointer._ptr(event).value
        w.posted.append((target.pid, Quartz.CGEventGetType(event), (round(p.x), round(p.y)),
                         dict(w.fields.get(ptr, {})), w.local.get(ptr)))
        if w.moves_pointer:
            w.pointer = (round(p.x), round(p.y))
    monkeypatch.setattr(bg_pointer, "_send", send)

    def look(target):
        shade = 200 if (w.changes and w.posted) else 10
        return Image.new("L", (40, 30), shade)
    monkeypatch.setattr(bg_pointer, "look", look)
    monkeypatch.setattr(ground, "_post", lambda kind, point, *a: w.real.append((kind, round(point.x), round(point.y))))
    monkeypatch.setattr(ground, "owner_at", lambda x, y: TG)
    monkeypatch.setattr(effect, "give_pointer_back", lambda was, clicked: False)
    return w


def test_the_target_is_the_window_on_top_skipping_mint_overlays(world):
    top = bg_pointer.target_at(300, 300)
    assert (top.pid, top.window_id, top.app) == (TG, 12, "Telegram")             # the glow is click-through
    assert bg_pointer.target_at(800, 300).app == "Notes"                         # what the real pointer would hit
    assert bg_pointer.target_at(800, 300, pid=TG).window_id == 12                # the app's own window, underneath
    assert bg_pointer.target_at(1300, 850, pid=TG) is None                       # no Telegram window there


def test_mint_s_own_window_is_never_a_target(world, monkeypatch):
    monkeypatch.setattr(bg_pointer, "_click_through", lambda: set())             # a real Mint window on top
    assert bg_pointer.target_at(300, 300) is None


def test_a_stale_window_is_refused_before_anything_is_sent(world):
    target = bg_pointer.target_at(300, 300)
    assert bg_pointer.still(target, 300, 300) == ""
    assert bg_pointer.still(target, 1200, 300) == "the window no longer holds that point"
    world.rows[2]["kCGWindowIsOnscreen"] = False
    assert "minimised" in bg_pointer.still(target, 300, 300)
    world.rows[2]["kCGWindowOwnerPID"] = OTHER
    assert bg_pointer.still(target, 300, 300) == "the window changed owner"
    world.rows.pop(2)
    assert bg_pointer.still(target, 300, 300) == "the window is gone"


def test_a_click_is_posted_to_that_window_only_with_its_id_stamped(world):
    sent = bg_pointer.click(300, 200)
    assert sent.landed and sent.checked and sent.route == "background_pointer"
    kinds = [k for _, k, _, _, _ in world.posted]
    assert kinds == [Quartz.kCGEventMouseMoved, Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp,
                     Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp]
    assert [where for _, _, where, _, _ in world.posted] == [(300, 200), (-1, -1), (-1, -1), (300, 200), (300, 200)]
    for pid, _, _, fields, _ in world.posted:
        assert pid == TG
        assert fields[bg_pointer.F_TARGET_PID] == TG
        assert fields[bg_pointer.F_WINDOW] == fields[bg_pointer.F_UNDER] == fields[bg_pointer.F_UNDER_HANDLER] == 12
    assert len({f[bg_pointer.F_GROUP] for _, _, _, f, _ in world.posted}) == 1      # one gesture
    # The point inside the window, from its top-left corner (the screen point is dropped on macOS 27).
    assert world.posted[0][4] == world.posted[3][4] == (300, 200)                   # Telegram's window is at 0, 0
    assert world.pointer == (50.0, 50.0) and world.real == []


def test_a_double_click_counts_up_and_a_right_click_has_no_primer(world):
    bg_pointer.click(300, 200, count=2)
    states = [f[bg_pointer.F_CLICK_STATE] for _, k, w, f, _ in world.posted if w == (300, 200)
              and k != Quartz.kCGEventMouseMoved]
    assert states == [1, 1, 2, 2]
    world.posted.clear()
    bg_pointer.click(300, 200, button="right")
    assert [k for _, k, _, _, _ in world.posted] == [Quartz.kCGEventMouseMoved, Quartz.kCGEventRightMouseDown,
                                                  Quartz.kCGEventRightMouseUp]
    assert world.posted[1][3][bg_pointer.F_BUTTON] == 1


def test_no_skylight_means_nothing_is_sent(world):
    world_spi = bg_pointer._spi
    world_spi.post = None
    world_spi.can_post = False
    sent = bg_pointer.click(300, 200)
    assert not sent.sent and not sent.landed and "SkyLight" in sent.why and world.posted == []


def test_mouse_click_uses_mint_s_pointer_and_says_so(world):
    assert ground.mouse_click(300, 200, spark=False, pid=TG) == "background_pointer"
    assert world.real == [] and world.pointer == (50.0, 50.0)


def test_mouse_click_falls_back_to_the_real_pointer_and_learns_the_app(world, monkeypatch):
    world.changes = False                     # the app ignores Mint's pointer; the real pointer's click changes it
    monkeypatch.setattr(bg_pointer, "look", lambda target: Image.new("L", (40, 30), 200 if world.real else 10))
    assert ground.mouse_click(300, 200, spark=False, pid=TG) == "global_input"
    assert [k for k, _, _ in world.real][-1] == Quartz.kCGEventLeftMouseUp
    assert not bg_pointer.reaches(TG)
    world.posted.clear()
    world.real.clear()
    assert ground.mouse_click(300, 200, spark=False, pid=TG) == "global_input"
    assert world.posted == [] and world.real                                     # straight to the real pointer


def test_a_drag_stays_in_one_window(world):
    sent = bg_pointer.drag(100, 100, 400, 300, seconds=0.25)
    kinds = [k for _, k, _, _, _ in world.posted]
    assert sent.landed and kinds[:2] == [Quartz.kCGEventMouseMoved, Quartz.kCGEventLeftMouseDown]
    assert kinds[-1] == Quartz.kCGEventLeftMouseUp and Quartz.kCGEventLeftMouseDragged in kinds
    assert world.posted[-1][2] == (400, 300)
    world.posted.clear()
    out = bg_pointer.drag(100, 100, 1300, 850)
    assert not out.sent and out.why == "the drop point is outside that window" and world.posted == []


def test_points_are_stamped_inside_the_window_it_goes_to(world):
    world.rows[2]["kCGWindowBounds"] = {"X": 100, "Y": 50, "Width": 600, "Height": 700}
    bg_pointer.click(400, 250)
    assert world.posted[0][4] == (300, 200) and world.posted[-1][4] == (300, 200)


def test_the_keyboard_focus_is_never_borrowed(world, monkeypatch):
    world.front = OTHER                       # another app in front: Telegram's window is behind
    taken = []
    monkeypatch.setattr(bg_pointer, "focus_without_raise", lambda target: taken.append(target))
    assert bg_pointer.click(300, 200).landed and taken == []


def test_a_wheel_turn_is_aimed_at_the_window(world):
    sent = bg_pointer.scroll(300, 200, dy=-5, ticks=3)
    wheels = [p for p in world.posted if p[1] == Quartz.kCGEventScrollWheel]
    assert sent.landed and len(wheels) == 3 and all(p[3][bg_pointer.F_WINDOW] == 12 for p in wheels)


def test_a_route_that_moves_the_real_pointer_is_switched_off(world):
    world.moves_pointer = True
    bg_pointer.click(300, 200)
    assert not bg_pointer.available() and "moved the real pointer" in bg_pointer.why_off()


def test_the_result_names_mint_s_pointer():
    line = effect.Effect(effect.CONFIRMED, "Clicked 'Search'", "background_pointer", "background",
                         ["the window changed"]).render()
    assert "Mint's own pointer" in line and "did not move" in line


def test_a_wheel_turn_that_moves_nothing_in_a_known_app_keeps_the_real_pointer_still(world, monkeypatch):
    try:
        from mint.tools import fastinput
    except ImportError:
        from mint import fastinput
    monkeypatch.setattr(fastinput, "has_accessibility", lambda: True)
    monkeypatch.setattr(fastinput, "_front_window_point", lambda: Quartz.CGPointMake(300, 200))
    hid = []
    monkeypatch.setattr(fastinput, "_post", lambda event, pid=None: hid.append(event))
    world.changes = False
    assert "at its end" not in fastinput.scroll("down", 2) and hid          # not known yet: the real wheel is tried
    hid.clear()
    world.posted.clear()
    world.changes = True
    assert bg_pointer.click(300, 200).landed                                 # now Mint's pointer is known to reach it
    world.changes = False
    world.posted.clear()
    out = fastinput.scroll("down", 2)
    assert "at its end" in out and hid == []
