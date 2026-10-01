# Changelog

## 0.5.3 (2026-10-01)

- **Claude mode: coding agents in the notch.** Claude Code and Codex sessions show live in the notch, read from
  their own session logs (nothing to install): the project, each step (Read, Edit, Run...) with a spinner, tick or
  cross, the lines read, the diff written (red out, green in), the command and its output, the question it asks,
  and its final answer. The wing shows Claude's sparkle while it works, an amber pulse when it waits for you and a
  green tick when it is done; the notch opens by itself when an agent needs you or finishes. ‹ › switches sessions,
  ↗ opens the session's own Terminal/iTerm tab, VS Code or app. Orb mode gets the same view as a card by the orb.
  Voice: "Claude mode", "what is Claude Code doing?" (`mint/tools/agent_watch.py`, `mint/ui/notch_agents.py`).
- **Approve Claude Code from the notch (opt-in).** Settings ▸ Appearance ▸ Claude mode ▸ Connect Claude Code adds
  one hook to `~/.claude/settings.json` (shows the change first, keeps a backup, removes only its own entries on
  Disconnect). Permission requests open the notch with Allow, Always allow (Claude Code's own suggested rule) and
  Deny; the terminal still asks too and the first answer wins, so Claude Code never waits on Mint
  (`mint/tools/agent_hooks.py`).
- **Control your agents from your phone.** While you're away, Claude Code and Codex message you on Telegram when
  they need you or finish: Allow / Always / Deny a permission there, or reply to their message ("push") and Mint
  types it into that session - its Terminal or iTerm tab (by tty, no focus change) or the Claude / Codex app.
  /agents lists them. By voice or chat: "tell Claude in korus to push" (`mint/tools/agent_remote.py`,
  `mint/app/telegram_agents.py`; Settings ▸ Claude mode ▸ Message me on Telegram: away / always / never).
- **Claude mode, small or full:** a minimize button folds the Agents tab into one line (live clock, current step,
  Allow / Deny inline, a dot per step); it opens small by itself. The app you're in (Claude, a terminal, VS Code,
  Codex) shows its session first, even idle ones from the last hours; Codex threads show their names; stopped
  servers get a grey mark instead of a red failure; the face carries the app's icon.
- **Livelier motion:** the notch opens on a gentle spring and folds back quicker; content grows in a beat after
  the shape and fades out instead of vanishing; the small row's buttons pop in one after another; Mint's hop
  squashes and stretches. (Off with "Playful motion".)
- **Notch:** resting on the small row's plain part opens the full notch; a folded notch closes sooner; pulling the
  little Mint out of the notch carries the orb with the pointer; dragging the orb near the notch opens a drop zone
  (it squeezes, swells and jiggles in). Fixed: a flight into the notch interrupted by a mouse press left a black
  shape and the orb stuck under the notch; "100%" was cut off in the battery wing.

## 0.5.2 (2026-10-01)

- **Paste works everywhere:** the first-run prompt for the Gemini (and other) keys is a native window of the
  launcher, which had no Edit menu, so ⌘V did nothing there. It now has one, and its key fields paste on their
  own (`launcher/ear/EditMenu.swift`). Every text field inside Mint also pastes, copies, cuts, selects all and undoes
  even without the menu (`mint/ui/pasteable.py`).
- **Install without the approval hunt:** `curl -fsSL https://hey-mint.pages.dev/install.sh | bash` downloads the
  latest release, checks it, installs it, approves it with macOS (removes the download mark a free, un-notarized
  app would otherwise be stopped by) and opens it (`packaging/install.sh`).
- **A proper DMG window:** the app, a dashed arrow to Applications, what to do if macOS blocks the app and a link
  to the install command (`packaging/dmg*`, built with dmgbuild, no Finder needed on the build Mac).

## 0.5.1 (2026-10-01)

- **Notch you control:** hovering only shows the small row of controls; a click on the notch opens the full notch
  and a second click folds it back (it also folds by itself after a few unvisited seconds). Drag the little Mint
  away from the notch to pull it out: it stretches on an elastic, springs back if let go, and leaves the notch
  (orb mode) when pulled far enough.
- **Fixed:** the small row's buttons (mic, voice, chat, sleep, eye, menu) didn't react to clicks: the full notch
  opened under the pointer while reaching for them, and a faded-out label sat over them.

## 0.5.0 (2026-10-01)

- **Files into apps** (`mint/tools/handoff.py`): "compress it with Keka", "open these in Preview", "extract this".
  Mint hands files over like Finder's Open With (sandboxed apps included), answers the app's save-location
  question for the job asked, and names the file it made. "Drag it into Keka" really drags: the file is shown in
  the notch, the pointer carries it onto the app's window, then goes back. Misheard app names are matched.
- **A richer notch** (`mint/ui/notch*.py`, with ideas from boring.notch): hover opens a home with Mint or the
  player, this week's calendar and the battery; a Shelf for files (drop on the notch, drag out, AirDrop, share);
  search in the notch (files, folders and apps as tiles, 3 × 1 to 3 × 3 grids, path on hover); a charging peek.
  Found files and a new song open the notch at once, even while Mint talks; "Hey Mint" always shows the little
  Mint, also over a song; hovering while Mint talks shows its words.
- **Connectors for file jobs:** a connector action can hand files to its app (Keka's scripted "compress" does
  nothing from a script); "compress", "zip", "extract" and other verbs count as asking for the action.
- **Calmer notch:** hovering is two-stage (a short rest shows a small row of controls, staying opens the full
  notch); clicking the notch no longer opens the chat (there is a chat button; a click only closes it). The
  right-click menu is down to Microphone, Spoken replies and Settings; the rest moved into Settings.
- **Fixed:** asking for the notch again while Mint was still leaving it was ignored, leaving the orb on screen
  (or hiding the notch); "stop" clears the words on screen.
- **Fixed:** closing the chat could hide every Mint window, so Mint looked crashed; "stop" now also refuses
  tool calls the model sends after it in the same turn; notch search and shelf tiles start a drag from their
  picture too.
- **Music: Spotify and Apple Music** (`mint/tools/music.py`, `mint/ui/music_player.py`): "play Blinding Lights",
  "play some lo-fi", "next", "pause", "what's playing", volume, shuffle, repeat, seek. Spotify links are found
  without a Spotify login or developer account; genres use Spotify's own playlists. A mini player (cover, progress,
  controls) shows while music plays; in notch mode the notch becomes the player (cover and bars in its wings,
  controls on hover). Settings ▸ Appearance turns it off.
- **Apple apps, all the way** (`mint/tools/apple_apps.py`): Notes (search, read, add to, change, rename, move),
  Reminders (due/overdue, complete, reschedule, move), Calendar (move, rename, edit; clash warnings), Contacts
  (lookups), Maps (directions and travel time), Safari (Reading List, bookmarks), Photos (find, export copies) and
  Pages/Keynote/Numbers (new documents). Every change can be undone; deleting only when asked.
- **Settings ▸ Connectors** (`mint/tools/connectors.py`, `connector_maker.py`): everything Mint can connect to and
  its status on this Mac, and "integrate <app>": Mint reads an app's scripting dictionary, plans a small tested
  connector and saves it after a yes. Connectors only talk to their own app and never send, delete or pay unless
  asked in that request.
- **Fixed:** a tool schema with an empty choice made the voice session refuse to connect; a new test
  (`tests/test_declarations.py`) checks every tool against the Live API's rules. Settings text no longer gets cut off.

## 0.4.0 (2026-09-30)

- **Notch mode (Dynamic Island at the camera):** Mint can live in the MacBook notch instead of the floating orb -
  its face beside the camera, the island dropping down from the notch for cards, calls and progress (menu bar
  toggle, or "become the notch" / "go out of my notch"). Switching is live, with no restart: the orb flies into the
  notch or drops back out. In notch mode the notch wraps each card into one black shape (compact sizing, Settings ▸
  Appearance ▸ Notch). Mint doesn't unload itself while in notch mode.
- **Feelings from the conversation, and a cuter face:** Mint now reads the moment (Jev, about 0.4 s) - a sad
  face when you share something sad, a laugh at a joke, applause for your good news, hearts when it comforts
  you - and stays neutral for ordinary requests. The orb has sparkly eyes, rosy cheeks, a tiny cat mouth at
  rest and floating "z"s while asleep.
- **A close (×) button on every pop-up:** island scenes, the schedule and info cards, video, trackers, drop, lessons,
  meetings, the picture card and the clipboard, in both modes. Closing hides the card; it never stops the work under
  it (a recording keeps recording).
- **Settings ▸ Models & agents:** keys for OpenAI, Anthropic (Claude), OpenRouter, Groq, xAI, Ollama and custom
  OpenAI-compatible endpoints, each with Test; up to three backup models for every agent with Gemini as the last
  resort; edit or add agents (name, role, instructions, model, backups, thinking, web tools). A second Gemini key
  is optional: "split" gives the voice its own key, or it is a backup that takes over on rate limits. OpenAI and
  Gemini were tested live; Claude, Groq, xAI and Ollama against local test servers only.

## 0.3.1 (2026-09-30)

- **Signed releases:** the app is signed with the Hey Mint Release certificate, so from now on updates install themselves and keep Microphone, Accessibility and Screen Recording. Coming from 0.3.0 (unsigned), install this version by hand once.

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
