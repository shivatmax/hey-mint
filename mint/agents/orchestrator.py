"""The tools Mint (the orchestrator) uses to run its sub-agents, and what it is
told about them in its instructions."""

from __future__ import annotations

from google.genai import types

from mint.agents import registry
from mint.agents.runtime import hub

STRING = {"type": types.Type.STRING}


def _fn(name, description, properties, required=None):
    return types.FunctionDeclaration(
        name=name, description=description,
        parameters=types.Schema(type=types.Type.OBJECT,
                                properties={k: types.Schema(**v) for k, v in properties.items()},
                                required=required or []))


def declarations() -> list[types.FunctionDeclaration]:
    return [
        _fn("list_agents", "List your sub-agents: name, what each is for, its model, and what each is doing now.", {}),
        _fn("delegate_task",
            "Hand a substantial task to a sub-agent, which works in the background while you keep talking. "
            "ONLY when it truly needs an agent: multi-step web research, a long document, building or "
            "revising code / RL environments, work producing files. Never for a quick answer, a single "
            "action on the Mac, anything on screen, or chit-chat - do those yourself. Write complete "
            "instructions: goal, constraints, deliverables (files, format). Choose thinking by difficulty: "
            "none for simple mechanical work, low for normal tasks, medium for research, design, debugging "
            "or RL environments (never more).",
            {"agent": {**STRING, "description": "The agent's name, e.g. Astra, Luna, Sage."},
             "task": {**STRING, "description": "Full instructions."},
             "why": {**STRING, "description": "One line: why this needs an agent rather than you."},
             "thinking": {**STRING, "enum": ["none", "low", "medium"]},
             "context": {**STRING, "description": "Anything it should know: what the user said, earlier "
                                                   "results, research notes to build from."},
             "folder": {**STRING, "description": "Optional: an existing project folder to work in (e.g. to "
                                                 "change something Codex built before). Omit for a new one."},
             "save_to": {**STRING, "description": "Where the user wants the result, exactly as they said it: a "
                                                  "full file path ('~/Notes/plan.md') or folder ('~/Desktop/'). "
                                                  "The result is saved there (the agent keeps its own folder for "
                                                  "drafts). Omit if the user did not say."},
             "helpers": {"type": types.Type.ARRAY, "items": types.Schema(type=types.Type.STRING),
                         "description": "Optional: the only teammates it may bring in, e.g. ['Astra'], or "
                                        "['none'] for no helpers. Omit to let it choose any."},
             "attach_window": {"type": types.Type.BOOLEAN, "description":
                               "Give the agent ALL the text of the window in front (e.g. the research ChatGPT "
                               "just wrote) - better than copying long text into context yourself."}},
            ["agent", "task", "why"]),
        types.FunctionDeclaration(
            name="delegate_tasks",
            description=("Start several sub-agent tasks at once (in parallel), e.g. Astra researches while Sage "
                         "drafts, or two Luna tasks side by side. Same rules as delegate_task."),
            parameters=types.Schema(type=types.Type.OBJECT, required=["tasks"], properties={
                "tasks": types.Schema(type=types.Type.ARRAY, items=types.Schema(
                    type=types.Type.OBJECT, required=["agent", "task"], properties={
                        "agent": types.Schema(type=types.Type.STRING),
                        "task": types.Schema(type=types.Type.STRING),
                        "thinking": types.Schema(type=types.Type.STRING, enum=["none", "low", "medium"]),
                        "context": types.Schema(type=types.Type.STRING),
                        "folder": types.Schema(type=types.Type.STRING),
                        "save_to": types.Schema(type=types.Type.STRING)}))})),
        _fn("agent_status", "What your sub-agents are doing, or have finished, right now.",
            {"agent": {**STRING, "description": "Optional: one agent's name."}}),
        _fn("message_agent",
            "Send new or changed instructions to a running sub-agent - when the user changes what they want "
            "mid-task ('tell Luna to use pytest', 'make it shorter'). It adapts at its next step. For Codex "
            "this also works after it finished: it continues the same project with the change.",
            {"agent": STRING, "message": STRING,
             "thinking": {**STRING, "enum": ["none", "low", "medium"],
                          "description": "Optional: change how hard it thinks from now on."}},
            ["agent", "message"]),
        _fn("answer_agent",
            "Pass the user's answer to a sub-agent that asked a question.",
            {"agent": STRING, "answer": STRING}, ["answer"]),
        _fn("stop_agent", "Stop a running sub-agent, or 'all'.", {"agent": STRING}, ["agent"]),
        _fn("create_agent",
            "Create (or update) a named sub-agent when the user asks for one: its name, what it is for, how it "
            "should work, and optionally which model. Confirm what you made.",
            {"name": STRING, "role": {**STRING, "description": "One line: what it is for."},
             "instructions": {**STRING, "description": "Its own system prompt: how it should work."},
             "thinking": {**STRING, "enum": ["none", "low", "medium"],
                          "description": "Its usual thinking level (every agent runs on GPT-6 Luna)."},
             "web": {"type": types.Type.BOOLEAN, "description": "Give it web search and page reading."},
             "color": {**STRING, "description": "Optional colour, e.g. '#FF6B9A' or 'pink'."}},
            ["name", "role"]),
    ]


_COLORS = {"pink": "#FF6B9A", "purple": "#8B7CFF", "violet": "#C77DFF", "teal": "#2EC4B6", "green": "#7BE07B",
           "orange": "#FF8A4C", "amber": "#FFB547", "yellow": "#FFD447", "blue": "#4DA3FF", "red": "#FF5A5A",
           "cyan": "#5CE1E6", "mint": "#6EE7B7"}


def _create(args: dict) -> str:
    name = str(args.get("name", "")).strip()
    if not name:
        return "An agent needs a name."
    existing = registry.get(name)
    agent = dict(existing) if existing and existing["name"].lower() == name.lower() else {"name": name}
    agent["role"] = str(args.get("role") or agent.get("role", ""))
    if args.get("instructions"):
        agent["instructions"] = str(args["instructions"])
    elif not existing:
        agent["instructions"] = f"You are {name}. {agent['role']} Work carefully and report concisely."
    if args.get("thinking") in ("none", "low", "medium"):
        agent["thinking"] = args["thinking"]
    tools = ["write_file", "read_file", "list_files", "ask_user", "report_progress"]
    if args.get("web", True):
        tools = ["web_search", "fetch_url"] + tools
    agent.setdefault("tools", tools)
    color = str(args.get("color") or "").strip().lower()
    agent["color"] = _COLORS.get(color, color if color.startswith("#") else agent.get("color") or registry.next_color())
    saved = registry.save(agent)
    return (f"{'Updated' if existing else 'Created'} {saved['name']} ({saved['role']}), on {saved['models'][0]} "
            f"via {saved['provider']}, thinking {saved['thinking']} by default, colour {saved['color']}.")


def _list(args: dict) -> str:
    lines = []
    for a in registry.load():
        run = hub.find(a["name"])
        doing = f" - now {run.status}: {run.doing}" if run and run.active else ""
        lines.append(f"{a['name']}: {a['role']} ({a['models'][0]} via {a['provider']}, thinks {a['thinking']}){doing}")
    return "\n".join(lines) or "No agents yet."


def _helpers(value) -> list[str] | None:
    if not value:
        return None
    names = [str(v).strip() for v in value if str(v).strip()]
    return [] if any(n.lower() in ("none", "no", "nobody") for n in names) else names


def _with_window(args: dict) -> str:
    """The context, plus the front window's text when asked for (read here, exactly,
    rather than retyped by the voice model)."""
    context = str(args.get("context", "") or "")
    if not args.get("attach_window"):
        return context
    from mint.tools import documents
    text = documents.read_window(max_chars=40000, with_links=True)
    if text.startswith("FAILED"):
        return context + f"\n\n(The window's text could not be read: {text})"
    return (context + "\n\n" if context else "") + \
        "The full text of the window the user was looking at (use what is relevant):\n" + text


HANDLERS = {
    "list_agents": _list,
    "delegate_task": lambda a: hub.delegate(str(a.get("agent", "")), str(a.get("task", "")),
                                            _with_window(a), a.get("thinking"), str(a.get("why", "")),
                                            str(a.get("folder", "") or ""), _helpers(a.get("helpers")),
                                            str(a.get("save_to", "") or "")),
    "delegate_tasks": lambda a: hub.delegate_many([dict(t) for t in (a.get("tasks") or [])]),
    "agent_status": lambda a: (hub.find(str(a["agent"])).brief() if a.get("agent") and hub.find(str(a["agent"]))
                               else hub.status()),
    "message_agent": lambda a: hub.steer(str(a.get("agent", "")), str(a.get("message", "")), a.get("thinking")),
    "answer_agent": lambda a: hub.reply(str(a.get("agent", "")), str(a.get("answer", ""))),
    "stop_agent": lambda a: hub.cancel(str(a.get("agent", ""))),
    "create_agent": _create,
}


def prompt_text() -> str:
    agents = registry.load()
    roster = "; ".join(f"{a['name']} - {a['role']}" for a in agents)
    return (
        "You are the orchestrator of sub-agents that work in the background on GPT-6 Luna: " + roster + ". "
        "When the user names an agent ('ask Luna', 'give it to Sage'), use exactly that agent. "
        "Otherwise, to BUILD something the user will open or run - a web page, an app, a script - give it to Codex "
        "(it writes, runs and checks the code itself); put everything it should build from (e.g. research "
        "you read) in context. When Codex finishes, show the result with preview_site and check it with "
        "look. Never make up material you were meant to get from somewhere else (ChatGPT's answer, a page, "
        "a file): if you could not read it, wait for it (wait_until_done), look, or tell the user - and hand "
        "on what you actually read (attach_window). "
        "Call an agent ONLY when the work truly needs one: multi-step web research, a long document, "
        "building or revising code or RL environments, anything that produces files over many steps. Do "
        "everything else yourself - answers you know, quick facts, one action on the Mac, anything on "
        "screen, reminders, chat. When you do delegate, give complete instructions and pick the thinking "
        "level by difficulty (none / low / medium). Several independent pieces can run at once "
        "(delegate_tasks). Agents work as a TEAM: give a job to the ONE agent best placed to lead it, and it "
        "brings in teammates itself when their specialty helps (Sage writing a report asks Astra for the "
        "research; Luna asks Codex to build the page) and reports back for all of them - so do not split a "
        "job across agents yourself unless the parts are truly independent. If the user limits who may help "
        "('just Sage, no research'), pass helpers. agent_status shows who is helping whom; stop_agent on "
        "the lead stops its helpers. Messages starting '(A message from your sub-agent' are NOT the user: relay an "
        "agent's question to the user in your own words and pass the reply with answer_agent; if the user "
        "changes a running task, use message_agent; when results arrive, tell the user in a sentence per "
        "agent and where the file is - never read documents aloud. When the user says where to save the result "
        "(a path or folder), pass it as save_to and keep it in the task; it is saved there, not only in the "
        "agent's own folder. You can check on them (agent_status), stop them (stop_agent), "
        "and create new ones on request (create_agent).")
