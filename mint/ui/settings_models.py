"""Settings ▸ Models & agents: API keys for every model provider, the backup models, and the sub-agents.

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

from mint.agents import catalog
from mint.agents import registry

KEYED = ("openai", "anthropic", "openrouter", "groq", "xai")
_refreshed = [0.0]


def _shown(value: str) -> str:
    return f"Set  ••••{value[-4:]}" if len(value) > 8 else ("Not set" if not value else "Set")


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
            "agents": registry.load()}


def page(win, page, facts: dict) -> None:
    _gemini_section(win, page, facts)
    _providers_section(win, page, facts)
    _backups_section(win, page, facts)
    _agents_section(win, page, facts)


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
        card, top, x, h = page.row(title, hint + (f"\nResting now: {resting[:90]}" if resting else ""), control_w=250)
        label = win._label(card, _shown(value), x, top + (h - 18) / 2, 150, size=12, alpha=0.7)
        label.setAlignment_(AppKit.NSTextAlignmentRight)
        win._button(card, "Change…" if value else "Add…", x + 158, top + (h - 28) / 2, 92,
                    lambda e=env, t=f"Gemini {title}": _ask_key(win, e, t, required=e == "GEMINI_API_KEY"))
    if facts["gemini_keys"] > 1:
        win._row_popup(page, "gemini_key_mode", "Using two keys",
                       [("split", "Voice on key 1, the rest on key 2"), ("primary", "Key 1 first, key 2 as backup")],
                       hint="Either way each key covers for the other when it is rate-limited or refused.", w=280)
    page.end("Keys stay on this Mac, in a file only you can read.")


# --- providers ---------------------------------------------------------------------------------------

def _providers_section(win, page, facts: dict) -> None:
    page.section("Model providers for agents")
    page.text("Add a key for any provider you want your agents to use; Test checks the key and lists its models. "
              "Each agent picks its models below, with backups when one is rate-limited, out of credit or down.",
              size=12, alpha=0.7)
    for provider in KEYED:
        spec = catalog.PROVIDERS[provider]
        value = facts["keys"].get(provider, "")
        card, top, x, h = page.row(spec["label"], spec["hint"], control_w=330)
        label = win._label(card, _shown(value), x, top + (h - 18) / 2, 130, size=12, alpha=0.7)
        label.setAlignment_(AppKit.NSTextAlignmentRight)
        label.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
        win._button(card, "Change…" if value else "Add…", x + 138, top + (h - 28) / 2, 92,
                    lambda p=provider: _ask_key(win, catalog.PROVIDERS[p]["env"], catalog.PROVIDERS[p]["label"]))
        button = win._button(card, "Test", x + 238, top + (h - 28) / 2, 92, lambda p=provider, l=label: _test(p, l))
        button.setEnabled_(bool(value))

    # Ollama: an address, not a key.
    card, top, x, h = page.row("Ollama (on this Mac)", catalog.PROVIDERS["ollama"]["hint"], control_w=330)
    field = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(x, top + (h - 24) / 2, 230, 24))
    field.setStringValue_(facts["settings"].get("ollama_base") or "")
    field.setPlaceholderString_(catalog.PROVIDERS["ollama"]["base"])
    field.setBezelStyle_(AppKit.NSTextFieldRoundedBezel)
    field.setDelegate_(win.target)
    save = lambda control: catalog.save_settings({"ollama_base": str(control.stringValue()).strip()})  # noqa: E731
    win._handlers[objc.pyobjc_id(field)] = save
    win._ended_handlers[objc.pyobjc_id(field)] = save
    card.addSubview_(field)
    status = win._label(card, "", 16, top + h - 16, 400, h=14, size=10, alpha=0.6)
    win._button(card, "Test", x + 238, top + (h - 28) / 2, 92, lambda: (save(field), _test("ollama", status)))

    for cid, spec in facts["custom"].items():
        card, top, x, h = page.row(spec["label"], f"OpenAI-compatible · {spec['base']}"
                                   + (" · key set" if spec["has_key"] else " · no key"), control_w=190)
        status = win._label(card, "", 16, top + h - 16, 400, h=14, size=10, alpha=0.6)
        win._button(card, "Test", x, top + (h - 28) / 2, 92, lambda c=cid, s=status: _test(c, s))
        win._button(card, "Remove", x + 98, top + (h - 28) / 2, 92,
                    lambda c=cid, label=spec["label"]: (_confirm(f"Remove the endpoint {label}?",
                                            "Agents that use it move on to their backups.")
                                   and (catalog.remove_custom(c), win.refresh())))
    win._row_buttons(page, "Another OpenAI-compatible endpoint",
                     [("Add endpoint…", 130, lambda: _add_endpoint(win))],
                     hint="LM Studio, vLLM, Together, DeepSeek, a company gateway - anything that speaks "
                          "/v1/chat/completions.")
    page.end()


def _test(provider: str, label) -> None:
    label.setStringValue_("Testing…")

    def run():
        words = catalog.test(provider)
        AppHelper.callAfter(label.setStringValue_, words)
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
    popup = AppKit.NSPopUpButton.alloc().initWithFrame_pullsDown_(AppKit.NSMakeRect(0, 0, width, 26), False)
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
        if buttons:
            made = win._row_buttons(page, f"●  {agent['name']}", buttons, hint=detail)
        else:
            win._row_value(page, f"●  {agent['name']}", "", hint=detail, w=40)
            made = []
        _tint_dot(page, agent)
    win._row_buttons(page, "", [("Add an agent…", 140, lambda: _edit_agent(win, None))],
                     hint="Your own agent: its job, how it should work, and which models it uses.")
    page.end("Mint picks an agent by its role; you can also ask by name (\"ask Nova to…\").")


def _tint_dot(page, agent) -> None:
    """Colour the ● of the row just added with the agent's colour."""
    try:
        rgb = registry.color_rgb(agent)
        for view in reversed(page.card.subviews()):
            if isinstance(view, AppKit.NSTextField) and str(view.stringValue()).startswith("●"):
                text = AppKit.NSMutableAttributedString.alloc().initWithString_(str(view.stringValue()))
                text.addAttribute_value_range_(AppKit.NSForegroundColorAttributeName,
                                               AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(*rgb, 1.0),
                                               AppKit.NSMakeRange(0, 1))
                view.setAttributedStringValue_(text)
                break
    except Exception:
        pass


def _remove_agent(win, name: str) -> None:
    if _confirm(f"Remove the agent {name}?", "Its files stay where they are."):
        registry.remove(name)
        win.refresh()


def _edit_agent(win, name: str | None) -> None:
    agent = registry.get(name) if name else None
    models = list(agent.get("models") or []) if agent else [registry.DEFAULT_MODEL]
    form = _Form.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 460, 404))
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
    y += 32
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
    thinking = AppKit.NSPopUpButton.alloc().initWithFrame_pullsDown_(AppKit.NSMakeRect(120, y, 200, 26), False)
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

    new = dict(agent) if agent else {"name": name_value, "color": registry.next_color()}
    new["role"] = str(role_field.stringValue()).strip() or new.get("role", "")
    instructions = str(text.string()).strip()
    new["instructions"] = instructions or f"You are {name_value}. {new['role']} Work carefully and report concisely."
    new["models"] = list(dict.fromkeys(chosen))
    new["models_v2"] = True
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
                 "key yet - add it above; until then the backups run.", only_ok=True)
    win.refresh()
