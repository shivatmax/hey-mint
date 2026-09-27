# Hey Mint: engineering notes

How each part works, what was measured, and why. For what Mint can do and what to say, see the [README](../README.md) and the [guide](https://shivatmax.github.io/hey-mint/).

**Illustrated guide:** [guide/index.html](guide/index.html), 11 pages with short clips of the real Mint, example things to say, and a page Mint that acts them out; to serve it, from this folder: `python3 -m http.server 8765 -d guide`, then open http://localhost:8765).

A hands-free voice assistant for macOS. Say **"Hey Mint"** and ask. Gemini Live
does the reasoning and the talking; Jev drives the screen.

It lives in the menu bar. When you speak to it, a small HUD appears in the top
right: an orb that moves with your voice and its own, live captions of both
sides, and what it is doing on screen.

**[GUIDE.md](GUIDE.md) is the complete guide**: every feature, how it works, what
to say, and a reference of all 79 tools.

## Install

Needs macOS 14.2+, [jev-use](https://github.com/savka777/jev-use) installed as
`~/Applications/Desktop Voice.app` (with its TypeSafe key and Accessibility), and
a Gemini API key from https://aistudio.google.com/apikey.

```sh
./set-key.sh             # paste your Gemini key; it is checked against the API
./install.sh             # builds ~/Applications/Mint.app
./install.sh --login     # same, and start Mint at login
open ~/Applications/Mint.app
```

On first use macOS asks for **Microphone**. For instant typing, scrolling and
keys, choose **Grant Accessibility…** from the menu bar item and switch Mint
on. Calendars, Reminders and Automation are asked for the first time you use
them.

Remove everything with `./install.sh --remove`.

## Using it

Say "Hey Mint", then talk. It stays listening while the conversation goes on
and falls asleep after 12 quiet seconds. "That's all" dismisses it at once.

Things it does well:

- **Apps and sites** — "open Figma", "go to youtube.com", "open my Downloads folder"
- **On-screen work** — "click Sign in", "search this page for pricing", "pick the second result"
- **Dictation** — "write a reply saying I'll be there at six" types into whatever field is focused
- **Your selection** — highlight text, then "summarise this", "translate this to Hindi", "rewrite this more politely"
- **Timers** — "set a ten minute timer for tea"; it tells you out loud when it is done
- **Calendar, reminders, notes** — "what's on today?", "remind me to call Sam at five", "make a note…"
- **Email** — "draft an email to priya@… saying…" opens a draft; it never sends. "What are my latest emails?" reads the Mail app.
- **System** — volume, media keys, lock, sleep display, dark mode, battery and time

From anywhere else — a terminal, Raycast, Shortcuts:

```sh
.venv/bin/python -m mint --say "what's on my calendar today?"
```

### The orb, the bubble and the chat

Mint is a small orb with a face, bottom right by default. It is always there -
dozing and grey while asleep, a coloured swirl when awake - and it is
click-through everywhere except its own circle.

- **Click the orb** (or press **⌘J**) to open the chat: the conversation so far,
  a box to type in, and buttons for stop, microphone, spoken replies and
  appearance. **Esc**, ⌘J or ✕ closes it. The chat is closed by default.
- **Drag the orb** anywhere; it remembers. **Right-click** it for settings.
- **The bubble** beside the orb shows your words and Mint's as they are said,
  and what Mint is doing. It fades when things go quiet.

Typing in the chat does not open the microphone. When Mint needs to click or
type in another app, the chat hands the keyboard back first; click the chat to
type again. ⌘J is taken from every app while Mint runs (Chrome's Downloads,
VS Code's panel); change it in `settings.json`, e.g. `"toggle": "ctrl+option+j"`.

### Talking to it while it works, and "stop"

Mint keeps listening while it works, so you can add to or correct a request
mid-task. Say **"stop"** (or "cancel", "never mind", "hold on", "enough") and
everything stops at once: the speech, the running step, Desktop Voice's
command, any clicks or keys not yet sent, and the task plan. Mint says
"Stopped." and does not resume. Typing "stop" in the chat, or the ■ button,
does the same. The words are in `settings.json` (`stop_words`); only short
commands count, so "the bus stop is near" does not stop anything.
`"listen_while_working": false` brings back the old behaviour (only the wake
word is heard during work).

### Asking it to change itself

Say it and Mint does it: "don't speak, just chat" (replies appear as text
only), "you can speak again", "turn off the mic", "make it pink", "move to the
top left", "hide the face", "show me the chat".

### What you see

- **Captions word by word** - your words and Mint's rise into place as they are
  said, each fading from the theme colour to white.
- **A face** - the orb has eyes. They follow the pointer, look up while
  thinking, look at the spot Mint is working on, squint happily (with
  blushing cheeks) when something works, close when asleep, and now and then
  wink. A little mouth moves with the voice while it speaks.
- **A Siri-like swirl** - two colour gradients turning against each other,
  faster while thinking or speaking, slow and grey while asleep.
- **What it is doing** - the orb itself becomes the task: its face melts away and it
  shows the app's icon (opening), a folder (files), a magnifier (search), a tapping
  hand (clicks), a hopping text cursor (typing), a scan line (reading); its shape
  morphs between round and a soft tile. It turns into a ✓ (and hops) or a ✗ (and
  shakes), then the face comes back.
- **On the screen itself** - only a small ripple where Mint clicks and a thin outline
  on the field being typed into; nothing else pops up. Both are click-through and
  invisible to screenshots.
- **Long tasks** - a progress ring round the orb (and a bar in the chat); confetti
  when the last step is done. "Stop" flashes a red ■.

`python -m mint --demo` plays all of it once, with no Gemini session.

### Customising

From the menu bar item, the chat's settings button, or by right-clicking the orb: **Theme** (Mint blue,
Aurora, Sunset, Mint, Rose, Mono), **Position** (any corner or top centre, or
drag it), face on/off, word-by-word captions on/off, on-screen effects on/off,
microphone, spoken replies. All of it lives in `settings.json` next to the
package (in the app: `~/Library/Application Support/Mint/settings.json`);
hand edits apply within two seconds. Accounts, aliases and routines stay in
`custom.json`.

### Menu bar

The glyph is the state: ○ asleep · ● listening · ◐ thinking · ◈ working on the
screen · ◉ speaking · ⏸ microphone off · ⊘ disconnected. The menu has **Type to
Mint… ⌘J**, **Wake now**, the settings above, **Grant Accessibility…**,
**Forget conversation history**, **Open log** and **Quit**.

## How it works

Two models, split by what each is good at. **Jev** is a classifier: it picks one
option from a list and returns a calibrated probability, and it cannot invent an
answer outside that list — the property you want when something is about to
click on your screen. **Gemini Live** reasons, converses, hears and speaks. So
Gemini decides *what* to do, and Jev finds *which control on screen* does it.

Every tool sits in a speed tier, and Gemini is told to use the fastest that fits:

| Tier | Tools | Cost |
|---|---|---|
| 0 | open app / site / folder, scroll, keys, typing, selection, clipboard, volume, media, timers, calendar, reminders, notes, email, status | direct system calls, milliseconds |
| 1 | `desktop(goal)` — click, fill a form, choose a menu item, pick a result | ~1s per step, through Jev |
| 2 | `look()` — a screenshot for Gemini to inspect | on demand only |

### Privacy

- **While asleep, nothing leaves the Mac.** The microphone feeds only a small
  on-device wake word model (about 1ms per 80ms of audio). Tested: unrelated
  speech put zero audio in the send queue.
- **While Mint works on the screen, the room is ignored.** In testing, a
  conversation nearby was transcribed as a new request mid-action and Mint
  redid finished work. During a tool, the mic goes back to the wake word model;
  "Hey Mint" still cuts in.
- A typed `--say` request does not open the microphone.
- Pause listening turns the microphone off entirely.
- The Gemini key lives in `.env` (mode 600). Email is draft-only. Shell access is
  off unless you pass `--allow-shell`.

## Running from the terminal (development)

```sh
./run.sh --hands-free      # the same as the app, from this checkout
./run.sh                   # always listening, no wake word
./run.sh --text            # type instead of speaking
./run.sh --grant           # ask for Accessibility for this terminal
```

Options: `--wake-word alexa|hey_mycroft|hey_rhasspy`, `--wake-threshold 0.7`,
`--sleep-after 20`, `--half-duplex` (close the mic while it speaks), `--no-hud`,
`--no-ui`, `--voice Puck`, `--model …`, `--allow-search`, `--verbose`,
`--demo` (the animation tour). If it ever hangs, `kill -USR1 <pid>` writes every
thread's stack to the log.

The installed app runs from `~/Library/Application Support/Mint`, so re-run
`./install.sh` after changing code. Its log is `~/Library/Logs/Mint/mint.log`.

## Configuration

`.env` (see `.env.example`):

| Variable | Default |
|---|---|
| `GEMINI_API_KEY` | required |
| `MINT_MODEL` | `models/gemini-3.8-live` (stronger on long tasks, ~4 s to a tool call) |
| `MINT_FALLBACK_MODEL` | `models/gemini-3.1-flash-live-preview` (used on a quota error, or when the main model keeps failing to connect - e.g. repeated "1011 internal error"; Mint tries the main model again every 10 min while asleep) |
| `MINT_VOICE` | `Zephyr` |
| `MINT_WAKE_WORD` | `hey_mint` |
| `MINT_SLEEP_AFTER` | `12` |
| `MINT_ALLOW_SEARCH` | off |
| `MINT_ALLOW_SHELL` | off |

## Things learned the hard way

- **Model and quota.** `gemini-3.8-live` once refused this key with "exceeded your
  current quota"; by 24 Sep it had quota (AI Studio: unlimited RPM, 65K TPM) and is the
  default, falling back to `gemini-3.1-flash-live-preview` on a quota error. On the
  ChatGPT → Codex task 3.1-flash opened chat.google.com for "ChatGPT" and invented the
  research it failed to read; 3.8 did it right. The Live models accept AUDIO output
  only; asking for TEXT is rejected. Free-tier Flash models allow ~20 requests a day
  each, so summaries and grounding try the lite models first.
- **A synthetic key inherits held modifiers.** A key event made without a source takes
  the current modifier state, so a Return right after a ⌘V went out as ⌘Return: Chrome
  opened addresses in background tabs, and ChatGPT dropped the typed prompt. Every key
  event now sets its flags explicitly.
- **Google Search grounding needs billing.** With it enabled, a free key's session
  is refused outright even though everything else in the config connects. It is
  off by default; `--allow-search` turns it on.
- **Desktop Voice's log updates headline and detail separately**, so a new
  command's first line still shows the previous command's detail. The bridge
  only trusts completion once a cycle has run for the current command.
- **Desktop Voice cannot see a web page scroll** — the title, focus and controls
  do not change — so it reports "no visible effect" on scrolls that worked (a
  pixel diff showed 34% of a page changing). The bridge drops that false negative.
- **Opening returns before the window is ready.** Screen actions within two
  seconds of an open wait for the remainder.
- **Without Accessibility, macOS drops synthesised keys silently.** Instant key,
  scroll and typing tools then route through Desktop Voice, which has the
  permission, instead of reporting a success that did not happen.
- **A key window is not enough to get the keyboard.** The console panel became
  key but keystrokes still went to the app in front; Mint has to become the
  active app while the console is open, and macOS's cooperative activation
  sometimes refuses a background app even after its own hotkey, so it then
  brings itself forward through Accessibility. Closing the console hands
  activation back to the app you were in.
- **A typed request once went unanswered** until Mint quit (the session loop
  was idle, not stuck). Typed turns now go as realtime text, after an explicit
  end of the microphone stream, and falling asleep ends the stream too. The
  cause was not pinned down; later typed requests were answered.
- **Apps in protected folders stall at launch.** Code under Downloads or Documents
  makes a new app wait on a privacy prompt before Python can even start. The app
  runs from Application Support for this reason.

## Visible or invisible in screen sharing

By default Mint is invisible to screen capture: in a Google Meet or Zoom share,
a recording, or a screenshot, people see your screen without the orb, the
chat, the click sparks or the marks Mint draws. To show someone Mint, make it
visible:

- say "Mint, be visible" / "show yourself on the screen share" (and "hide from
  the share" to undo),
- click the eye button in the chat header (eye = visible, crossed eye = hidden),
- or tick "Visible in screen sharing" in the menu bar / orb right-click menu.

While visible, a small pulsing red dot sits on the orb so you know the meeting
can see it. The setting is remembered (`share_visible` in settings.json).

Mint still never sees itself: while it is visible, it hides for the split
second of each screenshot it takes to read or click (about 0.08 s - in a share
it blinks out for a moment), so its own orb and captions never confuse it.

## Showing you things, and moving around

**Show on screen.** "Find the deadline in this PDF", "where does it mention
refunds?", "underline the part you mean" -> `show_on_screen`. Mint reads the
screen, scrolls the window a page at a time until it finds the words (down,
then back up), waits for the scroll to settle, and marks the exact words - not
the whole line - with a box, underline, highlighter, hand-drawn circle or
arrow, plus a short note. The orb flies over beside the mark and looks at it,
then goes home. It works in any app: PDFs, browsers, chats, documents, code. If
you say where ("in Preview", "in the browser"), that app is brought forward
first. For things without words (a chart, an image) it looks and uses
`mark_area`. Marks are click-through and invisible to screenshots.

**Moving the orb.** "Go a little down", "move up a lot", "you're covering
that" (hops to the other end of the edge), "go to the middle", "go back" ->
`move_orb`; the spot is kept, like a drag. "Circle around the window" sends it
once round the front window - easing in, slower round the corners, faster along
the edges. Every move is a real path flown at 60 frames a second: it swoops in
a curve, overshoots a touch and settles, leans into the direction it travels,
stretches when fast, squashes and wobbles when it lands, and looks where it is
going.

**Tricks.** Ask "do a trick", "show off", or name one: hops (hops along and
bounces back), loop (a loop-the-loop), figure eight, bounce (like a ball), zigzag
(a bumblebee flight), chase (a star appears and it chases and catches it), peek
(darts out and looks around), spin (a spinning hop). Each ends back home with a
little expression. Now and then - roughly once in ten minutes, only while you
have left the computer alone for a few seconds - it does a random one on its own
("wander": false in settings.json turns that off).

## Critter helpers and cute flourishes

**Sub-agents are little creatures.** When Mint hands work to a sub-agent, a
small gift box appears right beside the orb. It wiggles, the lid pops, and a
critter in the agent's colour jumps out in a spinning arc, lands with a squish
and waves. The critters are original designs, not any game's characters:
Astra is an owlet, Luna a bunny, Codex a sprout, Sage a fox kit. Any other
agent gets a kitten, chick, baby dragon or bear cub, never the same as one
already out. They stay in a row next to the box (on top of the chat when it is
open) and never wander across the screen.

| What the agent does | What its critter does |
|---|---|
| starts | pops out of the box, shows its name |
| thinks | a "..." bubble over its head |
| uses a tool | the bubble shows it: magnifier (search), globe (web page), pencil (writing), terminal (commands)... |
| saves a file | a hop, a paper icon and a few stars |
| asks you something | a "?" bubble; it hops for attention and its tag says "click me" |
| gets your answer | a happy hop and hearts |
| finishes | happy eyes, a somersault and confetti, then walks back and hops into the box |
| fails / is stopped | a sad face and a tear, or a startled "!", then home |
| calls another agent in to help | the helper pops out of the lead critter, a little smaller, stands right beside it, and hops back into it when done |

**Hover** a critter to see who it is and what it is doing. **Click** it and it
reacts (hop and heart, spin, or a giggle) and shows its status. Click one that
has a question, or double-click any, to open the chat. **Right-click** to open
the chat or stop that agent. **Click the box** and everyone waves. Poke one
five times quickly and it gets dizzy. The box vanishes when the last critter
is home.

**Mint's own actions get a flourish too**, a different one for each kind:
stars fan out when an app opens, a paper plane takes off for a web page, a
magnifier circles while searching, letters float up while typing, chevrons
bounce for scrolling, papers flutter for files, an envelope flies off with a
heart for mail, music notes for sound, `{ }` for code, a camera flash for a
screenshot. Hearts when something worked, a tiny rain cloud when it did not,
and a paw print where each click lands.

**Showtime.** Say "put on a show", "party time" or "perform for me" and the
whole critter family - Hoot the owlet, Bun the bunny, Kit the fox kit, Pip the
chick, Ember the baby dragon, Mochi the kitten (and Sprout and Teddy the bear
cub if the others are busy as agents) - pops out of the toy box one by one. They
do a stadium wave, then a solo each, then dance together (hops and spins to the
beat, music notes), jump for a confetti finale and take a bow while Mint claps,
then hop back into the box. About 20 seconds. Any real agent critters out at the
time dance along.

**Every species has its own move**, seen in its solo, now and then while it
waits, and sometimes when you click it: the owlet tilts its head ("hoo hoo!"),
the bunny flops an ear and thumps, the sprout whirls its leaves and lifts off,
the fox kit chases its tail, the kitten does a long stretch, the chick pecks,
the baby dragon puffs a tiny flame ("rawr!"), the bear cub waves for a hug.

Both are in the menu: "Cute critter helpers (sub-agents)" (takes effect after
a restart; off brings back the small moons) and "Cute action flourishes".

## Expressions, teasing and voices

The orb has a face and little hands, and can show how it feels. Say it -
"smile", "clap", "dance for me", "show me a heart", "cry", "wave", "put your
sunglasses on" - and Gemini calls `express`. It also uses them on its own now
and then: blushing when praised, a thumbs-up when a task went well, a wave
hello and goodbye.

| Expression | What happens |
|---|---|
| smile, laugh, wink | happy squint eyes, rosy cheeks, a bounce or giggle |
| love, kiss | heart eyes and floating hearts; a blown kiss flies off |
| blush | shy: pink cheeks, shrinks and tilts away |
| cry | droopy brows, tears falling, a sniffly wobble |
| angry | frowning brows, red flush, a huff of smoke |
| surprised | big round eyes, an O mouth, a jump and a "!" |
| sleepy, dizzy | Zzz rising; X eyes with stars circling |
| cool, thinking | sunglasses slide down with a glint; a "?" and a sideways look |
| wave, clap, praise | a mitten hand waves; hands clap with sparkles; thumbs-up and stars |
| dance, yes, no | sways, hops, hands up in turn, music notes; nods; shakes its head |

**Teasing works too.** Click it twice quickly and it giggles (the first click
still opens the chat) - keep poking and it gets surprised, dizzy, then cross.
Rest the pointer on it and it gets shy; flick the pointer on and off and it
reacts; circle it and it gets dizzy.

Everything is drawn on top of the orb from `emotes.py` - hand-made shapes (the
eyes, tears, glasses, mitten hands are paths drawn in code) plus Apple SF
Symbols for hearts, stars, music notes and the thumbs-up.

**Voices.** Gemini Live has 30 prebuilt voices (Zephyr, Puck, Charon, Kore,
Fenrir, Leda, Aoede, Sulafat…). "Use a deeper voice", "sound more cheerful",
"talk slower", "switch to Puck" → `set_voice`. It is saved, and applied within
seconds by reconnecting with the session's resumption handle, so the
conversation carries on (tested: a resumed session remembered an earlier
secret word in its new voice). Gemini cannot clone or train a voice from a
recording; a voice plus a speaking style is the closest it offers.

The same choice is in Settings ▸ **Mint's voice**: all 30 voices (filter by
female / male), a Listen button (one sentence in that voice and style, made by
a short live session and cached in `voice/previews/`, played on the chosen
speaker), and a style box with presets. A change there reconnects the same way,
once Mint has finished speaking.

Why only 30: the Voices API (`GET v1beta/voices`) lists about a thousand more
(120 of them Indian English, e.g. `en-in-advisor-1`), but they are for the TTS
models. Tested on `gemini-3.8-live`: any of them - and a made-up name - is
accepted without an error and spoken in the default voice. Compared with the
CAM++ speaker model, a male "en-in-advisor-1" came out as Zephyr (0.69, the
same as Zephyr against itself), while Puck really differs (0.3).
`bench/live_model_probe.py` checks that each live model answers and how fast.

## Skills: what Mint has learned to do

A skill is a short how-to - the steps that worked for a kind of task, with the
gotchas - kept as a Markdown file you can read and edit:

```
skills/
  apps/chatgpt/send-a-prompt-in-a-specific-chatgpt-project.md
  apps/zcode/start-a-new-task-in-zcode.md
  coding/claude-code/resume-a-session.md
  _archive/                      removed skills, kept just in case
```

- **Choosing is Jev's job.** Before a multi-step task Gemini calls `find_skill`;
  Jev picks the one skill that covers it from the real list (or says none
  fits) in about a second, and Gemini follows its steps. If Jev is slow or
  unreachable, a strict word-match fallback still finds a clear winner.
- **Learning happens by itself.** Every turn and tool result is watched. When a
  stretch of work ends and it was hard - six or more steps, a failure followed
  by a route that worked, or you correcting it ("no, click the project in the
  sidebar first") - a Flash model writes the skill from what actually
  happened: only the steps that worked, generalised (`<project name>`), with
  your corrections as notes. If a skill for that kind of task already exists,
  it is updated instead of duplicated.
- **You can teach it directly.** "Save how to do that as a skill", "next time
  use the sidebar", "update the ChatGPT skill: …" - Gemini calls
  `create_skill` / `update_skill`. `list_skills` and `delete_skill` (archived,
  not destroyed) do what they say.
- **Ranked by results.** After using one, Gemini reports `skill_result`; each
  file keeps uses / wins / fails, and skills are ranked new → learning →
  reliable (or shaky, if they keep failing).
- **Filed in folders.** New skills go into a category (`apps/chatgpt`,
  `coding/claude-code`, `browser/google-docs`…); Jev files a new skill under an
  existing folder when one fits.
- **No secrets.** Anything that looks like a password, API key or card number
  is refused, in skills and in memories alike.

### Sub-agents: Mint as orchestrator

Mint (Gemini Live) talks with you, routes work and keeps memory. Longer jobs go to
named **sub-agents** that work in the background, each with its own colour, model,
tools and instructions (`agents.json` next to the package; edit it, or just ask:
"create an agent called Nova that writes tweets, make it pink").

| Agent | For | Usual thinking |
|---|---|---|
| Astra (violet) | web research, sourced briefs | medium |
| Luna (teal) | RL environments and code | medium |
| Sage (amber) | long documents, PDFs | low |
| Codex (green) | building sites, apps, scripts in OpenAI Codex | low (medium for bigger builds) |

Every sub-agent runs on **OpenAI GPT-6 Luna** and nothing else; Mint's own voice stays on
Gemini Live. It goes through **OpenRouter** (`openai/gpt-6-luna`, `OPENROUTER_API_KEY`) when that
key works, otherwise **straight to OpenAI** (`gpt-6-luna`, `OPENAI_API_KEY`) - Mint checks both
keys at startup and logs `[agents: provider keys …]`. Direct OpenAI calls use the **Responses
API**: Chat Completions refuses function tools together with a reasoning effort for gpt-6-luna
("use /v1/responses or set reasoning_effort to 'none'"); each step continues from the previous
response (`previous_response_id`), so the model keeps its reasoning between tool calls.
Thinking (OpenRouter `reasoning.effort`) is set per task, never above medium:
Mint picks none / low / medium when it delegates; if it doesn't, a rule does - medium
for research, design, debugging or RL work, none only for clearly mechanical asks
(rename, list, translate…), otherwise the agent's usual level. `reasoning_details`
are passed back on tool turns so the model keeps its chain of thought.

Mint delegates **only when an agent is truly needed** (multi-step research, long
documents, building code or RL environments, work that produces files). Quick
answers, single Mac actions and anything on screen it does itself; `delegate_task`
requires a one-line `why`, and tasks under six words are refused as "do it yourself".
Several tasks run in parallel (`delegate_tasks`; a second task for a busy agent gets
its own folder), and results that land together reach Mint as one message. If
OpenRouter refuses the key, runs stop at once and Mint tells you to make a new key.

How it flows: Mint calls `delegate_task`, the agent runs its own tool loop
(web search, read pages, read/write files in `~/Documents/Mint/agents/<name>`,
PDFs, Jev classification, and opt-in `run_command`).
- **Questions:** when an agent needs something it asks; Mint wakes up if asleep,
  asks you in its own words, and passes your answer back (`answer_agent`).
- **Steering:** change your mind mid-task ("tell Luna to use pytest") and Mint sends
  the new instructions (`message_agent`); the agent adapts at its next step.
- **Results:** when it finishes, Mint tells you the outcome, the chat shows it, and
  it goes into memory. `agent_status`, `stop_agent`, `list_agents` and `create_agent`
  do what they say.

Keys in `.env`: `OPENROUTER_API_KEY` (all agents), and optionally `EXA_API_KEY` or
`TAVILY_API_KEY` for better web search (without them a free DuckDuckGo fallback is used). Models that answer 429 are benched 5 minutes, 503s 30
seconds, and an agent retries after 3, 8 and 15 s before giving up.

**Agents work as a team** (`mint/agents/team.py`). Mint gives a job to the one agent best placed
to lead; the lead brings in teammates itself when their specialty helps - Sage writing a report
asks Astra for the research, Luna asks Codex to build or run something - and reports back for all
of them ("Teammates who helped: Astra (…)"). Supervisor pattern plus a shared blackboard, as in
Anthropic's multi-agent research system:
- Agent tools: `ask_agent` (one teammate, waits for its result), `ask_agents` (several in
  parallel), `share_note` / `read_board` (the mission's team board, also `board.md`; notes reach
  teammates working right now at their next step), `list_team`.
- Folder: the lead's workspace is the mission root; helpers work in `helpers/<name>/` and can
  read everything in the mission, so nobody overwrites anybody.
- Limits: 2 levels below the lead, 6 agents at once, no asking an agent already up your own chain
  (no loops), a helper stopped after 20 minutes; stopping the lead stops its helpers. Mint can
  restrict helpers: `delegate_task … helpers: ["Astra"]` or `["none"]`.
- Memory: every finished run goes to `agents-experience.jsonl`; a new run starts with the 3 most
  relevant earlier results of the whole team and what Mint knows about the user. Long runs shorten
  old tool results past ~120k characters.
- Speed: independent tool calls in one step (several searches or page reads) run in parallel,
  and one keep-alive HTTP client serves every model call. Agents are told to scale effort (a fact:
  3-6 tool calls; a brief: 10-15).
- `agent_status` shows the tree (who is helping whom); hub events carry `parent`.

Verified 27 Sep: Sage -> Astra (JWST exoplanet brief, 80 s); Luna -> Astra + Codex (moon counts
script, 58 s; Astra 22 s with parallel searches); stop Sage -> Astra stopped too; helpers none ->
no team tools; live through Mint: "ask Sage … get the research from Astra first" -> brief in ~1.5 min.

**Codex** is not a model loop but OpenAI Codex itself: Mint runs `codex exec` from inside
the ChatGPT app (`/Applications/ChatGPT.app/Contents/Resources/codex`, signed in with your
ChatGPT account, GPT-6 Luna, workspace-write sandbox) in a new folder per build under
`~/Documents/Mint/agents/codex/<topic>`. Its JSON events drive the orb (thinking, running a
command, a file written). A change mid-run stops the turn and resumes the same Codex session
with it; `message_agent Codex …` after it finished resumes it too, so follow-ups keep the
project. The stand-alone `codex` CLI (0.145) is refused for gpt-6-luna with a ChatGPT
account; the app's (0.155) works. `delegate_task … attach_window: true` hands an agent the
exact text of the window in front, link URLs included (e.g. ChatGPT's answer with its
sources) instead of the voice model retyping it.

**On screen:** each agent is a critter that pops out of a toy box beside the orb and stays there (see "Critter helpers and cute flourishes"); a helper called in by another agent pops out of its lead. The menu switch "Cute critter helpers" off brings back the earlier minimal look: small moons circling the orb. What each is doing and its result also go to the chat.

Verified live (typed requests to the running Mint): Astra researched and wrote a
sourced brief; Sage's question "should it rhyme?" was relayed and answered; Luna,
told mid-task to reverse the order, delivered `count.py` printing 5 4 3 2 1.

## Chat: compact, clear, new session

The chat's **⋯** button (and the same words by voice or typed) offers:

- **Summarise & compact session** - a Gemini Flash model summarises the conversation
  (asked / done / failed / still open) into a card in the chat, and Mint restarts its
  live session from that summary alone, like compacting: the context is small again,
  but Mint still knows what happened. Verified: a fresh session answered "the magic
  number was 58, and I created a ChatGPT project called Mint test" from the summary
  only. Say "summarise our conversation" or "compact the session".
- **Clear chat** - empties the chat window; long-term memory is kept, and cleared lines
  do not come back after a restart (a marker in history.jsonl).
- **New session (fresh start)** - folds the conversation into long-term memory, then
  reconnects with no context carried over. Say "start a new session".

## Memory: one fact per block, looked up on demand

What the assistant knows about you lives in `memory/bank.json` (readable copy:
`memory/MEMORY.md`), one fact per block, in groups: core, people, work,
accounts, schedule, preferences, places, vocabulary, misc.

- **Fixed memories** (core, vocabulary, or anything you mark fixed: "make that a
  fixed memory") go into every conversation.
- **Everything else stays out of the prompt** until needed. `recall(question)`
  sends every block to Jev in ONE request, as an independent yes/no per block,
  and returns only those it marks relevant. In testing: "who's my boss?" ->
  just the manager; "standup time and the incident channel?" -> exactly those
  two; "what's 2+2" -> nothing; about a second each.
- **Writing is filed and de-duplicated by Jev** in the same single request:
  the group, and whether the new fact replaces an old one ("my manager is now
  Meera" replaced "…Rahul" instead of adding a contradiction). About a second.
- **Automatic**: the background summariser also asks Flash for durable facts
  from each conversation and adds them the same way.
- **Context pack**: on the first action of every request, Jev's pick of skill
  and memories is fetched in parallel with the action and attached to its
  result, so Gemini gets them without asking and without waiting.
- **Continuity**: the last few turns (within six hours) are given to every new
  session, so after a restart "what was the launch date again?" still works.
- Tools: `remember`, `recall`, `update_memory`, `forget`, `list_memories`.
  Passwords, keys and card numbers are refused.

### Seeing and editing skills and memory

**Skills & memory…** in the menu bar menu (and the orb's settings menu), the
**Skills & Memory** tab in Settings, or just asking ("show me your skills", "let
me see what you remember") opens one window with two tabs:

- **Skills** - every skill by category with its record (worked / used, and new ->
  learning -> reliable, or shaky). Pick one to edit its title, when to use it,
  apps, category (moving it to another folder), and its steps and notes as
  text; ⌘S saves. **New** starts a blank skill; **Delete** archives it to
  `skills/_archive` (recoverable); **Show in Finder** opens the file.
- **Memory** - every fact in a table. Tick **Fixed** to give it to every
  conversation; double-click the group or the fact to edit it; changes save at
  once. **Add** files a new fact (Jev picks the group and replaces an older
  version of it, or choose the group yourself); **Delete** asks first. **Test
  recall** shows exactly which facts Jev would hand over for a question.

Edits by hand are exact (no model in the loop) and the same secret filter
applies: passwords, keys and card numbers are refused. The window re-reads both
stores whenever it comes to the front, so what the learner adds in the
background shows up.

### Checking it still works

`.venv/bin/python bench/skills_memory_bench.py` runs the Jev-decided parts offline
(no screen): 12 skill-choice cases (including "no skill fits"), 6 recall-precision
and 3 update/replace cases on a scratch memory bank, and 7 app-name cases. Last run:
skills 100%, memory 100%, apps 100%, about a second per Jev call.

### Every app is reachable

ChatGPT (the Codex-based app), ZCode, Slack, VS Code and Claude are Electron
apps. Until an assistive client asks, Chromium builds only a sliver of their
accessibility tree (ZCode: 31 controls), which is why the desktop tool used to
give up on them. Mint now sets `AXManualAccessibility` on any Electron app
before acting in it, so the full tree is there for Jev and Desktop Voice.

- `type_text` finds the right box itself: the app's main prompt/message box, or
  the one named in `field` ("search box", "project name"), chosen by Jev from
  the boxes actually in the window.
- `open_app` resolves names against installed apps: exact, known names ("vs
  code"), partial names, one-letter mishearings ("Xcode" → ZCode), then Jev.
  It never opens something that is not installed.

## The harness: files, web, browser, menus, scrolling, scripts

`mint/harness_tools.py` adds its own tools, researched from five open-source Mac
agents kept in `ref/` (gitignored): aura, browser-use's macos-harness,
computer-harness, MacOS-Use and Mark-XXXV. The ideas were taken; the code is Mint's own.

- **Files** - `read_file` (text, code, PDF, Word/RTF/HTML), `write_file` (create,
  append, replace-once, overwrite with a backup), `find_files` (Spotlight by name
  and content, recent files, kinds), `file_action` (open, reveal, info, move, copy,
  rename, make folder, Trash only when asked). Secrets and system folders are off limits.
- **Web** - `web_search` (Exa/Tavily/DuckDuckGo) and `read_url`, no browser needed.
- **Browser** - `browser` is full browser control in Chrome/Safari/Brave/Arc/Edge:
  tabs (list, switch, new, close), go/back/forward/reload/wait, and inside the page
  read, links, find, click, fill, select, scroll, js. Page JavaScript over AppleScript
  when allowed, the accessibility tree when not (Chrome's default); fields are filled
  by setting their value, with no keystrokes. No password fields; no send, submit, buy,
  delete or closing tabs unless asked. `bench/browser_js_bench.py` checks the page
  scripts in headless Chrome. Also `group` (named tab groups: "YouTube and anime as
  Entertainment"), `toolbar` (an extension's button by name) and `rule` (which browser
  for what). `open_url` / `new_tab` pick the browser by itself (`mint/browser_choice.py`):
  the one you name, else your rules and remembered preferences (entertainment → Brave),
  else the one in front; opening another browser first is stopped.
- **Autopilot** (`mint/autopilot.py`) - several things in one request are done back to back: after a
  turn that left the request half done (plan steps left; "all / each / one by one" not reported
  finished; fewer actions than things listed; "next I'll…" / "shall I continue?"), the session tells
  the model to carry on. Never while something is running, after a send, while a sub-agent waits
  for an answer, or after STOP. `bench/autopilot_bench.py`.
- **pointer** - what is under your mouse ("click this", "what am I pointing at");
  `click_at` now reports what it hit.
- **Menus** - `menu` clicks or lists any menu-bar command by path, with shortcuts,
  and understands Show/Hide toggles.
- **scroll_to** - brings a named thing into view, pane by pane, with accessibility
  or on-screen text (System Settings' SwiftUI sidebar hides its text from
  Accessibility), or scrolls one named pane.
- **wait_for_text**, **run_applescript** (no shell escapes; send, delete and quit
  only when asked).
- **Guards on every call** - no typing into password fields; a `[Loop warning]` on
  the third identical call with the same result, or four failures in a row.
- **Shown on the orb** - files, search, pages, menus and scripts each turn the orb
  into their own glyph; nothing pops up elsewhere.

The prompt gives Gemini a background-first ladder: web → files → browser → AppleScript
(no pointer) → menu → scroll_to → ui_act → wait (foreground) → pointer → look + click_at
(last), and forbids saying "I can't" before trying. Checks:
`bench/harness_bench.py` (99/99); live `--script` runs for the browser, menus and
scroll_to; typed Gemini requests for web search, files, menus and the browser.
See GUIDE.md section 6 for examples.

## Clicking like a person: grounding

`ui_act(action, target, text)` is how Mint clicks and types in app windows.

1. **Inventory** - every visible control in the front window from the
   accessibility tree (Electron/Chromium apps unlocked first): role, label,
   exact box, enabled, focused, and whether it is in an open dialog or menu.
   ChatGPT: about 1,100 nodes in 0.15-0.7 s, 73 controls kept.
2. **Choose** - exact label with roles deciding (a button beats a heading with
   the same words; an open dialog or menu is the only thing that can be used),
   then Jev over a shortlist, then a vision model that picks a *numbered box*
   on a marked screenshot. It never invents coordinates.
3. **Act** - bring the app forward (checked; see below), make sure no other
   window covers the spot, move the pointer there, then a real mouse press.
   Chromium ignores Accessibility "press" actions, so the desktop tool's clicks
   there did nothing.
4. **Verify** - diff the window before and after; FAILED if nothing changed.

Benchmark (`--tool ground_bench '{"mode":"eval","case":"chatgpt","state":"main","app":"ChatGPT"}'`,
18 plain-language queries on the ChatGPT window, nothing clicked):

| method | hits | mean |
|---|---|---|
| full pipeline | 18/18 | 0.85 s |
| numbered boxes + gemini-3.5-flash-lite | 18/18 | 2.8 s |
| Jev on the element list | 15/18 | 0.85 s |
| raw pointing (robotics-ER coordinates) | 0/18 | 7.8 s |

**Bringing an app to the front.** From inside Mint (a background app), with
another app in front, macOS refused `NSRunningApplication.activate`,
`AXFrontmost`, `open -a`, an osascript subprocess and pressing the Dock icon.
An in-process AppleScript `activate` worked 5/5. `ground.bring_forward` uses
it and verifies; `open_app` now fails loudly if the app is still not in front.

## Talking over Mint

Audio runs through one AVAudioEngine with macOS voice processing - the echo
canceller FaceTime uses. Because the microphone and the speaker share the
engine, macOS removes Mint's own voice from what the mic hears, so the mic
stays open while Mint speaks. Start talking and it stops at once (after about
0.3 s of speech; room noise and a cough do not count) and listens. Gemini's
server also notices and stops generating. `--half-duplex` restores the old
behaviour of closing the mic while it speaks.

## Memory

Every turn is appended to `history.jsonl` (mode 600). Once enough new
conversation builds up - and whenever Mint quits - a Gemini Flash model folds
it into `summary.md` in the background: preferences, people and things, what
was done, what is still open, standing instructions. Every new session starts
with that summary, so Mint remembers across restarts. "Forget conversation
history" in the menu deletes both files. Gemini Live also compresses its own
context within a session.

## One engine, one interface

Desktop Voice (jev-use) runs as a hidden engine: `defaults write local.jev-use
Headless -bool YES`, which Mint sets. Headless, it has no menu bar item, no
widget, no hotkey and no audio visualiser; it reads its TypeSafe key from
Mint's `.env` instead of the Keychain (which asked for the login password
after every rebuild); and Mint starts it only the first time a screen action
needs it.

## Long tasks

For three or more steps Gemini calls `plan_task` first; the HUD shows "Step
3/5" with a progress bar, and every tool result reminds Gemini what is in front
and which step is next. Before opening anything Mint checks what is already
open and reuses it (`list_open`, `switch_to`); new tabs open in the window being
worked in. New Google Docs are named from their first line so "that doc" means
one document.

Across apps:
- `wait_until_done` waits for an app to finish (ChatGPT answering, a page loading): it
  watches that window's text through Accessibility until it stops changing and no Stop
  button ("stop answering", "stop streaming") is left. Up to 20 s it holds the call;
  longer, it returns and a "screen watcher" message tells Mint when it is done. A window
  that never changed is reported as "not working on anything - the prompt may not have
  been sent", never as done.
- `preview_site` serves a built folder on `http://localhost:8000` (127.0.0.1 only),
  opens it, checks HTTP status, title and missing local files, and confirms the browser
  in front shows it.
- Typing into a chat box with `press_return` clicks its Send button when it has one and
  checks the message went (a reply starting, or the text in the conversation). The same
  message is never sent twice within two minutes.

Verified (24 Sep, typed request, Gemini 3.8 Live): "open ChatGPT in the Acme Lab,
research X with sources, give it to Codex on Luna to build a one-page site, open it on
localhost and check it" - done in about 3.5 minutes (Voyager, honeybees), the page citing
ChatGPT's real sources. Mint then wrote skills for asking ChatGPT, building with Codex,
and the chained workflow.

## Settings (menu bar ▸ Settings…, ⌘,)

- **You** - the assistant's name (a new name gets its own "Hey <name>" wake
  word, built on the Mac in about a minute from the Mac's voices; "Hey Mint"
  keeps working until it is ready), your name, and "about you" (given to it at
  the start of every conversation).
- **Mint's voice** - which of the 30 Gemini voices it speaks in, with a
  Listen button, and a speaking style (see "Voices" above).
- **Your voice** - train, retrain or forget your voice; voice lock on/off;
  strictness (relaxed / balanced / strict shifts every threshold by -0.05 / 0 /
  +0.06); ignore things said to other people.
- **Audio** - microphone and speaker (or the system defaults), echo
  cancellation, and stepping aside for calls (below).
- **Appearance** - theme, position, spoken replies, face, effects, captions.

## Sharing the microphone and speakers

- **Echo cancellation** (Apple's voice processing, what lets you talk over
  Mint) turns every other app down while it runs. Mint sets that ducking to
  its minimum and lifts the extra flat -15 dB macOS adds for voice units.
  "Automatic" turns echo cancellation off entirely with headphones or AirPods -
  there is no echo to cancel - so then nothing else is touched at all.
  "Off" never touches other apps; the mic then closes while Mint speaks.
- **Calls and meetings** - every 1.5 s Mint asks CoreAudio which apps are
  capturing the mic (listeners on that do not fire on current macOS). When
  Zoom, Teams, FaceTime, Webex, Slack, a browser (Meet), WhatsApp, Discord,
  Skype, Telegram, Loom, OBS, QuickTime or Voice Memos is, Mint stops talking
  and lets go of the mic and speaker completely; three seconds after it
  stops, Mint takes them back. Other apps can be added in Settings ▸ Audio. An
  app that merely keeps a mic open (the Claude app's helper did, on this Mac)
  does not count.
- **It no longer goes deaf.** The audio engine stops itself whenever the
  audio setup changes; Mint used to stay silent after that. Now it rebuilds,
  and a watchdog restarts a microphone that delivered nothing for 3 s.
- **Choosing devices** works in every combination: with echo cancellation one
  voice unit drives both (Apple's rules for it: both set, before start);
  without it, one engine records and another plays, since a single engine
  without voice processing cannot use different input and output devices.

## Only your voice

### "Hey Mint"

There is no ready-made "Hey Mint" model, and an open-vocabulary keyword
spotter (sherpa-onnx, trained on GigaSpeech) caught only 21 of 36 test phrases
and missed the Indian English voices almost entirely. So Mint has its own: a
small classifier over openWakeWord's speech features (`wake.MintWake`),
trained on "Hey Mint" from twelve macOS voices at four speeds against everyday
speech, lookalikes ("hey man", "peppermint", "hey Mindy") and real LibriSpeech
speakers (`bench/train_hey_mint.py`). Shipped model, on held-out data: 5/6
phrases, 0/12 lookalikes, 0 false wakes in 21 minutes of unseen speakers. It
costs one dot product per 80 ms.

### Train my voice (menu bar ▸ Train my voice…)

Two minutes, once: say "Hey Mint" eight times, then read eight sentences. Mint
then

- builds your **voiceprint** with a CAM++ speaker model (3D-Speaker's "common
  advanced", 28 MB, ~9 ms per check, run on the onnxruntime Mint already loads,
  with Kaldi-standard filterbank features), with thresholds calibrated on your
  own held-out speech and raised above the Mac voice closest to yours;
- **retrains "Hey Mint" on your recordings** (in testing: 5/5 of the user's
  phrases, 1/40 lookalikes, 0 false wakes in 47 minutes of other people);
- shows a live test: each thing said is marked "that's you" or "not you".

### The voice lock

**Whose "Hey Mint" was that?** A short phrase scored against a voiceprint made
from long sentences is weak evidence: the first enrolment set that threshold at
0.29 and other people's "Hey Mint" got in. Now the phrase is also compared with
your own eight recorded "Hey Mint"s (same words, same voice - 0.91-0.98 for the
user against ~0.43 for other voices), with thresholds calibrated at enrolment
against "Hey Mint" in the Mac's twelve built-in voices. A clear match wakes
Mint; a clear mismatch is ignored; anything in between holds the phrase and
decides on the next second of speech. Your enrolment audio is kept on the Mac
(`voice/enrolment.npz`, mode 600) so later calibration fixes need no retraining.

**Goodbye.** "Bye", "bye Mint", "good night", "that's all", "chalo bye",
"alvida"... send Mint to sleep on the Mac itself - Gemini's transcript turned
"bye Mint" into "by mint" and "by means" and it kept talking. The moon button
in the chat, "Go to sleep" in the menu bar and on the orb's right-click menu do
the same.

With a voiceprint, only your speech reaches Gemini. The start of each
utterance is held on the Mac until it is verified (0.8-1.6 s), then sent in one
burst while the rest streams live; anything else is replaced by silence. Only
your "Hey Mint" wakes it, and only your voice can talk over it (another voice
merely turns it down while it checks).

On 26 real LibriSpeech speakers (`bench/voicelock_bench.py`): 0 of 48 other
speakers' clips leaked and all the enrolled speakers' clips were let through;
equal error rate 1.3% / 0.6% / 0.0% at 0.8 / 1.2 / 2.0 s of speech. The model
and its features mattered: sherpa-onnx's packaging of the same model scored
2.4 / 1.3 / 0.3%, and three other public speaker models 6-46%. With a voice
deliberately close to the user's (two Indian English system voices,
`bench/wake_check_bench.py`), the look-alike was blocked (0.51 against a bar
the enrolment raised to 0.75, user 0.87). End of speech to first sound
measured 1.9-2.8 s with the lock and 2.1-3.3 s without. Memory: about +52 MB
while the lock is on.

### Talking to Mint, or to someone else?

Only you get through, but you also talk to people. The first thing you say
after "Hey Mint" is always for Mint; for follow-ups, Jev classifies the words
("said to the assistant" / "said to someone else"), using what Mint last said
and how loud you were compared with your enrolment. Anything to do on screen
waits for that verdict (about a second); a reply that was not meant for Mint is
cut off and nothing is done. In testing: 9/10 correct on mixed examples, and
"Rahul, did you finish your lunch?" was ignored while "and what is the date
today?" was answered. "Ignore talk not meant for Mint" in the menu turns it
off.

### Your words, English and Hindi

Names from `custom.json` (accounts, workspaces, channels, aliases, routines,
"about me") plus `"vocabulary"` in `settings.json` are given to the
transcriber as custom vocabulary and to the model as words to expect. When you
correct a mishearing ("no, I said on-call"), Mint remembers it under the topic
"vocabulary", and the correction is part of every later session. Transcription
is set to Indian English and Hindi, the model is told to understand only those
(Hinglish included) and to answer in the language you used, and turns
transcribed in any other script are ignored.

### "Hey Mint" is cut out of what Gemini hears

The wake phrase is recognised on the Mac; Gemini only needs what follows. It
used to get the phrase too, and transcribed it as "payment" nearly every time
("payment I want you to open anime") - then answered about a electricity payment.
Now `wake.phrase_cut` finds where the phrase ended - where the detector's
scores began to rise, then the first 40 ms of quiet within -50..+300 ms, else
+150 ms - and only the audio after that is sent (`bench/wake_trim_bench.py`,
on the user's own recordings: median 60 ms early, 35 of 37 within 120 ms). In
the real session with real Gemini (`bench/wake_cut_session_test.py`, the
user's "Hey Mint" + their sentences): "payment" 4 of 4 times before, 0 of 4
after. If a scrap still leads the first turn after a wake, `hearing.strip_wake`
drops it from the transcript (never mid-sentence: "pay my electricity payment" is
kept), and the model is told such a scrap is the wake phrase.

### Teaching it words it gets wrong

Say so: "I said Aman, not Amen", "it's KubeCon, not cube con". The model calls
`fix_hearing(heard, meant)`; the pair is saved in `settings.json`
`"hearing_fixes"` and used three ways from then on: your words are rewritten
as they arrive (on screen, in memory, and for the goodbye / stop /
talking-to-me checks), the model is told the pair at every connect, and the
right word joins the transcriber's custom vocabulary. "Forget the correction
for Amen" removes one. (`hearing.py`)

### A voiceprint from an older version rebuilds itself

Training saves your recordings (`voice/enrolment.npz`, mode 600). When the
speaker model changes and an old voiceprint no longer fits, Mint rebuilds the
voiceprint and the personal "Hey Mint" from those recordings at start-up
(~35 s, in the background) and says so, instead of turning the lock off and
asking you to retrain. On 24-27 Sep the lock sat off for three days this way;
the recordings were on disk the whole time.

## Mint Ear: unloading when idle (off by default, experimental)

**Off since 27 Sep 18:42** (`unload_after_minutes: 0`): in the user's first hour
with it the listener missed "Hey Mint" twice (near misses scored 0.31-0.35 against
0.85), Mint unloaded 2 minutes after being opened (fixed: opened on purpose now
counts as used), and the user preferred ~270 MB that always works. With it off,
Mint.app's executable is just the launcher (~8 MB) plus Mint, as before, and a
crashed Mint is restarted (at most 3 times in 5 minutes). Turn it on in Settings ▸
Appearance ▸ Free memory to try it again.


Phones listen for "Hey Google" / "Hey Siri" with a tiny detector and load the
assistant only when it fires. Mint does the same with two processes:

* **Mint** (Python, ~270 MB): everything - Gemini, the orb, tools, the voice lock.
* **Mint Ear** (Mint.app's own executable, `launcher/ear/*.swift`):
  * the parent (~13 MB): starts Mint as its child (as the plain launcher did,
    so macOS still asks for permissions as "Mint"); while Mint is unloaded it
    shows the same menu-bar glyph, the orb as Mint left it (a picture Mint took of
    itself, breathing), keeps ⌘J and passes on `mint --say`;
  * the listener, `Mint --ear-helper` (~56 MB): the microphone - through voice
    processing and Mint's own 48 -> 16 kHz averaging, exactly as Mint hears, or
    the voice lock does not know the voice (with a plain tap and Apple's
    converter the user's live "Hey Mint" scored 0.45-0.49 against a 0.5 bar and
    was rejected four times in a row; 23 MB that way) - and the same
    "Hey Mint" detector - openWakeWord's two ONNX models run through the ONNX
    Runtime library already in Mint's Python environment (`Ort.swift`, the C API
    reached by slot number; every slot was read back by name from the library),
    and the same logistic regression. Its scores match the Python detector to
    2e-6 on the user's own recordings. It runs only while Mint is unloaded.

The flow:

1. Mint asleep and idle for `unload_after_minutes` (Settings ▸ Appearance ▸
   Free memory; default 20, 0 = never) - and no console, Settings or other
   window open, no task, timer, sub-agent or training - takes the orb picture,
   folds the conversation into memory and exits with code 75.
2. The parent shows its orb and glyph and starts the listener.
3. "Hey Mint": the listener starts Mint and hands over the last 3 s before the
   wake word plus everything after, over a Unix socket (`ear.sock`,
   `mint/ear.py`). ⌘J / the orb / the menu / `mint --say` do the same with a
   different reason (console, settings, say).
4. Mint replays that audio through its normal asleep path - the same wake word
   model, voice lock and phrase cut - so a request said while it loads is heard
   whole. When the user pauses (0.9 s of quiet audio, at most 12 s) it tells the
   listener STOP; the listener lets go of the microphone and exits - releasing
   all its memory (a process that merely freed its models stayed at ~30 MB) - and
   only then does Mint start its own: a second voice-processing reader turns the
   first one's audio into digital silence (measured: zeros 0.7 s into a sentence).
5. Mint exits 0 on Quit: the Ear quits too. Any other exit (a crash): the Ear
   logs it and listens, and the next wake starts Mint fresh.

Measured on this Mac (MacBook Air, `footprint`, the Activity Monitor number):

| | before | now |
|---|---|---|
| asleep, after the idle time | ~270 MB | **~69 MB** (parent 13 + listener 56) |
| in use | ~270 MB | ~284 MB (Mint 271 + parent 13) |
| "Hey Mint" to awake, cold | - | ~1 s (Mint started 17:56:05, awake 17:56:06) |
| end of the request to its transcript | ~1 s | ~1 s (the words said while loading are kept) |
| listener CPU | - | ~8% of one efficiency core (the models alone: 1 ms per 80 ms) |

Voice processing cancels whatever the Mac itself plays, so a recording played
from its speakers no longer wakes it (the earlier live tests did that, before
the listener used voice processing); a person speaking does.

Tests: `bench/ear_handoff_test.py` (Mint's side, real Gemini, the user's
recordings: full sentence, normal and fallback), the fake-Mint cycle test
described in memory.md (6 cycles, no memory growth), and live on the installed
app: wake by the user's recorded voice from the speakers, `mint --say`, the
open-console signal (`local.mint.open`), a killed Mint (the Ear took over),
Quit (both gone), microphone off (no listener).

## Known limits

- Talking over Mint relies on macOS voice processing; if it is unavailable,
  Mint falls back to closing the mic while it speaks.
- The voice lock needs about a second of speech to be sure, so a one-word
  "stop" from you takes ~0.5 s longer to land than a full sentence.
- The voice lock is a filter, not a password: a recording of your voice passes.
- One screen action at a time.
