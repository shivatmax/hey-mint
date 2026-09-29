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
import plistlib
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from mint.core import config

log = logging.getLogger("mint.tools.imagegen")

SHORTCUTS = {"create": "Mint Generate Image", "edit": "Mint Edit Image"}
# Image Playground's style ids (ImagePlaygroundStyle.id); the first is the default.
STYLES = {"animation": "Animation", "illustration": "Illustration", "sketch": "Sketch"}
APP = "Image Playground"
_ACTION = "com.apple.GenerativePlaygroundApp.GenerateImageIntent"
_DESCRIPTOR = {"AppIntentIdentifier": "GenerateImageIntent", "BundleIdentifier": "com.apple.GenerativePlaygroundApp",
               "Name": "Image Playground", "TeamIdentifier": "0000000000"}
_TIMEOUT = 180
_state: dict = {"last": None, "offered": 0.0}


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

    def create(style_id: str) -> list[dict]:
        made = str(uuid.uuid4()).upper()
        params = {"AppIntentDescriptor": dict(_DESCRIPTOR), "UUID": made,
                  "prompt": _token_string(_ref(prompt, "Dictionary Value")),
                  "style": {"identifier": style_id, "title": {"key": STYLES[style_id]},
                            "subtitle": {"key": STYLES[style_id]}},
                  "saveToLibrary": "never"}
        if image is not None:
            params["image"] = image
        return [_action(_ACTION, **params),
                _action("is.workflow.actions.output", WFOutput=_token_string(_ref(made, "Image")))]

    default, *others = STYLES
    for style_id in others:     # If style is <id>: create in that style and stop there
        group = str(uuid.uuid4()).upper()
        actions.append(_action("is.workflow.actions.conditional", GroupingIdentifier=group, WFControlFlowMode=0,
                               WFCondition=4, WFConditionalActionString=style_id,
                               WFInput={"Type": "Variable", "Variable": _attachment(_ref(style, "Dictionary Value"))}))
        actions += create(style_id)
        actions.append(_action("is.workflow.actions.conditional", GroupingIdentifier=group, WFControlFlowMode=2,
                               UUID=str(uuid.uuid4()).upper()))
    actions += create(default)
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


def _generate(prompt: str, style: str, image: Path | None) -> tuple[Path | None, str]:
    """One picture through the shortcut. -> (saved PNG, '') or (None, message)."""
    kind = "edit" if image is not None else "create"
    work = Path(tempfile.mkdtemp(prefix="mint-imagegen-run-"))
    spec, out = work / "spec.json", work / "out.png"
    spec.write_text(json.dumps({"prompt": prompt, "style": style}))
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
        target = _new_file(prompt)
        if out.read_bytes()[:4] == b"\x89PNG":
            shutil.move(str(out), target)
        else:
            convert = subprocess.run(["sips", "-s", "format", "png", str(out), "--out", str(target)],
                                     capture_output=True, text=True, timeout=60)
            if convert.returncode != 0 or not target.exists():
                return None, f"FAILED: could not save the picture as PNG ({convert.stderr.strip()[:120]})."
        _state["last"] = target
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


def _card(title: str, paths: list[Path], detail: str) -> None:
    try:
        from mint.tools import cards
        cards.show(title, subtitle=f"saved in {images_folder().name} · click one to open", icon="photo.fill",
                   tint="purple", seconds=30,
                   items=[{"title": p.stem, "detail": detail, "path": str(p)} for p in paths])
    except Exception as error:
        log.info("card: %s", error)


def create(prompt: str, style: str = "", count: int = 1) -> str:
    prompt = " ".join(str(prompt or "").split())
    if not prompt:
        return "FAILED: say what the picture should show."
    waiting = _ready(("create",))
    if waiting:
        return waiting
    style, count = _style(style), max(1, min(4, int(count or 1)))
    made, problem, started = [], "", time.time()
    for _ in range(count):
        path, problem = _generate(prompt, style, None)
        if path is None:
            break
        made.append(path)
    if not made:
        return problem
    _card("Image Playground", made, f"{STYLES[style]} · {prompt[:60]}")
    said = (f"Made {len(made)} {STYLES[style].lower()} picture{'s' if len(made) > 1 else ''} of '{prompt[:80]}' in "
            f"{time.time() - started:.0f} s, saved: " + "; ".join(str(p) for p in made) + ". Shown on a card.")
    return said + (f" Stopped early: {problem}" if problem else "")


def edit(instruction: str, image: str = "", style: str = "") -> str:
    instruction = " ".join(str(instruction or "").split())
    if not instruction:
        return "FAILED: say what to change."
    source = Path(image).expanduser() if image else last_image()
    if source is None or not source.exists():
        return ("FAILED: no picture to change - give the image's path, or make one first." if not image
                else f"FAILED: there is no picture at {source}.")
    if not _is_image(source):
        return f"FAILED: {source.name} is not a picture."
    waiting = _ready(("edit",))
    if waiting:
        return waiting
    style = _style(style)
    path, problem = _generate(instruction, style, source)
    if path is None:
        return problem
    _card("Image Playground", [path], f"from {source.name} · {instruction[:50]}")
    return (f"Made a new picture from {source.name} with '{instruction[:80]}', saved: {path}. Shown on a card. "
            "(Image Playground redraws the picture from the description; it does not paint on the original.)")


def open_app(image: str = "") -> str:
    """Hand a picture to the Image Playground app for Visual Edit, Add Caption or Describe a Change by hand."""
    source = Path(image).expanduser() if image else last_image()
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
        if action == "setup":
            missing = [n for n in SHORTCUTS.values() if n not in _installed()]
            return _offer(missing) if missing else "Both Image Playground shortcuts are installed."
        return create(str(args.get("prompt") or ""), str(args.get("style") or ""), int(args.get("count") or 1))
    except Exception as error:
        log.exception("make_image")
        return f"FAILED: {error}"


PROMPT = """Pictures: "make/draw/generate an image of ...", "a sketch of ...", "four versions", "change it so ...", \
"give it a hat" -> make_image (Apple's Image Playground, on this Mac). action=create with prompt (the picture in \
plain words, the user's own description), style animation (default), illustration or sketch, count 1-4. \
action=edit with prompt = the change and image = a path (empty = the picture made last): it redraws from that \
picture. action=open_app opens the picture in the Image Playground app for Visual Edit / Add Caption by hand. \
Pictures are saved in the Mint folder's Images and shown on a card - say one short sentence, don't read paths out. \
If the result says NOT DONE YET, pass on the one-click 'Add Shortcut' step. Don't try other image tools."""


def declarations():
    from google.genai import types
    S, N = types.Type.STRING, types.Type.NUMBER
    return [types.FunctionDeclaration(
        name="make_image",
        description=("Make pictures with Apple's Image Playground (on-device Apple Intelligence): create from a "
                     "description in a style, change a picture by description, or open one in the Image "
                     "Playground app. Saved as PNG in Mint's Images folder and shown on a card."),
        parameters=types.Schema(type=types.Type.OBJECT, properties={
            "action": types.Schema(type=S, enum=["create", "edit", "open_app", "setup"],
                                   description="create (default), edit, open_app, or setup (install the shortcuts)"),
            "prompt": types.Schema(type=S, description="create: what the picture shows; edit: the change to make"),
            "style": types.Schema(type=S, enum=list(STYLES), description="default animation"),
            "count": types.Schema(type=N, description="create: how many pictures, 1-4 (default 1)"),
            "image": types.Schema(type=S, description="edit/open_app: path of the picture; empty = the last one "
                                                      "made")},
            required=["action"]))]


HANDLERS = {"make_image": tool}
