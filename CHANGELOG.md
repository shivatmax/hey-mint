# Changelog

## 0.3.0 (2026-09-30)

- **Telegram: files and quick share.** Send Mint a PDF, document, photo, video or audio file from your phone:
  with a caption it is saved in the Mint folder's "From phone" and done ("summarise this"); without one you get
  buttons (Summarise, Translate, To spreadsheet, Copy text, Show on Mac). Albums arrive as one request. From the
  Mac: /clip (the clipboard), /last (the last screenshots), /files (what Mint made), /send <file or folder>,
  /paste <text> (onto the Mac's clipboard). The user's own words, not a file's name, decide what they asked.
- **Telegram looks like an app:** a command menu, buttons on every step (Pause, Stop, Screen, Run again, Undo),
  a quick keyboard, a live plan with a spinner and timings, Mint's cards as pictures, a welcome card.
- **Screenshots of any window:** "screenshot my Claude Code window / the Chrome window" now works when the window
  is covered, on another Space, or a full-screen app (live, not stale), saves exactly where asked
  (`~/Folder/name.png`), and is sent to your phone when the request came from Telegram.
- **Never sends by accident:** in a messaging app, Mint refuses Return, Send buttons and blind clicks unless you
  asked to send, reply or message someone.
- **Keys survive reinstalls:** install.sh merges .env instead of overwriting it; ⌘C/⌘V/⌘X/⌘A/⌘Z work in Mint's
  text fields (a hidden Edit menu).
- **Pictures with Image Playground** (`mint/tools/imagegen.py`, `mint/ui/image_card.py`): "make an illustration of…"
  draws on your Mac in seconds through two small shortcuts Mint installs ("Mint Draw", "Mint Redraw"; one Add
  Shortcut click each). A card by the orb shows the picture with "Describe a change…" (redraws, every version kept),
  Save…, Another, Improve (a richer prompt), Copy and Open in Image Playground; all by voice too.
- **Apple Shortcuts, made for you** (`mint/tools/shortcut_maker.py`, `shortcut_library.py`): "make me a shortcut
  that…" plans it from 39 verified Shortcuts actions, reads the steps back and, after a yes, opens it for one Add
  click. Settings ▸ Apple Shortcuts lists Mint's own shortcuts (Add), yours (Run, "Mint may run it"), and Create a
  shortcut. Never sends messages or deletes; email is a draft; shell commands are always shown.
- **Autopilot:** no nudge after a question that was answered; a bare "All done." to a new request is caught and the
  request is done.
- **Welcome tour:** pictures and Shortcuts added (14 features), the list fits, three things to try.
- **Welcome window, light theme:** pale sky-blue background with drifting light, Liquid Glass (NSGlassEffectView)
  cards, fields, chips and buttons, deep-navy text and sky-blue highlights.
- **Reliability, from the benchmark (67% -> 96% on 70 tasks):**
  - **Save and create where asked:**
    - spreadsheets and PDFs save where the user says (`save_to`, or the path in the request)
    - `data_to_sheet` takes `data` (no scratch CSVs) and writes a correct Total row
    - new `edit_spreadsheet` edits an .xlsx in place
    - web tables are read directly
  - **Apple apps:** reminders, notes and calendar events go into the named list/folder/calendar, matched ignoring spaces and case. New `create_event` checks clashes. Mail drafts really open in Mail.
  - **Dates:** "today/tomorrow" come from the user's words; Mint reconnects after midnight so the date is right.
  - **Files:**
    - a missing file with a close name is used (typo) or offered (a lookalike)
    - `find_files` suggests the same name in another format
    - listings show exact ages
    - "Markdown" means .md
    - creating a file that exists adds below instead of replacing
    - a guard stops overwrites that would lose the user's lines
  - **Tidy and merge:** tidy only skips files still being written; duplicates are by content. New `merge_folders` keeps the newest copy, puts older ones in `older/`, is undoable.
  - **New `calculate` tool:** exact arithmetic for totals and conversions.
  - **Clipboard:** `to_file` writes copies into a file exactly; restore reports the right item; Mint's own pastes are no longer recorded as the user's copies.
  - **TextEdit:** .rtf files are edited directly (formatting kept); stale windows are reopened; saves are verified.
  - **OCR:** `ocr_copy` returns the full text and can save it.
  - **Video:** "cut out 0:05-0:15 as clip.mp4" keeps that part.
  - **Agents:** save to the named path; a clear message (no retries) when the OpenAI account is out of credits.
  - **Session:** a typed request is resent after a dropped session; hearing fixes are never saved from typed requests.
  - **Also new:** Telegram remote control and ChatGPT/Claude desktop app control (from a parallel session).
- **Telegram remote control** (`mint/app/telegram.py`): text or send voice notes to your own Telegram bot from
  anywhere; Mint runs them on the Mac and mirrors its work live into the chat (one "working" message updated step
  by step, then the reply, plus screenshots and files it made). Pairing with a 6-digit code (only your account),
  /stop /status /screenshot /pause /resume, read-only mode, alerts from trackers and agents, an audit log.
  Outbound long polling only: no server, no port forwarding.
- **ChatGPT and Claude app control** (`mint/tools/agentapps.py`): "ask ChatGPT to …", "what did Claude answer?",
  "is Claude still working?", open, list and start chats or Claude Code sessions, stop a reply. Native
  accessibility, the prompt verified before one Send, replies treated as quoted data. Trackers: "tell me when
  ChatGPT finishes", "let me know when Claude is done".
- **Your own wake word** (`mint/voice/wake_train.py`): type "Hey Jarvis" (or any two-three words) in Settings ▸
  Voice & wake word and train it on your Mac in about 90 s, from system voices plus optional takes of your own
  voice. Checked before it is used (93% of unseen voices, ~1 false wake an hour in testing); "Hey Mint" can stay
  on too. Both the Python detector and the Swift Mint Ear load it.
- **Reliability benchmark** (`bench/reliability/`): 50 sandboxed long-horizon tasks with complications (typos,
  locked files, interruptions, slow and failing pages, two-turn follow-ups), each set up, run against the live
  app and checked by its end state. Mint prints `[done]` when a request is finished.
- **Deaf-session watchdog:** a typed request that gets nothing back in 30 s reconnects with a fresh session and
  is sent again (seen after a voice-change reconnect: the resumed session accepted requests but never answered).
- **Automatic updates** (`mint/app/updater.py`): the packaged app checks GitHub releases at launch and twice a
  day, downloads in the background, verifies the checksum and that the new app is signed with the same
  certificate, then swaps it in place while the Mac is idle and reopens it. No second copy; permissions stay. A
  free self-signed release certificate (`packaging/make_release_cert.sh`) keeps macOS permissions across updates
  without a paid Apple Developer ID.
- **Settings, redesigned:** a sidebar of pages like System Settings, with grouped cards. New pages: Accounts &
  keys (Gemini required; OpenAI and TypeSafe optional; Google via Internet Accounts), Usage, and Updates & Help.
- **Usage** (`mint/core/usage.py`): tokens per model per day (requests, input, output, thinking), counted from
  every Gemini request, the Live session and the OpenAI agents. Counts only, never content.
- **Language:** Mint answers in the language you speak, or always in one you pick (Settings ▸ Speaking, or "always
  answer in Hindi").
- **Notifications** (`mint/tools/notifications.py`): "what did I miss?", read, filter, open, dismiss and clear
  notifications, and reply where the app allows (read back first, sent only when asked). Through Accessibility:
  no new permission.
- **Undo** (`mint/tools/undo.py`): "undo that", "undo the last 3 things", "redo". A 24-hour journal of Mint's own
  reversible actions: Mac switches, window layouts, rewritten and typed text, reminders, notes, file moves and
  renames, clipboard changes, settings changed by voice.
- **Report a problem:** the menu bar and Settings open a prefilled GitHub issue; recent errors (paths, emails,
  keys and speech removed) go on the clipboard to paste in if you choose.
- **Google, locally:** Settings shows which Google accounts Mail and Calendar have, with a button to add one in
  Internet Accounts. No Mint sign-in, no server.

## 0.2.0 (2026-09-29)

- **Welcome window** (`mint/ui/onboarding.py`): the first launch opens a guided setup. It covers:
  - your name and what to call Mint, with a live preview (a new name gets its own wake word)
  - what you do
  - a voice, tap to hear it
  - Microphone, Accessibility, Screen Recording, Input Monitoring, Calendars and Reminders: each with why it's needed, its live status, and Allow (macOS's prompt, then the right Settings pane)
  - the shortcuts as keycaps, click to change
  - a 12-feature tour with clips
  - "try this first" cards

  Esc or Skip ends it on any page; the menu bar's **Welcome tour…** opens it again. The shortcut recorder is shared with Settings.
- **Clipboard** (`mint/tools/clipboard.py`, `mint/ui/clipboard_window.py`): everything you copy (⌘C), every
  screenshot and everything Mint copies, kept on this Mac for 7 days. ⌃⌥V or "open my clipboard" turns the orb into
  a compact card (All · Mint · Pinned, search): tap the circles to pick several in order, then paste, copy, pin (up
  to 20) or delete; double-click pastes one; drag a clip out. By voice: "paste the last 5 screenshots in the chat",
  labelled clips, pins, and secrets saved to the macOS Keychain (never in the history, cleared after 60 s).
- **Documents** (`mint/tools/convert.py`): `ocr_copy` (screen, window, image, PDF or clipboard picture to the
  clipboard), `data_to_sheet` (tables on screen or in files to a clean .xlsx), `convert_document` (PDF/DOCX/text
  into another language and format - e.g. an English PDF to a Hindi Word doc - keeping headings, lists, tables).
- **Video editing by voice** (`mint/tools/video_edit.py`): trim, cut, speed, vertical crops, captions, silence
  removal, compress, GIF, join, music, fades - planned by Gemini from safe operations, rendered with ffmpeg, and
  checked (QA) before Mint reports.
- **Dictation, Wispr-Flow style** (`mint/voice/dictation.py`): hold Right ⌥ (or your key), talk, let go - clean
  text typed at the cursor in any app (fillers dropped, corrections applied, Hindi/English), ~3 s. Tap twice for
  hands-free. A live waveform on the island.
- **Your own shortcuts** (Settings ▸ Shortcuts): talk without the wake word (⌃⌥Space), dictate, open the chat.
- **Drop things on Mint:** drag files, a folder or text to the orb; the island offers what fits (summarize,
  translate to a Word doc, spreadsheet, copy text, captions, transcribe) or "Tell Mint what to do".
- **Answers as cards** (`show_card`): counts as a big number, lists with icons (files open on click).
- **Translation in place:** foreign text is covered in its own colours with the translation written over it.
- **Trackers** (`track`, `mint/tools/trackers.py`): "let me know when the download finishes / this Claude session
  is done / the build finishes / the upload completes". Downloads by the browser's partial file, Claude Code
  sessions by their transcript (and "needs you" on a pending approval), Terminal/iTerm commands by the tab's
  foreground process, any window by its progress bars and a Gemini check against your words, files by size.
  Told by voice, notification and the island; up to 12 hours, kept across restarts.
- **Guide recordings** are checked frame by frame: a clip where anything but the demo backdrop shows is
  rejected before it can be published.
- **Teaching looks recorded** (`mint/ui/teach_fx.py`): a Mint-coloured ring on the pointer, and each click
  captured with a ripple, a snapping frame and its step number. The island's teach recorder counts steps,
  pauses and carries on (`teach action=pause/resume`), then shows "Saving the skill…" and "Learned".
- **Watching a video on the island:** the title, each step and the keyframes popping in as they are picked.
- **Guide:** new sections for the island and the Mac switches, and clips of every island scene.
- **Pointing at things without words:** `show_on_screen` ("where's the share button?", "point at the gear
  icon") now looks at the screen for icons, buttons and pictures when the words search finds nothing - a
  tight box around that one thing, found on a fresh screenshot with the mouse position as a hint.
- **The island** (`mint/ui/island.py`): like the Dynamic Island, the orb morphs into a black capsule with a
  tiny live Mint face for whatever is going on, and springs back after: a meeting recorder on calls
  (record, mic and video switches; REC timer, level bars, Stop; notes progress, Notes ready), screen
  recording and "Record this area?", teaching a skill (Learning, clicks, Done), tutor lessons (step, next),
  and a schedule card for briefings and today's calendar (weather, timeline, reminders, headlines).
- **Meetings with video** (`meeting action=start video=true`, or the island's video switch): the call's
  window with sound into the meeting folder; `mic=false` records only the call.
- **Meeting notes so far** (`meeting action=so_far`): a summary of the call up to now while it keeps
  recording. Asking for "the transcript and summary" mid-call had stopped the recording; it no longer
  does, and a meeting restarted within 15 minutes under the same title is one meeting in the notes.
- **Mac switches** (`mac`): brightness, keep awake, Do Not Disturb (through two small shortcuts Mint
  prepares), window layouts through Rectangle (halves, thirds, quarters, two apps side by side, full
  screen, other display), resume music, Night Shift, Wi-Fi, show desktop / Mission Control, Settings pages.
- **Screen recording** (`screen_record`): the whole screen, one window or an area you describe ("the left
  half", "the video player"); Mint shows a box and records only after you confirm. New Swift helper
  `MintScreen` (`launcher/screenrec`, ScreenCaptureKit).
- **More instant commands:** show desktop, Mission Control, keep awake, snap left/right, maximise, full
  screen, start and stop screen recording.
- **Agents on GPT-6 Luna only:** Astra, Luna and Sage run on OpenAI's `gpt-6-luna`; OpenRouter is no
  longer used by any agent.

- **Moods:** Mint shows feelings by itself as you talk (a smile at good news, a blush at thanks, a laugh at a
  joke) and slides on its sunglasses when it finishes a task. Off with the `auto_emotions` setting.

- **Daily briefing** (`briefing`): calendar, reminders, mail, tasks, weather and headlines in one spoken
  minute; schedule it with an automation.
- **Apple Shortcuts by voice** (`shortcut`): Home scenes and lights, Focus modes, music, your own shortcuts.
- **Screenshot search** (`find_screenshot`): finds screenshots by the text in them, read on the Mac.
- **Meeting action items to Reminders** (`meeting action=reminders`).
- **Translate the screen** (`translate_screen`): any language on screen, with the translations pinned beside
  the text.
- **Mail triage and replies in your style** (`mail`): what needs you in the inbox; drafts written the way you
  write, never sent. The Mail app's inbox is now read in bulk (a big inbox had timed out).

- **Meeting notes, no bot** (`meeting`, menu bar ▸ Record meeting):
  - Recording: `MintRecorder` (new Swift helper, `launcher/recorder`) records the call's audio through
    a Core Audio tap, which needs only the "System Audio Recording" permission, plus your mic, as two
    tracks.
  - Output: Gemini writes a timestamped transcript (You vs. the others, names when said) and notes:
    decisions, action items, open questions.
  - Where it goes: the Meetings folder.
  - Offer: Mint offers to take notes when a call starts.
- **Teach by showing** (`teach`): Mint watches you do a task once and saves it as a skill with
  parameters. Passwords are never recorded.
- **Tutor mode** (`tutor`): "show me how to…" points at each control on screen and moves on when you do
  it.
- **Edit selected text by voice** (`edit_selection`): rewrite, fix, shorten, translate in place, in any
  app.
- **Documents to spreadsheet** (`make_spreadsheet`): many PDFs, scans or receipts into one .xlsx (new
  dependency: openpyxl).
- **Tidy a folder** (`tidy`): a plan first, then apply, then undo. It never deletes.
- **Instant commands:** "volume 30", "next song", "pause", "mute" run the moment you stop talking,
  never twice.
- **"Which memory did you use?"** (`memory_used`).
- **Mint storage folder** (Settings ▸ Storage & Privacy, or by voice): everything Mint makes goes into
  one folder with Documents, Meetings, Videos, Agents and Spreadsheets.

- **Watching videos** (`watch_video`):
  - What it takes: a YouTube, X, Vimeo, Loom, TikTok or Instagram link, a video or audio file, or
    "this video" on screen.
  - What it gives: what the video is, the key moments with timestamps, and its look and feel (from
    about 12 scene-change keyframes on one contact sheet, plus measured pace and colours).
  - How speech is read: captions when there are any, otherwise Gemini transcribes it in parallel
    two-minute pieces (optionally `parakeet-mlx` on the Mac).
  - Follow-ups and frames: follow-up questions are answered from the cache, and `at` shows an exact
    frame.
  - Tools: frames come from AVFoundation and audio from `afconvert`, so no ffmpeg is needed. Web
    videos come through `yt-dlp` (new dependency).
  - Sub-agents with web access can watch videos too.
- **Automations** (`automation`):
  - Triggers: once, daily at a time on some days, every N minutes (optionally between two times), a
    new file in a folder, an app opening, N minutes before calendar events.
  - Actions: Mint does it, a sub-agent does it, or a notification.
  - Safety: automations never send, buy or delete.
  - When Mint has unloaded: Mint Ear starts Mint when one is due.
- **Tasks that last** (`task`):
  - Plans are saved and survive restarts and "stop" (which now pauses the task).
  - Steps can be split into sub-steps (3.1, 3.2), replanned or annotated.
  - "Where were we?" resumes with the outline and notes.
  - `step_done` records what was checked.
- **Memory:**
  - Without a TypeSafe key, filing facts, replacing outdated ones and recall now use Gemini Flash
    Lite instead of word matching.
  - Changed facts keep their earlier value.
  - Relative dates are stamped with the day they were said.
  - A daily tidy merges duplicates and drops facts whose date has passed, logging every change.
- **The journal** (`recall_history`): "what did we do yesterday?", "when did I ask about flights?",
  "what did Astra find on Monday?", answered from the conversation history, tasks, automations and
  videos.
- **Activity timeline** (opt-in): the app, window title and page address in front, text only, for
  14 days. It answers "what was I working on?" and "how long was I in Slack today?".

## 0.1.2 (2026-09-28)

- A developer handbook ([docs/development.md](docs/development.md)): running from a checkout,
  where new code goes, testing, building the Mac app and releasing.
- One app: the screen-control engine behind the `desktop` tool (from jev-use / Desktop Voice) is
  now built into Mint.app as `MintEngine`. It starts on demand as part of Mint, uses Mint's
  Accessibility permission and quits with it. The separate Desktop Voice app is no longer needed.
- Fix: Mint no longer refuses to start without Desktop Voice, which made the 0.1.1 app unusable on
  other Macs. Without a TypeSafe key the `desktop` tool is hidden and Mint uses `ui_act`.
- `install.sh` creates its own persistent signing identity, so macOS keeps Mint's permissions
  across rebuilds without jev-use installed.
- `run.sh` no longer needs the Desktop Voice app.

## 0.1.1 (2026-09-28)

- Fix: the voice package and the example skills were left out of the repository (and the
  0.1.0 app) by an over-broad `.gitignore`. Tests now import every module.

## 0.1.0 (2026-09-27)

First public release.

- Voice assistant for macOS on Gemini Live, with an on-device "Hey Mint" wake word and voice lock.
- 88 tools: apps and windows, clicking and typing through Accessibility, files, web search,
  full browser control, menus, screenshots, clipboard, calendar, reminders, notes, mail drafts.
- Showing things on screen (box, underline, highlight, circle, arrow) and 60 fps orb motion.
- Skills learned from experience and a searchable memory, both editable.
- Background agents (research, code, documents, Codex builds) shown as critters, with teams.
- Illustrated guide at https://hey-mint.pages.dev/.
- A downloadable app (DMG) with Python, libraries and models bundled, and a first-run window for the API keys.
