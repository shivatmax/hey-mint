"""When Mint's autopilot tells the Live model to carry on, and when it must not.

    .venv/bin/python bench/autopilot_bench.py
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from mint import autopilot as a, live  # noqa: E402

results: list[bool] = []


def case(name: str, ok: bool) -> None:
    results.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")


def turn(request: str, calls: list[tuple], said: str, step: str = "", asking: bool = False) -> bool:
    live.typed(request)
    for name, args, result in calls:
        a.note(name, args, result)
    return bool(a.decide(said, step, asking))


# Carry on
case("'all ... one by one' after one trick and 'next I will'", turn(
    "do all the tricks one by one", [("move_orb", {"trick": "hops"}, "Doing a hops trick")],
    "I am doing hops, and next I will do a figure eight."))
live.typed("next")
case("'next' keeps the original request", a._state["request"] == "do all the tricks one by one")
a.note("move_orb", {}, "Doing figure8")
case("...and it carries on again", bool(a.decide("Now I am doing a figure eight.")))
case("three things listed, one done", turn("open slack, gmail and notion", [("open_app", {}, "Opened Slack")],
                                           "I opened Slack."))
case("a plan with steps left", turn("make the report", [("read_file", {}, "text")], "I read it.",
                                    step="step 2 of 3 - write the summary"))
case("'shall I continue?' is not a real question", turn(
    "do every trick", [("move_orb", {}, "ok")], "That was the loop. Shall I continue with the next one?"))

# Leave it be
case("the plan's last step_done ends it", not turn(
    "do all your tricks, then smile, then tell me the time",
    [("plan_task", {}, "Task started"), ("move_orb", {}, "ok"), ("step_done", {}, "Recorded. Next is step 2"),
     ("express", {}, "smile"), ("step_done", {}, "All steps finished. Tell the user briefly")], "It is 5 PM."))
case("'each' request reported as finished", not turn(
    "Open example.com, example.org and example.net, each in its own tab.",
    [("open_url", {}, "Opened"), ("open_url", {}, "NOT RUN"), ("open_url", {}, "NOT RUN"),
     ("browser", {}, "Opened"), ("browser", {}, "Opened")],
    "I have opened example.com, example.org, and example.net in separate tabs."))
case("single request done", not turn("what time is it", [("get_status", {}, "14:00")], "It is 2 PM."))
case("everything listed was done", not turn(
    "open YouTube and anime and put them in a group called Entertainment",
    [("open_url", {}, "Opened"), ("open_url", {}, "Opened"), ("browser", {}, "Grouped")],
    "I've opened YouTube and the anime site and grouped them."))
case("a message was just sent: wait, never resend", not turn(
    "ask chatgpt about X and then summarise", [("ui_act", {"press_return": True}, "Typed and sent")],
    "I asked ChatGPT."))
case("a sub-agent works in the background", not turn(
    "research this and write a doc",
    [("delegate_task", {}, "Started Astra ... It works in the background; you will be told")], "Astra is on it."))
case("wait_until_done still waiting", not turn(
    "ask chatgpt and read all of the answer",
    [("wait_until_done", {}, "ChatGPT is still working after 20s. You will get a message the moment it is done")],
    "ChatGPT is still writing."))
case("a sub-agent's question waits for the user", not turn(
    "do all of these", [("move_orb", {}, "ok")], "Luna has a question.", asking=True))
case("'all done' at the end", not turn("do all tricks", [("move_orb", {}, "ok")], "All done, that was the last one!"))
case("a real question for the user", not turn("do all tricks", [("move_orb", {}, "ok")],
                                              "Which trick would you like?"))
live.typed("do every trick")
a.note("move_orb", {}, "ok")
first = bool(a.decide("That was hops."))
case("nudged, then did nothing: stops nudging", first and not a.decide("I did everything"))
live.typed("do every single trick please")
a.note("move_orb", {}, "ok")
a.stop()
case("after STOP", not a.decide("I did one."))
live.typed("do all the tricks forever")
count = 0
for _ in range(20):
    a.note("move_orb", {}, "ok")
    count += bool(a.decide("Next I will do another."))
case(f"at most {a.MAX_NUDGES} nudges per request", count == a.MAX_NUDGES)

print(f"\n{sum(results)}/{len(results)} passed")
raise SystemExit(0 if all(results) else 1)
