"""An app's own ways in (links, scripting, files, Electron) read from its bundle, plus routes known to work."""
import plistlib

try:
    from mint.tools import app_routes
except ImportError:
    from mint import app_routes


def _bundle(tmp_path, name, plist, electron=False):
    app = tmp_path / f"{name}.app"
    (app / "Contents").mkdir(parents=True)
    with open(app / "Contents" / "Info.plist", "wb") as fh:
        plistlib.dump(plist, fh)
    if electron:
        (app / "Contents" / "Frameworks" / "Electron Framework.framework").mkdir(parents=True)
    return app


def test_the_bundle_says_how_to_get_in(tmp_path):
    app = _bundle(tmp_path, "Notey", {"CFBundleIdentifier": "com.example.notey", "CFBundleName": "Notey",
                                      "CFBundleURLTypes": [{"CFBundleURLSchemes": ["notey"]}],
                                      "NSAppleScriptEnabled": True,
                                      "CFBundleDocumentTypes": [{"CFBundleTypeExtensions": ["note", "md"]}]},
                  electron=True)
    facts = app_routes.probe(app)
    assert facts["schemes"] == ["notey"] and facts["scriptable"] and facts["electron"]
    assert facts["files"] == ["md", "note"]


def test_a_card_prefers_known_routes_and_never_claims_progress(tmp_path, monkeypatch):
    app = _bundle(tmp_path, "Telegram", {"CFBundleIdentifier": "ru.keepcoder.Telegram", "CFBundleName": "Telegram"})
    monkeypatch.setattr(app_routes, "_bundle", lambda name: app)
    monkeypatch.setattr(app_routes, "_cache", {})
    card = app_routes.card("Telegram")
    assert "tg://resolve?domain=NAME" in card and "never say it is progressing" in card
    other = _bundle(tmp_path, "Blank", {"CFBundleIdentifier": "com.example.blank"})
    monkeypatch.setattr(app_routes, "_bundle", lambda name: other)
    assert app_routes.card("Blank") == ""                      # nothing useful to say: nothing said
