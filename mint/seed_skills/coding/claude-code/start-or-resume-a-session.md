---
title: Start or resume Claude Code in a project
when: The user wants to work with Claude Code (the claude command line tool) in a folder, or continue their last Claude Code session
apps: Terminal
source: seed
uses: 0
wins: 0
fails: 0
---
## Steps
1. open_app Terminal (a new window or tab: press_key t with command for a new tab).
2. type_text "cd <project folder>" with press_return.
3. To start: type_text "claude" with press_return. To continue the most recent session: "claude --continue". To pick an older one: "claude --resume".
4. Once its prompt appears, type_text the user's request with press_return.

## Notes
- claude is installed at ~/.local/bin/claude.
- Claude Code asks before editing files or running commands; read_window shows its question - ask the user rather than approving on their behalf unless they said to.
