---
title: Write a new Google Doc (and export it as PDF)
when: The user wants a new Google Doc written from information, optionally exported to PDF
apps: Google Chrome
source: seed
uses: 1
wins: 1
fails: 0
---
## Steps
1. Read everything needed first (calendar_events, read_window on Gmail…) - opening the doc brings it to the front.
2. open_chrome in the right account with url docs.new (a new doc every time; never reuse an old doc for a new write-up).
3. type_text the full content - it waits for the editor and clicks into the page itself; the first line becomes the doc's name.
4. If asked for a PDF: export_doc_pdf (saves to ~/Documents/Mint).

## Notes
- Verified end to end on 2026-09-23 (calendar -> Gmail -> new doc -> PDF).
- Typing before the doc finished loading used to create a stray "Tab 2"; type_text now waits.
