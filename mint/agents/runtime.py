"""The hub: runs sub-agents in the background and keeps Mint in the loop.

A run is one task given to one agent. Its loop: the model thinks, calls tools,
gets results, and repeats until it answers without calling a tool - that
answer is the result. Around that loop:

* Steering. Mint can send new instructions at any time ("tell Luna to use
  pytest"); they are added before the agent's next step, marked as updates
  from the user that override earlier instructions.
* Questions. `ask_user` pauses the run and sends the question to Mint - as a
  message the Live model hears (not a user turn); Mint wakes up if asleep,
  asks the user in its own words, and passes the reply back with
  answer_agent. The run resumes with the answer.
* Results. When a run ends, Mint is told (so it can tell the user), the chat
  shows it, and it goes into memory.
* Events. Every step emits an event for the animations and the chat.

Runs live on Mint's asyncio loop; model and tool calls run in threads.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import itertools
import logging
import time
from pathlib import Path

from mint.agents import deliver
from mint.agents import providers
from mint.agents import registry
from mint.agents import team
from mint.agents import tools as agent_tools

log = logging.getLogger("mint.agents")

ANSWER_TIMEOUT = 15 * 60
_ids = itertools.count(1)
GATHER = 2.0          # results finishing this close together are reported to Mint as one message

_HARD = ("research", "investigate", "compare", "analy", "design", "architect", "debug", "plan", "strategy",
         "reward", "rl environment", "reinforcement", "algorithm", "optimi", "prove", "evaluate", "benchmark",
         "trade-off", "tradeoff", "multi-step", "report")
_PARALLEL = {"web_search", "fetch_url", "read_file", "list_files", "classify", "read_board", "list_team"}
_EASY = ("rename", "reformat", "list", "copy", "translate", "summari", "fix typo", "hello", "print", "short")


def choose_effort(task: str, default: str = "low") -> str:
    """How hard the model should think, from the task: none for small and
    mechanical, medium for research/design/debugging/RL, low otherwise. The
    orchestrator may override it; high is never used."""
    text = task.lower()
    words = len(text.split())
    hard = sum(1 for k in _HARD if k in text)
    default = default if default in ("none", "low", "medium") else "low"
    if hard >= 2 or (hard and words > 40) or words > 150:
        return "medium"
    # "none" only for clearly mechanical work - short is not the same as easy
    # ("write the helper script with a CLI" got none in testing).
    if words <= 25 and not hard and any(k in text for k in _EASY):
        return "none"
    return default


def too_small(task: str) -> bool:
    """Orchestrating costs a model loop; tiny asks are Mint's to do itself."""
    return len(task.split()) < 6


class Run:
    def __init__(self, agent: dict, task: str, context: str = "", effort: str = "low") -> None:
        self.id = f"{agent['name'].lower()}-{next(_ids)}"
        self.effort = effort
        self.agent = agent
        self.task = task
        self.context = context
        self.status = "starting"        # starting | working | asking | done | failed | stopped
        self.messages: list[dict] = []
        self.inbox: list[str] = []
        self.question: str | None = None
        self.answer: asyncio.Future | None = None
        self.stop = False
        self.started = time.time()
        self.finished: float | None = None
        self.steps = 0
        self.doing = "starting"
        self.result = ""
        self.files: list[str] = []
        self.model = ""
        self.task_handle: asyncio.Task | None = None
        self.thread_id: str | None = None      # Codex runs: the session to resume for follow-ups
        self.parent: Run | None = None         # the agent that asked this one for help (None: Mint)
        self.depth = 0
        self.mission: team.Mission | None = None
        self.children: list[Run] = []
        self.done: asyncio.Future | None = None   # helpers: resolved with (status, result) at the end
        self.destination: deliver.Destination | None = None   # where the user asked for the result
        self.delivered: list[Path] = []

    @property
    def name(self) -> str:
        return self.agent["name"]

    @property
    def active(self) -> bool:
        return self.status in ("starting", "working", "asking")

    def brief(self) -> str:
        took = int((self.finished or time.time()) - self.started)
        line = (f"{self.name} [{self.id}] {self.status} after {took}s, {self.steps} steps, thinking {self.effort}"
                + (f", on {self.model}" if self.model else "")
                + (f", helping {self.parent.name}" if self.parent else ""))
        if self.status == "asking":
            line += f" - waiting for the answer to: {self.question}"
        elif self.active:
            line += f" - now {self.doing}"
        elif self.result:
            line += f" - result: {self.result[:300]}"
        if self.files:
            line += f" - files: {', '.join(self.files[-5:])}"
        if self.agent.get("runner") == "codex":
            line += f" - folder: {self.agent['workspace']}"
        return line


class Hub:
    def __init__(self) -> None:
        self.runs: dict[str, Run] = {}
        self.listeners: list = []
        self.mint = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._outbox: list[str] = []      # messages for Mint while its session is reconnecting
        self._finished: list[str] = []    # results waiting to be reported together
        self._flush_handle = None

    # --- wiring ----------------------------------------------------------------------

    def attach(self, mint) -> None:
        self.mint = mint
        self.loop = mint.loop
        import threading

        def keys():
            result = providers.check_keys()
            print(f"  [agents: provider keys {result}]", flush=True)
        threading.Thread(target=keys, daemon=True, name="agent-key-check").start()
        try:                                   # the orbs and chat lines, when there is a UI
            from mint.ui import agent_view
            agent_view.attach(self)
        except Exception:
            log.debug("no agent view", exc_info=True)

    def on(self, listener) -> None:
        if listener not in self.listeners:
            self.listeners.append(listener)

    def emit(self, kind: str, run: Run, text: str = "", **extra) -> None:
        event = {"kind": kind, "run": run.id, "agent": run.name, "color": run.agent.get("color"),
                 "text": text, "status": run.status,
                 "parent": run.parent.id if getattr(run, "parent", None) else None, **extra}
        log.info("agent event %s", event)
        for listener in list(self.listeners):
            try:
                listener(event)
            except Exception:
                log.exception("agent listener failed")

    async def tell_mint(self, text: str, wake: bool = False) -> None:
        """A message to the Live model from the sub-agents - it hears it, the user
        does not see it as their own words."""
        mint = self.mint
        if mint is None:
            return
        from mint.app import telegram
        telegram.on_event("notice", {"text": text})      # Mint's next words are news for the phone too
        if wake and getattr(mint, "asleep", False):
            try:
                await mint.wake_up("agent")
            except Exception:
                log.debug("could not wake for an agent", exc_info=True)
        self._outbox.append(text)
        await self.flush()

    async def flush(self) -> None:
        """Deliver what was said to Mint while its session was (re)connecting. Called by
        tell_mint and by the session as soon as it is connected."""
        mint = self.mint
        session = getattr(mint, "session", None) if mint is not None else None
        if session is None:
            return
        while self._outbox:
            message = self._outbox.pop(0)
            try:
                await session.send_realtime_input(text=message)
            except Exception:
                self._outbox.insert(0, message)
                log.debug("could not reach Mint; will retry", exc_info=True)
                return

    # --- what Mint's tools call (any thread) ---------------------------------------------

    def _call(self, coroutine):
        if self.loop is None:
            coroutine.close()
            raise RuntimeError("Mint's session is not running yet")
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout=10)

    def delegate(self, agent_name: str, task: str, context: str = "", thinking: str | None = None,
                 why: str = "", folder: str = "", helpers: list[str] | None = None, save_to: str = "") -> str:
        agent = registry.get(agent_name)
        if agent is None:
            names = ", ".join(a["name"] for a in registry.load())
            return f"There is no agent called '{agent_name}'. Agents: {names}. Or create one with create_agent."
        if too_small(task):
            return (f"NOT DELEGATED: '{task}' is too small to hand off - do it yourself, or give {agent['name']} "
                    "complete instructions (goal, constraints, deliverable).")
        # Judge the key by where the run will really go: an expired OpenRouter
        # key does not block an agent that runs on OpenAI directly instead.
        try:
            provider, models, note = providers.route(agent)
        except providers.NoProvider as error:
            return f"NOT STARTED: {error} Tell the user that, in one sentence."
        blocked = providers.key_problem(provider, recheck=True)
        if blocked:
            return (f"NOT STARTED: {blocked} You cannot fix this yourself - tell the user exactly that, in one "
                    "sentence. If the task is small enough to do with your own tools (e.g. a short text saved "
                    "with write_file where they asked), offer to do it yourself now.")
        effort = thinking if thinking in providers.EFFORTS else choose_effort(task, agent.get("thinking", "low"))
        run = Run(agent, task, context, effort)
        # Where the user asked for the result: the agent may write it there, and it is
        # copied there at the end if it stayed in the workspace (deliver.py).
        wanted = deliver.find(task, save_to)
        run.destination, refused = deliver.Destination.parse(wanted)
        busy = [r for r in self.runs.values() if r.active and r.name == agent["name"]]
        codex_run = agent.get("runner") == "codex"
        if folder:
            run.agent = dict(agent, workspace=str(Path(folder).expanduser()))
        elif codex_run:
            # Every build gets its own project folder, named after the task.
            from mint.agents import codex
            run.agent = dict(agent, workspace=str(codex.project_folder(
                Path(agent["workspace"]).expanduser(), task, run.id)))
        elif busy:
            # Several tasks for one agent run side by side, each in its own folder.
            run.agent = dict(agent, workspace=str(Path(agent["workspace"]).expanduser() / run.id))
        # The task starts a mission: this agent leads, and may bring in teammates.
        run.mission = team.Mission(Path(run.agent["workspace"]).expanduser(), agent["name"], helpers)
        run.mission.runs.append(run)
        self.runs[run.id] = run

        async def start():
            run.task_handle = asyncio.create_task(self._run(run))
        self._call(start())
        how = (f"{models[0]} in Codex, thinking {effort}, in {run.agent['workspace']}" if codex_run else
               f"{models[0]} via {provider}, thinking {effort}" + (f" ({note})" if note else ""))
        also = f" It runs alongside {len(busy)} other {agent['name']} task(s)." if busy else ""
        crew = ("" if codex_run or helpers == [] else
                " It leads: it may bring in teammates itself" +
                (f" (only {', '.join(helpers)})" if helpers else "") + " and reports back for all of them.")
        goes = (f" The result will be saved at {run.destination}." if run.destination else
                f" NOTE: it cannot save to '{wanted}' ({refused}); the result stays in {run.agent['workspace']} - "
                "tell the user." if wanted else "")
        return (f"Started {agent['name']} ({run.id}) on it, using {how}.{also}{crew}{goes} It works in the "
                "background; you will be told when it asks something or finishes. Tell the user briefly who is on it.")

    def delegate_many(self, jobs: list[dict]) -> str:
        """Several tasks at once, possibly to several agents."""
        lines = []
        for job in jobs[:8]:
            lines.append(self.delegate(str(job.get("agent", "")), str(job.get("task", "")),
                                       str(job.get("context", "")), job.get("thinking"), "",
                                       str(job.get("folder", "") or ""), None, str(job.get("save_to", "") or "")))
        return "\n".join(lines) or "No tasks given."

    def find(self, ref: str) -> Run | None:
        ref = (ref or "").strip().lower()
        if ref in self.runs:
            return self.runs[ref]
        matches = [r for r in self.runs.values() if r.name.lower() == ref or (ref and ref in r.name.lower())]
        active = [r for r in matches if r.active]
        pool = active or matches
        return max(pool, key=lambda r: r.started) if pool else None

    def steer(self, ref: str, message: str, thinking: str | None = None) -> str:
        run = self.find(ref)
        if run is not None and not run.active and run.thread_id and run.agent.get("runner") == "codex":
            # A change to a finished build: continue the same Codex session.
            if thinking in providers.EFFORTS:
                run.effort = thinking
            run.inbox.append(message)
            run.stop, run.status, run.finished, run.doing = False, "working", None, "starting the changes"

            async def again():
                run.task_handle = asyncio.create_task(self._run(run, follow_up=True))
            self._call(again())
            return (f"Sent to {run.name}: it continues the same project in {run.agent['workspace']}. "
                    "You will be told when it finishes.")
        if run is None or not run.active:
            return f"No running agent '{ref}'. {self.status()}"
        if thinking in providers.EFFORTS:
            run.effort = thinking
        run.inbox.append(message)
        self.emit("steered", run, message)
        if run.status == "asking" and run.answer is not None and not run.answer.done():
            # A new instruction while it waits counts as the answer too.
            self.loop.call_soon_threadsafe(run.answer.set_result, f"(new instructions) {message}")
        return f"Sent to {run.name}; it will adapt at its next step."

    def reply(self, ref: str, answer: str) -> str:
        run = self.find(ref)
        waiting = [r for r in self.runs.values() if r.status == "asking"]
        if (run is None or run.status != "asking") and len(waiting) == 1:
            run = waiting[0]
        if run is None or run.status != "asking" or run.answer is None:
            return "No agent is waiting for an answer right now."
        self.loop.call_soon_threadsafe(run.answer.set_result, answer)
        self.emit("answered", run, answer)
        return f"Passed the answer to {run.name}; it carries on."

    def cancel(self, ref: str) -> str:
        if (ref or "").strip().lower() in ("all", "everyone", "every agent", "all agents"):
            active = [r for r in self.runs.values() if r.active]
            for r in active:
                self.cancel(r.id)
            return f"Stopping {len(active)} agent task(s)." if active else "No agent is running."
        run = self.find(ref)
        if run is None or not run.active:
            return f"No running agent '{ref}'."
        self._stop_tree(run)
        helping = [c.name for c in self._descendants(run) if c.active]
        return f"Stopping {run.name}" + (f" and its helpers ({', '.join(helping)})." if helping else ".")

    def _descendants(self, run: Run) -> list[Run]:
        found = []
        for child in run.children:
            found += [child] + self._descendants(child)
        return found

    def _stop_tree(self, run: Run) -> None:
        for r in [run] + self._descendants(run):
            if not r.active:
                continue
            r.stop = True
            if r.answer is not None and not r.answer.done():
                self.loop.call_soon_threadsafe(r.answer.set_result, "(stopped)")

    def status(self) -> str:
        if not self.runs:
            return "No agent has been given a task yet."
        leads = sorted((r for r in self.runs.values() if r.parent is None), key=lambda r: r.started,
                       reverse=True)[:6]
        lines = []

        def add(run: Run, level: int) -> None:
            lines.append("  " * level + ("- " if level else "") + run.brief())
            for child in run.children:
                add(child, level + 1)
        for lead in leads:
            add(lead, 0)
        return "\n".join(lines)

    # --- a run ---------------------------------------------------------------------------

    def _system(self, run: Run, workspace: Path) -> str:
        now = dt.datetime.now().strftime("%A %B %-d %Y, %-I:%M %p")
        return (f"{run.agent['instructions']}\n\n"
                f"You are one of the sub-agents of Mint, the user's Mac assistant, which orchestrates you. "
                f"Mint gives you tasks and may send updated instructions while you work - an update "
                f"overrides earlier instructions where they conflict. Your workspace folder is {workspace}; "
                f"file paths are relative to it. It is {now}.\n"
                "Work step by step with your tools; call independent tools together in ONE step (e.g. 3 "
                "searches, or 3 pages to read, at once) - they run in parallel. Match effort to the task: a "
                "simple fact needs 3-6 tool calls, a brief or comparison about 10-15; stop once you can answer "
                "well. When the task is complete, reply WITHOUT calling any tool: that reply is your final "
                "result - short, concrete, with file names."
                + (run.destination.note() if run.destination is not None and run.parent is None else "")
                + team.team_prompt(run, registry.load()))

    async def _run_codex(self, run: Run, follow_up: bool = False) -> None:
        from mint.agents import codex
        run.status = "working"
        # A follow-up starts again (its orb had flown home), showing the change.
        self.emit("start", run, run.inbox[-1] if follow_up and run.inbox else run.task)
        try:
            status, result = await codex.run(self, run, Path(run.agent["workspace"]).expanduser())
        except asyncio.CancelledError:
            await self._end(run, "stopped", "Cancelled.")
            raise
        except Exception as error:
            log.exception("codex run %s crashed", run.id)
            status, result = "failed", f"{type(error).__name__}: {str(error)[:300]}"
        await self._end(run, status, result)

    async def _run(self, run: Run, follow_up: bool = False) -> None:
        if run.agent.get("runner") == "codex":
            return await self._run_codex(run, follow_up)
        agent = run.agent
        workspace = Path(run.agent["workspace"]).expanduser()
        workspace.mkdir(parents=True, exist_ok=True)
        names = list(dict.fromkeys(list(agent.get("tools") or []) + ["ask_user", "report_progress"]))
        schema = agent_tools.schemas(names)
        if run.mission is not None:
            delegating = run.depth < team.MAX_DEPTH and run.mission.allowed != []
            schema += [{"name": n, **team.SCHEMAS[n]} for n in team.TEAM_TOOLS
                       if delegating or n not in ("ask_agent", "ask_agents")]
        system = self._system(run, workspace)
        memory_note = await asyncio.to_thread(team.recall, run.task)
        source = f"{run.parent.name} (your teammate)" if run.parent else "Mint"
        run.messages = [{"role": "user", "content": f"Task from {source}:\n{run.task}" +
                         (f"\n\nContext:\n{run.context}" if run.context else "") + memory_note}]
        run.status = "working"
        self.emit("start", run, run.task)
        try:
            for step in range(int(agent.get("max_steps", 24))):
                if run.stop:
                    return await self._end(run, "stopped", "Stopped on request.")
                if run.inbox:
                    notes = [m for m in run.inbox if m.startswith("[Team note")]
                    updates = [m for m in run.inbox if not m.startswith("[Team note")]
                    run.inbox.clear()
                    if updates:
                        run.messages.append({"role": "user", "content": "[Updated instructions from the user, "
                                             "via Mint]\n" + "\n".join(updates)})
                    if notes:
                        run.messages.append({"role": "user", "content": "\n".join(notes)})
                team.trim(run.messages)
                run.steps = step + 1
                run.doing = "thinking"
                self.emit("thinking", run)
                out = await asyncio.to_thread(providers.chat, run.agent, system, run.messages, schema, run.effort)
                if step == 0 and out.get("note"):
                    self.emit("note", run, out["note"])
                run.model = f"{out.get('provider')}/{out.get('model')}"
                if not out["tool_calls"]:
                    if run.inbox:                 # instructions arrived while it thought
                        run.messages.append({"role": "assistant", "content": out["text"] or "(ok)",
                                             "_gemini": out.get("_gemini") if out.get("provider") == "gemini" else None,
                                             "_reasoning_details": out.get("_reasoning_details"),
                                     "_response_id": out.get("_response_id")})
                        continue
                    return await self._end(run, "done", out["text"] or "(no summary given)")
                run.messages.append({"role": "assistant", "content": out["text"], "tool_calls": out["tool_calls"],
                                     "_gemini": out.get("_gemini") if out.get("provider") == "gemini" else None,
                                     "_reasoning_details": out.get("_reasoning_details"),
                                     "_response_id": out.get("_response_id")})
                calls = out["tool_calls"]
                if len(calls) > 1 and all(c["name"] in _PARALLEL for c in calls):
                    # Independent lookups in one step run together (searches, page
                    # reads): Astra's 16 one-at-a-time searches took 65 s in testing.
                    results = await asyncio.gather(*(self._tool(run, c, workspace) for c in calls))
                else:
                    results = []
                    for call in calls:
                        results.append("(skipped: stopped)" if run.stop else await self._tool(run, call, workspace))
                for call, result in zip(calls, results):
                    run.messages.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"],
                                         "content": result})
            return await self._end(run, "failed", f"Ran out of steps ({agent.get('max_steps', 24)}) before finishing.")
        except asyncio.CancelledError:
            await self._end(run, "stopped", "Cancelled.")
            raise
        except providers.KeyProblem as error:
            return await self._end(run, "failed", str(error))
        except Exception as error:
            log.exception("agent run %s crashed", run.id)
            return await self._end(run, "failed", f"{type(error).__name__}: {str(error)[:300]}")

    async def _tool(self, run: Run, call: dict, workspace: Path) -> str:
        name, args = call["name"], call.get("args") or {}
        if name == "ask_user":
            question = str(args.get("question", "")).strip() or "(no question text)"
            run.status, run.question = "asking", question
            run.doing = "waiting for your answer"
            run.answer = self.loop.create_future()
            self.emit("ask", run, question)
            await self.tell_mint(
                f"(A message from your sub-agent {run.name} [{run.id}], not from the user.) {run.name} asks: "
                f"\"{question}\". Ask the user this now, briefly and in your own words; when they answer, pass "
                "the answer back with answer_agent.", wake=True)
            try:
                answer = await asyncio.wait_for(asyncio.shield(run.answer), ANSWER_TIMEOUT)
            except asyncio.TimeoutError:
                answer = "(no answer in 15 minutes - make a sensible assumption and say which)"
            run.status, run.question, run.answer = "working", None, None
            return f"The user answered: {answer}"
        if name == "report_progress":
            message = str(args.get("message", ""))[:200]
            run.doing = message
            self.emit("progress", run, message)
            return "Noted."
        if name in team.TEAM_TOOLS:
            return await self._team_tool(run, name, args)
        run.doing = agent_tools.describe_args(name, args)
        self.emit("tool", run, run.doing, tool=name)
        root = run.mission.root if run.mission is not None else None
        destination = run.destination if run.parent is None else None
        result = await asyncio.to_thread(agent_tools.run, name, args, workspace, root, destination)
        if name == "write_file" and result.startswith("Wrote "):
            written = str(args.get("path", ""))
            written = str(Path(written).expanduser()) if written.startswith("~") else written
            run.files.append(written)
            self.emit("file", run, written)
        if name == "create_pdf" and ".pdf" in result:
            run.files.append(result.split()[-1] if result else "pdf")
        return result

    # --- teamwork ----------------------------------------------------------------------------

    async def _team_tool(self, run: Run, name: str, args: dict) -> str:
        mission = run.mission
        if name == "share_note":
            note = str(args.get("note", "")).strip()[:1500]
            if not note:
                return "Nothing to share."
            mission.note(run.name, note)
            for other in mission.active():
                if other is not run and other.agent.get("runner") != "codex":
                    other.inbox.append(f"[Team note from {run.name}] {note}")
            self.emit("progress", run, f"shared a note")
            return "Shared on the team board."
        if name == "read_board":
            return mission.board_text()
        if name == "list_team":
            return team.roster_text(registry.load(), mission, run.name)
        jobs = ([args] if name == "ask_agent" else list(args.get("jobs") or []))[:4]
        if not jobs:
            return "No jobs given."
        started, refused = [], []
        for job in jobs:
            child = self._helper(run, str(job.get("agent", "")), str(job.get("task", "")), job.get("thinking"))
            (started if isinstance(child, Run) else refused).append(child)
        if not started:
            return "NOT STARTED: " + " ".join(refused) + " Do it yourself, or choose another teammate."
        who = ", ".join(c.name for c in started)
        run.doing = f"waiting for {who}"
        self.emit("progress", run, f"asked {who}")
        results = await asyncio.gather(*(self._wait_helper(run, c) for c in started))
        run.doing = "working"
        return "\n\n".join(results + [f"(Not started: {r})" for r in refused])

    def _helper(self, parent: Run, agent_name: str, task: str, thinking) -> "Run | str":
        """Start a teammate on part of `parent`'s task (on the loop)."""
        mission = parent.mission
        agent = registry.get(agent_name)
        if agent is None:
            return f"There is no agent called '{agent_name}' (see list_team)."
        if not mission.may_use(agent["name"]):
            return f"{agent['name']} is not allowed on this mission."
        if parent.depth >= team.MAX_DEPTH:
            return "Helpers at this level cannot bring in more teammates."
        if agent["name"].lower() in team.chain(parent):
            return f"{agent['name']} is already part of this chain of requests (it would loop)."
        if len(mission.active()) >= team.TEAM_CAP:
            return f"The team is at its limit of {team.TEAM_CAP} agents working at once."
        if too_small(task):
            return f"'{task}' is too small to hand off."
        try:
            provider, _, _ = providers.route(agent)
        except providers.NoProvider as error:
            return str(error)
        if providers.key_problem(provider):
            return providers.key_problem(provider)
        effort = thinking if thinking in providers.EFFORTS else choose_effort(task, agent.get("thinking", "low"))
        child = Run(agent, task, f"Part of a mission led by {mission.lead}; asked by {parent.name}.", effort)
        child.parent, child.depth, child.mission = parent, parent.depth + 1, mission
        child.agent = dict(agent, workspace=str(team.helper_folder(mission, agent, child.id)))
        child.done = self.loop.create_future()
        parent.children.append(child)
        mission.runs.append(child)
        self.runs[child.id] = child
        child.task_handle = asyncio.create_task(self._run(child))
        return child

    async def _wait_helper(self, parent: Run, child: Run) -> str:
        try:
            status, result = await asyncio.wait_for(asyncio.shield(child.done), team.HELPER_TIMEOUT)
        except asyncio.TimeoutError:
            self._stop_tree(child)
            return f"{child.name} did not finish within {team.HELPER_TIMEOUT // 60} minutes and was stopped."
        folder = Path(child.agent["workspace"])
        try:
            where = folder.relative_to(parent.mission.root)
        except ValueError:
            where = folder
        files = f" Its files (read_file): {', '.join(str(where / f) for f in child.files[-8:])}." \
            if child.files and child.agent.get("runner") != "codex" else ""
        return f"{child.name} {status}: {result[:6000]}{files}"

    async def _end(self, run: Run, status: str, result: str) -> None:
        delivered = ""
        if status == "done" and run.destination is not None and run.parent is None:
            try:
                run.delivered, problem = await asyncio.to_thread(
                    deliver.deliver, run.destination, run.files, Path(run.agent["workspace"]).expanduser(), result)
            except Exception as error:
                log.exception("delivering %s's result failed", run.id)
                run.delivered, problem = [], f"{type(error).__name__}: {error}"
            if run.delivered:
                shown = ", ".join(deliver._short(p) for p in run.delivered[:6])
                more = f" and {len(run.delivered) - 6} more" if len(run.delivered) > 6 else ""
                delivered = f" Saved where the user asked: {shown}{more}."
            else:
                delivered = f" It could NOT be saved at {run.destination} ({problem}) - tell the user."
            log.info("agent %s delivery to %s: %s", run.id, run.destination, delivered.strip())
        run.status, run.result, run.finished = status, result, time.time()
        run.doing = status
        self.emit(status, run, result)
        working = [f for f in run.files if Path(f).expanduser() not in run.delivered]
        where = delivered + (f" {'Working files' if delivered else 'Files'} are in {run.agent['workspace']}: "
                             f"{', '.join(working[-6:])}."
                             if working and run.agent.get("runner") != "codex" else "")
        try:
            from mint.knowledge.conversation import memory
            memory.add("agent", f"{run.name} ({status}) task: {run.task[:200]} -> {result[:600]}{where}")
        except Exception:
            pass
        await asyncio.to_thread(team.remember, run, status, result)
        if run.parent is not None:
            # A helper reports to the agent that asked it, not to Mint.
            if run.done is not None and not run.done.done():
                run.done.set_result((status, result + where))
            return
        helped = [c for c in self._descendants(run) if c.status == "done"]
        if helped:
            where += " Teammates who helped: " + "; ".join(
                f"{c.name} ({c.task[:60]})" for c in helped[:6]) + "."
        if status == "done":
            text = f"{run.name} [{run.id}] finished \"{run.task[:120]}\". Result: {result[:1500]}{where}"
        elif status == "failed":
            text = (f"{run.name} [{run.id}] could not finish \"{run.task[:120]}\": {result[:400]}"
                    + (f" Nothing was saved at {run.destination}." if run.destination is not None else ""))
        else:
            text = ""
        if text:
            # Results landing together go to Mint as one message, so it can
            # report several agents' outputs in one breath.
            self._finished.append(text)
            if self._flush_handle is None:
                self._flush_handle = self.loop.call_later(
                    GATHER, lambda: asyncio.ensure_future(self._flush_results()))

    async def _flush_results(self) -> None:
        self._flush_handle = None
        batch, self._finished = self._finished, []
        if not batch:
            return
        still = [r for r in self.runs.values() if r.active]
        pending = f" Still working: {', '.join(r.name for r in still)}." if still else ""
        head = ("(A message from your sub-agents, not from the user.) " if len(batch) > 1 else
                "(A message from your sub-agent, not from the user.) ")
        key_note = (" A key problem cannot be fixed by you or the agents: tell the user plainly that they need to "
                    "make a new API key and put it in .env - do not promise to fix it."
                    if any("refused the API key" in b for b in batch) else
                    " The agents' account is out of credits: tell the user plainly they need to add credits (or "
                    "use another key) - you and the agents cannot fix that. Offer to do a small task yourself."
                    if any("has no credits left" in b for b in batch) else "")
        await self.tell_mint(head + " | ".join(batch) + pending + key_note +
                             " Tell the user the outcome briefly - one sentence per agent; offer to open files.")


hub = Hub()
