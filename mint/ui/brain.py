"""Skills & Memory: see and edit everything Mint has learned.

    Skills  every skill by category with its record (uses, wins, rank); pick one
            to edit its title, when-to-use, apps, category and its steps and
            notes as text; New, Save, Delete (archived, not destroyed), Show in
            Finder, Pinned (nothing automatic may change it), and Undo change
            (puts back the skill as it was before its newest change - the
            ledger keeps every version, and an undo can itself be undone).
    Memory  every remembered fact: fixed or not, group and text are edited in
            the table itself and saved at once; add a fact (filed by Jev, or
            exactly as typed), delete, and "Test recall" to see which facts Mint
            would pull up for a question.

Open with open_window("skills" | "memory") from any thread: from Settings, the
menu, or by asking Mint ("show me your skills").
"""

from __future__ import annotations

import threading

import AppKit
import objc
from PyObjCTools import AppHelper

from mint.knowledge import memory as membank
from mint.core import prefs
from mint.knowledge import skill_ledger
from mint.knowledge import skills as skillbook

W, H = 900, 620
_window = None          # the one BrainWindow


def open_window(tab: str = "skills") -> None:
    """Any thread."""
    def show():
        global _window
        if _window is None:
            _window = BrainWindow()
        _window.show(tab)
    AppHelper.callAfter(show)


def _bg(work, done) -> None:
    """Run `work` off the main thread (Jev calls), then `done(result)` on it."""
    def run():
        try:
            result = work()
        except Exception as error:      # show it rather than lose it
            result = f"Failed: {error}"
        AppHelper.callAfter(done, result)
    threading.Thread(target=run, daemon=True).start()


class _BrainTarget(AppKit.NSObject):
    """Buttons, text fields, both tables' data source and delegate, the window's delegate."""

    def initWithOwner_(self, owner):
        self = objc.super(_BrainTarget, self).init()
        if self is None:
            return None
        self.owner = owner
        return self

    def act_(self, sender):
        self.owner._clicked(sender)

    def controlTextDidChange_(self, note):
        self.owner._typed(note.object())

    def textDidChange_(self, note):
        self.owner._body_changed()

    def numberOfRowsInTableView_(self, table):
        return self.owner._count(table)

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        return self.owner._value(table, str(column.identifier()), row)

    def tableView_setObjectValue_forTableColumn_row_(self, table, value, column, row):
        self.owner._edit(table, str(column.identifier()), row, value)

    def tableViewSelectionDidChange_(self, note):
        self.owner._selection(note.object())

    def windowDidBecomeKey_(self, note):
        self.owner._reload()

    def windowWillClose_(self, note):
        self.owner.window = None


class BrainWindow:
    def __init__(self) -> None:
        self.window = None
        self._handlers: dict[int, callable] = {}
        self._skills: list[dict] = []
        self._shown_skills: list[dict] = []
        self._current = None             # path of the skill in the editor
        self._dirty = False
        self._blocks: list[dict] = []
        self._shown_blocks: list[dict] = []

    # --- building blocks ---------------------------------------------------------------

    def _label(self, view, text, x, y, w, h=18, size=12, bold=False, alpha=1.0, lines=1):
        field = AppKit.NSTextField.labelWithString_(text)
        field.setFrame_(AppKit.NSMakeRect(x, y, w, h))
        field.setFont_(AppKit.NSFont.boldSystemFontOfSize_(size) if bold else AppKit.NSFont.systemFontOfSize_(size))
        field.setTextColor_(AppKit.NSColor.labelColor().colorWithAlphaComponent_(alpha))
        field.setMaximumNumberOfLines_(lines)
        field.setLineBreakMode_(AppKit.NSLineBreakByWordWrapping)
        view.addSubview_(field)
        return field

    def _field(self, view, x, y, w, placeholder="", h=24, handler=None):
        field = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(x, y, w, h))
        field.setPlaceholderString_(placeholder)
        field.setDelegate_(self.target)
        if handler is not None:
            self._handlers[objc.pyobjc_id(field)] = handler
        view.addSubview_(field)
        return field

    def _search(self, view, x, y, w, placeholder, handler):
        field = AppKit.NSSearchField.alloc().initWithFrame_(AppKit.NSMakeRect(x, y, w, 26))
        field.setPlaceholderString_(placeholder)
        field.setDelegate_(self.target)
        self._handlers[objc.pyobjc_id(field)] = handler
        view.addSubview_(field)
        return field

    def _button(self, view, title, x, y, w, handler, key=""):
        button = AppKit.NSButton.buttonWithTitle_target_action_(title, self.target, "act:")
        button.setBezelStyle_(AppKit.NSBezelStyleRounded)
        button.setFrame_(AppKit.NSMakeRect(x, y, w, 30))
        if key:
            button.setKeyEquivalent_(key)
        self._handlers[objc.pyobjc_id(button)] = lambda: handler()
        view.addSubview_(button)
        return button

    def _table(self, view, x, y, w, h, columns):
        """columns: [(identifier, title, width, editable, checkbox)]"""
        scroll = AppKit.NSScrollView.alloc().initWithFrame_(AppKit.NSMakeRect(x, y, w, h))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(AppKit.NSBezelBorder)
        table = AppKit.NSTableView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, w, h))
        table.setUsesAlternatingRowBackgroundColors_(True)
        table.setAllowsMultipleSelection_(True)
        table.setColumnAutoresizingStyle_(AppKit.NSTableViewLastColumnOnlyAutoresizingStyle)
        for ident, title, width, editable, checkbox in columns:
            column = AppKit.NSTableColumn.alloc().initWithIdentifier_(ident)
            column.headerCell().setStringValue_(title)
            column.setWidth_(width)
            column.setEditable_(editable)
            if checkbox:
                cell = AppKit.NSButtonCell.alloc().init()
                cell.setButtonType_(AppKit.NSButtonTypeSwitch)
                cell.setTitle_("")
                column.setDataCell_(cell)
            else:
                column.dataCell().setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
            table.addTableColumn_(column)
        table.setDataSource_(self.target)
        table.setDelegate_(self.target)
        scroll.setDocumentView_(table)
        view.addSubview_(scroll)
        return table

    def _text_area(self, view, x, y, w, h):
        scroll = AppKit.NSScrollView.alloc().initWithFrame_(AppKit.NSMakeRect(x, y, w, h))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(AppKit.NSBezelBorder)
        text = AppKit.NSTextView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, w, h))
        text.setFont_(AppKit.NSFont.monospacedSystemFontOfSize_weight_(12, AppKit.NSFontWeightRegular))
        text.setRichText_(False)
        text.setAutomaticQuoteSubstitutionEnabled_(False)
        text.setAutomaticDashSubstitutionEnabled_(False)
        text.setDelegate_(self.target)
        text.setMinSize_(AppKit.NSMakeSize(0, h))
        text.setMaxSize_(AppKit.NSMakeSize(1e7, 1e7))
        text.setVerticallyResizable_(True)
        text.textContainer().setWidthTracksTextView_(True)
        scroll.setDocumentView_(text)
        view.addSubview_(scroll)
        return text

    # --- the two tabs --------------------------------------------------------------------

    def _tab_skills(self, view) -> None:
        h = H - 60
        self._label(view, "Skills: learned how-tos. Jev picks one for each request.", 16, h - 30, 560,
                    bold=True, size=13)
        self.skill_search = self._search(view, 16, h - 66, 300, "Search skills", self._filter_skills)
        self.skill_table = self._table(view, 16, 56, 300, h - 132, [
            ("title", "Skill", 170, False, False), ("record", "Record", 110, False, False)])
        self._button(view, "New", 16, 14, 80, self._new_skill)
        self._button(view, "Delete", 100, 14, 90, self._delete_skill)
        self.skill_count = self._label(view, "", 196, 20, 120, alpha=0.6, size=11)

        x, w = 332, W - 32 - 332 - 16
        self._label(view, "Title", x, h - 60, 80, alpha=0.7)
        self.f_title = self._field(view, x + 84, h - 64, w - 84, handler=lambda: self._mark_dirty())
        self._label(view, "When to use", x, h - 92, 84, alpha=0.7)
        self.f_when = self._field(view, x + 84, h - 96, w - 84, handler=lambda: self._mark_dirty())
        self._label(view, "Apps", x, h - 124, 80, alpha=0.7)
        self.f_apps = self._field(view, x + 84, h - 128, 170, "ChatGPT, Slack…", handler=lambda: self._mark_dirty())
        self._label(view, "Category", x + 266, h - 124, 70, alpha=0.7)
        self.f_category = self._field(view, x + 336, h - 128, w - 336, "apps/chatgpt",
                                      handler=lambda: self._mark_dirty())
        self.skill_info = self._label(view, "", x, h - 152, w - 84, alpha=0.6, size=11)
        self.f_pinned = AppKit.NSButton.checkboxWithTitle_target_action_("Pinned", self.target, "act:")
        self.f_pinned.setFrame_(AppKit.NSMakeRect(x + w - 78, h - 154, 78, 20))
        self.f_pinned.setToolTip_("A pinned skill is never changed or removed by anything automatic.")
        self._handlers[objc.pyobjc_id(self.f_pinned)] = self._toggle_pin
        view.addSubview_(self.f_pinned)
        self.body = self._text_area(view, x, 56, w, h - 216)
        self._button(view, "Save", x + w - 100, 14, 100, self._save_skill, key="s")
        self._button(view, "Show in Finder", x + w - 250, 14, 146, self._reveal_skill)
        undo = self._button(view, "Undo change", x + w - 366, 14, 112, self._undo_skill)
        undo.setToolTip_("Put this skill back as it was before its newest change (that can be undone too).")
        self.skill_status = self._label(view, "", x, 12, w - 372, h=32, alpha=0.7, size=11, lines=2)

    def _tab_memory(self, view) -> None:
        h = H - 60
        self._label(view, "Memory: one fact per row. Fixed facts are in every conversation; the rest are "
                          "looked up when a question needs them.", 16, h - 38, W - 64, h=34,
                    bold=True, size=13, lines=2)
        self.new_fact = self._field(view, 16, h - 76, 470, "Add a fact, e.g. My manager is Meera")
        self.new_group = AppKit.NSPopUpButton.alloc().initWithFrame_pullsDown_(
            AppKit.NSMakeRect(492, h - 77, 150, 26), False)
        self.new_group.addItemWithTitle_("Jev decides")
        for group in membank.GROUPS:
            self.new_group.addItemWithTitle_(group)
        view.addSubview_(self.new_group)
        self.new_fixed = AppKit.NSButton.checkboxWithTitle_target_action_("Fixed", None, None)
        self.new_fixed.setFrame_(AppKit.NSMakeRect(650, h - 74, 70, 20))
        view.addSubview_(self.new_fixed)
        self._button(view, "Add", W - 32 - 16 - 100, h - 80, 100, self._add_fact)

        self.fact_search = self._search(view, 16, h - 112, 300, "Filter", self._filter_facts)
        self.fact_table = self._table(view, 16, 110, W - 64, h - 232, [
            ("pinned", "Fixed", 44, True, True), ("group", "Group", 110, True, False),
            ("text", "Fact", 520, True, False), ("info", "Saved", 120, False, False)])
        self._button(view, "Delete", 16, 70, 90, self._delete_facts)
        self.fact_status = self._label(view, "Double-click a group or fact to edit it; tick Fixed to keep it in "
                                             "every conversation.", 116, 76, 600, alpha=0.6, size=11)

        self._label(view, "Test recall", 16, 38, 90, alpha=0.7)
        self.recall_q = self._field(view, 100, 34, 420, "e.g. who do I report to?")
        self._button(view, "Ask", 526, 30, 70, self._test_recall)
        self.recall_out = self._label(view, "", 604, 6, W - 32 - 604 - 16, h=54, size=11, lines=4)

    # --- window ------------------------------------------------------------------------------

    def show(self, tab: str = "skills") -> None:
        if self.window is None:
            self.target = _BrainTarget.alloc().initWithOwner_(self)
            style = (AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable
                     | AppKit.NSWindowStyleMaskMiniaturizable)
            window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                AppKit.NSMakeRect(0, 0, W, H), style, AppKit.NSBackingStoreBuffered, False)
            window.setTitle_(f"{prefs.name()} - Skills & Memory")
            window.setReleasedWhenClosed_(False)
            window.setDelegate_(self.target)
            window.center()
            self.window = window
            tabs = AppKit.NSTabView.alloc().initWithFrame_(AppKit.NSMakeRect(8, 8, W - 16, H - 16))
            for ident, title, build in (("skills", "Skills", self._tab_skills), ("memory", "Memory", self._tab_memory)):
                item = AppKit.NSTabViewItem.alloc().initWithIdentifier_(ident)
                item.setLabel_(title)
                view = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, W - 32, H - 60))
                build(view)
                item.setView_(view)
                tabs.addTabViewItem_(item)
            self.tabs = tabs
            window.setContentView_(tabs)
            self._reload()
        self.tabs.selectTabViewItemWithIdentifier_("memory" if tab == "memory" else "skills")
        AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)

    def _reload(self) -> None:
        """Re-read both stores (the learner may have written since)."""
        if self.window is None:
            return
        self._skills = skillbook.all_skills()
        self._filter_skills()
        self._blocks = membank.blocks()
        self._filter_facts()

    # --- dispatch from _Target ------------------------------------------------------------------

    def _clicked(self, sender) -> None:
        handler = self._handlers.get(objc.pyobjc_id(sender))
        if handler:
            handler()

    def _typed(self, control) -> None:
        handler = self._handlers.get(objc.pyobjc_id(control))
        if handler:
            handler()

    def _is(self, table, name: str) -> bool:
        # The table asks for data as soon as it gets a data source, before the
        # attribute holding it is assigned.
        mine = getattr(self, name, None)
        return mine is not None and table == mine

    def _count(self, table) -> int:
        if self._is(table, "skill_table"):
            return len(self._shown_skills)
        if self._is(table, "fact_table"):
            return len(self._shown_blocks)
        return 0

    def _value(self, table, column, row):
        if not (self._is(table, "skill_table") or self._is(table, "fact_table")):
            return ""
        if self._is(table, "skill_table"):
            if row >= len(self._shown_skills):
                return ""
            skill = self._shown_skills[row]
            if column == "title":
                return f"{skill['title']} — {skill['category']}"
            m = skill["meta"]
            return f"{m['wins']}/{m['uses']} · {'stale' if skillbook.is_stale(skill) else skillbook.rank(skill)}"
        if row >= len(self._shown_blocks):
            return ""
        block = self._shown_blocks[row]
        if column == "pinned":
            return 1 if block.get("pinned") else 0
        if column == "info":
            return f"{block.get('updated', '')[5:]} {block.get('source', '')}"
        return block.get(column, "")

    def _edit(self, table, column, row, value) -> None:
        if not self._is(table, "fact_table") or row >= len(self._shown_blocks):
            return
        block = self._shown_blocks[row]
        if column == "pinned":
            message = membank.edit_block(block["id"], pinned=bool(value))
        elif column == "group":
            message = membank.edit_block(block["id"], group=str(value))
        elif column == "text":
            message = membank.edit_block(block["id"], text=str(value))
        else:
            return
        self.fact_status.setStringValue_(message)
        self._blocks = membank.blocks()
        self._filter_facts()

    def _selection(self, table) -> None:
        if not self._is(table, "skill_table"):
            return
        row = table.selectedRow()
        if 0 <= row < len(self._shown_skills):
            if self._dirty and self._current is not None:
                self._save_skill(quiet=True)
            self._load_skill(self._shown_skills[row])

    # --- skills ------------------------------------------------------------------------------

    def _filter_skills(self) -> None:
        wanted = str(self.skill_search.stringValue() or "").lower().strip()
        self._shown_skills = sorted(
            (s for s in self._skills
             if not wanted or wanted in (s["title"] + " " + s["category"] + " " + s["meta"].get("when", "")
                                         + " " + s["meta"].get("apps", "")).lower()),
            key=lambda s: (s["category"], -skillbook.score(s)))
        self.skill_table.reloadData()
        self.skill_count.setStringValue_(f"{len(self._skills)} skills")
        if self._current is not None:
            for i, s in enumerate(self._shown_skills):
                if s["path"] == self._current:
                    self.skill_table.selectRowIndexes_byExtendingSelection_(
                        AppKit.NSIndexSet.indexSetWithIndex_(i), False)
                    break
        elif self._shown_skills:
            self.skill_table.selectRowIndexes_byExtendingSelection_(AppKit.NSIndexSet.indexSetWithIndex_(0), False)

    def _load_skill(self, skill: dict) -> None:
        self._current = skill["path"]
        m = skill["meta"]
        self.f_title.setStringValue_(skill["title"])
        self.f_when.setStringValue_(m.get("when", ""))
        self.f_apps.setStringValue_(m.get("apps", ""))
        self.f_category.setStringValue_(skill["category"])
        self.body.setString_(skill["body"])
        self.body.scrollRangeToVisible_((0, 0))
        warnings = skillbook.lint(skill)
        self.skill_info.setStringValue_(
            f"Used {m['uses']}×, worked {m['wins']}, failed {m['fails']} · {skillbook.rank(skill)}"
            f"{' (stale)' if skillbook.is_stale(skill) else ''} · by {m.get('created_by', '?')} · "
            f"updated {m.get('updated') or m.get('created', '')}"
            + (f" · ⚠ {len(warnings)} lint" if warnings else ""))
        self.skill_info.setToolTip_("\n".join(warnings) if warnings else None)
        self.f_pinned.setState_(AppKit.NSControlStateValueOn if skillbook.is_pinned(skill)
                                else AppKit.NSControlStateValueOff)
        self._dirty = False
        self.skill_status.setStringValue_("")

    def _mark_dirty(self) -> None:
        self._dirty = True
        self.skill_status.setStringValue_("Unsaved changes (⌘S to save)")

    def _body_changed(self) -> None:
        self._mark_dirty()

    def _save_skill(self, quiet: bool = False) -> None:
        if self._current is None:
            return
        path, message = skillbook.save_raw(
            self._current, str(self.f_title.stringValue()), str(self.f_when.stringValue()),
            str(self.f_apps.stringValue()), str(self.f_category.stringValue()), str(self.body.string()))
        if message == "Saved.":
            self._current, self._dirty = path, False
        if not quiet:
            self.skill_status.setStringValue_(message)
        self._skills = skillbook.all_skills()
        self._filter_skills()

    def _new_skill(self) -> None:
        category = str(self.f_category.stringValue() or "general") if self._current else "general"
        self._current = skillbook.new_blank(category)
        self._skills = skillbook.all_skills()
        self.skill_search.setStringValue_("")
        self._filter_skills()
        fresh = next((s for s in self._skills if s["path"] == self._current), None)
        if fresh:
            self._load_skill(fresh)
        self.skill_status.setStringValue_("New skill - fill it in and Save.")
        self.window.makeFirstResponder_(self.f_title)

    def _delete_skill(self) -> None:
        if self._current is None:
            return
        skill = next((s for s in self._skills if s["path"] == self._current), None)
        if skill is None:
            return
        message = skillbook.archive(skill["name"], actor="user", reason="Delete in Skills & Memory", skill=skill)
        self._current, self._dirty = None, False
        self._skills = skillbook.all_skills()
        self._filter_skills()
        self.skill_status.setStringValue_(message)

    def _toggle_pin(self) -> None:
        if self._current is None:
            return
        if self._dirty:
            self._save_skill(quiet=True)
        on = self.f_pinned.state() == AppKit.NSControlStateValueOn
        self.skill_status.setStringValue_(skillbook.set_pinned(self._current, on))
        self._skills = skillbook.all_skills()
        self._filter_skills()

    def _undo_skill(self) -> None:
        if self._current is None:
            return
        name = self._current.stem
        rows = skillbook.history(name, 1)
        if not rows:
            self.skill_status.setStringValue_("No recorded change to undo for this skill.")
            return
        entry = rows[0]
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_("Undo the newest change to this skill?")
        alert.setInformativeText_(skill_ledger.describe(entry) + "\n\nUnsaved edits here are dropped. The undo is "
                                  "recorded too, so it can be undone.")
        alert.addButtonWithTitle_("Undo")
        alert.addButtonWithTitle_("Cancel")
        if alert.runModal() != AppKit.NSAlertFirstButtonReturn:
            return
        ok, message = skillbook.rollback(entry["id"], actor="user")
        self._dirty = False
        self._skills = skillbook.all_skills()
        if ok:
            restored = next((s for s in self._skills if s["path"].stem == name), None)
            self._current = restored["path"] if restored else None
        self._filter_skills()
        fresh = next((s for s in self._skills if s["path"] == self._current), None)
        if fresh:
            self._load_skill(fresh)
        self.skill_status.setStringValue_(message)

    def _reveal_skill(self) -> None:
        target = self._current or skillbook.ROOT
        AppKit.NSWorkspace.sharedWorkspace().activateFileViewerSelectingURLs_(
            [AppKit.NSURL.fileURLWithPath_(str(target))])

    # --- memory ------------------------------------------------------------------------------

    def _filter_facts(self) -> None:
        wanted = str(self.fact_search.stringValue() or "").lower().strip()
        self._shown_blocks = sorted(
            (b for b in self._blocks if not wanted or wanted in (b["text"] + " " + b["group"]).lower()),
            key=lambda b: (not b.get("pinned"), b["group"], b["id"]))
        self.fact_table.reloadData()

    def _add_fact(self) -> None:
        text = str(self.new_fact.stringValue() or "").strip()
        if not text:
            return
        pinned = self.new_fixed.state() == AppKit.NSControlStateValueOn
        choice = str(self.new_group.titleOfSelectedItem())
        self.fact_status.setStringValue_("Saving…")

        def work():
            if choice == "Jev decides":
                return membank.add(text, pinned=pinned or None, origin="edit")
            return membank.add_exact(text, choice, pinned)

        def done(message):
            self.fact_status.setStringValue_(message)
            self.new_fact.setStringValue_("")
            self._blocks = membank.blocks()
            self._filter_facts()
        _bg(work, done)

    def _delete_facts(self) -> None:
        rows = self.fact_table.selectedRowIndexes()
        chosen = [self._shown_blocks[i] for i in range(len(self._shown_blocks)) if rows.containsIndex_(i)]
        if not chosen:
            self.fact_status.setStringValue_("Select the facts to delete first.")
            return
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_(f"Delete {len(chosen)} fact{'s' if len(chosen) > 1 else ''}?")
        alert.setInformativeText_("\n".join(b["text"] for b in chosen[:6]))
        alert.addButtonWithTitle_("Delete")
        alert.addButtonWithTitle_("Cancel")
        if alert.runModal() != AppKit.NSAlertFirstButtonReturn:
            return
        for block in chosen:
            membank.delete_block(block["id"])
        self.fact_status.setStringValue_(f"Deleted {len(chosen)}.")
        self._blocks = membank.blocks()
        self._filter_facts()

    def _test_recall(self) -> None:
        question = str(self.recall_q.stringValue() or "").strip()
        if not question:
            return
        self.recall_out.setStringValue_("Searching…")

        def done(found):
            if isinstance(found, str):
                self.recall_out.setStringValue_(found)
                return
            fixed = sum(1 for b in self._blocks if b.get("pinned"))
            lines = [f"• {b['text']}" for b in found] or ["Nothing - no stored fact is relevant."]
            self.recall_out.setStringValue_("\n".join(lines[:3]) + f"\n(+ {fixed} fixed facts, always given)")
        _bg(lambda: membank.search(question, k=6), done)
