"""8 Oct: "What I can do" on its own (Settings ▸ General, the menu bar): the feature page from setup, whenever the user
wants it. Done, Esc or the close button just put it away - setup's own state (onboarded, the saved page) is untouched."""
import pytest

pytest.importorskip("AppKit")
try:
    from mint.ui import onboarding
except ImportError:
    from mint import onboarding


class _Window:
    def __init__(self):
        self.visible, self.ordered_out = True, False

    def isVisible(self):
        return self.visible

    def animator(self):
        return self

    def setAlphaValue_(self, value):
        pass

    def orderOut_(self, _):
        self.ordered_out = True


def _tour(monkeypatch):
    ob = onboarding.Onboarding()
    ob.window, ob.view, ob.tour_only = _Window(), object(), True
    ob.page = onboarding.PAGES.index("tour")
    left, saved = [], []
    monkeypatch.setattr(ob, "_leave", lambda: left.append(ob.page))
    monkeypatch.setattr(onboarding.prefs, "set", lambda key, value: saved.append((key, value)))
    monkeypatch.setattr(onboarding.AppHelper, "callLater", lambda delay, fn, *args: fn(*args))
    return ob, left, saved


@pytest.mark.parametrize("how", ["next", "skip", "closed"])
def test_putting_the_tour_away_leaves_setup_alone(monkeypatch, how):
    ob, left, saved = _tour(monkeypatch)
    getattr(ob, how)()
    assert left and saved == []                        # no "onboarded", no "onboarding_page"
    assert ob.view is None
    if how != "closed":
        assert ob.window.ordered_out


def test_back_does_nothing_on_its_own(monkeypatch):
    ob, left, saved = _tour(monkeypatch)
    went = []
    monkeypatch.setattr(ob, "_go", lambda page: went.append(page))
    ob.back()
    assert went == [] and saved == []


def test_setup_itself_still_saves_where_it_was(monkeypatch):
    ob, left, saved = _tour(monkeypatch)
    ob.tour_only = False
    ob.closed()
    assert saved == [("onboarding_page", onboarding.PAGES.index("tour"))]
