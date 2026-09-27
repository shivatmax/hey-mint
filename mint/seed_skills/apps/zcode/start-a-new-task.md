---
title: Start a new coding task in ZCode
when: The user wants ZCode (the GLM coding app; speech often hears it as "Xcode" or "Z code") to build or change something
apps: ZCode
source: seed
uses: 0
wins: 0
fails: 0
---
## Steps
1. open_app ZCode ("Xcode" and "Z code" resolve to it).
2. ui_act click "New task" in the sidebar.
3. If the user named a project or folder, ui_act click "Project" (or "Add project") and pick it.
4. If the user asked for a particular model (e.g. GLM), ui_act click the model picker near the message box and choose it.
5. type_text the task description into the message box.
6. Only when the user wants it started: ui_act click "Send" (or press_key return).

## Notes
- ZCode is an Electron app; Mint unlocks its accessibility tree (it showed 31 controls before).
- Starting a task runs an agent that edits files and uses model credits: confirm before sending if the user did not clearly ask to start.
