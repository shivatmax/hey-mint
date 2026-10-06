"""Parallel work (background.py): the screen taken in turns, slow or talked-over calls going on in the
background, and Mint's own background jobs running side by side through the agent hub."""
import asyncio
import time

import pytest

try:
    from mint.app import background, control, live, session
except ImportError:
    from mint import background, control, live, session
from mint.agents import providers, runtime


def _screen():
    s = background.Screen()
    s.idle_seconds = lambda: 99.0
    return s


def run(coro):
    return asyncio.run(coro)


# --- the screen ---------------------------------------------------------------------------

def test_conversation_waits_for_a_step_not_for_the_job(monkeypatch):
    s = _screen()

    async def go():
        await s.acquire("task-1")                 # a job is mid-step
        order = []

        async def you():
            await s.acquire(s.YOU)
            order.append("you")
            s.release(s.YOU)

        waiter = asyncio.create_task(you())
        await asyncio.sleep(0.3)
        assert order == []                        # the step in flight finishes first
        s.release("task-1")
        await asyncio.wait_for(waiter, 2)
        assert order == ["you"]
        # The job's next step waits while the conversation keeps the screen (grace).
        got = asyncio.create_task(s.acquire("task-1"))
        await asyncio.sleep(0.3)
        assert not got.done()
        s.last_use -= background.YOU_GRACE + 1
        s._poke()
        note = await asyncio.wait_for(got, 2)
        assert "used for something else" in note  # told to check what is in front
        s.release("task-1")
    run(go())


def test_jobs_take_turns_first_come_first_served():
    s = _screen()

    async def go():
        await s.acquire("task-1")
        s.release("task-1")
        second = asyncio.create_task(s.acquire("task-2"))
        await asyncio.sleep(0.3)
        assert not second.done()                  # task-1 keeps the screen between its steps
        assert await asyncio.wait_for(s.acquire("task-1"), 1) == ""   # its own next step: straight in
        s.release("task-1")
        s.forget("task-1")                        # task-1 ended: the screen is free at once
        await asyncio.wait_for(second, 2)
        assert s.owner == "task-2"
        s.release("task-2")
    run(go())


def test_job_waits_while_the_user_types():
    s = _screen()
    idle = {"v": 0.2}
    s.idle_seconds = lambda: idle["v"]
    said = []

    async def go():
        got = asyncio.create_task(s.acquire("task-1", said.append))
        await asyncio.sleep(0.8)
        assert not got.done() and said == ["waiting for you to pause typing"]
        idle["v"] = 5.0
        await asyncio.wait_for(got, 2)
        s.release("task-1")
    run(go())


def test_what_needs_the_screen():
    assert background.needs_screen("ui_act") and background.needs_screen("type_text")
    assert background.needs_screen("file_action", {"action": "open"})
    assert not background.needs_screen("file_action", {"action": "read"})
    assert not background.needs_screen("web_search") and not background.needs_screen("mail")
    assert background.parallel_safe([{"name": "web_search", "args": {}}, {"name": "read_url", "args": {}}])
    assert not background.parallel_safe([{"name": "web_search", "args": {}}, {"name": "ui_act", "args": {}}])


def test_schema_conversion():
    from google.genai import types
    schema = types.Schema(type=types.Type.OBJECT, required=["a"], properties={
        "a": types.Schema(type=types.Type.STRING, enum=["x", "y"], description="A"),
        "b": types.Schema(type=types.Type.ARRAY, items=types.Schema(type=types.Type.INTEGER))})
    out = background._json_schema(schema)
    assert out == {"type": "object", "required": ["a"], "properties": {
        "a": {"type": "string", "enum": ["x", "y"], "description": "A"},
        "b": {"type": "array", "items": {"type": "integer"}}}}


def test_job_sees_its_own_request():
    live.typed("what's on my calendar")
    state = {}
    with live.background("find flights to Goa", state):
        assert live.request() == "find flights to Goa" and live.was_typed()
        assert live.claim_context() == "find flights to Goa"
        assert live.claim_context() == ""          # the context pack once per job
    with live.background("find flights to Goa", state):
        assert live.claim_context() == ""          # still once, across calls
    assert live.request() == "what's on my calendar"


# --- the hub, with a fake Mint ---------------------------------------------------------------

class _Session:
    def __init__(self):
        self.said = []

    async def send_realtime_input(self, text=None, **kw):
        self.said.append(text)


class _Audio:
    playing = False


class _Mint:
    def __init__(self, loop):
        self.loop, self.session, self.audio = loop, _Session(), _Audio()
        self.audio_in = asyncio.Queue()
        self._turn_open = self._busy = False
        self._tool_task = None
        self.asleep = False


@pytest.fixture
def hub(monkeypatch):
    h = runtime.Hub()
    monkeypatch.setattr(runtime, "hub", h)
    monkeypatch.setattr(runtime, "GATHER", 0.05)
    monkeypatch.setattr(background, "screen", _screen())
    monkeypatch.setattr(background, "QUIET_WAIT", 3.0)
    import mint.agents.team as team
    monkeypatch.setattr(team, "remember", lambda *a, **k: None)
    try:
        from mint.app import telegram
    except ImportError:
        from mint import telegram
    monkeypatch.setattr(telegram, "on_event", lambda *a, **k: None)
    return h


def _attach(h):
    mint = _Mint(asyncio.get_running_loop())
    h.mint, h.loop = mint, mint.loop
    return mint


def test_slow_call_goes_on_in_the_background(hub, monkeypatch):
    monkeypatch.setattr(background, "DETACH_AFTER", 0.4)

    class Fake:
        _print = staticmethod(lambda *a: None)

        async def _run_one(self, name, args):
            await asyncio.sleep(1.2)
            return "Exported report.pdf", None

    async def go():
        mint = _attach(hub)
        fake = Fake()
        t0 = time.monotonic()
        result, _ = await session.Mint._run_detachable(fake, "export_doc_pdf", {"name": "report"})
        assert time.monotonic() - t0 < 1.0
        assert result.startswith("STILL RUNNING in the background as task-")
        assert background.running_note().startswith("Background jobs still running: task-")
        await asyncio.sleep(3.0)
        assert any("finished: Exported report.pdf" in t for t in mint.session.said), mint.session.said
        assert "Background work update" in mint.session.said[-1]
    run(go())


def test_talked_over_call_is_not_killed(hub):
    class Fake:
        _print = staticmethod(lambda *a: None)

        async def _run_one(self, name, args):
            await asyncio.sleep(0.6)
            return "Typed hello", None

    async def go():
        mint = _attach(hub)
        fake = Fake()
        call = asyncio.create_task(session.Mint._run_detachable(fake, "type_text", {"text": "hello"}))
        await asyncio.sleep(0.15)
        fake._detach_now.set()                     # the server cancelled it: the user talked
        result, _ = await asyncio.wait_for(call, 2)
        assert result.startswith("STILL RUNNING") and "interrupted" in result
        await asyncio.sleep(2.0)
        said = " ".join(mint.session.said)
        assert "Your interrupted call type_text" in said and "Typed hello" in said
        assert "say nothing about it" in said      # context only, no chatter
    run(go())


def test_answers_are_never_detached(hub, monkeypatch):
    monkeypatch.setattr(background, "DETACH_AFTER", 0.2)

    class Fake:
        _print = staticmethod(lambda *a: None)

        async def _run_one(self, name, args):
            await asyncio.sleep(0.8)
            return "3 memories", None

    async def go():
        _attach(hub)
        fake = Fake()
        call = asyncio.create_task(session.Mint._run_detachable(fake, "recall", {"query": "x"}))
        await asyncio.sleep(0.1)
        fake._detach_now.set()
        assert (await asyncio.wait_for(call, 2))[0] == "3 memories"
    run(go())


def test_two_jobs_run_side_by_side(hub, monkeypatch):
    """Two background jobs with fake models: their searches overlap in time, their screen steps don't,
    each reports when done, and the conversation hears both."""
    log = []

    def chat(agent, system, messages, schema, effort=None):
        task = messages[0]["content"]
        steps = sum(1 for m in messages if m["role"] == "tool")
        who = "A" if "job A" in task else "B"
        if steps == 0:
            return {"text": "", "tool_calls": [{"id": f"{who}1", "name": "web_search", "args": {"query": who}},
                                               {"id": f"{who}2", "name": "read_url", "args": {"url": who}}],
                    "provider": "gemini", "model": "fake"}
        if steps == 2:
            return {"text": "", "tool_calls": [{"id": f"{who}3", "name": "ui_act", "args": {"target": who}}],
                    "provider": "gemini", "model": "fake"}
        return {"text": f"Job {who} done.", "tool_calls": [], "provider": "gemini", "model": "fake"}

    async def dispatch(name, args):
        start = time.monotonic()
        await asyncio.sleep(0.4)
        log.append((name, list(args.values())[0], start, time.monotonic()))
        return f"{name} ok", None

    monkeypatch.setattr(providers, "chat", chat)
    try:
        from mint.tools import registry as tools
    except ImportError:
        from mint import tools
    monkeypatch.setattr(tools, "dispatch", dispatch)
    monkeypatch.setattr(background, "schemas", lambda: [{"name": n, "description": "", "parameters": {
        "type": "object", "properties": {}}} for n in ("web_search", "read_url", "ui_act")])
    monkeypatch.setattr(background, "system", lambda r: "sys")
    monkeypatch.setattr(background, "first_message", lambda r: f"The job:\n{r.task}")

    async def guard_ok(*a, **k):
        return ""
    try:
        from mint.core import guard
    except ImportError:
        from mint import guard
    monkeypatch.setattr(guard, "check", guard_ok)

    async def go():
        mint = _attach(hub)
        a = await asyncio.to_thread(background.start, "job A: research and click", "", "job A")
        b = await asyncio.to_thread(background.start, "job B: research and click", "", "job B")
        assert a.startswith("Started task-") and "1 other job" in b
        for _ in range(100):
            if not any(r.active for r in hub.runs.values()):
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(1.5)
        searches = [e for e in log if e[0] in ("web_search", "read_url")]
        clicks = sorted((e for e in log if e[0] == "ui_act"), key=lambda e: e[2])
        assert len(searches) == 4 and len(clicks) == 2
        assert min(e[3] for e in searches) > max(e[2] for e in searches)   # all four searches overlapped
        assert clicks[1][2] >= clicks[0][3]                                  # the clicks did not
        said = " ".join(mint.session.said)
        assert "Job A done." in said and "Job B done." in said
        assert all(r.status == "done" for r in hub.runs.values())
        number = a.split("task-")[1].split()[0]
        assert hub.find(f"task {number}").task.startswith("job A") and hub.find("job B").task.startswith("job B")
    run(go())


def test_stop_stops_the_screen_job_only(hub):
    async def go():
        _attach(hub)
        agent = background.worker_agent()
        on_screen, off_screen = runtime.Run(agent, "click things"), runtime.Run(agent, "research things")
        on_screen.id, off_screen.id = "task-1", "task-2"
        for r in (on_screen, off_screen):
            r.status = "working"
            hub.runs[r.id] = r
        await background.screen.acquire("task-1")
        before = control.generation()
        note = background.on_stop()
        assert on_screen.stop and not off_screen.stop
        assert control.generation() == before + 1
        assert "task-2" in note and "task-1" not in note
        control.resume()
        background.screen.release("task-1")
    run(go())


def test_reports():
    agent = background.worker_agent()
    r = runtime.Run(agent, "make a spreadsheet of my expenses")
    r.id, r.title, r.files = "task-4", "Expenses sheet", ["~/Desktop/expenses.xlsx"]
    assert background.report(r, "done", "Made it.") == \
        'Background job task-4 "Expenses sheet" finished. Result: Made it. Files: ~/Desktop/expenses.xlsx.'
    assert background.report(r, "stopped", "x") == ""
    assert "could not finish" in background.report(r, "failed", "no access")


def test_jobs_show_in_the_notch_list(hub, monkeypatch):
    try:
        from mint.tools import agent_remote, agent_watch
    except ImportError:
        from mint import agent_remote, agent_watch
    w = agent_watch.Watcher()
    monkeypatch.setattr(w, "start", lambda: None)
    monkeypatch.setattr(agent_watch, "watcher", w)
    monkeypatch.setattr(agent_watch, "get", w.get)
    r = runtime.Run(background.worker_agent(), "draft the weekly report from my notes")
    r.id, r.title, r.status = "task-9", "Weekly report", "working"
    hub.runs[r.id] = r
    background.notch_event({"run": "task-9", "kind": "start", "text": r.task})
    background.notch_event({"run": "task-9", "kind": "tool", "text": "Reading notes.md"})
    s = w.get("mint:task-9")
    assert s.app_name == "Mint" and s.project == "Weekly report" and s.state == "working"
    assert s.steps[-1].verb == "Reading" and s.steps[-1].target == "notes.md"
    assert [e[0] for e in w._events] == ["started"]
    background.notch_event({"run": "task-9", "kind": "ask", "text": "Which week?"})
    assert w.get("mint:task-9").question["text"] == "Which week?"
    said = []
    monkeypatch.setattr(hub, "steer", lambda ref, text, thinking=None: said.append(("steer", ref, text)) or "ok")
    monkeypatch.setattr(hub, "reply", lambda ref, text: said.append(("reply", ref, text)) or "ok")
    r.status = "asking"
    agent_remote.send("mint:task-9", "last week")            # a reply from the notch is the answer
    r.status = "working"
    agent_remote.send("mint:task-9", "make it shorter")      # ...otherwise new instructions
    assert said == [("reply", "task-9", "last week"), ("steer", "task-9", "make it shorter")]
    background.notch_event({"run": "task-9", "kind": "done", "text": "Saved report.md"})
    s = w.get("mint:task-9")
    assert s.state == "done" and s.summary == "Saved report.md" and all(x.status == "ok" for x in s.steps)


def test_report_waits_for_a_quiet_moment(hub):
    async def go():
        mint = _attach(hub)
        mint._turn_open = True                       # the user is mid-request
        told = asyncio.create_task(hub.tell_mint("(Background work update - not from the user.) done"))
        await asyncio.sleep(1.0)
        assert mint.session.said == []               # not over the user's words
        mint._turn_open = False
        await asyncio.wait_for(told, 3)
        assert len(mint.session.said) == 1
    run(go())


def test_the_conversation_goes_before_waiting_jobs():
    s = _screen()

    async def go():
        await s.acquire("task-1")
        job = asyncio.create_task(s.acquire("task-2"))
        await asyncio.sleep(0.2)
        you = asyncio.create_task(s.acquire(s.YOU))
        await asyncio.sleep(0.2)
        s.release("task-1")
        s.forget("task-1")
        await asyncio.wait_for(you, 2)
        await asyncio.sleep(0.6)
        assert s.owner == s.YOU and not job.done()      # the user's request first
        s.release(s.YOU)
        s.last_use -= background.YOU_GRACE + 1
        s._poke()
        await asyncio.wait_for(job, 3)
        s.release("task-2")
    run(go())


def test_a_second_plan_does_not_push_the_first_aside(monkeypatch):
    try:
        from mint.app import tasks, telegram
        from mint.core import guard
    except ImportError:
        from mint import guard, tasks, telegram
    monkeypatch.setattr(telegram, "gate", lambda n, a: "")
    monkeypatch.setattr(telegram, "send_guard", lambda n, a, r: "")
    monkeypatch.setattr(telegram, "on_event", lambda *a, **k: None)

    async def ok(*a, **k):
        return ""
    monkeypatch.setattr(guard, "check", ok)
    monkeypatch.setattr(tasks, "_save", lambda rows: None)
    monkeypatch.setattr(tasks, "_load", lambda: [])

    class UI:
        def progress(self, *a):
            pass

    class Fake:
        ui = UI()
        _print = staticmethod(lambda *a: None)
        task = {"goal": "Install the CSV extension in VS Code", "started": time.time(), "updated": time.time(),
                "state": "active", "steps": [{"n": "1", "text": "Open VS Code", "status": "done"},
                                             {"n": "2", "text": "Search CSV", "status": "todo"}]}
    fake = Fake()
    args = {"goal": "Book a table for two tonight", "steps": ["Open OpenTable", "Search", "Book"]}
    result, _ = asyncio.run(session.Mint._run_one(fake, "plan_task", args))
    assert result.startswith("NOT STARTED") and "background_task" in result
    assert fake.task["goal"].startswith("Install")                  # the first plan goes on
    # The user really wants to switch: asked again, it starts.
    monkeypatch.setattr(tasks, "start", lambda goal, steps: ({"goal": goal, "steps": []}, None))
    try:
        from mint.tools import extra as extra_tools
    except ImportError:
        from mint import extra_tools
    monkeypatch.setattr(extra_tools, "plan_context", lambda goal="": "")
    result, _ = asyncio.run(session.Mint._run_one(fake, "plan_task", args))
    assert result.startswith("Task started") and fake.task["goal"] == "Book a table for two tonight"


def test_a_waiting_job_can_be_stopped(hub, monkeypatch):
    monkeypatch.setattr(background, "MAX_JOBS", 1)

    async def go():
        _attach(hub)
        busy = runtime.Run(background.worker_agent(), "long job")
        busy.id, busy.status = "task-50", "working"
        hub.runs[busy.id] = busy
        said = await asyncio.to_thread(background.start, "second job that has to wait", "", "Second")
        assert "starts when one of the 1 running jobs ends" in said
        run_id = said.split()[1]
        await asyncio.sleep(0.3)
        assert hub.runs[run_id].active
        await asyncio.to_thread(hub.cancel, run_id)
        await asyncio.sleep(0.5)
        assert hub.runs[run_id].status == "stopped"
    run(go())


def test_a_model_that_times_out_is_benched():
    providers._dead.clear()
    assert providers._mark("gemini", "slow-model", RuntimeError("504 DEADLINE_EXCEEDED. Deadline expired")) is True
    assert providers._skip("gemini", "slow-model")
    assert providers._dead[("gemini", "slow-model")] - time.monotonic() > 500
    providers._dead.clear()
    assert background.WORKER_MODELS[0] != "gemini/gemini-3.8-flash"


@pytest.mark.parametrize("request_text,said,refused", [
    ("run a background job that opens Calculator and adds 56 plus 44",
     "I'm unable to interact with the Calculator app's buttons in the background, as that requires the "
     "application to be open on your screen, Boss.", True),
    ("start a background job: open Calculator and type 123 times 4",
     "I cannot use the Calculator app's buttons in the background.", True),
    ("run a background job that researches keyboards", "On it, I'll tell you when it's done.", False),
    ("open Calculator", "I can't find Calculator on this Mac.", False),
])
def test_a_refused_job_is_corrected(request_text, said, refused):
    assert background.refused_job(request_text, said) is refused


def test_a_job_without_a_result_is_not_guessed(hub, monkeypatch):
    replies = iter([{"text": "", "tool_calls": [], "provider": "gemini", "model": "fake"},
                    {"text": "The display shows 144.", "tool_calls": [], "provider": "gemini", "model": "fake"}])
    seen = []

    def chat(agent, system, messages, schema, effort=None):
        seen.append(messages[-1]["content"])
        return next(replies)
    monkeypatch.setattr(providers, "chat", chat)
    monkeypatch.setattr(background, "schemas", lambda: [])
    monkeypatch.setattr(background, "system", lambda r: "sys")
    monkeypatch.setattr(background, "first_message", lambda r: r.task)

    async def go():
        mint = _attach(hub)
        said = await asyncio.to_thread(background.start, "multiply 12 by 12 in Calculator", "", "12x12")
        run_id = said.split()[1]
        for _ in range(60):
            if hub.runs[run_id].status == "done":
                break
            await asyncio.sleep(0.1)
        assert hub.runs[run_id].result == "The display shows 144." and "WITHOUT calling a tool" in seen[-1]
        await asyncio.sleep(2.5)
        assert "The display shows 144." in " ".join(mint.session.said)
    run(go())
    r = runtime.Run(background.worker_agent(), "x")
    r.id, r.title = "task-7", "x"
    assert "Do NOT guess" in background.report(r, "done", "(no summary given)")


def test_a_job_pauses_mid_way_when_the_user_takes_over():
    """6 Oct, live: the user clicked into another app half way through a Calculator job; the job went on typing
    into the wrong window. Now each screen step waits while the user is busy, then is told to look again."""
    s = _screen()
    now = {"t": 1000.0}
    last_input = {"t": 0.0}
    s.clock = lambda: now["t"]
    s.idle_seconds = lambda: now["t"] - last_input["t"]

    async def go():
        await s.acquire("task-1")
        last_input["t"] = now["t"]                 # Mint's own click (a HID event too)...
        now["t"] += 0.4
        s.release("task-1")
        now["t"] += 0.2
        assert await asyncio.wait_for(s.acquire("task-1"), 1) == ""    # ...does not make it wait
        s.release("task-1")
        now["t"] += 0.5
        last_input["t"] = now["t"]                 # the user clicks somewhere
        got = asyncio.create_task(s.acquire("task-1"))
        await asyncio.sleep(0.8)
        assert not got.done()                      # the job waits while the user is busy
        now["t"] += 2.0                            # the user pauses
        note = await asyncio.wait_for(got, 2)
        assert "the user used the Mac since your last step" in note
        s.release("task-1")
    run(go())


def test_jobs_do_not_change_windows(monkeypatch):
    async def ok(*a, **k):
        return ""
    try:
        from mint.core import guard
    except ImportError:
        from mint import guard
    monkeypatch.setattr(guard, "check", ok)
    r = runtime.Run(background.worker_agent(), "multiply 12 by 12 in Calculator")
    r.id = "task-8"
    said = run(background.call(r, "mac", {"control": "window", "value": "fullscreen"}))
    assert said.startswith("NOT RUN") and "full screen" in said
