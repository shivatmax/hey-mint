"""Learning skills from experience, in the background.

Every turn and tool result passes through memory.add, which hands it here.
Entries are grouped into episodes - a stretch of work separated by quiet. When
an episode ends, it is worth a skill if it was hard:

* many steps (at least LONG_EPISODE tool calls of real work), or
* something failed and a later attempt worked (the route that worked is the lesson), or
* the user helped or corrected along the way, or asked to save it.

Then two models split the job. Jev decides, from the real list, whether an
existing skill already covers this task (so it is updated rather than
duplicated). A Flash model writes the skill itself from the transcript: only
the steps that worked, generalised, with the user's corrections as notes.
Nothing here is on the voice path; it runs after the work is done.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time

from . import config, jev, skillbook

log = logging.getLogger("mint.learner")

QUIET = 25.0          # seconds of nothing that close an episode
LONG_EPISODE = 6      # tool calls that make a task "long"
_BOOKKEEPING = {"plan_task", "step_done", "find_skill", "skill_result", "list_skills", "recall",
                "remember", "forget", "get_status", "list_open", "frontmost_app", "stop_listening",
                "set_preference", "show_chat", "list_accounts", "create_skill", "update_skill",
                "delete_skill", "express", "set_voice", "ui_elements", "show_chat", "recall_memory"}
_TEACHING = re.compile(r"\b(save (this|that|it) as a skill|make (this|that|it) a skill|learn (this|that|how)|"
                       r"remember how|next time|no,? (click|use|it's|its|the)|not that|wrong)\b", re.I)

_listeners: list = []


def on_learned(callback) -> None:
    """callback(action, title, category) whenever a skill is created or updated."""
    _listeners.append(callback)


class Learner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._episode: list[dict] = []
        self._timer: threading.Timer | None = None
        self._last_request = ""
        self._skills_used: list[str] = []

    def last_request(self) -> str:
        return self._last_request

    def note_skill(self, name: str) -> None:
        """A skill was handed to Gemini in this episode (context pack or find_skill)."""
        with self._lock:
            if name not in self._skills_used:
                self._skills_used.append(name)

    def observe(self, role: str, text: str) -> None:
        if os.environ.get("MINT_NO_LEARNING"):
            return
        with self._lock:
            # Tool results arrive BEFORE the user's words: Gemini Live records a
            # user turn only when the whole exchange ends. So collect everything.
            self._episode.append({"role": role, "text": text[:600], "t": time.time()})
            if role == "user":
                self._last_request = text[:600]
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(QUIET, self._close)
            self._timer.daemon = True
            self._timer.start()

    def _close(self) -> None:
        with self._lock:
            episode, self._episode, self._timer = self._episode, [], None
            used, self._skills_used = self._skills_used, []
        try:
            self._score(episode, used)
            self.consider(episode, used)
        except Exception:
            log.exception("learning from an episode failed")

    # --- deciding -------------------------------------------------------------------

    @staticmethod
    def _tool(entry: dict) -> str:
        return entry["text"].split("(", 1)[0].strip()

    def worth_learning(self, episode: list[dict]) -> str:
        """Why this episode deserves a skill, or '' if it does not."""
        tools = [e for e in episode if e["role"] == "tool" and self._tool(e) not in _BOOKKEEPING]
        if any(self._tool(e) in {"create_skill", "update_skill"} for e in episode if e["role"] == "tool"):
            return ""                       # Mint already saved it by hand
        failed = [i for i, e in enumerate(tools) if re.search(r"FAILED|Could not|NOT RUN|cannot", e["text"])]
        recovered = failed and any(i > failed[0] and not re.search(r"FAILED|Could not|cannot", e["text"])
                                   for i, e in enumerate(tools))
        users = [e["text"] for e in episode if e["role"] == "user"]
        taught = len(users) > 1 and any(_TEACHING.search(u) for u in users[1:])
        asked = any(re.search(r"\b(save|make|create) (this|that|it|a skill)\b.*\bskill\b", u, re.I) for u in users)
        if asked:
            return "the user asked to save it"
        if taught and tools:
            return "the user corrected or helped"
        if len(tools) >= LONG_EPISODE:
            return f"it took {len(tools)} steps"
        if recovered:
            return "a first attempt failed and another worked"
        return ""

    def _score(self, episode: list[dict], used: list[str]) -> None:
        """Wins and fails for skills Gemini followed but did not report on."""
        if not used:
            return
        reported = {e["text"] for e in episode if e["role"] == "tool" and self._tool(e) == "skill_result"}
        work = [e for e in episode if e["role"] == "tool" and self._tool(e) not in _BOOKKEEPING]
        if not work:
            return
        # A skill counts as worked only if real work happened and the final
        # reply does not admit failure: one successful open_app followed by
        # silence was once scored as a win.
        replies = [e["text"] for e in episode if e["role"] in ("jarvis", "mint")]
        if len(work) < 2 or not replies:
            return
        gave_up = re.search(r"couldn't|could not|can't|cannot|unable|failed|try again|didn't work",
                            replies[-1], re.I)
        worked = not gave_up and not re.search(r"FAILED|Could not|cannot", work[-1]["text"])
        for name in used:
            if not any(name in r for r in reported):
                skillbook.record_result(name, worked)
                print(f"  [skill {name}: {'worked' if worked else 'failed'} (scored from the result)]", flush=True)

    def consider(self, episode: list[dict], used: list[str] | None = None) -> None:
        if not any(e["role"] == "user" for e in episode):
            return                          # timers, system notes: nobody asked for anything
        why = self.worth_learning(episode)
        if not why:
            return
        transcript = "\n".join(f"{e['role']}: {e['text']}" for e in episode)[-9000:]
        request = next((e["text"] for e in episode if e["role"] == "user"), "")

        # Jev: is there already a skill for this? Update it rather than duplicate.
        existing = None
        if used:
            existing = skillbook.get(used[0], fuzzy=False)   # the skill that was being followed
        if existing is None and skillbook.all_skills():
            existing, _ = skillbook.find(request)
        draft = self._draft(transcript, existing, why)
        if not draft or draft.get("action") == "skip":
            log.info("learner skipped an episode (%s): %s", why, (draft or {}).get("reason", "no draft"))
            print(f"  [learning: nothing reusable ({why})]", flush=True)
            return
        steps, notes = draft.get("steps") or [], draft.get("notes") or []
        if existing is None and draft.get("action") == "update" and draft.get("update_name"):
            existing = skillbook.get(str(draft["update_name"]), fuzzy=False)
        if existing is not None and draft.get("action") == "update":
            skill, message = skillbook.update(existing["name"], steps=steps or None,
                                              replace_notes=notes if notes else None,
                                              when=draft.get("when", ""), source="auto")
            action = "updated"
        else:
            skill, message = skillbook.create(
                draft.get("title", ""), draft.get("when", ""), steps, notes,
                category=draft.get("category", ""), apps=", ".join(draft.get("apps") or []), source="auto")
            action = "created"
        if skill is None:
            print(f"  [learning: {message}]", flush=True)
            return
        print(f"  [skill {action}: {skill['category']}/{skill['name']} - {why}]", flush=True)
        for callback in list(_listeners):
            try:
                callback(action, skill["title"], skill["category"])
            except Exception:
                log.exception("on_learned callback failed")

    def _draft(self, transcript: str, existing: dict | None, why: str) -> dict | None:
        from google import genai
        from google.genai import types

        from .memory import MODELS

        prompt = f"""You write "skills" for Mint, a voice assistant that operates a Mac through tools
(open_app, open_url, open_chrome, ui_act(action, target, text) - THE way to click or type in an app;
ui_act action "dismiss" closes a menu/dialog/popup (use it, not Escape: some menus ignore Escape),
type_text(text, field), press_key, scroll, read_window, look, open_slack...). A skill is a short, reusable how-to that
lets Mint do the same kind of task quickly next time.

This episode was flagged because {why}. Transcript (user, mint, and tool calls with results):
{transcript}

{"EXISTING SKILL that covers this task (update it with what was learned):" + chr(10) + existing["body"] if existing else "Saved skills (update one instead of creating a near-duplicate; give its name as update_name):" + chr(10) + (chr(10).join(f"- name: {k['name']} | {k['title']} | when: {k['meta'].get('when', '')}" for k in skillbook.all_skills()[:40]) or "(none)")}

Existing categories: {", ".join(skillbook.categories()) or "(none yet)"}

Write JSON only:
{{"action": "create" | "update" | "skip",
  "reason": "one line",
  "update_name": "name of the saved skill to update, when action is update",
  "title": "short imperative title, e.g. Create a project in ChatGPT",
  "when": "when to use it, in terms of what the user asks",
  "apps": ["app names involved"],
  "category": "folder path, 1-3 levels, e.g. apps/chatgpt or coding/claude-code or browser/google-docs",
  "steps": ["numbered-free step text naming the tool and the exact on-screen label to use"],
  "notes": ["gotchas: what failed and why, what the user corrected, timing"]}}

For "update": return the COMPLETE, consolidated notes list for the skill (at most 6) - merge
the existing notes with what was learned, drop duplicates and anything outdated. Never advise
click_at or guessed coordinates over ui_act, and never record a workaround for a tool bug as
a rule; record what the app needs (which control, what order, what to wait for).

Rules: keep only steps that WORKED, in order, generalised (a placeholder like <project name>
instead of this one's specifics). Name exact on-screen labels that worked. Put what failed and
the user's corrections in notes. "update" when a saved skill already covers this kind of task (even with different specifics). "skip" if the
work did not succeed at all and nothing was learned, or it is a one-off with no reusable path.
Never include passwords, keys, card numbers or private message content."""
        client = genai.Client(api_key=os.environ[config.API_KEY_ENV])
        for model in MODELS:
            try:
                reply = client.models.generate_content(
                    model=model, contents=prompt,
                    config=types.GenerateContentConfig(response_mime_type="application/json"))
                text = (reply.text or "").strip()
                data = json.loads(text[text.find("{"): text.rfind("}") + 1])
                if isinstance(data, dict):
                    return data
            except Exception as error:
                log.info("skill draft with %s failed: %s", model, str(error)[:160])
        return None


learner = Learner()
