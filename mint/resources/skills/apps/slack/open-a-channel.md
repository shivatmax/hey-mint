---
title: Open a Slack channel or DM
when: The user wants a Slack channel or conversation in a given workspace
apps: Slack
source: seed
uses: 0
wins: 0
fails: 0
---
## Steps
1. open_slack with the workspace and channel in the user's words (aliases like "on call" resolve to oncall-support).
2. For anything else inside Slack (Threads, a message, Huddles), ui_act click it.

## Notes
- open_slack uses saved deep links for known channels, and the quick switcher otherwise.
- Never send a message unless the user asked for that exact message to be sent.
