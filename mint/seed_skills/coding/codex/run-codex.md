---
title: Build or change code with OpenAI Codex (GPT-6 Luna)
when: The user wants Codex (OpenAI's coding agent) to build or change code, a web page, a site or an app
apps: ChatGPT, Codex
source: seed
uses: 0
wins: 0
fails: 0
---
## Steps
1. delegate_task with agent "Codex" and the complete task (what to build, style, files). Put what it should build from in context, or attach_window true when that material is in the window in front (e.g. a ChatGPT answer). Thinking low; medium for a complex app.
2. Tell the user in a few words that Codex is on it, then wait for its message - do not poll or open Terminal.
3. When it finishes: for a web page, preview_site with the path from its message, then look to check it renders, and tell the user what you see.
4. Changes afterwards: message_agent Codex - it continues the same project.

## Notes
- Codex always runs GPT-6 Luna (the ChatGPT app's own Codex CLI, signed in with the user's ChatGPT account). Each new build gets its own folder under ~/Documents/Mint/agents/codex/.
- Do not type codex into Terminal: the stand-alone CLI (~/.local/bin/codex, 0.145) is refused for gpt-6-luna with a ChatGPT account.
