---
title: Start a chat in a ChatGPT project
when: The user wants to ask ChatGPT something inside one of their ChatGPT projects (e.g. "in the data-labeling-portal project, ask…"), optionally with a particular model
apps: ChatGPT
source: seed
uses: 0
wins: 1
fails: 0
---
## Steps
1. open_app ChatGPT.
2. ui_act click "New chat" (top of the sidebar).
3. ui_act click the project picker pop-up by the message box (it reads "Choose project" or "Change project: <current project>").
4. ui_act type into "Search projects" the project name, with press_return true - this selects the project.
5. Model, only if the user asked: ui_act click "the model picker", then "Select model", then the model ("GPT-6 Luna"; "Luna Light" = Luna at Light effort), then press_key escape.
6. type_text the prompt (it goes into the message box, "Work with ChatGPT").
7. Only if the user asked to send it: ui_act click "Send button".

## Notes
- Route verified 2026-09-24: New chat -> project picker -> Search projects + Return -> type.
- The sidebar's "Start new chat in <project>" buttons only appear on hover; ui_act picked "New chat" instead of them.
- Send with the Send button, not press_return: with Return the chat once landed outside the project.
- After sending, a "Full access is on" banner can appear; ignore it. Replies take ~10 s; read_window to read them.
