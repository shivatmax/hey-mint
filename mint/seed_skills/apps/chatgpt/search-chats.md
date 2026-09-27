---
title: Search chats in ChatGPT
when: The user wants to find an old ChatGPT chat or project by words in it
apps: ChatGPT
source: seed
uses: 0
wins: 0
fails: 0
---
## Steps
1. open_app ChatGPT.
2. ui_act click "Search" in the sidebar - the search box only exists after this.
3. ui_act type the search words into the search box.
4. read_window to see the results, then ui_act click the one the user wants.

## Notes
- type_text with field "search box" fails until Search has been clicked: the box is not on screen before that.
