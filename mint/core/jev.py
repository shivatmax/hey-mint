"""Direct access to Jev (TypeSafe) for fast, calibrated choices.

Jev picks one option from a list it is given and says how sure it is. It cannot
answer outside the list. That makes it the right tool for resolving a spoken
reference - "my work account", "alex at the rate example", "the on call
one" - against things that really exist on this Mac: Chrome profiles, Slack
workspaces, routines. Gemini decides what to do; Jev decides which real thing
was meant.

The TypeSafe key is TYPESAFE_API_KEY in .env. Without it, resolution falls back
to exact and substring matching only.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
from dataclasses import dataclass

from mint.core import config  # noqa: F401 - importing it loads .env, where the key lives

log = logging.getLogger("mint.core.jev")

URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
_key: str | None = None


def _api_key() -> str | None:
    """Mint's own TypeSafe key, from .env.

    Deliberately not read from Desktop Voice's Keychain item: re-saving the key
    in Desktop Voice rebuilds that item's access list, after which reading it
    blocks on a Keychain dialog. In testing that froze the caller for minutes. A
    voice assistant must never stall behind a dialog nobody asked for.
    """
    global _key
    if _key is None:
        _key = os.environ.get("TYPESAFE_API_KEY") or ""
    return _key or None


def available() -> bool:
    """A TypeSafe key is set (without one, callers use their Gemini fallback)."""
    return _api_key() is not None


@dataclass
class Pick:
    id: str | None          # None when Jev chose "none"
    probability: float
    confidence: float

    @property
    def sure(self) -> bool:
        return self.id is not None and self.confidence >= 0.5


def choose(question: str, options: dict[str, str], context: dict | None = None,
           timeout: float = 6.0, instructions: str | None = None) -> Pick | None:
    """Ask Jev which option `question` means. Returns None if Jev is unreachable.

    `options` maps an id to a description. A "none" option is always added, so
    Jev can say that nothing listed fits instead of being forced to pick.
    """
    key = _api_key()
    if not key or not options:
        return None
    criteria = dict(options)
    criteria["none"] = "None of the listed options is the one meant."
    body = {
        "model": MODEL,
        "state": {"request": question, **(context or {})},
        "questions": {
            "pick": {
                "type": "choice",
                "instructions": instructions or (
                    "Which listed option does `request` refer to? The request may be "
                    "speech-to-text, so 'at the rate' or 'at' can mean '@' and 'dot' can "
                    "mean '.'; match on meaning, names, emails and domains. Choose none "
                    "if nothing listed fits."),
                "criteria": criteria,
            }
        },
    }
    request = urllib.request.Request(
        URL, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            answer = json.load(response)["answers"]["pick"]
    except Exception as error:
        log.warning("Jev unavailable: %s", str(error).replace(key, "[redacted]")[:120])
        return None
    chosen = answer.get("choice")
    return Pick(
        id=None if chosen == "none" else chosen,
        probability=float((answer.get("probabilities") or {}).get(chosen, 0.0)),
        confidence=float(answer.get("confidence") or 0.0),
    )


def normalise_spoken(text: str) -> str:
    """Undo common speech-to-text spellings of addresses."""
    text = text.lower().strip()
    text = re.sub(r"\s+at the rate\s+|\s+at the rate of\s+|\s+@\s+", "@", text)
    text = re.sub(r"\s+dot\s+", ".", text)
    return text


def resolve(said: str, options: dict[str, str], aliases: dict[str, str] | None = None,
            what: str = "option") -> tuple[str | None, str]:
    """Map what the user said onto one option id.

    Cheap and exact first: an alias, then a unique substring match. Only when
    that is ambiguous or empty does it ask Jev. Returns (id or None, explanation).
    """
    if not options:
        return None, f"There is no {what} to choose from."
    wanted = normalise_spoken(said)
    for alias, target in (aliases or {}).items():
        if normalise_spoken(alias) == wanted:
            wanted = normalise_spoken(target)
            break

    # Words that describe the request rather than identify the thing.
    filler = {"the", "my", "an", "one", "account", "accounts", "profile", "chrome", "open",
              "for", "and", "with", "workspace", "channel", "slack", "use", "using", "that",
              "this", "please", "can", "you", "work", "personal", "routine", "window"}
    # Split addresses at the @ as well: Chrome names a profile "Alex
    # (example.com)", so "alex@example.com" must match on its parts.
    tokens = [t for t in re.split(r"[^a-z0-9.]+", wanted) if len(t) >= 3 and t not in filler]
    hits = [oid for oid, desc in options.items()
            if wanted in desc.lower() or (tokens and all(t in desc.lower() for t in tokens))]
    if len(hits) == 1:
        return hits[0], "exact match"

    pick = choose(said, {oid: options[oid] for oid in (hits or options)})
    if pick is None:
        if hits:
            return None, f"'{said}' matches several: " + "; ".join(options[h] for h in hits)
        return None, f"No {what} matches '{said}'."
    if pick.sure:
        return pick.id, f"Jev picked it ({pick.confidence:.2f} confident)"
    listing = "; ".join(options[o] for o in (hits or options))
    return None, f"Not sure which {what} '{said}' means. Options: {listing}"


# --- several questions in one request ---------------------------------------------------

def ask(state: dict, questions: dict, timeout: float = 6.0, retries: int = 0) -> dict | None:
    """One Jev request with several questions; returns the raw answers by name.

    A question is {"type": "choice", "instructions": ..., "criteria": {id: text}}
    or {"type": "noul", "instructions": ..., "criteria": {"true": ..., "false": ...}}.
    A choice answer carries a probability for every option, not just the pick,
    which is what lets Jev rank a list (rank() below).
    """
    key = _api_key()
    if not key or not questions:
        return None
    body = {"model": MODEL, "state": state, "questions": questions}
    for attempt in range(retries + 1):
        request = urllib.request.Request(
            URL, data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response).get("answers") or {}
        except Exception as error:
            log.warning("Jev unavailable (%d/%d): %s", attempt + 1, retries + 1,
                        str(error).replace(key, "[redacted]")[:120])
    return None


def rank(request: str, options: dict[str, str], instructions: str, context: dict | None = None,
         timeout: float = 6.0, extra: dict | None = None, retries: int = 0):
    """Score every option for `request` in one call.

    Returns (sorted [(id, probability)], answers) with "none" left out of the
    list, or (None, None) when Jev is unreachable. `extra` adds more questions
    (yes/no gates) to the same request, answered in `answers`.
    """
    if not options:
        return [], {}
    criteria = dict(options)
    criteria["none"] = "None of the listed options is relevant."
    questions = {"rank": {"type": "choice", "instructions": instructions, "criteria": criteria}}
    questions.update(extra or {})
    answers = ask({"request": request, **(context or {})}, questions, timeout=timeout, retries=retries)
    if answers is None or "rank" not in answers:
        return None, None
    probabilities = answers["rank"].get("probabilities") or {}
    ranked = sorted(((k, float(v)) for k, v in probabilities.items() if k != "none" and k in options),
                    key=lambda kv: -kv[1])
    return ranked, answers


def yes(answers: dict | None, name: str) -> float:
    """Probability of yes for a noul answer, 0 if missing."""
    try:
        return float((answers or {}).get(name, {}).get("noul") or 0.0)
    except (TypeError, ValueError):
        return 0.0
