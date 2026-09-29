"""Mint's undo journal, offline: fake inverses, a journal in a temporary folder, nothing on the Mac touched.

    .venv/bin/python tests/checks/undo.py
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from mint.tools import undo  # noqa: E402

results: list[bool] = []


def case(name: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if not ok and detail else ""))


folder = pathlib.Path(tempfile.mkdtemp(prefix="mint-undo-bench-"))
undo.JOURNAL = folder / "undo.jsonl"

# A fake switch with a hook, like macctl.brightness: setting it records the way back.
state = {"level": 50, "log": []}


def set_level(value: int) -> str:
    was = state["level"]
    state["level"] = value
    undo.record("level", f"level {was} → {value}", {"kind": "fake_level", "value": was})
    return f"Level {value}."


@undo.inverse("fake_level")
def _fake_level(spec):
    set_level(spec["value"])
    state["log"].append(spec["value"])
    return f"level back to {spec['value']}."


@undo.inverse("fake_fail")
def _fake_fail(spec):
    return "FAILED: it is not there any more."


@undo.inverse("fake_boom")
def _fake_boom(spec):
    raise RuntimeError("boom")


def rows():
    return undo._load()


print("record, list, undo")
set_level(70)
case("record writes one entry", len(rows()) == 1 and rows()[0]["kind"] == "level")
case("journal file is private (0600)", oct(undo.JOURNAL.stat().st_mode & 0o777) == "0o600")
case("list shows it", "level 50 → 70" in undo.listing())
said = undo.undo_last()
case("undo last runs the inverse", state["level"] == 50 and "Undid level 50 → 70" in said, said)
case("the undo itself is not journalled as a new entry", len(rows()) == 1)
case("the entry is marked undone with a redo", rows()[0]["undone"] and rows()[0]["redo"]["value"] == 70)
case("nothing left to undo says so", undo.undo_last().startswith("Nothing to undo"))

print("redo")
said = undo.redo()
case("redo puts it forward again", state["level"] == 70 and said.startswith("Redid"), said)
case("after redo it can be undone again", undo.undo_last().startswith("Undid") and state["level"] == 50)
undo.redo()

print("several, in order")
set_level(10)
set_level(20)
set_level(30)
said = undo.undo_last(3)
case("undo 3 goes newest first", state["log"][-3:] == [20, 10, 70] and state["level"] == 70, str(state["log"]))
case("each undo says what it reversed", said.count("Undid") == 3, said)
said = undo.undo_last(5)
case("asking for more than there is says so", "nothing older" in said and state["level"] == 50, said)

print("things that can't be undone")
set_level(80)
undo.record("typed", "typing \"hi\" in Slack and pressing Return", None, "a sent message can't be taken back.")
said = undo.undo_last()
case("refuses plainly, with the reason", said.startswith("Can't undo typing") and "sent message" in said, said)
case("and offers the thing before it", "level 50 → 80" in said and state["level"] == 80, said)
said = undo.undo_last()
case("the next undo reaches the older one", said.startswith("Undid level 50 → 80") and state["level"] == 50, said)
case("redo skips the refused entry", undo.redo().startswith("Redid level 50 → 80"))
undo.undo_last()

print("match")
set_level(60)
undo.record("window", "Safari window → left half", {"kind": "fake_fail"})
said = undo.undo_last(match="the level change")
case("match picks the older matching entry", said.startswith("Undid level 50 → 60") and state["level"] == 50, said)
said = undo.undo_last(match="email")
case("no match explains what can't be undone", "Sent messages and emails" in said, said)

print("failures")
said = undo.undo_last()
case("a failing inverse is reported, entry kept", said.startswith("Couldn't undo Safari") and
     not rows()[-1]["undone"], said)
undo.record("x", "the boom thing", {"kind": "fake_boom"})
said = undo.undo_last()
case("an inverse that raises is reported, not raised", said.startswith("Couldn't undo the boom thing"), said)
undo.record("x", "an unknown kind", {"kind": "no_such_kind"})
case("an unknown kind is refused", "doesn't know how" in undo.undo_last())

print("together")
undo.JOURNAL.unlink()
with undo.together("level", "two level changes as one"):
    set_level(1)
    set_level(2)
case("a batch is one entry of steps", len(rows()) == 1 and rows()[0]["inverse"]["kind"] == "steps")
said = undo.undo_last()
case("its steps are undone in reverse", state["level"] == 50 and state["log"][-2:] == [1, 50], str(state["log"]))
case("redo of a batch replays forward", undo.redo().startswith("Redid") and state["level"] == 2)
undo.undo_last()

print("settled (a module's own undo)")
set_level(33)
undo.settled("level")
case("settled marks the newest of that kind undone", rows()[-1]["undone"] and state["level"] == 33)
state["level"] = 50

print("journal limits")
undo.JOURNAL.unlink()
for i in range(60):
    undo.record("x", f"thing {i}", {"kind": "fake_fail"})
case("only the last 50 are kept", len(rows()) == 50 and rows()[0]["summary"] == "thing 10")
lines = undo.JOURNAL.read_text().splitlines()
old = json.loads(lines[0])
old["at"] = time.time() - 25 * 3600
lines[0] = json.dumps(old)
lines.insert(1, "not json {")
undo.JOURNAL.write_text("\n".join(lines) + "\n")
case("entries over 24 h old are dropped, bad lines skipped", len(rows()) == 49 and rows()[0]["summary"] == "thing 11")
undo.record("x", "not json-able", {"kind": "fake_fail", "bad": object()})
case("an inverse that isn't JSON is not recorded (and doesn't raise)", rows()[-1]["summary"] == "thing 59")

print("survives a restart")
import importlib  # noqa: E402

undo.JOURNAL.unlink()
set_level(90)
path = undo.JOURNAL
importlib.reload(undo)
undo.JOURNAL = path
undo.inverse("fake_level")(_fake_level)
said = undo.undo_last()
case("a fresh module undoes what the old one recorded", said.startswith("Undid level 50 → 90") and
     state["level"] == 50, said)

print("files (in a temporary folder)")
work = folder / "files"
work.mkdir()
(work / "a.txt").write_text("hello")
(work / "a.txt").rename(work / "b.txt")
undo.record("file", "renaming a.txt -> b.txt", {"kind": "file_move", "from": str(work / "b.txt"), "to": str(work / "a.txt")})
said = undo.undo_last()
case("file_move puts a rename back", (work / "a.txt").exists() and not (work / "b.txt").exists(), said)
case("and redo renames it again", undo.redo().startswith("Redid") and (work / "b.txt").exists())
(work / "a.txt").write_text("someone else's")
said = undo.undo_last()
case("file_move never overwrites", said.startswith("Couldn't") and (work / "a.txt").read_text() == "someone else's", said)
(work / "log.txt").write_text("one\n")
before = (work / "log.txt").stat().st_size
with open(work / "log.txt", "a") as f:
    f.write("two\n")
undo.record("file", "appending to log.txt", {"kind": "file_truncate", "path": str(work / "log.txt"), "size": before,
                                             "after": (work / "log.txt").stat().st_size})
undo.undo_last()
case("file_truncate takes appended text out", (work / "log.txt").read_text() == "one\n")
(work / "new").mkdir()
undo.record("file", "making the folder new", {"kind": "folder_remove", "path": str(work / "new")})
undo.undo_last()
case("folder_remove removes an empty folder Mint made", not (work / "new").exists())
(work / "doc.txt").write_text("v2")
(work / "doc.bak").write_text("v1")
undo.record("file", "overwriting doc.txt", {"kind": "file_restore", "path": str(work / "doc.txt"),
                                            "backup": str(work / "doc.bak"), "mtime": (work / "doc.txt").stat().st_mtime})
undo.undo_last()
case("file_restore brings the previous version back", (work / "doc.txt").read_text() == "v1")
said = undo.redo()
case("and redo brings the new one back", (work / "doc.txt").read_text() == "v2", said)

print("tidy delegates to its own undo")
from mint.tools import tidy  # noqa: E402

(work / "Sorted").mkdir()
(work / "Sorted" / "x.pdf").write_text("pdf")
journal = folder / "tidy.json"
journal.write_text(json.dumps({"folder": str(work), "moves": [{"from": str(work / "x.pdf"),
                                                                "to": str(work / "Sorted" / "x.pdf")}]}))
undo.record("tidy", "tidying files (1 file moved)", {"kind": "tidy", "journal": str(journal)})
said = undo.undo_last()
case("tidy's undo moved the file back", (work / "x.pdf").exists() and not (work / "Sorted").exists(), said)
case("and marked that tidy-up done", journal.with_suffix(".undone").exists())
case("undoing it twice is refused", tidy.undo(str(journal)).startswith("FAILED"))

print("edit_selection falls back to the clipboard")
from mint.tools import clipboard as clip_tools
from mint.tools import rewrite  # noqa: E402

pasted = []
real_undo, real_put = rewrite.undo, clip_tools._put_text
rewrite.undo = lambda: "The edited text is no longer selected and a different app is in front."
clip_tools._put_text = pasted.append
try:
    undo.record("edit_selection", "rewriting the selection in Mail (more formal)",
                {"kind": "edit_selection", "original": "hey", "new": "Dear Sir", "app": "Mail"})
    said = undo.undo_last()
finally:
    rewrite.undo, clip_tools._put_text = real_undo, real_put
case("original goes on the clipboard, and it says so", pasted == ["hey"] and "on the clipboard" in said, said)

print("app_keys (typed text) refuses when the app is gone")
undo.record("typed", "typing \"x\" in NoSuchApp", {"kind": "app_keys", "app": "NoSuchApp", "title": "", "key": "z"})
said = undo.undo_last()
case("no key is pressed into another app", "isn't open any more" in said, said)

print("Mint's settings (fake settings store)")
from mint.core import prefs  # noqa: E402

store = {"theme": "mint"}
real_get, real_set = prefs.get, prefs.set


def fake_set(key, value):
    store[key] = value
    undo._on_setting(key, value)


prefs.get, prefs.set = (lambda k: store.get(k)), fake_set
try:
    undo._seen.clear()
    undo._seen.update({"theme": "mint"})
    fake_set("theme", "rose")
    undo.after_preference({"setting": "theme", "value": "rose"}, "Theme is now rose.")
    case("a voice change is journalled", "theme setting (mint → rose)" in undo.listing())
    said = undo.undo_last()
    case("undo restores the old value", store["theme"] == "mint" and said.startswith("Undid"), said)
    case("redo sets it again", undo.redo().startswith("Redid") and store["theme"] == "rose")
    fake_set("theme", "blue")       # changed in the Settings window: no after_preference
    case("a change not made by voice is not journalled", "rose → blue" not in undo.listing())
    undo.after_preference({"setting": "activity_timeline", "value": "clear"}, "Deleted.")
    case("deleting the timeline is recorded as irreversible", "Can't undo deleting the activity timeline"
         in undo.undo_last())
finally:
    prefs.get, prefs.set = real_get, real_set
    undo._seen.clear()

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
