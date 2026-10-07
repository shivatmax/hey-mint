# Changelog

## 0.6.1 (2026-10-07)

- **A livelier Mint (character system):** a small status badge on Mint's face - dots while it works, red on an
  error, green when done, the word when you point at it - with the lower half of the face tinted to match. The eyes
  curve round the ball as they follow your cursor, blink like a real blink, and change shape with the mood (flat,
  half-moon, happy arcs). Poke Mint: a slap; three quick ones make it dizzy; keep going and it gets annoyed. The menu
  bar icon is Mint's face too, showing its state.
- **The notch, smoother and clearer:** it opens in one smooth move (cards blur in one after another, Mint's face
  travels into the open card) and closes 8 s after you leave, with a thin countdown line; sliding past never pops it
  open. Its outline says the state - a light sweeping while busy, amber when something needs you, a green flash when
  done, a red shake on an error - and alerts come one at a time.
- **New notch scenes:** a sparkly hello the first time Mint wakes each day; an error card with a Retry button; a
  drop zone where Mint turns into a folder; a progress bar with Mint's face as the moving thumb; and a "+" to type a
  request right in the notch.
- **Coding agents get faces:** each Claude Code or Codex session gets its own critter. The closed notch shows them
  as a little cluster; the open notch has a focus card with a rolling list of steps, test results and risk flags, and
  colored chips for the others (click one to focus it). A new diff line types itself in. A compact, centred bar under
  the notch shows the lead agent's current step.
- **A glow round the window Mint is working in** (Apple-Intelligence colours), so you can see where it is; windows in
  front still cover it, and it never catches your clicks.
- **Soft sounds,** made by Mint itself: the notch opening, a task done or failed, pokes, the morning hello. Quiet while
  you dictate or are on a call.
- **Settings > Appearance & Sound,** one page for all of it: where Mint lives (orb or notch), theme, what the open
  notch shows first; how much it moves (Full, Calm - no bounce, fewer idle tricks - or Minimal, fades only; macOS
  Reduce motion always means Minimal); every effect on its own switch; sounds on/off, a volume slider, and the notch,
  task and play sounds each on their own.
- **Fixes:** after "+" in the notch, the header buttons and the chat button work again and an empty box no longer
  keeps the notch open; a hidden Mint no longer shows the coding agents' faces poking out of the notch.

- **Your videos and music keep their volume while Mint waits:** echo cancellation (Apple's voice processing) used
  to run all the time on the Mac's speakers, and macOS turns every other app down while it runs - so a YouTube
  video or an anime played quieter with Mint just sitting there, and the mic heard you differently. Now, waiting
  for "Hey Mint", Mint uses the plain microphone: nothing else is turned down. Echo cancellation comes on once you
  have said your request (about 1 s, while Gemini thinks), so you can still talk over Mint's reply, and goes off
  when Mint is asleep again. Headphones never needed it. The voice lock allows a little for the plain mic
  (`voicelock.PLAIN_MIC`); Settings key `quiet_while_waiting: false` brings back the old behaviour.

## 0.6.0 (2026-10-07)

- **Optional Jev key in setup:** the welcome window has a new optional page after Connect - open TypeSafe's console
  (console.typesafe.ai/keys) in one click, paste the key, and Mint checks it with TypeSafe before saving it
  (`TYPESAFE_API_KEY`, in .env, mode 600). Skip keeps Gemini making those choices. install.sh asks the same,
  optionally, after the Gemini key.
- **Web tasks in seconds (`web_goal`):** a flight search, a form, a result to click through to - done in one go in
  Mint's own browser tab, usually in seconds, with what the page shows at the end. Ported from browser-use's
  jev-ultrafast (MIT): each step reads the visible page in one call, and Jev picks the operation and the element in
  one decision; a small model writes text only for typing (Gemini flash-lite, then OpenAI nano/mini, then Jev
  choosing from the request's own words). Event-based waits, no screenshots (`mint/tools/webgoal.py`).
  - Jev is optional: without a TypeSafe key (or while Jev is down) a fast Gemini model makes the same step
    decision, checked the same way - first through a Gemini Live model's tool call (Live quota is far larger; never a model the conversation needs: its own only with room to spare that minute), then the regular models in one JSON answer (only offered operations and observed elements; no DONE
    while parts of the goal are undone; told what was typed but not submitted yet), then OpenAI as a last resort.
  - Headless (nothing on screen, side by side with other jobs), as a window to watch, or in your own Chrome with
    your logins after a one-time OK - Mint never clicks Chrome's prompt (`mint/tools/cdp.py`).
  - Your own Chrome, with your logins, the best way available - each one hands over to the next instead of
    failing: (1) a connection you allowed, held by a small helper so restarting Mint never asks again (Chrome asks
    "Allow remote debugging?" once per Chrome start; only Mint's own process can use the helper); (2) Chrome's
    "Allow JavaScript from Apple Events" switch - turned on once, it stays on and nothing is ever asked again; the
    same engine runs in a background tab through it (`mint/tools/chrome_script.py`); (3) on screen, the old way.
    When nothing is set up, Mint brings Chrome forward, says in plain words where the switch is, waits, and starts
    the task by itself. After a "no" it doesn't ask again on its own. A site that turns away Mint's hidden browser
    with a robot check is tried once as a visible window.
  - With several Chrome profiles, the task goes to the profile signed in to that site (its tabs and cookies for
    it, counted, never read), and Mint says which profile it used.
  - Checked before each action: same page, element still there, nothing covering it; a step that did nothing isn't
    repeated, and no element is tried more than three times. No passwords, card numbers or codes; sending, buying,
    booking, deleting asked first; money never moved; page text fenced as data.
  - `python -m mint.tools.webbench`: five tasks with independent checks (Google Flights, Wikipedia, DuckDuckGo, a
    form, a local page). 15/15 verified; medians 3-11 s.
- **Mint checks your coding agents' work:** for Claude Code and Codex sessions, Mint now reads what really happened
  instead of trusting the agent's word: each test run's result from its own output (pytest, jest, vitest, go, cargo,
  node:test, mocha and more; "2 of 48 failed - expected 3, got -1"), whether code changed after the last run, retries
  ("fixed on try 2"), risky steps (`.env` changed, force-push, `reset --hard`, `curl | sh`, a command failing 3
  times), a test weakened while failing, and two agents changing the same file. Ask "did Claude's tests pass?",
  "what did my agents do today?", "recap", "how much Codex do I have left?" or "hand this to Codex" (writes a
  hand-off note and starts Codex on it). Telegram's done messages carry the verdict. A week of finished requests is
  kept on this Mac (`agent-history.json`). Rules adapted from dotpals (MIT).
- **Optional fix loop** (Settings > Appearance & Sound > Claude mode, off by default; needs Mint's Claude Code hooks): when Claude tries to
  finish, commit or push while its tests fail, weren't run after its last change, or pass only because a test was
  weakened, it's sent back with the reason - at most twice per request. Failures that were there before it changed
  anything, and requests that changed no code, are never held up. Before an edit, Claude Code asks you if another
  agent changed that file minutes ago.
- **Claude's usage limits** (optional): Settings > Appearance & Sound > Claude mode > Show Claude's usage limits sets a small status line that
  saves Claude Code's 5-hour and weekly limits for Mint and still shows your own status line. Codex's limits are
  read from its logs.
- **Agents can ask Mint (MCP):** a small read-only MCP server (`mint/tools/agent_mcp.py`) lets Claude Code, Codex or
  any MCP client call `check_my_work` before saying "done" ("Not done yet: you changed calc.py after the last test
  run", then "Looks ready"), plus `test_status`, `recap`, `today`, `risky_steps`, `handoff_note`, `usage_limits` and
  `agents_now`. Settings > Appearance & Sound > Claude mode > Let agents ask Mint adds it with each tool's own
  `mcp add`. Tested with a real Codex session.
- **Newer Codex versions read properly:** commands and patches Codex runs from its tool scripts (`exec`), and its
  `patch_apply_end` events, now show as steps, test runs and changed files.
- **An accuracy suite** for the checks: 36 hand-labeled real sessions (from dotpals) score Mint's test verdicts,
  failure reasons, retries, risky steps and weakened tests - 162 of 174 right, 4 wrong - and CI fails if that drops.
- **`mint --doctor`** (and Settings > Updates & Help > Check my setup): one checklist for the key, voice models,
  permissions, the app and updates, Claude Code and Codex, Telegram, email control and disk space.

- **Mint stops taking over your mouse (computer use, after Cua Driver - MIT):** buttons, checkboxes, rows, menus,
  pop-ups and Send are pressed through Accessibility with the app left where it is - behind your window, even on
  another Space; your pointer and your front app stay yours. The pointer is only a last resort (web content that
  ignores Accessibility, double clicks), then said as "(foreground)", with your pointer and front app given back.
  On the new test app: pointer moved in 0 of 26 tasks and the front app changed in 0 of 26 (was 24 of 26 each).
  - Every action reports one of CONFIRMED / PARTIAL / UNVERIFIED / SUSPECTED NO-OP / FAILED, with evidence read
    back from the target and what to try next (`mint/screen/effect.py`). An app that acts and then returns an
    error is never pressed a second time.
  - Typing goes straight into the field (accessibility insert, read back), then key events; the clipboard is only
    a last resort.
  - Pop-up and context menus: the choices are read and the right one picked through Accessibility, without leaving
    a menu open in front of you.
  - A focus guard puts your app back in front if a background press makes the target app jump forward.
- **Seeing the screen:** window-only captures (another window on top can't confuse it; Mint's own overlays never
  appear), a 1x/2x pixel check, a 2 s accessibility timeout so a frozen app can't freeze Mint, Electron apps
  switched on by polling for their web content, apps opened in the background found through their windows, rows
  scrolled out of view listed as "off screen", web-page buttons inside apps named, and element ids like [s12:7]
  that stay valid for one look, with "what changed since" (`mint/screen/capture.py`).
- **Choosing the target:** a strict Jev chooser (at most 24 candidates plus "look again" and "not sure",
  a confidence floor, ids from role + label + row; delete/send/buy/close never offered), a near-miss check ("Save
  all" never becomes "Save"), zoomed second looks for small targets, and click_at now finds what you named - the
  voice model's rough point is only a hint (`mint/screen/choose.py`).
- **verify_state:** checks up to 8 conditions (window, front app, control value/state, text, file) until they hold
  twice in a row; "unknown" never counts as done (`mint/screen/verify.py`).
- **Computer-use test bench:** a fixture app that records what really happened, 26 tasks scored only from its state
  file, a desktop-disturbance check (did the pointer or front app move?), ScreenSpot-style grounding with "not on
  screen" items, and offline replay (`bench/run_cu.sh`, `bench/cu_tasks.py`, `mint.groundbench replay`).
- **Install: approve once, never again.** The DMG is no longer signed with the free certificate (macOS Sequoia
  and later refused to even open a disk image signed but not notarized; an unsigned one opens), so macOS now asks
  only once, for the app, with "Open Anyway". Opened from the DMG, Downloads or the Desktop, Hey Mint offers to
  move itself into Applications (where it can update itself) and opens there without a second question. Updates
  install themselves without asking and keep the permissions. README, website, release notes and the DMG window
  now show both ways: the download with the three "Open Anyway" clicks, or one line in Terminal that asks nothing
  (GitHub link first; some networks block pages.dev).
## 0.5.10 (2026-10-06)

- **No more silent or very late answers:** sometimes Gemini had your words (they showed on screen) but never heard
  you finish - the voice lock had cut the audio mid-word, or noise kept the turn open - so Mint went to sleep, or
  answered long after. Now, 1.2 s after your voice stops with nothing back, Mint tells the voice service you're
  done; if there's still no answer 5 s later and you had just said "Hey Mint", it sends your words as text. Mint
  stays listening meanwhile, and a transcript alone no longer counts as an answer for the stall watch.
- **Always the quickest voice model:** Mint now uses a pool of Gemini Live models - 3.8 Live, 3.1
  Flash Live and 3.8 Live Extended Thinking (its own quota; last, as it sometimes promises an action and doesn't
  call the tool - such a promise with no action in 10 s goes to the next model) - found from your key once a day (newer ones join on their own;
  transcribe/translate/old native-audio models are skipped). Each has its own input-tokens-a-minute limit, and every
  turn re-sends the whole conversation (~23K input tokens), so one model fills up after a couple of quick turns and then
  stalls. Mint counts each model's input tokens and moves on before the limit; a request with nothing back in 3 s moves
  to the next model at once and is asked again; slow answers (median over 2 s) move at the next quiet moment.
  A model that misbehaves rests 5 min, then 20 min, 1 h, 5 h, a day, a week; good answers and a clean day forgive
  it, and Mint moves back to the best model when it's ready (`mint/voice/live_models.py`, voice-models.json;
  settings `switch_when_slow`, `voice_models`, `live_tpm_limit`).

## 0.5.9 (2026-10-06)

- **No more lost questions when the connection ends:** Google ends every voice connection after about an hour (and
  the backup voice model after ~2.5 idle minutes). Mint now reconnects ahead of Google's warning at a quiet moment,
  keeping the conversation; an ordinary drop reconnects in half a second with just "Reconnecting…" instead of an
  outage message, and words you said just before a drop that got no answer are sent again, so you don't have to
  repeat them. The outage caption and spoken notice now come only when reconnecting fails twice in a row.
- **Easier first run:** the welcome window has a Connect page - get a free Gemini key at Google AI Studio, paste it,
  and Mint checks it right there (a refused key says why and is never saved). Mint waits for the key instead of
  showing an alert. You also choose where Mint lives (in the notch or floating), and the tour is shorter.

## 0.5.8 (2026-10-06)

- **Much lighter at rest:** idle CPU went from 35-45% of a core to about 6%, with nothing looking different.
  - Claude mode's session list was deep-copied ~90 times a second; it is now copied only when a session changes.
  - The audio engine stops while the microphone is off, and comes back to speak or when the mic is turned on.
  - The notch looks 10 times a second instead of 30 while nothing on it moves (back to full speed the moment the
    pointer comes near or Mint wakes).
- **Memory stays low after big jobs:** ~255 MB at rest, instead of ~600 MB kept after a helper agent or
  background job. The shelf's first thumbnail used to load a framework in a way that wrapped every Objective-C
  class: 232 MB, never freed. Night Shift and the privacy checks had the same pattern; also fixed. The system
  dictionary is no longer kept in memory either (19 MB).
- **Google Meet:**
  - "End the call" ends it directly; the model had tried clicking around Chrome instead.
  - A crash when a call started (two audio rebuilds at once) is fixed.
  - The "who joined" message shows the person's name.
- **Several things at once:** hand Mint a job, then another, and keep talking. Long or multi-step requests
  ("research…and put it in a note", "tidy my Downloads", "also…", "meanwhile…") become background jobs that run
  side by side with Mint's own tools, each in its own context, and report when they end - when Mint is quiet,
  never over your words. Quick questions are still answered at once (`mint/app/background.py`).
  - The screen takes turns: jobs that only search, read, write files or mail run in parallel; clicking and typing
    are taken in turns (a job keeps the screen for its consecutive steps, your own request goes first, and a job
    waits while you type).
  - Nothing is thrown away: a tool that runs long, or that you talk over, carries on in the background instead of
    being cancelled, and Mint is told when it finishes. A second plan no longer pauses the one in progress.
  - Jobs show in the notch's Agents tab with Claude Code and Codex, step by step; reply there to change one or
    answer its question. `agent_status`, `message_agent`, `answer_agent` and `stop_agent` work on jobs (task-N).
    "Stop" stops what is in front of you (and a job clicking at that moment); jobs working off-screen go on.
- **Memory, rebuilt:** three kinds - about you (how you like things), facts, and a daily journal.
  - Every conversation starts with the facts that matter most, ranked and never cut mid-line (before, the most
    important pinned facts could be dropped by a length limit). The rest is found by words and by meaning, with
    no model call per question; typed, phone and email requests carry what's relevant.
  - Saying a thing twice confirms it; a change supersedes the old fact and keeps it as history; only lasting
    things are saved (not prices, battery levels or clicks). A daily pass merges duplicates and retires facts
    unused for three months (still findable).
  - Conversations are summarised from disk, so a crash or restart loses nothing; history is rotated past 4 MB
    (`mint/knowledge/memory.py`, `mint/knowledge/conversation.py`).
- **Fix:** in the notch's Agents card, the time label no longer runs under the minimize button.
- **Pages and emails can't give Mint orders:** web pages, email, files, search results, window and screen text reach
  the AI fenced as outside data; fake fence markers and invisible characters are removed and a fast pattern scan
  flags likely prompt injections (`mint/core/untrusted.py`). Memory and skills refuse instruction-like or
  data-sending notes; one already saved shows as [BLOCKED] until you fix it.
- **Guard:** Mint's own memory, skills, settings and app, and Claude Code's / Codex's settings, always ask first -
  even with the guard off, and in the Claude Code hook too. Three noes to the same task and Mint stops and asks
  what to do instead. "Yes to all" by voice and an "Allow all (N)" Telegram button when several jobs ask at once.
- **Skills that learn safely:** after a task, one cheap review learns from corrections, long tasks and
  recoveries - patching the skill it used first, writing rules not logs, refusing one-off or broken-setup lessons,
  and judging whether the skills used worked. Automatic learning only rewrites Mint's own skills (yours get
  "Suggested" notes; pinned ones are never touched). Every change is recorded with before/after text and can be
  undone ("undo what you learned about X", or Undo change in Skills & Memory) (`mint/knowledge/skill_ledger.py`).
  Unused skills go stale after a month; Mint's own are archived after three. The full skill list is in every
  conversation; skills can carry reference files; "learn this" turns a page, window, clipboard or the
  conversation into a skill; background jobs use and feed skills.
- **History search, indexed:** a private SQLite full-text index of every conversation; "what did I ask about…"
  answers in 1-3 s from just the matching moments, and can read a conversation word for word
  (`mint/knowledge/history_index.py`).
- **Long conversations keep your instructions:** near the voice model's limit Mint compacts at a quiet moment into
  a sectioned summary with your rules and requests quoted verbatim, the files/apps/links involved and the task in
  progress; plans and background jobs carry on (`mint/app/compaction.py`, setting `auto_compact`).
- **Lighter sessions:** 68 tools declared instead of 143 (about half the tokens per connect); rarer tools are found
  with `find_tools` and run with `use_tool` through the same safety checks (`mint/tools/diet.py`, setting
  `tool_diet`). A repeated identical call with nothing changing is stopped; results over 20k characters are
  trimmed with the full text saved under ~/Library/Logs/Mint/results/.
- **Outages explained:** a one-line caption (quota, Google's side, network, key) and a countdown to the next try;
  a short spoken notice if you were waiting.
- **Automations:** "tell me when this page/file changes" (no AI call until it changes), quiet runs when there's
  nothing new, notes kept between runs, natural schedules ("every weekday at 9", "every 2 hours", "tomorrow at 7"),
  no double runs after a crash, stalled runs stopped, an offer to pause after three failures, and an offer to
  automate a request made three days running. Work automations run as background jobs.
- **Pause everything / resume everything:** by voice or the menu bar; holds automations, background jobs and helper
  agents, even across a restart.

## 0.5.7 (2026-10-06)

- **Google Meet with Mint:** "start a Google Meet" (by voice, Telegram `/meet`, or email). Mint makes a meeting,
  joins it, shares the Mac's screen and sends you the link. Join from your phone and talk with Mint as on a call:
  the call is Mint's microphone and speaker. The call's chat works too, and Mint answers there as well. Whoever
  asks to join is let in (Settings ▸ Voice ▸ Google Meet calls), and Telegram tells you who. Mint leaves when
  everyone has gone. A red camera in the notch shows while a call is on.
  - How: Mint's own Chrome profile, driven over a private pipe (no port, no server, no audio driver). The first
    call opens Google's sign-in there; you type the password, Mint never does (`mint/app/meet_call.py`).
- **Email control:** email Mint a request with a subject starting `Mint:`. It does it and answers in the same
  thread, as a formatted HTML email with the steps, files, screenshots and a Join button for calls.
  - Gmail with an app password (kept in the Keychain) and IMAP IDLE, so mail is seen in a second or two. Apple
    Mail is the fallback.
  - Only verified senders on your list, or anyone with your secret word. Requests run one at a time, each with
    its whole answer (`mint/app/email_remote.py`).
- **Fixes:**
  - A request to start a call is started by Mint itself; the model had answered from an earlier failed try.
  - Finding the running session no longer scans memory from background threads, which could crash Mint.

## 0.5.6 (2026-10-05)

- **Hide Mint:** a hide button in the notch's hover row (and the menu, "hide yourself", ⌃⌥H) takes the little Mint
  and the icons beside the camera away - the notch still opens on hover - or fades the orb away. It comes back on
  "Hey Mint", when it has something to say, with the Show button or ⌃⌥H (Settings ▸ Shortcuts).
- **Notch buttons:** bigger (30 pt), bright white and fully opaque again - a pop-in that kept restarting had left
  them flickering at almost no opacity and shifted half outside the notch.
- **Stop button:** while Mint talks or works, ■ Stop shows first in the hover row - the same as saying "stop".

## 0.5.5 (2026-10-05)

- **Listens only when you mean it.** After a reply Mint keeps listening for a follow-up only a few seconds
  (Settings ▸ General ▸ Keep listening after a reply, default 6 s), then waits for "Hey Mint" again; other voices
  and background audio no longer keep it awake. Before answering or acting on words heard without the wake word,
  a quick check (rules, then Jev) decides they are meant for Mint and ask something - talk with others, videos,
  calls and stray phrases get no reply and no action; its own reply waits for that verdict (`mint/voice/listening.py`).
- **Never stuck again:** Settings pages read their data off the main thread (Loading… / Try again instead of a
  frozen window), settings changes notify on a worker. A watchdog in Mint.app restarts a frozen Mint by itself
  (⚠ in the menu bar after ~10 s, its stacks to the log, restart after ~40 s), ⌃⌥⌘M or Settings ▸ Restart Mint
  restart it any time, and Quit always works.
- **Teach and clicks in apps like VS Code:** fixed a crash while teach recorded (Accessibility asked about Mint's
  own windows off the main thread); clicks land on the centre of the target's own words, pointing snaps to the
  named button nearby; clicking into a text box counts as success and typing works in Electron editors; skills are
  matched to the right app (VS Code's "Code" no longer pulls in Claude Code skills); teach records what you click
  even where Accessibility shows nothing. "Format Document" / "Reset Zoom" no longer trigger the guard.
- **VS Code and other Electron apps, for real:** text boxes VS Code hides (a 1×1 input under a drawn placeholder)
  are found and typed into; ui_elements lists named things only (up to 140) and says which row a button is in
  ("button 'Install' in 'CSV Colorful Table…'"), and "Install CSV Colorful Table" picks that row's button; apps
  are found under all their names ("Visual Studio Code" runs as "Code"); typing the same search again is no longer
  blocked as a duplicate message (that rule is for messaging apps); Mint stays awake while its task has steps
  left; a run you stopped or that never finished is never saved as a skill. Every tool call and its full result
  is in ~/Library/Logs/Mint/tools.log.
- **Knows when not to speak:** words that stop mid-sentence ("I want to do", "can you"), fillers and a stray word
  right after "Hey Mint" get silence while Mint keeps listening for the rest - no more "your request got cut off" or
  "could you repeat that".
- **Rides out Gemini outages:** when the main voice model fails ("1011 Internal error"), Mint moves to the backup
  model after one fresh retry (it used to take ~30 s), and spoken words that get nothing back within 10 s make it
  reconnect on the backup and say so ("say that again") instead of silently falling asleep.
- **Reads the Terminal properly:** a terminal is read from its newest lines (the command just run and its output, then
  the prompt), not from the top of a scrollback that can be a million characters long.

## 0.5.4 (2026-10-01)

- **The guard: nothing deleted or changed without your yes.** Before Mint trashes, overwrites, moves or renames
  files, deletes notes / reminders / events / skills / memories, clears notifications, runs a script or command that
  deletes, overwrites, force-pushes, uses sudo or changes system settings, presses ⌘⌫ or clicks "Delete", it shows
  exactly what (files with sizes, the command) on a card under the notch or by the orb and waits: say or type
  "yes" / "no", or click. From the phone or while you're away the question comes on Telegram with ✅ / ❌. No answer
  in 90 s (5 min on the phone) means no. Helper agents ask the same way, and with Claude Code connected a
  destructive shell command makes Claude Code ask first even where it was allowed (PreToolUse hook). Settings ▸
  General ▸ Ask before deleting or changing (all / deleting only / never); a log in guard.log (`mint/core/guard.py`).

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
