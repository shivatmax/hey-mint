"""Your coding agents on Telegram: Claude Code and Codex tell you when they need you, and you answer from the phone.

    🔐 Claude Code · korus wants to run:          [✅ Allow] [♾ Always] [⛔ Deny]
       npm test -- --coverage                     [📸 Screen] [💬 Reply]
    ❓ Codex · atlas asks: Which branch?           ↩️ reply to it: your words are typed into that session
    ✅ Claude Code · korus is done (2 min):        ↩️ reply "push" and Claude gets "push"
       Bumped TVA to 20%...

When: while you are away from the Mac (no keyboard or mouse for a while, or the screen locked) - or always, or
never (Settings: agent_telegram away / always / off). After you answer a session from the phone, that session keeps
reporting here for a while even if the Mac thinks you are back.

Answering: Allow / Always / Deny go to agent_hooks.decide (Claude Code with "Approve from the notch" connected).
A reply to an agent's message (or 💬 Reply, or /agents) is typed into that session by agent_remote.send - its
Terminal or iTerm tab, or the Claude / Codex app. Only the paired account, never in read-only mode, and every
press and reply goes to the remote log. Words that came from a session are shown, never acted on.

Bridge hooks (telegram.py): attach(bridge) once; plan(bridge, name, arg, message) for the buttons "ag", "agto",
"agopen"; on_reply(bridge, message, text) -> True when the message was a reply to an agent; command(bridge).
"""

from __future__ import annotations

import html as _html
import logging
import time

log = logging.getLogger("mint.app.telegram_agents")

AWAY_AFTER = 90.0          # seconds without keyboard or mouse that count as away from the Mac
FOLLOW = 30 * 60           # a session answered from the phone keeps reporting here this long
KEEP = 60                  # agent messages remembered for replies


def esc(text) -> str:
    return _html.escape(str(text or ""), quote=False)


def _away() -> bool:
    try:
        import Quartz
        idle = Quartz.CGEventSourceSecondsSinceLastEventType(Quartz.kCGEventSourceStateHIDSystemState,
                                                             Quartz.kCGAnyInputEventType)
        if idle > AWAY_AFTER:
            return True
        session = Quartz.CGSessionCopyCurrentDictionary() or {}
        return bool(session.get("CGSSessionScreenIsLocked"))
    except Exception:
        return False


def attach(bridge) -> None:
    """Once: listen to the agents (the bridge holds agent_msgs, agent_btns and agent_follow)."""
    if getattr(bridge, "_agents_attached", False):
        return
    bridge._agents_attached = True
    try:
        from mint.ui import notch_agents
        notch_agents.on_event(lambda kind, s: _event(bridge, kind, s))
    except Exception:
        log.exception("telegram agents: could not listen to the agents")


def _wanted(bridge, kind: str, s) -> bool:
    if not getattr(bridge, "bot", None) or not bridge._state.get("user_id"):
        return False
    mode = str(bridge.setting("agent_telegram") or "away")
    if getattr(s, "app", "") == "mint":
        return False          # Mint's own jobs reach the phone through Mint's words (hub notice)
    if mode == "off" or kind not in ("waiting", "asking", "finished", "failed"):
        return False
    following = time.time() < bridge.agent_follow.get(s.key, 0)
    return mode == "always" or following or _away()


def _event(bridge, kind: str, s) -> None:
    """On the main thread (notch_agents); the message goes out from a worker."""
    try:
        if _wanted(bridge, kind, s):
            from mint.app.telegram import _later
            _later(alert, bridge, kind, s)
    except Exception:
        log.exception("telegram agent event")


def _remember(bridge, sent, key: str) -> None:
    mid = (sent or {}).get("message_id")
    if mid:
        bridge.agent_msgs[mid] = key
        while len(bridge.agent_msgs) > KEEP:
            bridge.agent_msgs.pop(next(iter(bridge.agent_msgs)))


def _checks(s) -> str:
    """How its tests stand and anything risky, under a done / failed message ("" when there's nothing)."""
    try:
        from mint.tools import agent_checks
        line = agent_checks.emoji_line(s)
    except Exception:
        return ""
    return f"\n\n{esc(line)}" if line else ""


def alert(bridge, kind: str, s) -> None:
    from mint.tools import agent_hooks
    from mint.tools import agent_remote
    from mint.app.telegram import keys
    head = f"<b>{esc(s.app_name)} · {esc(s.project)}</b>" + (f" <i>({esc(s.title)})</i>" if s.title else "")
    can_type = bool(agent_remote.route(s))
    rows = []
    if kind == "waiting" and s.approval:
        a = s.approval
        d = a.get("detail") or {}
        what = d.get("cmd") or a.get("target") or a.get("tool")
        verb = {"Run": "run", "Edit": "edit", "Write": "write"}.get(a.get("verb"), "use " + str(a.get("tool")))
        text = f"🔐 {head} wants to {verb}:\n<pre>{esc(str(what)[:900])}</pre>"
        bid = bridge._keep(bridge.agent_btns, {"key": s.key, "approval": a.get("id")})
        rule = agent_hooks.rule_for(s.key)
        rows.append([("✅ Allow", f"ag:{bid}:allow")] + ([("♾ Always", f"ag:{bid}:always")] if rule else [])
                    + [("⛔ Deny", f"ag:{bid}:deny")])
        if rule:
            text += f"\n<i>♾ Always adds {esc(rule)}</i>"
    elif kind == "asking" and s.question:
        q = s.question
        text = f"❓ {head} asks:\n{esc(q.get('text'))}"
        if q.get("options"):
            text += "\n" + "\n".join(f"  {n}. {esc(o)}" for n, o in enumerate(q["options"], 1))
        if can_type:
            text += "\n\n↩️ <i>Reply to this message with your answer.</i>"
    elif kind == "failed":
        text = f"❌ {head} failed.\n{esc((s.summary or '')[:900])}" + _checks(s)
    else:
        took = ""
        if s.turn_started and s.since > s.turn_started:
            secs = int(s.since - s.turn_started)
            took = f" ({secs // 60} min {secs % 60:02d} s)" if secs >= 60 else f" ({secs} s)"
        from mint.ui.notch_agents import _plain
        text = f"✅ {head} is done{took}.\n{esc(_plain(s.summary)[:1500])}" + _checks(s)
        if can_type:
            text += "\n\n↩️ <i>Reply to this message to tell it what to do next.</i>"
    bid = bridge._keep(bridge.agent_btns, {"key": s.key, "approval": None})
    second = [("📸 Screen", "shot")] + ([("💬 Reply", f"agto:{bid}")] if can_type else []) + \
        [("🪟 Open on Mac", f"agopen:{bid}")]
    rows.append(second)
    sent = bridge.html(text, markup=keys(*rows))
    _remember(bridge, sent, s.key)
    bridge.audit("agent alert", f"{s.app} {kind}")


def plan(bridge, name: str, arg: str, message: dict):
    """(toast, act, used label) for the agent buttons."""
    from mint.tools import agent_hooks
    from mint.tools import agent_watch
    from mint.app.telegram import _later
    bid, _, decision = arg.partition(":")
    item = bridge.agent_btns.get(bid)
    if not item:
        return "That button has expired.", None, ""
    s = agent_watch.get(item["key"])
    if s is None:
        return "That session is gone.", None, ""
    if name == "ag":
        if bridge.setting("telegram_read_only"):
            return "🔒 Read-only is on: approvals are off from the phone.", None, ""
        if not s.approval or s.approval.get("id") != item["approval"] or not agent_hooks.pending(s.key):
            return "Already answered (or it moved on).", None, "✓ Answered"
        label = {"allow": "✅ Allowed", "always": "♾ Always allowed", "deny": "⛔ Denied"}[decision]

        def act():
            bridge.agent_follow[s.key] = time.time() + FOLLOW
            if not agent_hooks.decide(s.key, decision):
                bridge.reply("It had already moved on - nothing was sent.")
        return label, act, label
    if name == "agto":
        if bridge.setting("telegram_read_only"):
            return "🔒 Read-only is on: nothing is typed into sessions from the phone.", None, ""

        def ask():
            sent = bridge.html(f"💬 What should I tell <b>{esc(s.app_name)} · {esc(s.project)}</b>?",
                               markup={"force_reply": True, "input_field_placeholder": "e.g. push"})
            _remember(bridge, sent, s.key)
        return "💬 Reply with what to send", lambda: _later(ask), ""
    if name == "agopen":
        def show():
            from PyObjCTools import AppHelper

            from mint.ui.notch_agents import open_session
            AppHelper.callAfter(open_session, s)
        return "🪟 Opening it on the Mac…", show, ""
    return "That button no longer works.", None, ""


def on_reply(bridge, message: dict, text: str, age: float) -> bool:
    """A reply to an agent's message: type it into that session. True when handled."""
    to = message.get("reply_to_message") or {}
    key = getattr(bridge, "agent_msgs", {}).get(to.get("message_id"))
    if not key or not text or text.startswith("/"):
        return False
    from mint.app.telegram import STALE, _later, _took
    if bridge.setting("telegram_read_only"):
        bridge.reply("🔒 Read-only is on: nothing is typed into your agents from the phone.")
        bridge.audit("agent reply", key, "refused (read-only)")
        return True
    if age > STALE:
        bridge.reply(f"This came while the Mac was asleep or offline ({_took(age)} ago), so I didn't send it to "
                     "the agent. Send it again if you still want it.")
        return True
    _later(_send, bridge, key, text)
    return True


def _send(bridge, key: str, text: str) -> None:
    from mint.tools import agent_remote
    from mint.tools import agent_watch
    bridge._action("typing")
    said = agent_remote.send(key, text)
    bridge.audit("agent reply", f"{key} ({len(text)} chars)", said[:100])
    s = agent_watch.get(key)
    ok = said.startswith("Sent")
    if ok:
        bridge.agent_follow[key] = time.time() + FOLLOW
    name = f"{s.app_name} · {s.project}" if s else "the session"
    bridge.html((f"✅ Sent to <b>{esc(name)}</b>. I'll tell you here when it needs you or is done."
                 if ok else f"⚠️ {esc(said)}"))


def command(bridge) -> None:
    """/agents: every Claude Code and Codex session, with a 💬 to talk to each."""
    from mint.tools import agent_remote
    from mint.tools import agent_watch
    from mint.app.telegram import keys
    items = agent_watch.sessions()[:8]
    if not items:
        bridge.html("🤖 No Claude Code or Codex session in the last hours.")
        return
    lines, rows = ["🤖 <b>Your coding agents</b>"], []
    for s in items:
        lines.append("• " + esc(agent_remote.describe(s)))
        if agent_remote.route(s):
            bid = bridge._keep(bridge.agent_btns, {"key": s.key, "approval": None})
            rows.append([(f"💬 {s.project[:18]}", f"agto:{bid}"), ("🪟 Open", f"agopen:{bid}")])
    mode = str(bridge.setting("agent_telegram") or "away")
    lines.append({"away": "\n<i>They message you here while you're away from the Mac.</i>",
                  "always": "\n<i>They message you here whenever they need you or finish.</i>",
                  "off": "\n<i>Agent messages are off (Settings ▸ Models & agents ▸ Coding agents).</i>"}.get(mode, ""))
    sent = bridge.html("\n".join(lines), markup=keys(*rows, [("📸 Screen", "shot")]) if rows else
                       keys([("📸 Screen", "shot")]))
    if len(items) == 1:
        _remember(bridge, sent, items[0].key)
