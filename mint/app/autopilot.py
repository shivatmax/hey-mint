"""Autopilot: keep going until everything the user asked for is done.

The Live model tends to do one step of a multi-part request, say what it did, and wait - the
user then has to say "next", "next", "next". In testing (27 Sep, 17:04) "do all the tricks one
by one" gave one orb trick per "next". After each of the model's turns the session asks
`decide()` whether the request is still half done; if so it sends the model a short note -
not the user's words - to carry on.

It never nudges when waiting is right:
  - a tool result says something is still running (wait_until_done, a sub-agent) - the result
    arrives as its own message; acting early read half-written ChatGPT answers;
  - a message was just sent (typing with Return) - the next step is to wait, never to resend;
  - a sub-agent's question is waiting for the user, or the model asked the user a real question;
  - after STOP, or when the model did nothing after the previous nudge (it thinks it is done).
"""

from __future__ import annotations

import re
import threading

MAX_NUDGES = 8

_CONTINUE = re.compile(r"^\s*(next( one)?|continue|go on|keep going|carry on|more|do it|go ahead|yes|yeah|yep|"
                       r"ok(ay)?|sure|and then|then|again|proceed)\b[\s.!?,]*"
                       r"(next|continue|go on|keep going|carry on|do it|please|more|it)?[\s.!?,]*$", re.I)
_UNBOUNDED = re.compile(r"\b(all|every|each|one by one|one after (another|the other)|in turn|every one|"
                        r"all of them|the rest|everything|whole list|each of)\b", re.I)
_DONE = re.compile(r"\b(all (done|finished|set|complete)|that'?s (all|everything|it)|finished (all|everything)|"
                   r"(completed|did|done) (all|everything)|every (one|trick|step) (is )?done|"
                   r"that was the last|last one)\b", re.I)
_OFFERS = re.compile(r"\b((shall|should|can|may) i (continue|go on|keep going|do the (next|rest)|proceed|carry on)|"
                     r"(want|like) me to (continue|go on|keep going|do the (next|rest)|proceed|carry on)|"
                     r"next,? i('| wi)ll|next i('| wi)ll|next up|next is|up next|say next|tell me when|"
                     r"let me know when (you'?re|you are) ready|ready for the next|then i('| wi)ll)\b", re.I)
_REPORTED = re.compile(r"\b(i have|i've|i had|i did|i've now|i just|done|finished|completed|all set|here (is|are))\b",
                       re.I)
_WAITING = re.compile(r"(still working|you will get a message|you will be told|works in the background|"
                      r"do not read or act on|wait for the reply|wait_until_done|in the background)", re.I)

_lock = threading.Lock()
_state = {"request": "", "latest": "", "nudges": 0, "tools_total": 0, "tools_since": 0,
          "last_name": "", "waiting": False, "sent": False, "off": False, "finished": False}


def _reset(request: str) -> None:
    _state["changed"] = False
    _state.update(request=request, latest=request, nudges=0, tools_total=0, tools_since=0,
                  last_name="", waiting=False, sent=False, off=False, finished=False)


def _sync_request() -> str:
    """The request being worked on. "next" / "continue" keep the original one."""
    try:
        from mint.app import live
        latest = " ".join((live.request() or "").split())
    except Exception:
        latest = _state["latest"]
    if latest and latest != _state["latest"]:
        if _state["request"] and _CONTINUE.match(latest) and len(latest.split()) <= 6:
            _state["latest"] = latest
            _state["off"] = False            # the user wants more: listen for it
        else:
            _reset(latest)
    return _state["request"]


_READING = re.compile(r"^(read_|find_|get_|list_|look|calendar_events|ui_elements|frontmost_app|video_info|"
                      r"search_|web_search|recall|find_skill|notifications$|clipboard$)")


def tools_done() -> int:
    """Tool calls made for the current request."""
    with _lock:
        _sync_request()
        return _state["tools_total"]


def note(name: str, args: dict, result: str) -> None:
    """After every tool call."""
    with _lock:
        _sync_request()
        _state["tools_total"] += 1
        _state["tools_since"] += 1
        _state["last_name"] = name
        if not _READING.match(name):
            _state["changed"] = True        # something was done, not only looked at
        text = str(result or "")
        _state["waiting"] = bool(_WAITING.search(text[:600])) or name in {"delegate_task", "delegate_tasks",
                                                                         "wait_until_done"} and not \
            text.lower().startswith(("failed", "not delegated", "there is no agent"))
        _state["sent"] = name in {"ui_act", "type_text"} and bool((args or {}).get("press_return"))
        if name == "step_done" and text.startswith("All steps finished"):
            _state["finished"] = True       # the plan covered the whole request
        elif name == "plan_task":
            _state["finished"] = False
    if name == "set_preference":
        # The session changes settings itself; this is the one place that sees it happen (undo.py).
        from mint.tools import undo
        undo.after_preference(args, result)


def stop() -> None:
    with _lock:
        _state["off"] = True


def parts(request: str) -> int:
    """How many things a request lists: "open YouTube, anime and Slack" -> 3."""
    pieces = [p for p in re.split(r",|;|\band then\b|\bthen\b|\balso\b|\band\b|\bafter that\b", request, flags=re.I)
              if len(p.split()) >= 1 and p.strip()]
    return len(pieces)


def decide(said: str, task_step: str = "", agent_asking: bool = False) -> str:
    """The note to send the model now, or '' to leave it be. `task_step`: the plan's next step, if any."""
    with _lock:
        request = _sync_request()
        st = _state
        if st["off"] or st["nudges"] >= MAX_NUDGES or not request:
            return ""
        if st["nudges"] and st["tools_since"] == 0:
            st["off"] = True                 # nudged, and it did nothing: it believes it is done
            return ""
        if st["waiting"] or st["sent"] or agent_asking:
            return ""
        said = " ".join((said or "").split())
        offer = bool(_OFFERS.search(said))
        if said.endswith("?") and not offer:
            return ""                        # a real question for the user
        if task_step:
            why = f"your plan's next step is: {task_step}"
        elif st["finished"]:
            return ""
        else:
            if st["tools_total"] == 0:
                return ""
            if _DONE.search(said) and not offer:
                return ""
            if not st.get("changed") and len(said.split()) >= 8 and not offer:
                return ""                    # it only looked things up and answered: a question, answered
            # "I have opened X, Y and Z" is a report of finished work, not a pause mid-list.
            unbounded = bool(_UNBOUNDED.search(request)) and not _REPORTED.search(said)
            listed = parts(request)
            if not (offer or unbounded or (listed >= 2 and st["tools_total"] < listed)):
                return ""
            why = "part of it is still to do"
        st["nudges"] += 1
        st["tools_since"] = 0
        return (f"(Mint autopilot - this is not the user speaking. You stopped part-way: {why}. The user asked: "
                f"\"{request[:300]}\". Carry on now with everything that is left, back to back, without asking "
                "or waiting for them to say next. When all of it is done, say so in one short sentence. If it is "
                "already all done, just say 'All done.' But if a tool said to ask the user first (for example before "
                "replacing what is in a file), ask them that one question instead.)")
