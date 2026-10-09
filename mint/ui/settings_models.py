"""Settings ▸ Models & agents: API keys for every model provider, the backup models, and the sub-agents.

Basic view: the Gemini keys, "AI models for your agents" with OpenAI (and every provider that already has a key),
More providers… to open the rest right there, and the agents. All settings (the switch at the top) also shows
every provider, how two Gemini keys share the work, your own endpoints, and the backups for every agent.
Each provider row: its brand tile, what it gives, a Set / Not set badge, Add key… and Test.

- Gemini: key 1 (required, the voice) and an optional key 2; how the two share the work (gemini_keys.py).
- OpenAI, Anthropic, OpenRouter, Groq, xAI: add / change / remove a key, and Test it (lists its models).
- Ollama: its address on this Mac, and Test. Custom OpenAI-compatible endpoints: add, test, remove.
- Backups for every agent: up to three models tried after an agent's own, and Gemini as the last resort.
- Agents: every agent with its model and backups; Edit… (name, role, instructions, model, two backups,
  thinking, web tools) and Remove for agents the user made; Add an agent….

Keys go to .env (mode 600) through catalog.write_key and are never shown - only their last four
characters. Model menus are built from cached lists (catalog.cached_models); the live lists load in the
background when the page opens, so the page never waits on the network. What the page shows (keys,
the saved settings, the agents) is read by `facts` on Settings' reader thread, not the main thread.
"""

from __future__ import annotations

import os
import threading

import AppKit
import objc
from PyObjCTools import AppHelper

from mint.ui import settings_art
from mint.agents import catalog
from mint.agents import registry

KEYED = ("openai", "anthropic", "openrouter", "groq", "xai")
ESSENTIAL = ("openai",)                  # shown in the basic view even without a key
# What each provider gives, in plain words (and where its key comes from).
GIVES = {"openai": "GPT models. Key: platform.openai.com/api-keys",
         "anthropic": "Claude - great at writing and code. Key: console.anthropic.com",
         "openrouter": "Hundreds of models, one key. Key: openrouter.ai/keys",
         "groq": "Very fast open models, free tier. Key: console.groq.com/keys",
         "xai": "Grok models. Key: console.x.ai",
         "ollama": "Free models that run on this Mac, no key. Get it at ollama.com"}
_refreshed = [0.0]


def facts() -> dict:
    """Settings' reader thread: everything the page shows that lives in files, the environment or
    another module's lock - and the background refresh of the model lists, every 5 minutes."""
    import time

    from mint.core import gemini_keys
    if time.monotonic() - _refreshed[0] > 300:
        _refreshed[0] = time.monotonic()
        catalog.refresh_lists()
    return {"gemini_status": gemini_keys.status(), "gemini_keys": len(gemini_keys.keys()),
            "keys": {provider: catalog.key(provider) for provider in KEYED}, "settings": catalog.settings(),
            "custom": {cid: dict(spec, has_key=bool(catalog.key(cid)))
                       for cid, spec in catalog.custom_providers().items()},
            "agents": registry.load(), "codex_problem": _codex_problem(), "codex_models": _codex_models(),
            "pals": _pals()}


def _pals() -> dict:
    """{agent name: (species, rgb)} - the critter each agent wears (critters.pal_for reads the registry: here, on
    the reader thread)."""
    from mint.ui import critters
    out = {}
    for agent in registry.load():
        try:
            pal = critters.pal_for(agent)
        except Exception:
            pal = None
        if pal:
            out[agent["name"]] = pal
    return out


def _codex_models() -> dict:
    """What Codex offers ([(slug, name)], from its own cache) and the model it is set to - read with the facts."""
    try:
        from mint.agents import codex as codex_runner
        return {"list": codex_runner.models(), "selected": codex_runner.selected()}
    except Exception:
        return {"list": [], "selected": ""}


def _codex_problem() -> str:
    """Why Codex can't run on this Mac ('' when it can) - read with the facts: it runs codex --version."""
    try:
        from mint.agents import codex as codex_runner
        return codex_runner.problem()
    except Exception as error:
        return f"Codex can't be checked ({error})."


def page(win, page, facts: dict) -> None:
    advanced = win._advanced()
    _gemini_section(win, page, facts)
    win._anchor(page, "model_keys")                 # the AI model providers connector's Settings…
    _providers_section(win, page, facts)
    if advanced:
        _backups_section(win, page, facts)
    _agents_section(win, page, facts)
    win._part(page, "coding", win._coding_section, "Coding agents")     # Claude Code and Codex (their own reader)
    win._more_section(page, "Backup models for every agent, how two Gemini keys share the work, your own "
                            "OpenAI-compatible endpoints, and more for Claude Code and Codex.")


# --- Gemini ----------------------------------------------------------------------------------------

def _gemini_section(win, page, facts: dict) -> None:
    page.section("Gemini")
    status = facts["gemini_status"]
    for env, title, hint in (("GEMINI_API_KEY", "Key 1", "Required - Mint's voice runs on it. Free at aistudio.google.com/apikey."),
                             ("GEMINI_API_KEY_2", "Key 2 (optional)",
                              "A second free key doubles the free limits: when one key is rate-limited, the other "
                              "takes over - for the voice and for everything else.")):
        value = os.environ.get(env, "")
        resting = status.get(env, "")
        win._key_row(page, title, hint + (f"\nResting now: {resting[:90]}" if resting else ""), value,
                     lambda e=env, t=f"Gemini {title}": _ask_key(win, e, t, required=e == "GEMINI_API_KEY"),
                     icon="gemini")
    if facts["gemini_keys"] > 1 and win._advanced():
        win._row_popup(page, "gemini_key_mode", "Using two keys",
                       [("split", "Voice on key 1, the rest on key 2"), ("primary", "Key 1 first, key 2 as backup")],
                       hint="Either way each key covers for the other when it is rate-limited or refused.", w=280)
    page.end("Keys stay on this Mac, in a file only you can read.")


# --- providers ---------------------------------------------------------------------------------------

def _providers_section(win, page, facts: dict) -> None:
    """AI models for your agents: the common providers (and every one with a key) first; More providers… opens the
    rest right here, without All settings."""
    ui = win.__dict__.setdefault("_models_ui", {"more": False})
    every = win._advanced() or ui["more"]
    keys = facts["keys"]
    ollama = str(facts["settings"].get("ollama_base") or "").strip()
    shown = [p for p in KEYED if every or p in ESSENTIAL or keys.get(p)] + (["ollama"] if every or ollama else [])
    hidden = [p for p in KEYED + ("ollama",) if p not in shown]
    page.section("AI models for your agents")
    page.text("Your agents can think with other AI models too. To add one: get a key on the provider's site, press "
              "Add key… and paste it. Test checks the key and lists its models; each agent picks its models below.",
              size=12, alpha=0.7)
    for provider in shown:
        spec = catalog.PROVIDERS[provider]
        if provider == "ollama":
            _ollama_row(win, page, facts)
            continue
        where = {}
        win._key_row(page, spec["label"], GIVES.get(provider, spec["hint"]), keys.get(provider, ""),
                     lambda p=provider: _ask_key(win, catalog.PROVIDERS[p]["env"], catalog.PROVIDERS[p]["label"]),
                     icon=provider, test=lambda p=provider, w=where: _test(p, w.get("label")))
        where["label"] = page.hint

    if every:
        for cid, spec in facts["custom"].items():
            card, top, x, h = page.row(spec["label"], f"OpenAI-compatible · {spec['base']}"
                                       + (" · key set" if spec["has_key"] else " · no key"), control_w=164,
                                       icon="custom")
            status = page.hint
            win._button(card, "Test", x, top + (h - 28) / 2, 64, lambda c=cid, s=status: _test(c, s))
            win._button(card, "Remove", x + 72, top + (h - 28) / 2, 92,
                        lambda c=cid, label=spec["label"]: (_confirm(f"Remove the endpoint {label}?",
                                                "Agents that use it move on to their backups.")
                                       and (catalog.remove_custom(c), win.refresh())))
        win._row_buttons(page, "Another OpenAI-compatible endpoint",
                         [("Add endpoint…", 130, lambda: _add_endpoint(win))],
                         hint="LM Studio, vLLM, Together, DeepSeek, a company gateway - anything that speaks "
                              "/v1/chat/completions.", icon="custom")

    def toggle(more: bool) -> None:
        ui["more"] = more
        AppHelper.callAfter(lambda: win.refresh(keep_scroll=True))      # after the click: the button is rebuilt
    if hidden:
        names = ", ".join(catalog.PROVIDERS[p]["label"].replace(" (on this Mac)", "") for p in hidden)
        win._row_buttons(page, "More providers…", [(f"Show {len(hidden)} more", 120, lambda: toggle(True))],
                         hint=f"{names} - and your own endpoint.", icon="models")
    elif ui["more"] and not win._advanced():
        win._row_buttons(page, "", [("Show fewer", 120, lambda: toggle(False))])
    page.end("Keys stay on this Mac, in a file only you can read. Each provider bills you directly; Groq and "
             "OpenRouter have free models, Ollama is free.")


def _ollama_row(win, page, facts: dict) -> None:
    """Ollama: an address on this Mac, not a key."""
    card, top, x, h = page.row("Ollama (on this Mac)", GIVES["ollama"], control_w=314, icon="ollama")
    status = page.hint
    field = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(x, top + (h - 24) / 2, 242, 24))
    field.setStringValue_(facts["settings"].get("ollama_base") or "")
    field.setPlaceholderString_(catalog.PROVIDERS["ollama"]["base"])
    field.setBezelStyle_(AppKit.NSTextFieldRoundedBezel)
    field.setDelegate_(win.target)
    save = lambda control: catalog.save_settings({"ollama_base": str(control.stringValue()).strip()})  # noqa: E731
    win._handlers[objc.pyobjc_id(field)] = save
    win._ended_handlers[objc.pyobjc_id(field)] = save
    card.addSubview_(field)
    win._button(card, "Test", x + 250, top + (h - 28) / 2, 64, lambda: (save(field), _test("ollama", status)))


def _test(provider: str, label) -> None:
    """Test: the result goes where the row's hint was, in the accent colour."""
    if label is not None:
        label.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
        label.setTextColor_(AppKit.NSColor.controlAccentColor())
        label.setStringValue_("Testing…")

    def run():
        words = " ".join(str(catalog.test(provider)).split())
        if label is not None:
            AppHelper.callAfter(label.setStringValue_, words)
            AppHelper.callAfter(label.setToolTip_, words)
    threading.Thread(target=run, daemon=True, name="provider-test").start()


def _secure_field(width: float = 320):
    return AppKit.NSSecureTextField.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, width, 24))


def _ask_key(win, env: str, title: str, required: bool = False) -> None:
    """Paste a key; it goes to .env (mode 600). Empty removes an optional key."""
    alert = AppKit.NSAlert.alloc().init()
    alert.setMessageText_(f"{title} API key")
    alert.setInformativeText_("Paste the key. It is saved on this Mac only." +
                              ("" if required else " Leave it empty and press Save to remove it."))
    field = _secure_field()
    alert.setAccessoryView_(field)
    alert.addButtonWithTitle_("Save")
    alert.addButtonWithTitle_("Cancel")
    alert.window().setInitialFirstResponder_(field)
    if alert.runModal() != AppKit.NSAlertFirstButtonReturn:
        return
    value = str(field.stringValue()).strip()
    if required and not value:
        return
    try:
        catalog.write_key(env, value)
    except ValueError as error:
        _confirm("That key was not saved.", str(error), only_ok=True)
        return
    if env.startswith("GEMINI_API_KEY"):
        from mint.core import gemini_keys
        from mint.core import llm
        gemini_keys._bench.clear()
        llm._client_cache = None
    win.refresh()


def _confirm(title: str, text: str = "", only_ok: bool = False) -> bool:
    alert = AppKit.NSAlert.alloc().init()
    alert.setMessageText_(title)
    if text:
        alert.setInformativeText_(text)
    alert.addButtonWithTitle_("OK")
    if not only_ok:
        alert.addButtonWithTitle_("Cancel")
    return alert.runModal() == AppKit.NSAlertFirstButtonReturn


class _Form(AppKit.NSView):
    def isFlipped(self):
        return True


def _form_label(view, text, y, width=110):
    label = AppKit.NSTextField.labelWithString_(text)
    label.setFrame_(AppKit.NSMakeRect(0, y + 3, width, 18))
    label.setAlignment_(AppKit.NSTextAlignmentRight)
    view.addSubview_(label)


def _form_field(view, y, x=120, w=320, value="", placeholder="", secure=False):
    cls = AppKit.NSSecureTextField if secure else AppKit.NSTextField
    field = cls.alloc().initWithFrame_(AppKit.NSMakeRect(x, y, w, 24))
    field.setStringValue_(value)
    field.setPlaceholderString_(placeholder)
    field.setUsesSingleLineMode_(True)             # one line that scrolls: a long role was wrapped and cut in half
    field.cell().setWraps_(False)
    field.cell().setScrollable_(True)
    view.addSubview_(field)
    return field


def _add_endpoint(win) -> None:
    form = _Form.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 440, 132))
    rows = []
    for i, (title, placeholder, secure) in enumerate((("Name", "e.g. LM Studio", False),
                                                      ("Address", "http://localhost:1234/v1", False),
                                                      ("API key", "optional", True),
                                                      ("Models", "optional, comma-separated", False))):
        _form_label(form, title, i * 32)
        rows.append(_form_field(form, i * 32, placeholder=placeholder, secure=secure))
    alert = AppKit.NSAlert.alloc().init()
    alert.setMessageText_("Add an OpenAI-compatible endpoint")
    alert.setInformativeText_("Its address ends in /v1 (Mint calls /v1/chat/completions and /v1/models).")
    alert.setAccessoryView_(form)
    alert.addButtonWithTitle_("Add")
    alert.addButtonWithTitle_("Cancel")
    alert.window().setInitialFirstResponder_(rows[0])
    if alert.runModal() != AppKit.NSAlertFirstButtonReturn:
        return
    name, address, key, models = (str(r.stringValue()).strip() for r in rows)
    if not name or not address.startswith(("http://", "https://")):
        _confirm("Not added.", "It needs a name and an address starting with http:// or https://.", only_ok=True)
        return
    try:
        catalog.add_custom(name, address, key, [m.strip() for m in models.split(",") if m.strip()])
    except ValueError as error:
        _confirm("Not added.", str(error), only_ok=True)
        return
    win.refresh()


# --- model menus ---------------------------------------------------------------------------------

OTHER = "__other__"


def _model_menu(win, current: str, allow_none: bool, width: float = 320):
    """A pop-up of every provider's models, grouped; providers without a key are shown but disabled.
    "Other model…" asks for any "provider/model"."""
    popup = settings_art.popup(AppKit.NSMakeRect(0, 0, width, 26))
    popup.setAutoenablesItems_(False)
    menu = popup.menu()

    def add(title, value, enabled=True, indent=0):
        item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
        item.setRepresentedObject_(value)
        item.setEnabled_(enabled)
        item.setIndentationLevel_(indent)
        menu.addItem_(item)
        return item

    selected = None
    if allow_none:
        selected = add("None", "")
    for provider, spec in catalog.all_providers().items():
        ready = catalog.configured(provider)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        add(spec["label"] + ("" if ready else " - add a key first"), None, enabled=False)
        names = catalog.cached_models(provider)
        if provider == "ollama" and not names:
            names = []
        for name in names:
            ref = catalog.join(provider, name)
            item = add(name, ref, enabled=ready, indent=1)
            if ref == current:
                selected = item
    if current and (selected is None or selected.representedObject() != current):
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        selected = add(current, current)
    menu.addItem_(AppKit.NSMenuItem.separatorItem())
    add("Other model…", OTHER)
    if selected is not None:
        popup.selectItem_(selected)

    def picked(control):
        item = control.selectedItem()
        if item is None or item.representedObject() != OTHER:
            return
        typed = _ask_text("Which model?", "Write it as provider/model - e.g. openrouter/deepseek/deepseek-r2, "
                          "groq/qwen/qwen3.8-27b, ollama/llama3.2:3b, anthropic/claude-sonnet-5-5 - or just "
                          "the model's name ('Claude Opus', 'GPT-6 Sol').")
        ref = catalog.resolve(typed) or (typed if "/" in typed else "")
        if not ref:
            control.selectItemAtIndex_(0)
            return
        new = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(ref, None, "")
        new.setRepresentedObject_(ref)
        control.menu().insertItem_atIndex_(new, control.numberOfItems() - 2)
        control.selectItem_(new)
    win._on(popup, picked)
    return popup


def _ask_text(title: str, text: str) -> str:
    alert = AppKit.NSAlert.alloc().init()
    alert.setMessageText_(title)
    alert.setInformativeText_(text)
    field = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 340, 24))
    alert.setAccessoryView_(field)
    alert.addButtonWithTitle_("OK")
    alert.addButtonWithTitle_("Cancel")
    alert.window().setInitialFirstResponder_(field)
    if alert.runModal() != AppKit.NSAlertFirstButtonReturn:
        return ""
    return str(field.stringValue()).strip()


def _chosen(popup) -> str:
    item = popup.selectedItem()
    value = item.representedObject() if item is not None else ""
    return "" if value in (None, OTHER) else str(value)


# --- backups for every agent -------------------------------------------------------------------------

def _backups_section(win, page, facts: dict) -> None:
    data = facts["settings"]
    chain = [str(m) for m in data.get("fallbacks", [])]
    page.section("Backups for every agent")
    shown = " → ".join(chain) if chain else "None of your own yet."
    win._row_buttons(page, "Backup models", [("Choose…", 110, lambda: _choose_backups(win))],
                     hint=f"Tried in this order after an agent's own models, when they are rate-limited, out of "
                          f"credit, refused or down.\n{shown}")
    card, top, x, h = page.row("Gemini as the last resort", "Gemini 3.5 Flash, then Flash-Lite, after everything "
                               "else - on your Gemini keys.", control_w=38)

    def switch(on: bool) -> None:
        catalog.save_settings({"gemini_last_resort": on})
    win._switch(card, x, top + (h - 22) / 2, bool(data.get("gemini_last_resort", True)), switch)
    page.end()


def _choose_backups(win) -> None:
    chain = [str(m) for m in catalog.settings().get("fallbacks", [])] + ["", "", ""]
    form = _Form.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 440, 100))
    menus = []
    for i in range(3):
        _form_label(form, f"Backup {i + 1}", i * 34)
        popup = _model_menu(win, chain[i], allow_none=True)
        popup.setFrameOrigin_(AppKit.NSMakePoint(120, i * 34))
        form.addSubview_(popup)
        menus.append(popup)
    alert = AppKit.NSAlert.alloc().init()
    alert.setMessageText_("Backups for every agent")
    alert.setInformativeText_("Tried in order after an agent's own models.")
    alert.setAccessoryView_(form)
    alert.addButtonWithTitle_("Save")
    alert.addButtonWithTitle_("Cancel")
    if alert.runModal() != AppKit.NSAlertFirstButtonReturn:
        return
    picked = [m for m in (_chosen(p) for p in menus) if m]
    catalog.save_settings({"fallbacks": list(dict.fromkeys(picked))})
    win.refresh()


# --- agents --------------------------------------------------------------------------------------

BUILT_IN = {"astra", "luna", "sage", "codex"}
WEB_TOOLS = ["web_search", "fetch_url"]
BASE_TOOLS = ["write_file", "read_file", "list_files", "ask_user", "report_progress"]


def _agents_section(win, page, facts: dict) -> None:
    page.section("Agents")
    for agent in facts["agents"]:
        codex = agent.get("runner") == "codex"
        models = agent.get("models") or []
        detail = (f"{agent.get('role', '')}\n"
                  + ("Runs in OpenAI Codex (the ChatGPT app), on " + (models[0] if models else "gpt-6-luna")
                     if codex else f"Model: {models[0] if models else '?'}"
                     + (f"  ·  backups: {', '.join(models[1:])}" if models[1:] else "")
                     + f"  ·  thinking {agent.get('thinking', 'low')}"))
        buttons = [] if codex else [("Edit…", 80, lambda a=agent["name"]: _edit_agent(win, a))]
        if agent["name"].lower() not in BUILT_IN:
            buttons.append(("Remove", 84, lambda a=agent["name"]: _remove_agent(win, a)))
        face = _face_icon(agent, (facts.get("pals") or {}).get(agent["name"]))
        if codex:
            _codex_row(win, page, agent, facts.get("codex_problem", ""), facts.get("codex_models") or {}, face)
        elif buttons:
            win._row_buttons(page, agent["name"], buttons, hint=detail, icon=face)
        else:
            page.row(agent["name"], detail, icon=face)
    win._row_buttons(page, "", [("Add an agent…", 140, lambda: _edit_agent(win, None), True)],
                     hint="Your own agent: its job, how it should work, and which models it uses.")
    page.end("Mint picks an agent by its role; you can also ask by name (\"ask Nova to…\").")


def _codex_row(win, page, agent, missing: str, offered: dict, face=None) -> None:
    """Codex: which model it runs (Auto = whatever Codex itself is set to, or one of the models Codex offers this
    account) and an on/off switch. Without Codex on this Mac (it comes with the ChatGPT app) both are off: Mint never
    offers it, and hands building work to Luna instead."""
    chosen = (agent.get("models") or ["auto"])[0]
    names = dict(offered.get("list") or [])
    selected = offered.get("selected") or ""
    auto = f"Auto ({names.get(selected, selected)})" if selected else "Auto (Codex's default)"
    options = [("auto", auto)] + list(offered.get("list") or [])
    if chosen not in [v for v, _ in options]:
        options.append((chosen, chosen))                 # a model Codex no longer lists: still shown, still chosen
    hint = (f"{agent.get('role', '')}\nRuns in OpenAI Codex (the ChatGPT app). Auto: the model you picked in Codex."
            + ("\n" + missing.replace("The user needs to", "To use it,") if missing else ""))
    card, top, x, h = page.row(agent["name"], hint, control_w=210 + 12 + 38, icon=face)
    popup = settings_art.popup(AppKit.NSMakeRect(x, top + (h - 26) / 2, 210, 26))
    for _, title in options:
        popup.addItemWithTitle_(title)
    values = [v for v, _ in options]
    popup.selectItemAtIndex_(values.index(chosen))

    def picked(control) -> None:
        model = values[control.indexOfSelectedItem()]
        registry.set_model(agent["name"], model)
        print(f"  [agents: {agent['name']} runs on {model}]", flush=True)
        win.refresh(keep_scroll=True)
    win._on(popup, picked)
    popup.setToolTip_("The model Codex runs Mint's jobs on. Auto follows the model you picked in Codex.")
    card.addSubview_(popup)

    def switched(on: bool) -> None:
        registry.set_on(agent["name"], on)
        print(f"  [agents: {agent['name']} {'on' if on else 'off'}]", flush=True)
    switch = win._switch(card, x + 222, top + (h - 22) / 2, not agent.get("off") and not missing, switched)
    if missing:
        switch.setEnabled_(False)
        popup.setEnabled_(False)
        switch.setToolTip_(missing)


# --- critter faces ---------------------------------------------------------------------------------------

def _face_view(x: float, y: float, size: float, species: str, rgb: tuple, face: float | None = None):
    """An NSView holding critters.mini_face(species) in `rgb`, `size` points square."""
    from mint.ui import critters
    host = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(x, y, size, size))
    host.setWantsLayer_(True)
    layer = critters.mini_face(species, face or size, rgb)
    layer.setPosition_((size / 2, size / 2))
    host.layer().addSublayer_(layer)
    host.setAccessibilityElement_(True)
    host.setAccessibilityRole_(AppKit.NSAccessibilityImageRole)
    host.setAccessibilityLabel_(f"{species} character")
    return host


def _face_icon(agent: dict, pal):
    """A row icon (_Page.row's callable icon): the agent's critter face in its colour; a plain dot in its colour
    if it has none."""
    species, rgb = pal if pal else (None, registry.color_rgb(agent))

    def draw(card, x, y, size):
        if species:
            card.addSubview_(_face_view(x, y, size, species, rgb))
            return
        dot = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(x + size / 2 - 6, y + size / 2 - 6, 12, 12))
        dot.setWantsLayer_(True)
        dot.layer().setCornerRadius_(6)
        dot.layer().setBackgroundColor_(AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(*rgb, 1.0).CGColor())
        card.addSubview_(dot)
    return draw


class _SettingsPickTile(AppKit.NSView):
    """A clickable square in the agent editor (a character or a colour); .pick() runs on click. Not flipped: the
    critter layers are drawn y-up."""

    def acceptsFirstMouse_(self, event):
        return True

    def mouseDown_(self, event):
        self.pick()

    def accessibilityPerformPress(self):
        self.pick()
        return True


def _hex(rgb) -> str:
    return "#%02X%02X%02X" % tuple(max(0, min(255, round(c * 255))) for c in rgb[:3])


def _character_rows(form, y: float, species: str, color: str) -> tuple:
    """The editor's Character row (the eight critters in the agent's colour, the chosen one ringed) and Colour row
    (registry.PALETTE swatches and a colour well for any colour). -> (state dict with "pal" and "color", new y)."""
    from mint.ui import critters
    state = {"pal": species if species in critters.SPECIES else critters.SPECIES[0], "color": color.upper()}
    tiles, swatches = {}, {}
    accent = AppKit.NSColor.controlAccentColor()

    def ring(tile, on: bool) -> None:
        tile.layer().setBorderWidth_(2.0 if on else 0.5)
        tile.layer().setBorderColor_((accent if on else AppKit.NSColor.separatorColor()).CGColor())

    def paint() -> None:
        rgb = registry.color_rgb({"color": state["color"]})
        for sp, tile in tiles.items():
            for layer in list(tile.layer().sublayers() or []):
                layer.removeFromSuperlayer()
            face = critters.mini_face(sp, 28, rgb)
            face.setPosition_((18, 18))
            tile.layer().addSublayer_(face)
            ring(tile, sp == state["pal"])
        for hexv, tile in swatches.items():
            ring(tile, hexv == state["color"])
        well.setColor_(AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(*rgb, 1.0))

    _form_label(form, "Character", y + 8)
    for i, sp in enumerate(critters.SPECIES):
        tile = _SettingsPickTile.alloc().initWithFrame_(AppKit.NSMakeRect(120 + i * 40, y, 36, 36))
        tile.setWantsLayer_(True)
        tile.layer().setCornerRadius_(9)
        tile.layer().setBackgroundColor_(AppKit.NSColor.labelColor().colorWithAlphaComponent_(0.05).CGColor())
        tile.setAccessibilityElement_(True)
        tile.setAccessibilityRole_(AppKit.NSAccessibilityButtonRole)
        tile.setAccessibilityLabel_(sp)
        tile.setToolTip_(sp.capitalize())

        def pick(sp=sp) -> None:
            state["pal"] = sp
            paint()
        tile.pick = pick
        tiles[sp] = tile
        form.addSubview_(tile)
    y += 44
    _form_label(form, "Colour", y + 4)
    for i, hexv in enumerate(registry.PALETTE):
        tile = _SettingsPickTile.alloc().initWithFrame_(AppKit.NSMakeRect(120 + i * 30, y + 1, 24, 24))
        tile.setWantsLayer_(True)
        tile.layer().setCornerRadius_(12)
        tile.layer().setBackgroundColor_(AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(
            *registry.color_rgb({"color": hexv}), 1.0).CGColor())
        tile.setAccessibilityElement_(True)
        tile.setAccessibilityRole_(AppKit.NSAccessibilityButtonRole)
        tile.setAccessibilityLabel_(f"Colour {hexv}")

        def choose(hexv=hexv) -> None:
            state["color"] = hexv.upper()
            paint()
        tile.pick = choose
        swatches[hexv.upper()] = tile
        form.addSubview_(tile)
    well = AppKit.NSColorWell.alloc().initWithFrame_(AppKit.NSMakeRect(120 + len(registry.PALETTE) * 30 + 6, y, 44, 26))
    if hasattr(well, "setColorWellStyle_"):
        well.setColorWellStyle_(getattr(AppKit, "NSColorWellStyleMinimal", 1))
    well.setToolTip_("Any colour")
    form.addSubview_(well)
    state["well"] = well
    state["paint"] = paint
    paint()
    return state, y + 34


def _well_changed(state: dict) -> None:
    color = state["well"].color().colorUsingColorSpace_(AppKit.NSColorSpace.sRGBColorSpace())
    if color is not None:
        state["color"] = _hex((color.redComponent(), color.greenComponent(), color.blueComponent()))
        state["paint"]()


def _remove_agent(win, name: str) -> None:
    if _confirm(f"Remove the agent {name}?", "Its files stay where they are."):
        registry.remove(name)
        win.refresh()


def _edit_agent(win, name: str | None) -> None:
    agent = registry.get(name) if name else None
    models = list(agent.get("models") or []) if agent else list(registry.GEMINI_FLASH)
    form = _Form.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 460, 494))
    y = 0
    _form_label(form, "Name", y)
    name_field = _form_field(form, y, value=agent["name"] if agent else "", placeholder="e.g. Nova")
    if agent:
        name_field.setEditable_(False)
        name_field.setSelectable_(True)
    y += 32
    _form_label(form, "What it's for", y)
    role_field = _form_field(form, y, value=agent.get("role", "") if agent else "",
                             placeholder="One line, e.g. Writes marketing copy")
    y += 36
    from mint.ui import critters
    if agent:
        pal = critters.pal_for(agent)
        species, color = (pal[0] if pal else critters.SPECIES[0]), str(agent.get("color") or registry.next_color())
    else:                                        # a new agent: the first character and colour nobody wears yet
        worn = {p[0] for p in (critters.pal_for(a) for a in registry.load()) if p}
        species = next((sp for sp in critters.SPECIES if sp not in worn), critters.SPECIES[0])
        color = registry.next_color()
    looks, y = _character_rows(form, y, species, color)
    win._on(looks["well"], lambda c: _well_changed(looks))
    y += 6
    _form_label(form, "How it works", y)
    scroll = AppKit.NSScrollView.alloc().initWithFrame_(AppKit.NSMakeRect(120, y, 320, 96))
    scroll.setHasVerticalScroller_(True)
    scroll.setBorderType_(AppKit.NSBezelBorder)
    text = AppKit.NSTextView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 316, 96))
    text.setString_(agent.get("instructions", "") if agent else "")
    text.setFont_(AppKit.NSFont.systemFontOfSize_(12))
    text.setRichText_(False)
    scroll.setDocumentView_(text)
    form.addSubview_(scroll)
    y += 106
    menus = []
    for i, title in enumerate(("Model", "Backup 1", "Backup 2")):
        _form_label(form, title, y)
        popup = _model_menu(win, models[i] if i < len(models) else "", allow_none=i > 0)
        popup.setFrameOrigin_(AppKit.NSMakePoint(120, y))
        form.addSubview_(popup)
        menus.append(popup)
        y += 34
    _form_label(form, "Thinking", y)
    thinking = settings_art.popup(AppKit.NSMakeRect(120, y, 200, 26))
    levels = [("none", "None (fastest)"), ("low", "Low"), ("medium", "Medium (careful)")]
    for _, title in levels:
        thinking.addItemWithTitle_(title)
    current = agent.get("thinking", "low") if agent else "low"
    thinking.selectItemAtIndex_([v for v, _ in levels].index(current) if current in dict(levels) else 1)
    form.addSubview_(thinking)
    y += 34
    web = AppKit.NSButton.checkboxWithTitle_target_action_("Can search and read the web", None, None)
    web.setFrame_(AppKit.NSMakeRect(118, y, 320, 20))
    tools = list(agent.get("tools") or []) if agent else WEB_TOOLS + BASE_TOOLS
    web.setState_(AppKit.NSControlStateValueOn if "web_search" in tools else AppKit.NSControlStateValueOff)
    form.addSubview_(web)
    y += 24
    pdf = AppKit.NSButton.checkboxWithTitle_target_action_("Can make PDFs", None, None)
    pdf.setFrame_(AppKit.NSMakeRect(118, y, 320, 20))
    pdf.setState_(AppKit.NSControlStateValueOn if "create_pdf" in tools else AppKit.NSControlStateValueOff)
    form.addSubview_(pdf)
    form.setFrameSize_(AppKit.NSMakeSize(460, y + 26))     # just as tall as what is in it

    alert = AppKit.NSAlert.alloc().init()
    alert.setMessageText_(f"Edit {agent['name']}" if agent else "Add an agent")
    alert.setInformativeText_("If its model can't answer (rate limit, no credit, down), the backups take over, "
                              "then the backups for every agent.")
    alert.setAccessoryView_(form)
    alert.addButtonWithTitle_("Save")
    alert.addButtonWithTitle_("Cancel")
    alert.window().setInitialFirstResponder_(role_field if agent else name_field)
    while True:
        if alert.runModal() != AppKit.NSAlertFirstButtonReturn:
            return
        name_value = str(name_field.stringValue()).strip()
        chosen = [m for m in (_chosen(p) for p in menus) if m]
        problem = ""
        if not name_value:
            problem = "It needs a name."
        elif not agent and registry.get(name_value) and registry.get(name_value)["name"].lower() == name_value.lower():
            problem = f"There is already an agent called {name_value}."
        elif not chosen:
            problem = "Pick a model."
        if not problem:
            break
        _confirm("Not saved yet.", problem, only_ok=True)

    new = dict(agent) if agent else {"name": name_value}
    new["pal"], new["color"] = looks["pal"], looks["color"]
    new["role"] = str(role_field.stringValue()).strip() or new.get("role", "")
    instructions = str(text.string()).strip()
    new["instructions"] = instructions or f"You are {name_value}. {new['role']} Work carefully and report concisely."
    new["models"] = list(dict.fromkeys(chosen))
    new["models_v2"] = new["chose_models"] = True
    new["thinking"] = levels[thinking.indexOfSelectedItem()][0]
    kept = [t for t in (agent.get("tools") or []) if t not in WEB_TOOLS + ["create_pdf", "watch_video"]] if agent \
        else list(BASE_TOOLS)
    if web.state() == AppKit.NSControlStateValueOn:
        kept = WEB_TOOLS + kept
    if pdf.state() == AppKit.NSControlStateValueOn:
        kept.append("create_pdf")
    new["tools"] = list(dict.fromkeys(kept))
    registry.save(new)
    missing = sorted({m.split("/", 1)[0] for m in new["models"] if not catalog.configured(m.split("/", 1)[0])})
    if missing:
        _confirm(f"Saved {new['name']}.", f"{', '.join(catalog.all_providers()[p]['label'] for p in missing)} has no "
                 "key yet - add it under AI models for your agents on this page; until then the backups run.", only_ok=True)
    win.refresh()
