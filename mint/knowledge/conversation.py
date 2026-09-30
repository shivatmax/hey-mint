"""Conversation history and a rolling summary of it.

Every turn is appended to history.jsonl. Once enough new conversation has
built up, a fast Flash model folds it into summary.md in the background - never
on the voice path. Each new Live session starts with that summary in its
instructions, so Mint remembers across reconnects, restarts and long days.

Gemini Live separately compresses its own context within a session (sliding
window); this is the layer that survives beyond one session.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

from mint.core import config

log = logging.getLogger("mint.knowledge.conversation")

HISTORY = config.PROJECT_ROOT / "history.jsonl"
SUMMARY = config.PROJECT_ROOT / "summary.md"
# Tried in order until one answers. On this free key, single models were
# often out: gemini-3-flash-preview hit its quota (429) and gemini-3.5-flash
# was overloaded (503) at the same moment, while the lite models answered.
# On the free tier the Flash models allow ~20 requests a day each and ran out
# (AI Studio, 24 Sep: 3/3.5/3.6/3.8 Flash all at their daily cap) while the
# lite models had room (3.5 Flash Lite: 15 a minute) - so lite first.
MODELS = [m for m in (os.environ.get("MINT_SUMMARY_MODEL"), "gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
                      "gemini-flash-lite-latest", "gemini-3.7-flash", "gemini-3.5-flash",
                      "gemini-flash-latest") if m]

# Summarise once this much new conversation has accumulated (~2k tokens).
SUMMARIZE_AFTER_CHARS = 8000
KEEP_SUMMARY_CHARS = 2500   # small on purpose: facts live in the memory bank

_PROMPT = """You keep the running notes of a voice assistant called Mint that operates the \
user's Mac. Merge the new conversation into the existing notes.

The notes are about the CONVERSATION and the WORK: what the user has been \
asking for, what was done and where (which app, doc, tab, project, with exact \
names), what is unfinished or promised, and decisions made. Durable facts \
about the user (people, accounts, preferences) are kept elsewhere - do not \
repeat them here. Drop chit-chat and tool mechanics. Short Markdown bullets \
under: In progress, Done recently, Open / promised. Never invent anything. At \
most {limit} characters.

EXISTING NOTES:
{summary}

NEW CONVERSATION:
{turns}

UPDATED NOTES:"""


class Memory:
    def __init__(self) -> None:
        self._pending: list[dict] = []
        self._pending_chars = 0
        self._lock = threading.Lock()
        self._working = False

    # --- recording ---------------------------------------------------------------

    def add(self, role: str, text: str) -> None:
        text = " ".join(str(text).split())
        if not text:
            return
        entry = {"t": time.strftime("%Y-%m-%d %H:%M"), "role": role, "text": text[:2000]}
        try:
            with open(HISTORY, "a") as f:
                f.write(json.dumps(entry) + "\n")
            os.chmod(HISTORY, 0o600)
        except OSError:
            pass
        with self._lock:
            self._pending.append(entry)
            self._pending_chars += len(entry["text"])
        if self._pending_chars >= SUMMARIZE_AFTER_CHARS:
            self.summarize_in_background()
        # The same stream teaches skills: long or corrected tasks become how-tos.
        from mint.knowledge.learner import learner
        learner.observe(role, entry["text"])

    # --- summarising -------------------------------------------------------------

    def summary(self) -> str:
        try:
            return SUMMARY.read_text().strip()
        except OSError:
            return ""

    def summarize_in_background(self) -> None:
        with self._lock:
            if self._working or not self._pending:
                return
            self._working = True
            batch, self._pending, self._pending_chars = self._pending, [], 0
        threading.Thread(target=self._summarize, args=(batch,), daemon=True,
                         name="mint-summarize").start()

    def summarize_now(self) -> str:
        """Fold everything pending into the summary, synchronously. Returns it."""
        with self._lock:
            batch, self._pending, self._pending_chars = self._pending, [], 0
        if batch:
            self._summarize(batch)
        return self.summary()

    def _summarize(self, batch: list[dict]) -> None:
        try:
            from google import genai

            turns = "\n".join(f"[{e['t']}] {e['role']}: {e['text']}" for e in batch)
            prompt = _PROMPT.format(limit=KEEP_SUMMARY_CHARS, summary=self.summary() or "(empty)",
                                    turns=turns)
            from mint.core import gemini_keys
            client = gemini_keys.client()          # key 2 first, key 1 as backup
            last_error = None
            for model in MODELS:
                try:
                    started = time.monotonic()
                    reply = client.models.generate_content(model=model, contents=prompt)
                    text = (reply.text or "").strip()
                    if text:
                        SUMMARY.write_text(text[: KEEP_SUMMARY_CHARS + 500] + "\n")
                        os.chmod(SUMMARY, 0o600)
                        log.info("memory summarised %d turns with %s in %.1fs",
                                 len(batch), model, time.monotonic() - started)
                        print(f"  [memory: summarised {len(batch)} turns with {model}]", flush=True)
                        # Durable facts go to the memory bank, block by block.
                        try:
                            from mint.knowledge import memory as membank
                            membank.extract("\n".join(f"{e['role']}: {e['text']}" for e in batch
                                                       if e["role"] in ("user", "mint")))
                        except Exception as error:
                            log.warning("fact extraction failed: %s", str(error)[:120])
                        return
                except Exception as error:   # try the next model
                    last_error = error
            raise RuntimeError(last_error)
        except Exception as error:
            log.warning("summary failed: %s", str(error)[:160])
            with self._lock:   # keep the turns for next time
                self._pending = batch + self._pending
                self._pending_chars += sum(len(e["text"]) for e in batch)
        finally:
            with self._lock:
                self._working = False

    def clear(self) -> None:
        with self._lock:
            self._pending, self._pending_chars = [], 0
        for path in (HISTORY, SUMMARY):
            try:
                path.unlink()
            except OSError:
                pass


memory = Memory()
