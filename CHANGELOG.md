# Changelog

## Unreleased

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
