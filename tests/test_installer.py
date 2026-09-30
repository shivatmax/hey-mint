"""The one-line installer and the DMG's layout files: cheap checks that catch the mistakes that only show up on
someone else's Mac (a `$NAME…` read as one variable name in a C locale broke the script in Terminal once)."""
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "packaging" / "install.sh"


def test_installer_parses():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_installer_has_no_variable_glued_to_a_non_ascii_character():
    # bash in a C locale reads the UTF-8 bytes of "…" as part of the name: "$DEST…" is an unbound variable.
    bad = re.findall(r"\$[A-Za-z_][A-Za-z_0-9]*[^\x00-\x7f]", SCRIPT.read_text())
    assert not bad, bad


def test_installer_is_executable_and_named_in_the_guide():
    assert SCRIPT.stat().st_mode & 0o111
    assert "hey-mint.pages.dev/install.sh" in (ROOT / "README.md").read_text()


def test_dmg_files_are_there():
    assert (ROOT / "packaging" / "dmg" / "background.png").exists()
    assert (ROOT / "packaging" / "dmg" / "background@2x.png").exists()
    settings = (ROOT / "packaging" / "dmg_settings.py").read_text()
    assert "Applications" in settings and "icon_locations" in settings


def test_edit_menu_source_is_part_of_the_launcher():
    # The first-run key prompt must accept a paste: its Edit menu and pasteable fields (launcher/ear/EditMenu.swift).
    swift = ROOT / "launcher" / "ear"
    assert (swift / "EditMenu.swift").exists()
    assert "PasteableSecureTextField" in (swift / "Packaged.swift").read_text()
    assert "EditMenu.install()" in (swift / "main.swift").read_text()
