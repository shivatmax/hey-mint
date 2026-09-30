"""Pictures with Apple's Image Playground: "make an image of a giraffe in a hat", "draw it as
a sketch", "make four of them", "now give it a red scarf", "open it in Image Playground".

The ImageCreator API is closed to apps like Mint on macOS 27 ("ImageCreator is deprecated and no
longer available for this application"), but Image Playground's own App Intent - the Shortcuts
action "Create Image" (com.apple.GenerativePlaygroundApp.GenerateImageIntent: prompt, style,
optional image, saveToLibrary) - runs from any shortcut. So Mint builds two small shortcuts, signs
them and opens them in Shortcuts once; the user clicks "Add Shortcut" (Mint can't), and from then
on `shortcuts run` makes the pictures with no window at all:

    Mint Generate Image   input: a JSON file {"prompt", "style"}              -> the image
    Mint Edit Image       input: that JSON file, then the image to start from -> the new image

The style goes in as a fixed entity per branch (If style is sketch -> Create Image in Sketch, ...):
a Shortcuts entity parameter can't take text from a variable. Pictures are saved as PNG in the
Mint folder's Images sub-folder and shown on a card; `open_app` hands one to the Image Playground
app for what only it can do by hand (Visual Edit, Add Caption, Describe a Change on its own images).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import plistlib
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from mint.core import config

log = logging.getLogger("mint.tools.imagegen")

SHORTCUTS = {"create": "Mint Draw", "edit": "Mint Redraw"}
# Image Playground's style ids (ImagePlaygroundStyle.id); the first is the default.
STYLES = {"animation": "Animation", "illustration": "Illustration", "sketch": "Sketch"}
APP = "Image Playground"
_ACTION = "com.apple.GenerativePlaygroundApp.GenerateImageIntent"
_DESCRIPTOR = {"AppIntentIdentifier": "GenerateImageIntent", "BundleIdentifier": "com.apple.GenerativePlaygroundApp",
               "Name": "Image Playground", "TeamIdentifier": "0000000000"}
_TIMEOUT = 180
_state: dict = {"last": None, "offered": 0.0, "prompt": "", "style": ""}
_prompts: dict[str, str] = {}     # picture path -> the prompt it was drawn from (for "another" / "improve")
_job = threading.Lock()           # one picture at a time: the card and the voice share it


# --- The shortcuts -----------------------------------------------------------------------

def _ref(output_uuid: str, name: str) -> dict:
    return {"OutputName": name, "OutputUUID": output_uuid, "Type": "ActionOutput"}


def _attachment(value: dict) -> dict:
    return {"Value": value, "WFSerializationType": "WFTextTokenAttachment"}


def _token_string(value: dict) -> dict:
    return {"Value": {"attachmentsByRange": {"{0, 1}": value}, "string": "￼"},
            "WFSerializationType": "WFTextTokenString"}


def _action(identifier: str, **params) -> dict:
    return {"WFWorkflowActionIdentifier": identifier, "WFWorkflowActionParameters": params}


def _workflow(edit: bool) -> dict:
    """The plist of 'Mint Generate Image' (edit=False) or 'Mint Edit Image' (edit=True)."""
    actions, source, image = [], _attachment({"Type": "ExtensionInput"}), None
    if edit:            # input: [spec.json, picture] - the spec first, the picture last
        first, last = str(uuid.uuid4()).upper(), str(uuid.uuid4()).upper()
        actions += [_action("is.workflow.actions.getitemfromlist", UUID=first, WFItemSpecifier="First Item",
                            WFInput=source),
                    _action("is.workflow.actions.getitemfromlist", UUID=last, WFItemSpecifier="Last Item",
                            WFInput=_attachment({"Type": "ExtensionInput"}))]
        source, image = _attachment(_ref(first, "Item from List")), _attachment(_ref(last, "Item from List"))
    spec, prompt, style = (str(uuid.uuid4()).upper() for _ in range(3))
    actions += [_action("is.workflow.actions.detect.dictionary", UUID=spec, WFInput=source),
                _action("is.workflow.actions.getvalueforkey", UUID=prompt, WFGetDictionaryValueType="Value",
                        WFDictionaryKey="prompt", WFInput=_attachment(_ref(spec, "Dictionary"))),
                _action("is.workflow.actions.getvalueforkey", UUID=style, WFGetDictionaryValueType="Value",
                        WFDictionaryKey="style", WFInput=_attachment(_ref(spec, "Dictionary")))]

    made = str(uuid.uuid4()).upper()
    # No style: a fixed style entity written by Mint was rejected ("Please choose a value for each parameter",
    # 30 Sep); left out, Image Playground uses its default. The style asked for goes into the prompt instead.
    params = {"AppIntentDescriptor": dict(_DESCRIPTOR), "UUID": made,
              "prompt": _token_string(_ref(prompt, "Dictionary Value")), "saveToLibrary": "never"}
    if image is not None:
        params["image"] = image
    actions += [_action(_ACTION, **params),
                _action("is.workflow.actions.output", WFOutput=_token_string(_ref(made, "Image")))]
    del style
    inputs = ["WFGenericFileContentItem", "WFImageContentItem", "WFStringContentItem", "WFRichTextContentItem",
              "WFPDFContentItem", "WFURLContentItem"]
    return {"WFWorkflowActions": actions, "WFWorkflowClientVersion": "4042.0.2.2",
            "WFWorkflowMinimumClientVersion": 900, "WFWorkflowMinimumClientVersionString": "900",
            "WFWorkflowIcon": {"WFWorkflowIconStartColor": 3980825855, "WFWorkflowIconGlyphNumber": 59784},
            "WFWorkflowImportQuestions": [], "WFWorkflowInputContentItemClasses": inputs, "WFWorkflowTypes": [],
            "WFWorkflowOutputContentItemClasses": [], "WFWorkflowHasOutputFallback": False,
            "WFWorkflowHasShortcutInputVariables": True, "WFQuickActionSurfaces": []}


def build_shortcuts(folder: Path | None = None) -> dict[str, Path]:
    """Write and sign both shortcuts. -> {name: signed .shortcut path}; raises RuntimeError."""
    folder = folder or Path(tempfile.mkdtemp(prefix="mint-imagegen-"))
    built = {}
    for kind, name in SHORTCUTS.items():
        raw, signed = folder / (name.replace(" ", "_") + ".wflow"), folder / (name + ".shortcut")
        raw.write_bytes(plistlib.dumps(_workflow(kind == "edit"), fmt=plistlib.FMT_BINARY))
        done = subprocess.run(["shortcuts", "sign", "--mode", "anyone", "--input", str(raw), "--output", str(signed)],
                              capture_output=True, text=True, timeout=90)
        if done.returncode != 0 or not signed.exists():
            raise RuntimeError((done.stderr or done.stdout or "shortcuts sign failed").strip()[:200])
        built[name] = signed
    return built


def _installed() -> set[str]:
    done = subprocess.run(["shortcuts", "list"], capture_output=True, text=True, timeout=20)
    return {line.strip() for line in done.stdout.splitlines() if line.strip()}


def _offer(missing: list[str]) -> str:
    """Open the missing shortcuts in Shortcuts (at most once every 3 minutes), for one 'Add Shortcut' click each."""
    ask = (f"Opened {' and '.join(repr(n) for n in missing)} in Shortcuts: the user clicks 'Add Shortcut' "
           f"{'on each' if len(missing) > 1 else ''} (once), then asks again.")
    if time.time() - _state["offered"] < 180:
        return ("NOT DONE YET: waiting for the user to click 'Add Shortcut' in Shortcuts for " +
                " and ".join(repr(n) for n in missing) + ". Tell them in one sentence; don't click it for them.")
    try:
        built = build_shortcuts()
    except Exception as error:
        return f"FAILED: Mint could not prepare its Image Playground shortcut ({error}). Tell the user."
    for name in missing:
        subprocess.run(["open", str(built[name])], check=False)
        time.sleep(1.5)
    _state["offered"] = time.time()
    return ("NOT DONE YET: Image Playground makes pictures for Mint through a small shortcut. " + ask +
            " Tell them that in one sentence; do not click it for them and do not try other ways.")


def _ready(kinds: tuple[str, ...]) -> str:
    """'' when the shortcuts for `kinds` are there, else the offer message."""
    have = _installed()
    missing = [SHORTCUTS[k] for k in SHORTCUTS if SHORTCUTS[k] not in have]
    if any(SHORTCUTS[k] in missing for k in kinds):
        return _offer(missing)
    return ""


# --- Running them -------------------------------------------------------------------------

def images_folder() -> Path:
    return config.storage("Images")


def _new_file(prompt: str) -> Path:
    words = re.sub(r"[^\w\s-]", "", prompt).split()
    base = (" ".join(words[:6])[:48].strip() or "Image") + f" {dt.datetime.now():%Y-%m-%d at %H.%M.%S}"
    path, n = images_folder() / f"{base}.png", 2
    while path.exists():
        path, n = images_folder() / f"{base} ({n}).png", n + 1
    return path


# Image Playground's own messages (ImagePlaygroundInternal Localizable.loctable), by what they mean.
_REFUSED = ("guardrails rejected", "safety rejected", "unable to use that", "not allowed", "rejected prompt")
_OFF = ("turn on apple intelligence", "apple intelligence required", "image playground unavailable",
        "image playground is not available", "is not available in your", "couldn’t be downloaded",
        "couldn't be downloaded", "generation service is not available", "unavailable on shared devices",
        "update required", "is out of date")
_LATER = ("usage limit", "timed out", "try again later", "can't be completed right now", "can’t be completed right now",
          "capabilities are unavailable", "system state")


def _explain(text: str) -> str:
    low = text.lower()
    if any(w in low for w in _REFUSED):
        return ("REFUSED: Image Playground would not make this picture (its content rules). Tell the user and "
                f"suggest describing it differently. ({text[:160]})")
    if "selected style is not available" in low:
        return f"FAILED: that style is not available in Image Playground right now; try another style. ({text[:120]})"
    if any(w in low for w in _OFF) or "apple intelligence" in low:
        return ("FAILED: Image Playground is not available - Apple Intelligence must be on (System Settings > Apple "
                f"Intelligence & Siri) and its image support downloaded. ({text[:160]})")
    if any(w in low for w in _LATER):
        return f"FAILED: Image Playground can't do it right now; try again in a little while. ({text[:160]})"
    if "could not be read" in low or "smaller image" in low:
        return f"FAILED: Image Playground could not use that picture ({text[:160]}). Try another one."
    if any(w in low for w in ("couldn’t find", "couldn't find", "no shortcut")):
        _state["offered"] = 0.0
        return f"FAILED: the shortcut is missing or broken ({text[:160]}). Ask again to set it up."
    return f"FAILED: Image Playground stopped with: {text[:300]}"


def _is_image(path: Path) -> bool:
    try:
        head = path.read_bytes()[:12]
    except OSError:
        return False
    return (head.startswith((b"\x89PNG", b"\xff\xd8\xff", b"MM\x00*", b"II*\x00", b"GIF8"))
            or head[4:8] == b"ftyp" or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"))


def _generate(prompt: str, style: str, image: Path | None, name: str = "") -> tuple[Path | None, str]:
    """One picture through the shortcut. -> (saved PNG, '') or (None, message). `name`: what the file is
    named after (the picture's prompt, for an edit)."""
    kind = "edit" if image is not None else "create"
    work = Path(tempfile.mkdtemp(prefix="mint-imagegen-run-"))
    spec, out = work / "spec.json", work / "out.png"
    styled = f"{prompt}, in {style} style" if style and style.lower() not in prompt.lower() else prompt
    spec.write_text(json.dumps({"prompt": styled, "style": style}))
    command = ["shortcuts", "run", SHORTCUTS[kind], "--input-path", str(spec)]
    if image is not None:
        command += ["--input-path", str(image)]
    command += ["--output-path", str(out), "--output-type", "public.png"]
    started = time.time()
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=_TIMEOUT)
    except subprocess.TimeoutExpired:
        shutil.rmtree(work, ignore_errors=True)
        return None, (f"FAILED: Image Playground took more than {_TIMEOUT // 60} minutes and was stopped. Try again, "
                      "maybe with a simpler description.")
    log.info("imagegen %s %s in %.1f s rc=%s", kind, style, time.time() - started, done.returncode)
    try:
        if done.returncode != 0 or not out.exists() or not _is_image(out):
            said = (done.stderr or done.stdout or "").strip()
            return None, _explain(said) if said else "FAILED: the shortcut ran but gave back no picture."
        target = _new_file(name or prompt)
        if out.read_bytes()[:4] == b"\x89PNG":
            shutil.move(str(out), target)
        else:
            convert = subprocess.run(["sips", "-s", "format", "png", str(out), "--out", str(target)],
                                     capture_output=True, text=True, timeout=60)
            if convert.returncode != 0 or not target.exists():
                return None, f"FAILED: could not save the picture as PNG ({convert.stderr.strip()[:120]})."
        _state["last"] = target
        _prompts[str(target)] = name or prompt
        return target, ""
    finally:
        shutil.rmtree(work, ignore_errors=True)


def last_image() -> Path | None:
    """The picture made last: this session's, else the newest in the Images folder."""
    last = _state.get("last")
    if last and Path(last).exists():
        return Path(last)
    try:
        pictures = [p for p in images_folder().iterdir() if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".heic")]
    except OSError:
        return None
    return max(pictures, key=lambda p: p.stat().st_mtime) if pictures else None


def _style(value) -> str:
    style = str(value or "").strip().lower()
    for key in STYLES:
        if style and (style == key or style.startswith(key[:4])):
            return key
    return next(iter(STYLES))




# --- The picture card (image_card.py): shown after every picture, and what voice can do to it ----------

_BUSY = ("BUSY: Mint is still drawing the last picture (a few seconds). Wait until it is on the card, then do "
         "this; tell the user in a few words.")
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".heic", ".tiff", ".tif")


def _on_card(name: str, *args) -> None:
    try:
        from mint.ui import image_card
        getattr(image_card, name)(*args)
    except Exception as error:
        log.info("image card %s: %s", name, error)


def _card_picture() -> Path | None:
    """The picture on the open card, else None."""
    try:
        from mint.ui import image_card
        return image_card.current()
    except Exception:
        return None


def _card_prompt_style() -> tuple[str, str]:
    try:
        from mint.ui import image_card
        if image_card.is_open():
            return image_card.current_prompt(), image_card.current_style()
    except Exception:
        pass
    return "", ""


def _prompt_of(path: Path) -> str:
    """What a picture was drawn from: remembered, else its file name without the date."""
    known = _prompts.get(str(path))
    if known:
        return known
    return re.sub(r"\s*\d{4}-\d{2}-\d{2} at [\d.]+( \(\d+\))?$", "", path.stem).strip() or path.stem


def file_name(prompt: str) -> str:
    """A good file name for a picture of `prompt`: 'Small red fox in the snow.png'."""
    words = re.sub(r"[^\w\s-]", "", str(prompt or "")).split()
    while len(words) > 1 and words[0].lower() in ("a", "an", "the"):
        words = words[1:]
    name = ""
    for word in words:
        if len(name) + len(word) + 1 > 44:
            break
        name = f"{name} {word}".strip()
    name = name or "Image"
    return name[:1].upper() + name[1:] + ".png"


def _stamp(started: float) -> str:
    return f"{time.time() - started:.0f} s"


def _create(prompt: str, style: str, count: int, fresh: bool, note: str) -> str:
    """Draw `count` pictures of `prompt` onto the card (a fresh card, or new versions on the open one).
    The caller holds _job."""
    words = "Drawing…" if count == 1 else f"Drawing 1 of {count}…"
    if fresh:
        _on_card("start", prompt, style, words, None, prompt[:70])
    else:
        _on_card("work", words, prompt[:70])
    waiting = _ready(("create",))
    if waiting:
        _on_card("fail", waiting)
        return waiting
    made, problem, started = [], "", time.time()
    for n in range(count):
        path, problem = _generate(prompt, style, None)
        if path is None:
            break
        made.append(path)
        more = f"Drawing {n + 2} of {count}…" if n + 1 < count else ""
        _on_card("add", path, prompt, note or STYLES[style], more, style)
    _state.update(prompt=prompt, style=style)
    if not made:
        _on_card("fail", problem)
        return problem
    if problem:
        _on_card("work", "")
    said = (f"Made {len(made)} {STYLES[style].lower()} picture{'s' if len(made) > 1 else ''} of '{prompt[:80]}' in "
            f"{_stamp(started)}, saved: " + "; ".join(str(p) for p in made) + ". It is on the picture card, where "
            "the user can type a change, save, copy or draw another.")
    return said + (f" Stopped early: {problem}" if problem else "")


def create(prompt: str, style: str = "", count: int = 1) -> str:
    prompt = " ".join(str(prompt or "").split())
    if not prompt:
        return "FAILED: say what the picture should show."
    style, count = _style(style), max(1, min(4, int(count or 1)))
    if not _job.acquire(blocking=False):
        return _BUSY
    try:
        return _create(prompt, style, count, True, "")
    finally:
        _job.release()


def edit(instruction: str, image: str = "", style: str = "") -> str:
    instruction = " ".join(str(instruction or "").split())
    if not instruction:
        return "FAILED: say what to change."
    on_card = _card_picture()
    source = Path(image).expanduser() if image else (on_card or last_image())
    if source is None or not source.exists():
        return ("FAILED: no picture to change - give the image's path, or make one first." if not image
                else f"FAILED: there is no picture at {source}.")
    if not _is_image(source):
        return f"FAILED: {source.name} is not a picture."
    if not _job.acquire(blocking=False):
        return _BUSY
    try:
        card_prompt, card_style = _card_prompt_style()
        same = on_card is not None and on_card.resolve() == source.resolve()
        base = (card_prompt if same and card_prompt else _prompt_of(source))
        style = _style(style or (card_style if same else "") or _state.get("style"))
        if same:
            _on_card("work", "Redrawing…", instruction[:70])
        else:
            _on_card("start", base, style, "Redrawing…", [{"path": str(source), "prompt": base, "note": "Original"}],
                     instruction[:70])
        waiting = _ready(("edit",))
        if waiting:
            _on_card("fail", waiting)
            return waiting
        started = time.time()
        path, problem = _generate(instruction, style, source, name=base)
        if path is None:
            _on_card("fail", problem)
            return problem
        _on_card("add", path, base, f"Changed: {instruction}", "", style)
        _state.update(prompt=base, style=style)
        return (f"Made a new picture from {source.name} with '{instruction[:80]}' in {_stamp(started)}, saved: "
                f"{path}. It is on the picture card (earlier versions stay in its strip). (Image Playground redraws "
                "the picture from the description; it does not paint on the original.)")
    finally:
        _job.release()


def _base() -> tuple[str, str]:
    """The prompt and style of the picture on the card, else of the last one made."""
    prompt, style = _card_prompt_style()
    if not prompt:
        last = last_image()
        prompt = _state.get("prompt") or (_prompt_of(last) if last else "")
        style = _state.get("style") or ""
    return prompt, _style(style)


def another() -> str:
    """The same prompt again: a new variation, added to the open card."""
    prompt, style = _base()
    if not prompt:
        return "FAILED: there is no picture yet to make another of - say what to draw."
    if not _job.acquire(blocking=False):
        return _BUSY
    try:
        return _create(prompt, style, 1, _card_picture() is None, "Another take")
    finally:
        _job.release()


_IMPROVE = """Rewrite this description of a picture for Apple's Image Playground so the picture comes out more \
detailed and better looking. Keep the same subject and idea; add concrete details: the setting, lighting, colours, \
mood and composition. One sentence, at most 35 words. Image Playground makes animation, illustration and sketch \
pictures only: no photorealism, no words or text in the picture, no real people's names, and don't name an art \
style. Reply with the new description only.

Description: """


def _better(prompt: str) -> str:
    from mint.core import llm
    text, _model = llm.generate(_IMPROVE + prompt)
    text = " ".join(str(text or "").strip().strip('"“”').split())
    text = re.sub(r"^(description|prompt)\s*:\s*", "", text, flags=re.I)
    return text[:400]


def improve() -> str:
    """Gemini rewrites the prompt with more detail and quality, then Image Playground draws that."""
    prompt, style = _base()
    if not prompt:
        return "FAILED: there is no picture yet to improve - say what to draw."
    if not _job.acquire(blocking=False):
        return _BUSY
    try:
        fresh = _card_picture() is None
        if fresh:
            _on_card("start", prompt, style, "Improving the prompt…", None, prompt[:70])
        else:
            _on_card("work", "Improving the prompt…", prompt[:70])
        try:
            better = _better(prompt)
        except Exception as error:
            log.info("improve: %s", error)
            better = ""
        if not better:
            problem = "FAILED: Gemini could not rewrite the prompt just now; try again in a moment."
            _on_card("fail", problem)
            return problem
        said = _create(better, style, 1, False, "Improved prompt")
        return f"Improved the prompt to: '{better}'. " + said
    finally:
        _job.release()


def _save_target(path: str, prompt: str) -> Path:
    """Where 'save it (to ...)' puts the picture: a file, a folder (named after the prompt) or the Desktop."""
    from mint.tools.saveto import _free
    home, name = Path.home(), file_name(prompt)
    raw = str(path or "").strip()
    if not raw:
        return _free(home / "Desktop" / name)
    folder_hint = raw.endswith("/")
    target = Path(os.path.expanduser(raw))
    if not target.is_absolute():
        parts = target.parts
        places = {"desktop": "Desktop", "documents": "Documents", "downloads": "Downloads", "pictures": "Pictures"}
        if parts and parts[0].lower() in places:
            target = home.joinpath(places[parts[0].lower()], *parts[1:])
        else:
            target = home / "Desktop" / target
    if target.is_dir() or folder_hint:
        target = target / name
    elif target.suffix.lower() not in _IMAGE_SUFFIXES:
        target = target.with_name(target.name + ".png")
    return _free(target)


def save(path: str = "", source: str = "", replace: bool = False) -> str:
    """A copy of the card's picture (or `source`) at `path`: a file, a folder, or '' = the Desktop.
    `replace`: the user chose that exact file in a save panel (it asked about replacing)."""
    picture = Path(source).expanduser() if source else (_card_picture() or last_image())
    if picture is None or not picture.exists():
        return "FAILED: there is no picture to save - make one first."
    if replace and path:
        target = Path(path).expanduser()
    else:
        target = _save_target(path, _card_prompt_style()[0] or _prompt_of(picture))
    try:
        from mint.tools.harness import _blocked
        why = _blocked(target, write=True)
    except Exception:
        why = ""
    if why:
        return f"FAILED: can't save there - {why}. Ask where else."
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.suffix.lower() in ("", ".png") or target.suffix.lower() == picture.suffix.lower():
            shutil.copyfile(picture, target)
        else:
            kind = {".jpg": "jpeg", ".jpeg": "jpeg", ".heic": "heic", ".tif": "tiff", ".tiff": "tiff"}[target.suffix.lower()]
            done = subprocess.run(["sips", "-s", "format", kind, str(picture), "--out", str(target)],
                                  capture_output=True, text=True, timeout=60)
            if done.returncode != 0 or not target.exists():
                return f"FAILED: could not save it as {kind.upper()} ({done.stderr.strip()[:120]})."
    except OSError as error:
        return f"FAILED: could not save the picture: {error}"
    where = str(target.parent).replace(str(Path.home()), "~")
    _on_card("flash", f"Saved to {target.parent.name}  ·  {target.name}")
    return f"Saved the picture as {target.name} in {where} ({target})."


def copy_image(image: str = "") -> str:
    """Put the picture on the clipboard; it lands in the clipboard history as Mint's."""
    picture = Path(image).expanduser() if image else (_card_picture() or last_image())
    if picture is None or not picture.exists():
        return "FAILED: there is no picture to copy."
    data = picture.read_bytes()
    if not data.startswith(b"\x89PNG"):
        work = Path(tempfile.mkdtemp(prefix="mint-imagegen-copy-"))
        try:
            subprocess.run(["sips", "-s", "format", "png", str(picture), "--out", str(work / "copy.png")],
                           capture_output=True, timeout=60)
            data = (work / "copy.png").read_bytes()
        except OSError as error:
            return f"FAILED: could not copy the picture: {error}"
        finally:
            shutil.rmtree(work, ignore_errors=True)
    from mint.tools import clipboard as clip_tools
    clip_tools._put_png(data)
    _on_card("flash", "Copied - paste it with ⌘V")
    return "Copied the picture to the clipboard."


def close_card() -> str:
    try:
        from mint.ui import image_card
        was = image_card.is_open()
        image_card.close()
    except Exception as error:
        return f"FAILED: {error}"
    return "Closed the picture card." if was else "The picture card was not open."


def open_app(image: str = "") -> str:
    """Hand a picture to the Image Playground app for Visual Edit, Add Caption or Describe a Change by hand."""
    source = Path(image).expanduser() if image else (_card_picture() or last_image())
    if image and (source is None or not source.exists()):
        return f"FAILED: there is no picture at {source}."
    command = ["open", "-a", APP] + ([str(source)] if source is not None else [])
    done = subprocess.run(command, capture_output=True, text=True, timeout=20)
    if done.returncode != 0:
        return f"FAILED: could not open Image Playground: {(done.stderr or '').strip()[:200]}"
    if source is None:
        return "Opened Image Playground."
    return (f"Opened {source.name} in Image Playground. There the user can use Visual Edit, Add Caption or "
            "Describe a Change from the picture's menu (Mint cannot press those for them reliably).")


def tool(args: dict) -> str:
    action = str(args.get("action") or "create").lower()
    try:
        if action == "edit":
            return edit(str(args.get("prompt") or args.get("instruction") or ""), str(args.get("image") or ""),
                        str(args.get("style") or ""))
        if action in ("open_app", "open"):
            return open_app(str(args.get("image") or ""))
        if action == "save":
            return save(str(args.get("path") or ""), str(args.get("image") or ""))
        if action in ("another", "again", "variation"):
            return another()
        if action == "improve":
            return improve()
        if action in ("close", "dismiss"):
            return close_card()
        if action == "copy":
            return copy_image(str(args.get("image") or ""))
        if action == "setup":
            missing = [n for n in SHORTCUTS.values() if n not in _installed()]
            return _offer(missing) if missing else "Both Image Playground shortcuts are installed."
        return create(str(args.get("prompt") or ""), str(args.get("style") or ""), int(args.get("count") or 1))
    except Exception as error:
        log.exception("make_image")
        return f"FAILED: {error}"


PROMPT = """Pictures: "make/draw/generate an image of ...", "a sketch of ...", "four versions" -> make_image \
(Apple's Image Playground, on this Mac). action=create with prompt (the picture in plain words, the user's own \
description), style animation (default), illustration or sketch, count 1-4. Every picture opens on the PICTURE \
CARD by the orb; while it is open, "it" means the picture on the card: "make the sky purple" / "give it a hat" -> \
action=edit with prompt = the change (image empty); "save it (to my Desktop / as fox.png)" -> action=save with path \
(a file or folder; empty = the Desktop); "make another one" -> action=another (same prompt, new variation); "make it \
better / more detailed" -> action=improve (rewrites the prompt, then draws it); "copy it" -> action=copy; "close it" \
-> action=close; "open it in Image Playground" -> action=open_app (Visual Edit / Add Caption by hand). Pictures are \
saved in the Mint folder's Images - say one short sentence, don't read paths out. If the result says NOT DONE YET, \
pass on the one-click 'Add Shortcut' step; BUSY means one is still being drawn. Don't try other image tools."""


def declarations():
    from google.genai import types
    S, N = types.Type.STRING, types.Type.NUMBER
    return [types.FunctionDeclaration(
        name="make_image",
        description=("Make pictures with Apple's Image Playground (on-device Apple Intelligence) and work on the "
                     "picture card that shows them: create from a description in a style, change the picture by "
                     "description, save a copy, draw another, improve the prompt, copy, close the card, or open "
                     "it in the Image Playground app. Saved as PNG in Mint's Images folder."),
        parameters=types.Schema(type=types.Type.OBJECT, properties={
            "action": types.Schema(type=S, enum=["create", "edit", "save", "another", "improve", "copy", "close",
                                                 "open_app", "setup"],
                                   description="create (default), edit, save, another, improve, copy, close, "
                                               "open_app, or setup (install the shortcuts). All but create act on "
                                               "the picture on the card (else the last one made)."),
            "prompt": types.Schema(type=S, description="create: what the picture shows; edit: the change to make"),
            "style": types.Schema(type=S, enum=list(STYLES), description="default animation"),
            "count": types.Schema(type=N, description="create: how many pictures, 1-4 (default 1)"),
            "path": types.Schema(type=S, description="save: a file (…/fox.png) or a folder; empty = the Desktop"),
            "image": types.Schema(type=S, description="edit/open_app/save/copy: path of a picture; empty = the one "
                                                      "on the card, else the last one made")},
            required=["action"]))]


HANDLERS = {"make_image": tool}
