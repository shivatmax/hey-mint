"""Sub-agents: named workers Mint (the orchestrator) hands tasks to.

    registry.py   who the agents are (agents.json: name, colour, model, tools, prompt)
    providers.py  one chat() for Gemini and OpenAI, with tool calling
    tools.py      what agents can do: search the web, read pages, files, PDFs, ask the user
    runtime.py    the hub: runs, events, steering, questions relayed through Mint
    orchestrator.py  the tools Mint itself uses: delegate, steer, answer, stop, create
"""
