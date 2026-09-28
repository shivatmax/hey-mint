# Mint — the complete guide

Everything Mint can do, how it does it, and what to say. Install and overview: [README](../README.md); how it is built: [architecture](architecture.md).

- [1. What Mint is](#1-what-mint-is)
- [2. Getting started](#2-getting-started)
- [3. Talking to Mint](#3-talking-to-mint)
- [4. Apps, windows and tabs](#4-apps-windows-and-tabs)
- [5. Clicking, typing and reading the screen](#5-clicking-typing-and-reading-the-screen)
- [6. The harness: files, web, browser, menus, scrolling, scripts](#6-the-harness-files-web-browser-menus-scrolling-scripts)
- [7. Everyday Mac tasks](#7-everyday-mac-tasks)
- [8. Long tasks across apps](#8-long-tasks-across-apps)
- [9. Skills: what Mint has learned](#9-skills-what-mint-has-learned)
- [10. Memory](#10-memory)
- [11. Sub-agents](#11-sub-agents)
- [12. The orb, animations and effects](#12-the-orb-animations-and-effects)
- [13. Customising](#13-customising)
- [14. Voice: wake word, voice lock, talking over Mint](#14-voice-wake-word-voice-lock-talking-over-mint)
- [15. Safety rules built in](#15-safety-rules-built-in)
- [16. Configuration and files](#16-configuration-and-files)
- [17. Testing and troubleshooting](#17-testing-and-troubleshooting)
- [18. Tool reference](#18-tool-reference)

---

## 1. What Mint is

Mint is a hands-free assistant that lives on your Mac. You say **"Hey Mint"** (or
press **⌘J** and type), ask for something, and it does it on your screen while
talking back.

Two models, each doing what it is best at:

| Model | Role |
|---|---|
| **Gemini Live** (`gemini-3.8-live`, falls back to `3.1-flash-live` on quota) | Listens, talks, reasons, plans, and calls tools |
| **Jev** (TypeSafe `jev-latest`) | Fast, calibrated choices from real lists: which control to click, which skill fits, which memories are relevant, which app you meant, whether a remark was meant for Mint. About a second per decision |
| Gemini Flash (lite first) | Background work: conversation summaries, fact extraction, writing skills, vision grounding |
| GPT-6 Luna / Codex | Sub-agents for long background jobs (research, code, documents, building sites) |

Mint acts through **79 tools**. Wherever it can, it uses a direct route (an API,
the accessibility tree, a menu command, AppleScript) rather than guessing pixels.

## 2. Getting started

```sh
./set-key.sh          # paste your Gemini key
./install.sh          # builds ~/Applications/Mint.app
open ~/Applications/Mint.app
```

macOS asks for **Microphone** first. Grant **Accessibility** from the menu bar item
(needed for clicking, typing, menus and scrolling). **Screen Recording**,
**Automation** (per app, e.g. "Mint wants to control Google Chrome"),
**Calendars**, **Reminders** and **Files and Folders** are asked for the first time
a tool needs them.

Keys live in `.env` (mode 600): `GEMINI_API_KEY`, `TYPESAFE_API_KEY` (Jev), and
optionally `OPENAI_API_KEY` / `OPENROUTER_API_KEY` (sub-agents) and `EXA_API_KEY`
/ `TAVILY_API_KEY` (better web search; without them DuckDuckGo is used, free).

## 3. Talking to Mint

| Do this | How |
|---|---|
| Wake it | "Hey Mint", or click the orb, or ⌘J and type |
| Talk over it | Just start talking; it stops and listens (echo cancellation keeps its own voice out) |
| Stop everything | "stop", "cancel", "never mind", "hold on"; or ■ in the chat |
| Send it to sleep | "bye", "that's all", "good night"; it also sleeps after 12 quiet seconds |
| Type instead | ⌘J opens the chat; typing never opens the microphone |
| From a script | `.venv/bin/python -m mint --say "what's on my calendar today?"` |

**It is one continuous conversation.** Follow-ups ("and add cheese to that list",
"no, the other project") continue the current task. After a restart, the last few
turns (within six hours) and a running summary carry over, so "what was that date
again?" still works.

**Mint changes itself when asked:** "don't speak, just chat", "you can speak
again", "turn off the mic", "make it pink", "move to the top left", "hide the face",
"use a deeper voice", "talk slower", "switch to Puck".

## 4. Apps, windows and tabs

| Say | What happens |
|---|---|
| "open Figma", "open Xcode" (meaning ZCode) | `open_app` resolves the name against installed apps (exact, known names, partial, mishearings, then Jev) and checks the app really came to the front |
| "go to youtube.com" | `open_url`, in a new tab of the right browser (see *Which browser* below), or the tab already showing it |
| "open my Downloads folder" | `open_folder` |
| "open Chrome as my work account on Gmail" | `open_chrome` with the profile from `custom.json` |
| "open the on-call channel in Slack" | `open_slack` |
| "what's open?", "switch to the Jira tab" | `list_open`, `switch_to` (reuses tabs and windows instead of opening duplicates) |
| "quit Spotify" | `quit_app`, only when you said quit or close |

Electron apps (ChatGPT, ZCode, Slack, VS Code, Claude, Notion) are unlocked
(`AXManualAccessibility`) before Mint works in them, so their full control tree is
visible.

## 5. Clicking, typing and reading the screen

| Say | Tool | How |
|---|---|---|
| "click Sign in", "pick the second result" | `ui_act` | Every control in the window from the accessibility tree → exact label, then Jev, then a vision model on a numbered screenshot; a real mouse press; the window is diffed to confirm it changed |
| "type 'see you at six'" | `type_text` | Types into the focused box, or into the box named in `field` ("the search box"), chosen by Jev from the real boxes |
| "read this page", "what does the email say?" | `read_window` | All text in the front window, including what is scrolled out of view |
| "summarise this" (with text selected) | `get_selected_text` | |
| "look at my screen" | `look` | A screenshot to Gemini; `click_at` / `click_text` for things with no accessibility. `click_at` says what it hit ("that is the text field 'Search'"), so a wrong point is noticed at once instead of clicked again |
| "what's under my mouse?", "click this" | `pointer` | `where`: the control under your pointer, its app and nearby text; `click` / `double_click` / `right_click` right there (Send/Delete-type buttons still need you to ask) |
| "press command shift T" | `press_key` | |
| "scroll down" | `scroll` | Aimed at the front window without moving your pointer |

## 6. The harness: files, web, browser, menus, scrolling, scripts

The harness (`mint/tools/harness.py`) gives Mint the everyday powers of a desktop
agent. Mint works **in the background first** and moves your pointer only when nothing
else can do the job. It is also told never to say "I can't" about something on the Mac
before trying the tools below. The order:

*Background (no pointer, nothing has to come to the front)*
1. **web_search / read_url** for facts, news, docs and prices;
2. **file tools** for anything on disk;
3. **open_url / browser** for web pages: the browser is chosen for you, and page actions,
   tab groups and extension buttons are done through JavaScript and Accessibility;
4. **run_applescript** for scriptable apps.

*Foreground (the app comes to the front, still no guessing)*
5. **menu** for any app command; 6. **scroll_to** for things off screen; 7. **ui_act** for a
control by its name; 8. **wait_for_text** after something slow.

*Pointer (last)*
9. **pointer** when you say "this" / "here" / "where my mouse is";
10. **look + click_at** only for things nothing else can see (canvases, games).

### Files

| Say | Tool | Notes |
|---|---|---|
| "what did I download today?", "show my recent files" | `find_files` (no query) | Recently changed items on Desktop, Documents and Downloads, newest first |
| "find the PDF about the lease" | `find_files query=lease kind=pdf` | Spotlight, by name and by content; `kind`: pdf, image, doc, sheet, slides, code, video, audio, folder, app |
| "what's in my Projects folder?" | `find_files folder=~/Projects` | |
| "read notes.md on my desktop and summarise it" | `read_file` | Text, code, Markdown, CSV, JSON, **PDF** (text layer), **Word / RTF / HTML** (via `textutil`). Long files come in parts (`start_line`) |
| "make a file called plan.md with…" | `write_file mode=create` | A bare name goes to `~/Documents/Mint` |
| "add cheese to that list" | `write_file mode=append` | |
| "change the deadline in plan.md to Friday" | `write_file mode=replace find=…` | The text to replace must occur exactly once |
| "rewrite the whole file" | `write_file mode=overwrite` | The previous version is copied to `~/Library/Application Support/Mint/backups/` first |
| "open it", "show it in Finder", "how big is it?" | `file_action open / reveal / info` | |
| "move it to Archive", "rename it to final.md", "make a folder called Receipts" | `file_action move / rename / copy / make_folder` | Never overwrites an existing file |
| "delete the old draft" | `file_action trash` | Only when you asked to delete; it goes to the Trash, so it can be put back |

Off limits, for reading and writing: `~/.ssh`, `~/.aws`, `~/.gnupg`, keychains,
cookies, Mail and Messages data, browser profiles, `.env` files, keys and
credential files. Writing is also refused in hidden folders and `~/Library`
(except iCloud Drive), and outside the home folder. Text that looks like a
password, API key or card number is never written.

### Web

| Say | Tool |
|---|---|
| "what's the latest version of Python?", "search the web for flights to Goa" | `web_search`: Exa or Tavily with a key, DuckDuckGo without; titles, links and snippets |
| "read that article", "what does python.org/downloads say?" | `read_url`: the page's readable text, without opening a browser |

### Browser

One `browser` tool gives full control of Chrome, Safari, Brave, Arc or Edge, including
logged-in pages that `read_url` cannot see.

**Which browser.** `open_url` and `browser new_tab` pick the browser themselves
(`mint/tools/browser_choice.py`), first match wins:

1. a browser you name in the request: "on this Chrome only", "in Brave", "open Chrome and…"
   ("brief" is understood as Brave when it is clearly the browser: "my brief browser");
2. your **browser rules**: rules you set ("always use Brave for entertainment"), and
   remembered preferences that name a browser. Your memory "Brave is the default for
   entertainment … YouTube, anime, music" is read as the rule *anime, entertainment, music,
   youtube → Brave*. Topics cover their sites: entertainment is YouTube, Netflix, Twitch,
   Crunchyroll, Hotstar, Spotify and any site with "anime" in its address;
3. the browser Gemini asked for;
4. the browser in front, then the one Mint last worked in, then the Mac's default.

If the rule says Brave and Gemini tries to `open_app` Chrome first, that call is stopped
("NOT RUN: the user's browser rule puts this in Brave"). This was why you used to get both
browsers. A tab already showing the page is reused, a fresh window's empty New Tab takes the
page, and a browser that is not running is started first.

| Say | What happens |
|---|---|
| "always open YouTube in Brave", "use Chrome for replit.com" | `rule` saves it (`browser_rules.json` in the runtime folder) |
| "which browser do you use for what?" | `rule` with nothing else lists the rules, including remembered ones |
| "stop using Brave for music" | `rule` with text=remove |

| Say | Action |
|---|---|
| "what tabs do I have open?" | `tabs`: every tab in every window, numbered, the showing one marked |
| "switch to my Gmail tab", "go to tab 3" | `switch` (by title, address or number) |
| "open github.com in a new tab" | `new_tab`, then waits for it to load |
| "go to news.ycombinator.com" | `go`: this tab |
| "go back", "forward", "reload" | `back`, `forward`, `reload` (each waits for the page) |
| "close this tab", "close the YouTube tab" | `close_tab`, only when you said close |
| "read this page", "what links are on it?" | `read`, `links` |
| "what page am I on?" | `url` |
| "is there a pricing section?" | `find` |
| "click Learn more" | `click` (the link or button with that visible text) |
| "put my name in the Full name field" | `fill` (text fields, text areas, rich text boxes) |
| "choose Blue in the colour list" | `select` (dropdowns) |
| "scroll to the bottom" | `scroll` (down, up, top, bottom) |
| "wait for it to load" | `wait` |
| "run document.title on the page" | `js` |
| "put YouTube and anime in a group called Entertainment" | `group`: a named tab group (Chrome, Brave, Edge) |
| "open the KeepWatching extension", "click the Extensions button" | `toolbar`: presses the browser's own button by name |

**Tab groups.** `group` target=name, text=the tabs ("YouTube, anime": titles or addresses;
empty = the current tab). It finds the browser that has those tabs. Tabs from other windows
are reopened in the first tab's window, and the originals closed (AppleScript's `move` lost
the page in testing). It then opens each tab's own menu through Accessibility: *Add tab to
new group*, or *Add tab to group > New group* when other groups exist. It pastes the name
and adds the rest with *Add tab to group > Entertainment*. The result is checked from the
tabs' labels ("Part of group Entertainment"). A full-screen window leaves full screen for
the few seconds this takes, then goes back. Chrome may also list the group under *Saved tab
groups* in the bookmarks bar; that is Chrome's own feature.

**Extensions and toolbar buttons.** `toolbar` target="KeepWatching" presses a pinned
extension's button through Accessibility, which opens its pop-up. For an extension that
is not pinned, it opens the Extensions (puzzle) menu and picks it there.

**How it works.** Tabs and navigation use each browser's AppleScript (no JavaScript
needed). Page actions use the page's JavaScript, sent through AppleScript, when the
browser allows it, and the page's accessibility tree when it does not; Safari's own
page text and source cover `read` and `links` there. Fields are filled by setting their
value through Accessibility, with no keystrokes, so nothing can land in another app.
Keys are only typed as a fallback, and only when the browser is really in front.

**Turning JavaScript on.** Page actions are fastest and most exact with it. Chrome and
Brave: in the menu bar at the top of the screen, **View > Developer > Allow JavaScript from
Apple Events** (Chrome: once per profile). In fullscreen the menu bar is hidden; move the
pointer to the top edge to show it. Safari: **Develop > Allow JavaScript from Apple
Events**. Mint never switches this on itself. Without it, everything except `js` still
works through Accessibility.

**Details that matter.** Fields match by their label, placeholder or name, even spelt a bit
differently ("color" finds "Favourite colour"). `fill` only fills text fields and `select`
only dropdowns; if a page has just one of the kind, that one is used. Back and forward use
the page's own history, because Chrome's back button skips pages opened by a script-made
click. A click that opens a new page waits for it to load before reporting its title.

**Safety.** Password and card fields are never filled. Clicking Send, Submit, Post, Buy,
Pay or Delete, and closing tabs, need you to have asked. Page JavaScript may not read
cookies or storage, send data out, open windows, or submit forms.

`tests/checks/harness.py` covers the choice rules: memory → rule, named browser, "brief",
the rule beating Gemini's guess, the stopped `open_app`, and set, list and remove.

### Menus

Every command an app has is in its menu bar, with its keyboard shortcut. `menu`
reads the menu bar through Accessibility (no clicking, no Automation permission).

| Say | What happens |
|---|---|
| "in Finder go to Applications" | `menu path="Go > Applications"`: chooses it and reports the shortcut (⇧⌘A) |
| "export this as PDF" | `menu path="Export as PDF"`: the top menu can be left out |
| "what's in the View menu?", "what's the shortcut for…?" | `menu action=list path=View`: items with shortcuts (⌘↑, ⌥⌘L…), ✓ marks, disabled items and submenus |
| "show the path bar" (already shown) | Mint notices the item reads "Hide Path Bar" and says it's already showing |

Quit, Close, Delete, Send, Empty Trash and the like are refused unless you asked.

### Scrolling to things

| Say | What happens |
|---|---|
| "scroll to Privacy & Security" (System Settings) | `scroll_to target=…`: finds it in the accessibility tree (or reads the screen), scrolls the right pane until it appears, then highlights it |
| "scroll the sidebar down" | `scroll_to where=sidebar direction=down`: scrolls that pane only (sidebar, left, right, main, a named list) |

Long lists that only build rows as they scroll, and SwiftUI panes that hide their
text from Accessibility, are handled by scrolling pane by pane and checking again
(accessibility, then on-screen text recognition) after each step. Verified in
System Settings: Privacy & Security after 2 scrolls, Wallet & Apple Pay, back up
to Wi-Fi after 5 scrolls; a made-up name failed cleanly after both panes.

### Waiting

`wait_for_text` waits seconds, up to 2 minutes, for text to appear in the front
window (a dialog, a page, "Export complete"), or to go away with `gone=true` (a
"Loading…" label). For an AI chat still writing its answer, or anything that takes
minutes, Mint uses `wait_until_done` instead (section 8).

### AppleScript

`run_applescript` covers scriptable apps where no tool fits: Finder, Music, Notes,
Reminders, Calendar, Mail drafts, Safari/Chrome, System Events. Examples: "how much free
space do I have?" (Finder), "which apps are open?" (System Events), "how many tabs are
in my Chrome window?", "how many tracks are in my Focus playlist?". Multi-line scripts
work; errors come back as text. Refused: `do shell script` (even split up), running or
loading other scripts, Objective-C (`use framework`, `current application's`), anything
touching passwords or the keychain, and sending, deleting, closing or quitting unless
you asked. macOS asks once per app before Mint may control it.

### Screenshots

`screenshot` saves a picture where macOS saves screenshots (the Desktop unless you changed
it) and also puts it on the clipboard, ready to paste. Mint's own orb and chat never appear
in it.

| Say | What happens |
|---|---|
| "take a screenshot" | the whole display the front window is on |
| "screenshot this window", "screenshot Chrome" | that window only (works even if it is covered), no shadow |
| "take a proper screenshot of this image", "screenshot the chart" | `what=element`: the picture found in the window's accessibility tree (with no name, the biggest one), cropped exactly to it |
| "screenshot 1280 by 720", "make it 800 wide" | `size`: exactly that many pixels (cropped evenly if the shape differs), or that width keeping the shape |
| "take an 800x600 screenshot" | `what=region` with a size: a box that size in the middle of the front window |
| "screenshot the area at 100,200, 640 by 480" | `what=region target="100,200,640,480"` (screen points) |
| "let me pick the area" | `what=select`: you drag a box, as with ⌘⇧4 |
| "don't save it, just copy it" | `save=false` |

### Clipboard

`clipboard` manages what you copy and paste.

| Say | Action |
|---|---|
| "what's on my clipboard?" | `get`: text, an image (with its size), or files |
| "copy this text: …" | `copy` |
| "copy the path of this", "copy this link" | `copy_path`: the file selected in Finder, else the address of the page in the browser, else the file open in the front window; or the path you name |
| "copy this file" (to paste into Finder, Mail or a chat) | `copy_file` |
| "copy this picture" | `copy_image` (an image file, as a picture) |
| "copy what I selected" | `copy_selection` (⌘C in the front app, then checks it worked) |
| "paste it", "paste 'thanks!' here" | `paste` (optionally copying the text first), into the front app |
| "what did I copy before?", "bring back the second one" | `history`, `restore` |
| "clear the clipboard" | `clear` |

The history keeps the last 30 copies in memory only (nothing is written to disk), and only
the last few images. Anything a password manager marks as hidden, and text that looks like a
password, key or card number, is never kept, and Mint never pastes into a password field.

### Guards on every tool call

- **Password fields:** typing into a secure text field is refused; you type passwords.
- **Loops:** the third identical call with the identical result, or four failures
  in a row, adds a `[Loop warning]` to the result telling Mint to change approach
  (menu, browser, scroll_to, look) or tell you what is in the way. Scrolling,
  key presses and waiting are exempt.

## 7. Everyday Mac tasks

| Say | Tool |
|---|---|
| "set a ten minute timer for tea" | `set_timer` (says so out loud when done) |
| "what's on today?", "what's on this week?" | `calendar_events` |
| "remind me to call Sam at five" | `create_reminder` |
| "make a note: …" | `create_note` |
| "draft an email to priya@… saying …" | `compose_email` (draft only; never sends) |
| "what are my latest emails?", "read the second one" | `list_emails`, `read_email` |
| "volume 30", "pause the music", "next track" | `set_volume`, `media_key` |
| "lock the screen", "dark mode on", "mute" | `system_action` |
| "what's the battery?", "what time is it?" | `get_status` |
| "make a PDF of this summary" | `create_pdf` (to `~/Documents/Mint`) |
| "export this Google Doc as PDF" | `export_doc_pdf` |
| "start work" (a routine in `custom.json`) | `run_routine` |
| "run `ls` in my projects folder" | `run_shell`, only when started with `MINT_ALLOW_SHELL=1` |

### Meeting notes (no bot joins your call)

Click **Record meeting** in the menu bar, or say "take notes of this meeting". Mint records silently
until you click **Stop recording**, or say "stop recording" once the call is over.

- **Works with:** Google Meet in any browser, Zoom, Teams, Slack huddles, FaceTime, Webex.
- **Two tracks:** the call's audio (a Core Audio tap: macOS's "System Audio Recording Only" permission,
  no screen recording, no virtual driver) and your microphone. The mic is "You"; the call is everyone
  else, with names filled in when the conversation makes them clear.
- **At the end:** Gemini writes a timestamped transcript and the notes: summary, decisions, action
  items (who, by when), open questions and key quotes. Times are aligned on the Mac, not guessed.
- **Where:** `Meetings/<date time> <title>/` in the storage folder, with `transcript.md`, `notes.md` and
  the recording (compressed to .m4a; Settings ▸ Storage & Privacy can drop it). The title comes
  from the calendar event, the Meet/Teams/Zoom tab, or the call app.
- **When a call starts:** Mint offers to take notes (a notification and the menu), because it has lent
  the microphone to the call. Turn the offer off in Settings.
- **Afterwards:** "what did we decide in the Acme call?", "open yesterday's meeting notes", "list my
  meetings" → `meeting action=open/list`.

### Teach by showing, and the tutor

| Say | What happens |
|---|---|
| "watch me do this once", "let me show you how" | `teach start`: Mint watches your clicks, typing and app switches (say what you're doing as you go) |
| "done", "that's how" | `teach stop`: the recording becomes a skill with general steps and `<parameters>`, saved to Skills |
| "show me how to export a PDF in Preview", "walk me through adding a filter in Sheets" | `tutor start`: Mint points at each control and waits for you to do it |
| "next", "go back", "stop the lesson" | `tutor next / back / stop` |

- **Teach** (`mint/knowledge/teach.py`).
  - How it listens: a listen-only event tap (the Input Monitoring permission) records each click
    with the name of the control under it (Accessibility), typing per field, shortcuts, menu choices,
    app switches and pages visited, plus a few small screenshots kept in memory.
  - Privacy: nothing typed into a password field (or while macOS secure input is on) is recorded,
    and anything that looks like a key or card number is blanked.
  - How the skill is written: Gemini keeps the steps that matter and names controls the way Mint's
    tools target them. Things that change each time become parameters, and steps that send or delete
    get "confirm first".
  - Limits: it stops after 10 minutes.
- **Tutor** (`mint/ui/tutor.py`).
  - The plan: a saved skill for that app, else Gemini with the app's real menus and visible
    controls.
  - Pointing: each step is shown with an arrow or box and a short instruction (menus through
    Accessibility, then on-screen text, then vision), and Mint says it.
  - Moving on: Mint goes to the next step by itself when you click the control or the expected change
    happens (a menu opens, a sheet appears), or when you say "next". Mint never clicks for you.

### Power tools for everyday work

| Say | What happens |
|---|---|
| "make this more formal", "fix the grammar", "turn this into bullets", "translate this to Hindi" | `edit_selection` rewrites the selected text in any app and replaces it in place; "undo that" or ⌘Z brings it back |
| "put all the invoices in Downloads into a spreadsheet" | `make_spreadsheet` reads every file, including scans and photos of receipts, and saves an .xlsx with one row per document |
| "clean up my Downloads", "sort my Desktop by project" | `tidy` plans first (nothing moves), asks, then moves; "undo that" puts everything back |
| "volume 30", "next song", "pause", "mute", "lock the screen" | runs the moment you stop talking (`mint/app/instant.py`) |
| "save everything in my Dropbox from now on" | `set_preference storage_folder` |

- **Editing selected text** (`mint/tools/rewrite.py`).
  - Gemini Flash Lite is told which app and window the text is in, so an email stays an email and a
    chat message stays short.
  - Names, numbers and links are kept exactly.
  - A selection that looks like a password, key or card number is refused.
- **Spreadsheets** (`mint/tools/sheets.py`).
  - Columns: when you don't name them, they are chosen from the first files (invoices get Vendor,
    Invoice Number, Date, Due Date, Total, Currency).
  - Values: numbers stay numbers and dates stay dates, and a source-file column is added.
  - Big folders: more than six files are processed in the background, and a message says when the
    sheet is ready.
- **Tidying** (`mint/tools/tidy.py`).
  - Groups: by kind (Documents, Images, Screenshots, Installers, Archives…) or by topic when it's
    clear (Invoices & Receipts, Statements, Tickets & Travel), or however you ask.
  - Renames: only names that say nothing ("document (3).pdf") are changed, from the file's own text.
  - Duplicates: exact duplicates go to a Duplicates folder. Nothing is ever deleted and nothing is
    overwritten.
  - Undo: every move is journaled in `~/Library/Application Support/Mint/tidy/`.
- **Instant commands.**
  - How they work: the live transcript is checked against a short, fixed list of whole sentences.
    If one matches and nothing more is said for 0.6 s, it runs locally.
  - No doubles: when the model's own call for the same tool arrives, it is answered "already done".
  - Not covered: longer requests ("pause the video in Chrome", "volume 30 and open Slack") go the
    usual way.
  - Off switch: "turn off instant commands".
- **Mint storage** (Settings ▸ Storage & Privacy, or by voice).
  - Where: one folder, `~/Documents/Mint` unless you choose another.
  - Sub-folders: Documents, Meetings, Videos, Agents and Spreadsheets.
  - Existing files: files saved before a change stay where they are.

### Watching videos

`watch_video` understands a video in seconds without playing it. It works on a YouTube, X, Vimeo, Loom,
TikTok or Instagram link, a video or audio file, or "this video" (the video page in front, else the
video selected in Finder, else the newest screen recording on the Desktop or in Downloads or Movies).

| Say | What happens |
|---|---|
| "watch this and tell me what it's about" | a digest: what it is, 4-8 key moments with timestamps, the look and feel, and saves the transcript to `~/Documents/Mint/videos/` |
| "what did he say about pricing?" | answered from the saved transcript and keyframes, with timestamps (~5 s) |
| "show me the frame at 3:20" | that exact frame is sent to Mint to look at |
| "what's the aesthetic of this reel?" | the frames plus measured cuts per minute, brightness and main colours |
| "transcribe this recording" | the timestamped transcript file |

How it stays fast and cheap (`mint/tools/video.py`):

- **Speech.** The site's own captions when there are any (YouTube: instant, no tokens). Otherwise
  Gemini transcribes the audio in parallel two-minute pieces. The pieces start at exact offsets, so
  timestamps stay within seconds; one 5-minute piece drifted by 2 minutes in testing. When
  `parakeet-mlx` is installed, the audio is transcribed on the Mac instead.
- **Picture.** About 12 keyframes, chosen where the picture changes (scene cuts), laid out with their
  times on one contact sheet.
- **Cost.** A 15-minute talk comes to about 6k tokens, against about 180k for sending the video
  itself. In testing it took 13 s with captions and 50 s without.
- **Tools.** Frames come from AVFoundation and audio from `afconvert`, both part of macOS, so no
  ffmpeg is needed. `yt-dlp` fetches web videos. If a YouTube video can't be fetched, Gemini watches
  it from its address at low resolution.
- **Cache.** Everything is cached in `~/Library/Application Support/Mint/videos/`.
- **Sub-agents.** Sub-agents with web access have `watch_video` too, so Astra can use a talk or a
  demo as a source.

## 8. Long tasks across apps

- **Plans.** For three or more steps Mint calls `plan_task`. The orb shows a progress ring and
  "Step 2/5", each tool result reminds Mint what is next (`step_done`), and confetti plays at the
  end.
- **Tasks that last** (`mint/app/tasks.py`). Plans are saved in `tasks.json`, so they survive restarts,
  unloading and "stop":
  - **Checked steps.** `step_done` records what Mint checked (what the window, file or page showed),
    not just "done". A step that didn't happen is marked failed and retried another way.
  - **Sub-steps.** When a step turns out bigger than planned, it is split with
    `task action=add_steps under=3`, which gives 3.1, 3.2 and so on. A step whose sub-steps are all
    finished finishes by itself.
  - **Replanning.** `task action=replan` replaces the steps not done yet.
  - **Notes.** `task action=note` keeps facts later steps need, such as a path, a name or a price.
  - **Stop and continue.** "Stop" pauses the task instead of dropping it. Unfinished tasks are
    listed in every new session, and "where were we?" or "continue" resumes one: Mint gets the
    outline, what each finished step found, and the notes.
- **Automations** (`mint/tools/automations.py`, the `automation` tool). Things Mint does by itself:

  | Say | Trigger → action |
  |---|---|
  | "every weekday at 9, brief me on my calendar and unread mail" | daily at 09:00 on weekdays → Mint |
  | "every Friday at 5, have Astra write a brief on this week's AI news" | daily, Fri → a sub-agent |
  | "remind me to stretch every hour between 10 and 6" | every 60 min, 10:00-18:00 → a notification |
  | "when a PDF lands in Downloads, file it in Documents/Invoices" | a new `*.pdf` in the folder → Mint |
  | "10 minutes before any meeting, open its notes doc" | before calendar events → Mint |
  | "when I open Figma, turn on Do Not Disturb" | an app opens → Mint |
  | "tomorrow at 7, read me the news" | once → Mint |

  - **Managing them.** "What automations do I have?" lists them. They can also be paused, resumed,
    deleted or run now, by name.
  - **Safety.** An automation runs on its own, so it never sends, posts, buys or deletes. It prepares
    the draft or the list and tells you it's ready.
  - **Mint unloaded.** If Mint has unloaded to save memory, it writes the next due time to
    `automations-next` and Mint Ear starts it then.
  - **Missed runs.** A daily run missed while the Mac was asleep still runs within 3 hours;
    otherwise it is skipped and noted.
  - **Watchers.** While a folder or app watcher is on, Mint stays loaded.
- **All of it in one go (autopilot).** Ask for several things ("do all your tricks, then smile, then
  tell me the time", "show me all your emotions one by one", "open X, Y and Z") and Mint does them
  back to back, with no "next" from you. It is told to report once at the end. `mint/app/autopilot.py`
  backs this up: when a turn ends with the request half done, the session sends Mint a short note to
  carry on. Half done means one of these:
  - plan steps are left;
  - the request says *all / each / one by one* and Mint didn't report the work as finished;
  - fewer actions were taken than the things you listed;
  - Mint said "next I'll…" or asked "shall I continue?".

  The note waits for 2 s of quiet from Mint. It is never sent in these cases:
  - while something is still running (`wait_until_done`, a sub-agent);
  - right after a message was sent;
  - while a sub-agent's question waits for you;
  - after STOP;
  - after the last plan step;
  - if the previous note produced no action.

  At most 8 notes per request; "next" / "continue" from you keeps the same request going. The
  "[Loop warning]" for many calls in a row now applies only to searching tools (find_files, look,
  ui_act…), not to doing a list of things.
- **Waiting for apps.** `wait_until_done` watches a window until the text stops
  changing and no Stop button is left (ChatGPT answering, a page loading). Long
  waits return at once and Mint is told when the app is done.
- **Showing what was built.** `preview_site` serves a folder on
  `http://localhost:8000` (127.0.0.1 only), opens it, and checks status, title and
  missing files.
- **Sending safely.** Typing with `press_return` in a chat clicks its Send button
  and checks the message went; the same message is never sent twice within two
  minutes.

Example: *"Open ChatGPT, research honeybees with sources, give it to Codex to
build a one-page site, open it on localhost and check it."*

## 9. Skills: what Mint has learned

A skill is a short Markdown how-to in `skills/<category>/<name>.md` (in the app:
`~/Library/Application Support/Mint/skills`).

| Say | What happens |
|---|---|
| (nothing, it is automatic) | For each request Jev picks the one skill that fits from the real list, or none, in about a second; it is attached to the first action's result |
| "save how to do that as a skill" | `create_skill` (Jev files it into a category folder) |
| "next time use the sidebar", "update the ChatGPT skill: …" | `update_skill` |
| "what skills do you have?" | `list_skills` |
| "delete that skill" | `delete_skill` (moved to `skills/_archive`) |

**Mint learns by itself.** A task that took six or more steps, recovered from a
failure, or where you corrected it is written up as a skill (or merges into the
existing one) by a Flash model: only the steps that worked, generalised, with your
corrections as notes. Skills are ranked by results: new → learning → reliable (or
shaky). Shipped seeds cover ChatGPT (projects, chats, search), ZCode, Claude Code,
Codex, Slack channels, Google Docs, and what to do when a click does nothing.

Skills for one app are only used when the request is about that app: a general
web question does not pull in the "research in ChatGPT" skill (bench case).

## 10. Memory

One fact per block, in `memory/bank.json` (readable copy `memory/MEMORY.md`),
grouped: core, people, work, accounts, schedule, preferences, places, vocabulary,
misc.

| Say | Tool |
|---|---|
| "remember my manager is Meera" | `remember` (Jev files it and replaces a contradicting fact in one ~1 s call) |
| "make that a fixed memory" | `remember fixed=true`: always in the prompt |
| "my manager is now Rahul", "update my standup time" | `update_memory` |
| "who's my boss?" | `recall`: Jev checks every block in one request and returns only the relevant ones |
| "forget my old address" | `forget` |
| "what do you remember about me?" | `list_memories` |
| "what was my manager before?" | `recall`: a changed fact keeps what it used to say, and until when |
| "why did you say that?", "which memory did you use?" | `memory_used`: the facts looked up in this conversation, and the fixed ones; fix or forget a wrong one |
| "what did we do yesterday?", "when did I ask about flights?" | `recall_history` (the journal) |
| "what was that site you found last week?", "what did Astra find on Monday?" | `recall_history` |
| "remember what I work on" | `set_preference activity_timeline on` (the timeline, below) |
| "what was I working on yesterday afternoon?", "how long was I in Slack today?" | `recall_history` with the timeline |

Only fixed memories and a group index go into the prompt; everything else is recalled on demand,
which keeps sessions small. Facts are also extracted automatically from conversation summaries.
Passwords, keys and card numbers are refused.

- **Without a TypeSafe key.** Filing a fact, spotting the older fact it replaces, and recall ("who's
  my boss?" finds "my manager is Rahul") all use one Gemini Flash Lite request each. With a key,
  Jev does them.
- **Dates.** A fact that says "tomorrow" or "this Friday" is saved with the day it was said.
- **Tidying, once a day.**
  - A background pass merges duplicate facts, drops ones whose date has passed ("dinner with Sam
    tomorrow", saved last week) and files `misc` facts into their group.
  - Every change goes to `memory/changes.jsonl` with the old text. Fixed memories are never touched.
- **The journal** (`mint/knowledge/journal.py`). `recall_history` answers questions about the past, with the
  day and time, from what Mint already keeps:
  - every turn of every conversation (`history.jsonl`);
  - the tasks, the automations' runs and the videos watched;
  - the activity timeline, when it's on.

  It narrows these to the days asked about ("yesterday", "last week", "on Monday", "20 sep", "3 days
  ago"). Longer spans are narrowed further to the entries that share the question's rarest words.
- **The activity timeline** (`mint/knowledge/timeline.py`, off until you turn it on).
  - What it records: while Mint runs, the app in front, its window title and the page address in a
    browser. Text only: never screenshots or typing.
  - What it skips: password managers and private windows, and nothing is recorded while the screen
    is locked or the Mac is idle.
  - Safari: Safari can't tell its private windows apart, so its pages are never recorded, only that
    Safari was in front.
  - Where it's kept: `timeline.jsonl`, on this Mac only, for 14 days. "Delete my timeline" erases it.

**Chat housekeeping:** "summarise and compact the session" (a Flash summary, then
a fresh session from it), "clear the chat", "start a new session".

## 11. Sub-agents

Named background agents with their own colour, model and tools (`agents.json`):
**Astra** (research, sourced briefs), **Luna** (code, RL environments), **Sage**
(long documents), **Codex** (builds sites and apps with OpenAI Codex).

| Say | Tool |
|---|---|
| "have Astra research X and write a brief" | `delegate_task` |
| "Luna and Astra, at the same time: …" | `delegate_tasks` |
| "how's Luna doing?" | `agent_status` |
| "tell Luna to use pytest" | `message_agent` |
| (an agent asks a question; you answer) | `answer_agent` |
| "stop Astra" | `stop_agent` |
| "create an agent called Nova that writes tweets, make it pink" | `create_agent` |

Each agent is a small orb that pops out of Mint's, works beside it, and flies back
when done.

## 12. The orb, animations and effects

- **The orb** has a face: eyes follow the pointer (even while you type), look up
  while thinking, look at where Mint is working, squint happily when something
  works, wink, and sleep. A Siri-like swirl turns faster while thinking or
  speaking.
- **Word-by-word captions** of both sides rise into place as they are spoken.
- **The orb becomes the task.** While Mint works, its face melts away and the orb
  turns into what it is doing: the app's own icon when opening an app, a folder for
  files, a magnifier that looks around for a search, a globe for the web, a tapping
  hand for clicks, a text cursor that hops while typing, a scan line for reading, a
  spinning clock for waits, braces for scripts, a menu glyph for menu commands. Its
  shape morphs too: a soft rounded tile for apps, files and menus, round for the
  rest, with a little jelly squish on each change. At the end the glyph becomes a
  green ✓ (with a hop) or a red ✗ (with a shake), then the face comes back and the
  orb is round again.
- **On the screen, only two small things**, both click-through and invisible to
  screenshots: a small soft ripple where a click lands (the orb's eyes glance
  there), and a thin outline round the box being typed into. Nothing else pops up.
- **Long tasks**: a progress ring, a bar in the chat, confetti at the end.
- **Expressions**: "smile", "dance", "clap", "show me a heart", "cry", "wave", "put
  your sunglasses on". Poke the orb and it giggles, then gets dizzy, then cross.

`python -m mint --demo` plays the whole tour (`MINT_CAPTURE=1
MINT_DEMO_SHOTS=/tmp/shots` saves a screenshot per step).

The orb, face, expressions and effects are drawn in code with Core Animation;
icons are Apple SF Symbols and the apps' own icons. No animation library is used.

## 13. Customising

From the chat's settings button, the orb's right-click menu, the menu bar item or
**Settings… (⌘,)**: theme (Mint blue, Aurora, Sunset, Mint, Rose, Mono), position,
face, captions, effects, microphone, spoken replies, voice, speaking style,
assistant name (a new name trains its own "Hey <name>" wake word), your name and
"about you".

Files: `settings.json` (applies within two seconds of an edit; shortcut
`"toggle": "cmd+j"`, `stop_words`, `listen_while_working`, `vocabulary`) and
`custom.json` (accounts, Slack workspaces and channels, aliases, routines, standing
instructions).

### Quitting Mint (fully off)

Mint has no Dock icon, so there are four ways to quit it, and each one stops **everything**:
the assistant, sub-agents and Codex runs (their whole process groups), preview servers, and
the hidden screen-control engine. The conversation is still saved to memory on the way out,
and if anything hangs Mint exits anyway after 15 seconds.

- **Settings** (menu bar icon ▸ Settings…, or ⌘, ) ▸ **Quit Mint** at the bottom of every tab
  (⌘Q while Settings is open). Next to it: **Start Mint when I log in**.
- The menu bar icon's menu, the orb's right-click menu, or the chat's settings button ▸ **Quit Mint**.
- Say or type **"quit Mint"**, "turn yourself off", "close Mint", or just "quit". Mint says goodbye and
  stops four seconds later. "Go to sleep" / "stop listening" only put it to sleep, and "quit
  Spotify" quits Spotify, never Mint.
- To start it again: open `~/Applications/Mint.app` (Spotlight: "Mint").

## 14. Voice: wake word, voice lock, talking over Mint

- **"Hey Mint"** is Mint's own on-device wake word model. Nothing leaves the Mac
  while it is asleep.
- **Train my voice** (menu bar): about two minutes. Mint then only wakes for your
  "Hey Mint", and only your speech reaches Gemini.
- **Talking to someone else?** Jev classifies follow-ups as said to Mint or to
  someone in the room; the second kind is ignored.
- **English and Hindi** (Hinglish included); names from `custom.json` are given to
  the transcriber as vocabulary, and corrections ("no, I said on-call") are
  remembered.
- **Calls:** when Zoom, Meet, Teams, FaceTime and the like use the mic, Mint steps
  aside and comes back three seconds after.

## 15. Safety rules built in

- Never enters passwords or card numbers, never fills password fields, never
  writes secrets into files, skills or memories.
- Never sends an email (drafts only). A message, post or form is sent or submitted
  only when you asked, and never twice within two minutes.
- Never quits or closes your apps unless you asked; never deletes files (Trash
  only, and only when asked); never overwrites a file without a backup.
- Private folders (keys, keychains, browser profiles, Mail, Messages) are off
  limits; writing stays in normal folders.
- AppleScript cannot run shell commands; shell access is off unless
  `MINT_ALLOW_SHELL=1`.
- Browser JavaScript cannot read cookies or storage, send data out, or submit forms.
- "Stop" halts everything at once, including queued clicks and keys.

## 16. Configuration and files

| Where | What |
|---|---|
| `.env` | keys; `MINT_MODEL`, `MINT_FALLBACK_MODEL`, `MINT_VOICE`, `MINT_WAKE_WORD`, `MINT_SLEEP_AFTER`, `MINT_ALLOW_SHELL`, `MINT_ALLOW_SEARCH` (Gemini's own Google Search grounding; needs billing) |
| `settings.json` | appearance, microphone, voice, shortcuts, stop words |
| `custom.json` | accounts, workspaces, aliases, routines, instructions |
| `agents.json` | sub-agents |
| `skills/` | skills, by category; `_archive/` for removed ones |
| `memory/bank.json`, `memory/MEMORY.md` | memories |
| `history.jsonl`, `summary.md` | conversation log and running summary |
| `tasks.json` | plans and their steps, open and recently finished |
| `automations.json`, `automations-next` | automations; when the next one is due (read by Mint Ear) |
| `timeline.jsonl` | the activity timeline, when it is on (14 days) |
| `Meetings/`, `Spreadsheets/`, `Documents/`, `Videos/`, `Agents/` in the storage folder | what Mint makes (Settings ▸ Storage & Privacy; default `~/Documents/Mint`) |
| `~/Library/Application Support/Mint/tidy/` | journals of folder tidy-ups, for undo |
| `memory/changes.jsonl` | what the daily memory tidy changed, with the old text |
| `~/Documents/Mint/videos/` | notes and transcripts of videos Mint watched |
| `~/Library/Application Support/Mint/videos/` | the video cache: keyframes, transcripts, contact sheets |
| `~/Documents/Mint/` | files Mint makes (PDFs, notes, agent work) |
| `~/Library/Application Support/Mint/backups/` | previous versions of files Mint overwrote or edited |
| `~/Library/Logs/Mint/mint.log` | the log (every tool call and result) |

The installed app runs from `~/Library/Application Support/Mint`; re-run
`./install.sh` after changing code.

## 17. Testing and troubleshooting

```sh
make test            # offline checks
.venv/bin/python -m mint --demo   # the animation tour
```

Run any tool directly, with the app's permissions, and read the result in the log:

```sh
open -g -n -W ~/Applications/Mint.app --args --tool menu '{"action":"list","path":"View"}'
open -g -n -W ~/Applications/Mint.app --args --script steps.json   # [["tool", {args}], ["sleep", 1], …]
```

Diagnostics not offered to the model: `harness_probe {"target": "…"}` (how a
target is found in the front window), `harness_probe_attrs {"role": "AXRow"}`,
`harness_probe_ocr {}`, `ground_bench`.

| Problem | Fix |
|---|---|
| "Mint lacks Accessibility permission" | Menu bar ▸ Grant Accessibility…, switch Mint on |
| "has not allowed Mint to control that app" | System Settings ▸ Privacy & Security ▸ Automation ▸ Mint |
| browser: "does not allow JavaScript from Apple Events" | Fine: reading and clicking still work through Accessibility. For `js`, turn it on in Chrome's View ▸ Developer menu |
| "macOS did not let Mint read it" | System Settings ▸ Privacy & Security ▸ Files and Folders ▸ Mint |
| It hears the room | Train your voice, or turn the mic off and type (⌘J) |
| It hangs | `kill -USR1 <pid>` writes every thread's stack to the log |

## 18. Tool reference

Generated from the live declarations (`tools.tools()`); the first sentence of
each description.

| Tool | Arguments | What it does |
|---|---|---|
| `open_app` | name | Instantly launch or switch to a Mac application by name. |
| `open_url` | url, browser | Instantly open a web address in a new browser tab (reuses a tab already showing it). |
| `open_folder` | name | Instantly open a folder in Finder. |
| `scroll` | direction, amount | Instantly scroll the window under the pointer. |
| `press_key` | key, modifiers, times | Instantly press a key, optionally with modifiers. |
| `set_volume` | level | Instantly set the Mac's output volume. |
| `media_key` | action | Instantly send a media key: play/pause, next or previous track. |
| `frontmost_app` |  | Which application is in front right now. |
| `list_windows` |  | List the visible windows as app name and title. |
| `quit_app` | name | Ask an application to quit. |
| `type_text` | text, press_return, field | Instantly insert text at the cursor in whatever field is focused, in any app. |
| `get_selected_text` |  | Read the text the user has selected in the front app, to answer about it ('summarise this', 'what does this mean'). |
| `get_status` |  | Current local time and date, battery level, and volume. |
| `set_timer` | minutes, label | Start a timer. |
| `notify` | title, message | Show a macOS notification. |
| `system_action` | action | Lock the screen, sleep the display, switch dark mode, or mute. |
| `calendar_events` | days | The user's calendar events from today onwards. |
| `create_reminder` | title, in_minutes, when | Create a reminder in the Reminders app, optionally due at a time. |
| `create_note` | title, body | Create a note in the Notes app. |
| `compose_email` | to, subject, body | Open a pre-filled email draft in the user's mail app for them to review and send. |
| `list_emails` | count | List the newest messages in the macOS MAIL APP's inbox. |
| `read_email` | number | Read one message from the macOS Mail app's inbox in full (see list_emails). |
| `open_chrome` | account, url | Open Google Chrome as one particular account (Chrome profile), optionally at a URL. |
| `open_slack` | workspace, channel | Open Slack, optionally switch to a workspace, and optionally jump to a channel, DM or person by name. |
| `list_accounts` |  | List the Chrome profiles and Slack workspaces available. |
| `read_window` | max_chars | Read ALL the text in the front window - a web page, a Gmail inbox or open email, a document, an app - exactly, including parts scrolled out of view. |
| `list_open` |  | What is open right now: the app in front, and every Chrome window with its profile and tabs (* = the selected tab). |
| `switch_to` | what | Bring an already-open Chrome tab or window to the front, by words from its title, e.g. |
| `export_doc_pdf` | open_after | Export the open Google Doc itself as a PDF, exactly as Docs renders it, and open it. |
| `plan_task` | goal, steps | Start a multi-step task: state the goal and the ordered steps BEFORE doing any of them. |
| `step_done` | step, result, failed | Mark a step of the current task finished (or failed), with a one-line result saying what you checked (what the window, file or page showed). |
| `create_pdf` | title, content, open_after | Write a nicely formatted PDF and open it. |
| `run_routine` | name | Run one of the user's saved routines by name. |
| `desktop` | goal | Drive the screen to do something that the instant tools cannot: click a specific button or link, type into a particular field, choose a menu item, pick a search result. |
| `set_preference` | setting, value | Change how you (Mint) behave or look, when the user asks: 'don't speak, just chat' -> spoken_replies off; 'talk to me again' / 'speak' -> spoken_replies on; 'turn off the mic' -> microphone off (then they can only type); 'make it purple' -> theme; 'move to the bottom left' -> position; also the orb's face, the on-screen effects, word-by-word captions, whether you listen while you work, and the activity timeline ('remember what I work on' -> activity_timeline on; 'delete my timeline' -> activity_timeline clear), where Mint saves what it makes ('save everything in my Dropbox' -> storage_folder = that folder's path, e.g. |
| `chat_action` | action | Manage the chat window's conversation when the user asks: 'clear the chat' -> clear (window only; memory is kept); 'summarise / compact our conversation' -> summarize (a Flash model summarises it, a summary card appears, and you are restarted with just that summary as context - like compacting); 'start a new session / start fresh / new conversation' -> new_session (saves this conversation to memory, then restarts you with a clean context). |
| `show_chat` | open | Open or close the chat window (conversation history, typing box, settings). |
| `stop_listening` |  | Go back to sleep and wait for the wake word. |
| `look` | display | Capture the screen and look at it. |
| `ui_act` | action, target, text, press_return, app | THE way to click, type into or pick anything in an app's window. |
| `ui_elements` |  | List the controls in the front window right now (numbered, with role, label and where they are; says if a dialog is open). |
| `click_text` | text, double | Click a piece of text you can name on screen - a sidebar item, tab, button or link label - in apps the desktop tool cannot see into (Slack, some Electron apps). |
| `click_at` | x, y, button, double | Click a point you can see in the latest look screenshot. |
| `find_skill` | task, app | Find the saved skill (a learned how-to) for a task. |
| `skill_result` | name, worked, note | Report how following a skill went, so good skills rise and bad ones get fixed. |
| `create_skill` | title, when, steps, notes, category, apps | Save a new skill: how to do a kind of task, as steps that worked. |
| `update_skill` | name, steps, note, when | Change a saved skill: replace its steps, add a note (e.g. |
| `list_skills` | category | List saved skills by category, ranked by how well they work. |
| `delete_skill` | name | Remove a saved skill (a copy is archived). |
| `remember` | fact, group, fixed | Save a fact about the user for future conversations: people, accounts by name, work, schedule, preferences, places, or a word you misheard. |
| `recall` | question | Look up what you remember that is relevant to a question. |
| `update_memory` | what, new_fact, fixed, group | Change a saved fact (the user says it changed or was wrong), or make it fixed / not fixed. |
| `forget` | what | Forget a saved fact the user no longer wants kept. |
| `list_memories` | group | List what is remembered, optionally one group. |
| `memory_used` | minutes | Which remembered facts you were given in this conversation (and the fixed ones) - for 'why did you say that?', 'which memory did you use?', 'how do you know that?'. |
| `show_skills_and_memory` | tab | Open the Skills & Memory window on screen, where the user can see and edit every skill and every remembered fact. |
| `show_on_screen` | text, style, note, scroll, app | Show the user where something is, in whatever app is in front (a PDF, a web page, a chat, a document, code): it finds the words on screen - scrolling the window to them if needed - and draws a box, underline, highlight, circle or arrow around them, with an optional short note; your orb flies over beside it. |
| `mark_area` | x, y, w, h, style, note | Mark a region with no text (an image, a chart, an icon) using 0-1000 coordinates of your latest look screenshot: x, y of its top-left corner, and its width and height. |
| `clear_marks` |  | Remove the marks you drew on screen. |
| `move_orb` | direction, amount, tricks | Move your orb on screen when the user asks: 'go a little down', 'move up', 'move left a lot', 'you are covering that' (direction away), 'go to the middle'. |
| `screen_share_visibility` | visible | Show or hide Mint in screen sharing, recordings and screenshots. |
| `express` | emotion, requested | Play an expression on your orb body. |
| `set_voice` | voice, style | Change your speaking voice and/or style when the user asks ('use a deeper voice', 'sound more cheerful', 'talk slower', 'use Puck'). |
| `list_agents` |  | List your sub-agents: name, what each is for, its model, and what each is doing now. |
| `delegate_task` | agent, task, why, thinking, context, folder, helpers, attach_window | Hand a substantial task to a sub-agent, which works in the background while you keep talking. |
| `delegate_tasks` | tasks | Start several sub-agent tasks at once (in parallel), e.g. |
| `agent_status` | agent | What your sub-agents are doing, or have finished, right now. |
| `message_agent` | agent, message, thinking | Send new or changed instructions to a running sub-agent - when the user changes what they want mid-task ('tell Luna to use pytest', 'make it shorter'). |
| `answer_agent` | agent, answer | Pass the user's answer to a sub-agent that asked a question. |
| `stop_agent` | agent | Stop a running sub-agent, or 'all'. |
| `create_agent` | name, role, instructions, thinking, web, color | Create (or update) a named sub-agent when the user asks for one: its name, what it is for, how it should work, and optionally which model. |
| `wait_until_done` | app, until_text, timeout_minutes | Wait until an app has FINISHED what it is doing before you use the result: ChatGPT (or any AI chat) still writing its answer, a page loading, something generating. |
| `preview_site` | path | Show a web page or site that was built on this Mac (e.g. |
| `edit_selection` | instruction, replace, action | Rewrite the text the user has selected, in any app, and replace it in place: more formal, friendlier, shorter, fix grammar, bullet points, translate, expand, as a reply... |
| `make_spreadsheet` | source, pattern, what, columns, name | Read many documents (PDFs, Word, text, scans, photos of receipts) and put the same fields from each into one .xlsx spreadsheet, one row per document (or per item in a statement), then open it. |
| `tidy` | action, folder, how | Organise a folder's loose files into sub-folders (by kind, topic, project or month) with a preview: plan (nothing moves), apply (after the user agrees), undo (put the last tidy-up back). |
| `teach` | action, goal, title, narration | Learn a task by watching the user do it once, then save it as a skill Mint can follow later. |
| `tutor` | action, task, app | Teach the user a task on screen instead of doing it: Mint plans the steps, points at each control with an arrow and a note, waits until the user has done it, then shows the next. |
| `meeting` | action, title, which, show | Record a meeting/call on this Mac without a bot (the user's microphone and the call audio as two tracks), then a transcript and notes (summary, decisions, action items, quotes) saved in Mint's Meetings folder. |
| `task` | action, steps, under, after, text, which | Manage the current multi-step task (started with plan_task): add_steps (new steps at the end, after a step, or as sub-steps `under` a step that turned out bigger), replan (replace the steps not done yet), note (keep a finding for later steps), pause, resume (a paused or unfinished task - after a restart, a stop, or 'where were we'), list (unfinished tasks), abandon. |
| `recall_history` | question, when | Look back at what happened: past conversations, what Mint did and found, agents' results, tasks, automations and videos watched, by date. |
| `automation` | action, name, trigger, time, days, every_minutes, between, folder, pattern, app, minutes_before, match, do, how, agent | Things Mint does by itself: on a schedule (at a time once, daily/weekdays/some days at a time, every N minutes) or when something happens (a new file in a folder, an app opens, N minutes before calendar events). |
| `watch_video` | source, question, at, frames, fresh | Watch and understand a video in seconds: a YouTube / X / Vimeo / Loom / TikTok / Instagram link, a video or audio file, or - with no source - the video page in front, the video selected in Finder, or the newest screen recording. |
| `screenshot` | what, target, size, copy, save, name, format | Take a screenshot and save it as a file (and copy it to the clipboard). |
| `clipboard` | action, text, path, index | Manage the clipboard. |
| `read_file` | path, start_line, max_chars | Read a file on this Mac: text, code, Markdown, CSV, JSON, PDF, Word/RTF/Pages-exported docs. |
| `write_file` | path, content, mode, find | Create or change a text file (notes, code, Markdown, CSV, HTML...). |
| `find_files` | query, folder, kind, content, limit | Find files and folders by name or content with Spotlight, newest first. |
| `file_action` | action, path, to | Do something with a file or folder: open (in its default app), reveal (in Finder), info, move / copy / rename (to `to`), make_folder, trash (moves it to the Trash - only when the user asked to delete it; it can be restored from there). |
| `web_search` | query, max_results | Search the web and get titles, links and snippets - for facts, news, prices, docs, anything current. |
| `read_url` | url, max_chars | Fetch a web page or text URL and get its readable text, without opening it on screen. |
| `browser` | action, target, text, browser | Full control of the web browser (Chrome, Safari, Brave, Arc, Edge) - faster and surer than clicking pixels. |
| `scroll_to` | target, where, direction, amount | Scroll so something is visible, in any app: `target` is text or a control to bring into view (scrolls a long list or page until it appears). |
| `menu` | path, action, app | Use the app's menu bar - every command an app has is there, with its keyboard shortcut. |
| `wait_for_text` | text, seconds, gone | Short wait (seconds, at most 2 minutes) for `text` to appear in the front window - or to go away, with gone=true (a 'Loading…' or 'Exporting…' label, a dialog closing). |
| `pointer` | action | The user's mouse pointer: where (what is under it - the control, its app, nearby text) or click / double_click / right_click right where it is. |
| `quit_mint` |  | Quit Mint itself completely - the assistant (you), everything you started (sub-agents, Codex, preview servers) and the screen-control engine - when the user says 'quit Mint', 'turn yourself off', 'shut down Mint', 'close Mint'. |
| `run_applescript` | script | Run an AppleScript for apps with a scripting dictionary: Finder, Music, Notes, Reminders, Calendar, Mail drafts, Safari/Chrome tabs, System Events UI scripting. |
| `fix_hearing` | heard, meant, forget | Remember a word or name you misheard, so it is heard right from now on. |

99 tools.
