---
title: Create a project in ChatGPT
when: The user wants a new project in the ChatGPT app
apps: ChatGPT
source: seed
uses: 0
wins: 1
fails: 0
---
## Steps
1. open_app ChatGPT.
2. ui_act click "the plus next to Projects to add a new project" (its label is "Add new project").
3. ui_act type into "Project name field" in the Create project dialog: the project name.
4. ui_act click "Create project button in the dialog" (the button, not the dialog's heading of the same words).
5. The dialog greys out, then closes, and the new project appears under Projects.

## Notes
- Verified end to end with ui_act on 2026-09-24 (project "Mint test").
- ChatGPT is often "active" with no visible window or on another Space; ui_act brings the window forward before clicking.
