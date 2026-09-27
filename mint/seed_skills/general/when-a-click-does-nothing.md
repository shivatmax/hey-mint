---
title: Recover when a click or typing does nothing
when: A click, ui_act or type_text reported no change or FAILED, or the app seems not to respond
apps:
source: seed
uses: 0
wins: 0
fails: 0
---
## Steps
1. list_open or frontmost_app: make sure the right app is in front; switch_to it if not.
2. Look for something covering it: a dialog, a menu, a sign-in sheet - read_window or look. Deal with that first (usually its button or Escape).
3. Retry with a more specific ui_act target: name the role and place ("the Create project button in the dialog", "the search field at the top").
4. If there is still no effect, look, then ui_act again describing what you see.
5. Tell the user what did not work, plainly, and what you will try next - never say it is blocked.
