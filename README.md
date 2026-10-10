<div align="center">

# Hey Mint

**An open-source, Jarvis-style voice assistant for macOS.**
Say “Hey Mint” and it talks back, clicks and types in your apps, browses, manages files,
marks things on your screen, and hands long jobs to a family of background agents.

[**Illustrated guide**](https://hey-mint.pages.dev/) ·
[What's new](#new-in-05) ·
[Install](#install) ·
[What it can do](#what-it-can-do) ·
[How it works](#how-it-works) ·
[Development](#development) ·
[Contributing](CONTRIBUTING.md)

[![CI](https://github.com/shivatmax/hey-mint/actions/workflows/ci.yml/badge.svg)](https://github.com/shivatmax/hey-mint/actions/workflows/ci.yml)
![License: GPL-3.0](https://img.shields.io/badge/license-GPL--3.0-2EC4B6)
![Platform: macOS](https://img.shields.io/badge/platform-macOS-172033)
![Python](https://img.shields.io/badge/python-3.11%2B-8B7CFF)
![Voice: Gemini Live](https://img.shields.io/badge/voice-Gemini%20Live-FFB547)

</div>

<br>

https://github.com/user-attachments/assets/165b3c07-b49e-4b4e-9660-080de831b36c

<p align="center">
  <sub>▶ <b>Hey Mint</b>, the launch film (1:51) · unmute for the voiceover · also on <a href="https://youtu.be/dmxK-wFhuow">YouTube</a></sub>
</p>

<br>

<p align="center">
  <img src="docs/media/talk.gif" alt="Asking Mint to open Slack: captions, the orb turning into the Slack icon, a check mark, and the spoken reply" width="560">
</p>

## New in 0.5

- **Google Meet calls with Mint.** Say “start a Google Meet” (or send `/meet` on Telegram, or email it). Mint
  creates a meeting, joins it, shares your Mac's screen and sends you the link. Join from your phone and talk to
  Mint as on a phone call while you watch it work, or type in the call's chat. [Guide](https://hey-mint.pages.dev/docs#meet)
- **Control Mint by email.** Give Mint an address to watch (a separate Gmail account is best) and mail it a request
  whose subject starts with `Mint:`. Only senders on your allowlist are acted on (or anyone who includes your secret
  word), and only when the mail server confirms the sender is genuine. The answer comes back in the same thread,
  with the steps, files and screenshots. [Guide](https://hey-mint.pages.dev/docs#email)
- **Checks on your coding agents' work.** Mint reads Claude Code's and Codex's own test output, so "did Claude's
  tests pass?" gets the real answer ("2 of 48 failed, expected 3, got -1; changed invoice.ts since"), not the agent's
  word. Risky steps, retries and two agents on one file are flagged, Telegram's done messages carry the verdict, and
  "hand this to Codex" passes the work on with a note. An optional fix loop sends Claude back when it tries to finish
  or push with failing tests, and Claude Code or Codex can ask Mint to check their work themselves (MCP).
  [Guide](https://hey-mint.pages.dev/docs#agent-checks)
- **An easier first run.** The welcome window has a Connect page: it takes you to Google AI Studio for a free
  Gemini key, and when you paste it, Mint checks it with Google before saving it. [Install](#install)
- **A pool of voice models.** Mint finds the Gemini Live models your key can use and moves to the next one when one
  is slow, near its per-minute limit or failing, then moves back when the better one is ready. When your words
  show on screen but no answer follows, Mint tells the voice service you have finished and, if needed, sends your
  words again as text, so a question no longer goes unanswered. [Guide](https://hey-mint.pages.dev/docs#talking)

Every change, release by release, is in the [changelog](CHANGELOG.md).

## What it can do

Mint is a small orb with a face that lives in the corner of your screen - or, on a MacBook,
right in the camera notch like the iPhone's Dynamic Island. Ask in your own words: say
“Hey Mint”, hold <kbd>fn</kbd> <kbd>⌃</kbd> and talk, or type (<kbd>⌘J</kbd>). It changes shape for what's
going on: a meeting recorder, your day's schedule, a lesson, a video it's watching, an answer
as a card - and every card has a × to close it.

| Ask | What happens |
|---|---|
| “open Figma”, “switch to the Jira tab” | Opens or switches apps, windows and browser tabs |
| “click Sign in”, “type ‘see you at six’”, “read this page” | Finds controls by name through Accessibility, presses them like a person, checks it worked |
| “find the PDF about the lease and summarise it” | Spotlight search, reads PDFs / Word / Markdown / CSV, writes and edits files (with backups) |
| “put YouTube and anime in a group called Entertainment” | Full browser control: tabs, forms, tab groups, extension buttons (Chrome, Safari, Brave, Arc, Edge) |
| “where's the deadline in this PDF?” | Finds the words on screen, scrolls to them, and boxes, underlines, highlights, circles or points at them |
| “remind me to call Sam at five”, “what's on today?” | Calendar, reminders, notes, timers, email drafts, volume, media keys, screenshots, clipboard |
| “have Astra research the best keyboards and write a brief” | Background agents (research, code, documents, websites) that ask questions, team up and report back |
| “remember my manager is Meera”, “save how to do that as a skill” | Long-term memory and self-learned skills, both editable |
| “watch this video: what's the aesthetic?”, “what did he say at 3:20?” | Understands a video (link, file or the one on screen) in seconds from its transcript and a few keyframes, with timestamps |
| “every weekday at 9, brief me on my calendar and mail”, “when a PDF lands in Downloads, file it” | Automations: schedules, folder and app triggers, reminders before meetings |
| *(on a call, press ● on the orb)* “what did we decide in the Acme call?” | Meeting notes with no bot: the orb turns into a recorder (mic and video switches), then a transcript, decisions and action items; “what's been said so far?” mid-call |
| *hold fn ⌃ and talk* | Talk to Mint without the wake word: let go and it works on what you said |
| *hold fn and talk* | Dictation, Wispr-Flow style: let go and clean text is typed where your cursor is (fillers dropped, corrections applied, Hindi or English) |
| “let me know when this download finishes”, “tell me when the Claude session is done” | Trackers: downloads, Claude Code sessions, Terminal commands, uploads in any window, files; Mint speaks up the moment they finish |
| “watch me do this once”, “show me how to export a PDF in Preview” | Learns a task from one demonstration; or guides you step by step, pointing at each control |
| “good morning, brief me”, “turn on the living room lights”, “find the screenshot with the invoice number” | A spoken daily briefing, your Apple Shortcuts (Home scenes, Focus, music), screenshots found by what's in them |
| “translate this page”, “triage my email”, “draft a reply saying I'll send it Friday” | Translations written over the original, in its own colours; inbox sorted by what needs you; replies drafted in your style (never sent) |
| “OCR this and copy it”, “make an Excel of this table”, “convert this English PDF to a Hindi Word doc” | Text off any screen or scan to the clipboard; tables to a clean .xlsx; documents translated and converted with headings, lists and tables kept |
| “make it vertical for Reels and add captions”, “remove the silent parts”, “compress it under 25 MB” | Video editing by voice: planned by Gemini, rendered with ffmpeg next to the original, then checked (QA) |
| “brighter”, “Chrome left, Slack right”, “turn on Do Not Disturb”, “record just this part of the screen” | Your Mac's switches (brightness, keep awake, Focus, Night Shift, windows via Rectangle) and screen recordings of the screen, a window or an area you describe |
| *drag a file onto Mint* | Drop files, folders or text on the orb: it offers what fits (summarise, translate, spreadsheet, captions, transcribe) |
| *a Telegram message or voice note from your phone* | Remote control from anywhere through your own Telegram bot: Mint does it on the Mac and shows each step in the chat, then the reply, screenshots and files |
| “ask ChatGPT to outline the talk”, “is Claude still working?”, “tell me when Claude is done” | Drives the ChatGPT and Claude desktop apps: asks, reads replies, opens and lists chats and Claude Code sessions, and tells you when they finish |
| “open my clipboard”, “paste the last 5 screenshots in the chat”, “pin this as my address” | A clipboard that remembers everything you copy and every screenshot: Mint turns into it (⌃⌥V), pick several in order, pin up to 20, passwords and keys go to the Keychain |
| “what did I miss?”, “undo that”, “merge these folders, keep the newest” | Reads and clears your notifications; undoes what Mint just did (brightness, windows, text, files, reminders); merges folders keeping the newest copy |
| “make an illustration of a lighthouse at sunset”, “add a sailing boat”, “make me a shortcut that turns on dark mode” | Pictures drawn on your Mac by Apple's Image Playground, edited by describing a change; new Apple Shortcuts planned from plain words |
| “Hey Jarvis” (your own wake word), “always answer in Hindi” | Train a wake word of your own on your Mac in about a minute and a half; pick the language Mint answers in |
| “make this more formal”, “put these invoices in a spreadsheet”, “clean up my Downloads” | Rewrites selected text in place, turns documents into an .xlsx, tidies folders with a preview and an undo |
| “where were we?”, “what did we do yesterday?”, “what was I working on on Monday?” | Tasks that survive restarts and grow sub-steps, a journal of what happened when, and an opt-in activity timeline |
| “become the notch”, “go out of my notch” | Notch mode: Mint lives in the camera notch as a Dynamic Island - words, tasks and cards grow out of it, the notch wraps whatever opens. Switching is live: the orb flies into the notch, or drops out and bounces home |
| *look at the notch* | Its outline says the state (a light sweeping while busy, amber when an agent needs you, a green flash when done, a red shake on an error); alerts come one at a time (“+2 waiting”, Esc snoozes); it opens in one smooth move and folds 8 s after you leave, with a countdown line - sliding past never opens it |
| *in notch mode* | Scenes: a sparkly hello on the first wake of the day, an error card with a Retry button, a drop zone where Mint turns into a folder, a progress bar with Mint's face as the thumb, and a “+” to type a request right in the notch |
| *a Claude Code or Codex session* | Each agent gets its own critter face: a little cluster in the closed notch, a focus card with a rolling list of steps when open, coloured chips for the others (click one to focus it), new diff lines typing themselves in, and a compact bar under the notch with the lead agent's step |
| *just watch, or poke it* | A status badge on Mint's face (dots while working, red on an error, green when done, the word when you point at it), eyes that change shape with the mood, and pokes: a slap, three quick ones for dizzy, keep going and it's annoyed. The menu bar icon is Mint's face too |
| *Mint working in an app* | A soft Apple-Intelligence-coloured glow round the window it's working in; soft sounds made by Mint itself, quiet while you dictate or are on a call; Settings ▸ Appearance & Sound sets Full, Calm or Minimal motion (macOS Reduce motion means Minimal), each effect and each sound group |
| “do a trick”, “show me a heart”, “put on a show” | Expressions, 60 fps tricks, and a critter family that performs |
| *just talk* | Feelings from the moment: a sad face when you share bad news, a laugh at a joke, applause for good news, sunglasses when a task is done |

<p align="center">
  <img src="docs/media/notch.gif" alt="Mint in the MacBook notch: a question drops the notch down with the task, its progress bar and the reply word by word" width="560">
  <img src="docs/media/notch-switch.gif" alt="Switching looks live: the little Mint drops out of the notch and bounces home as the orb, then flies back into the notch" width="260">
</p>

<p align="center">
  <img src="docs/media/mark-box.gif" alt="Mint boxing the deadline in a document and flying over beside it" width="560">
</p>

<p align="center">
  <img src="docs/media/island-recorder.gif" alt="On a call the orb turns into a recorder: record, mic and video switches, the timer and level bars, then the notes being written" width="480">
</p>

<p align="center">
  <img src="docs/media/dictation.gif" alt="Holding Right Option to dictate: a waveform on the island, then clean text typed into the comment box" width="560">
  <img src="docs/media/drop.gif" alt="A PDF dragged onto Mint: a drop target, then a card of things to do with it" width="320">
</p>

<p align="center">
  <img src="docs/media/clipboard.gif" alt="Mint turning into the clipboard: three screenshots picked in order, then the Mint and Pinned tabs" width="320">
</p>

### Agents that pop out of a toy box

Long jobs go to named agents (Astra researches, Luna codes, Sage writes, Codex builds sites).
Each shows up as a little critter beside the orb: it thinks, shows what tool it is using,
asks you questions, calls teammates in, and hops back into the box when done.

<p align="center">
  <img src="docs/media/agent-life.gif" alt="A real agent run: Astra pops out of the toy box beside the orb and starts researching while Mint replies" width="480">
  <img src="docs/media/showtime.gif" alt="The whole critter family performing a show" width="520">
</p>

Everything is in the **[illustrated guide](https://hey-mint.pages.dev/)**, with a
short clip of every feature and a live orb on the page that acts out the examples.

## Install

### Download the app (easiest)

**What you need:** an Apple silicon Mac (M1 or later) on macOS 14.2+, an internet connection,
and a free [Gemini API key](https://aistudio.google.com/apikey). That's all: the app carries its own
Python, libraries and models, and nothing else has to be installed.

Optional extras, each unlocking one thing:

| Extra | Unlocks |
|---|---|
| A second Gemini key (Settings ▸ Models & agents) | More room: when one key hits its limit, the other takes over - for the voice and everything else |
| OpenAI, Anthropic or OpenRouter key | Other models for the background agents (they run on Gemini Flash without one) |
| The [ChatGPT app](https://openai.com/chatgpt/desktop/) (it brings Codex) | The agent that builds websites and code projects |
| TypeSafe key (Settings ▸ Accounts & keys) | Jev: the `desktop` tool (multi-step clicking and typing in any app, by the engine built into Mint) and surer picks of which button, skill or memory is meant |

**Way 1: download the app (no Terminal).**

1. Download the latest **Hey-Mint-…-arm64.dmg** from [Releases](https://github.com/shivatmax/hey-mint/releases/latest).
   Everything it needs is inside: no Python, Homebrew or Xcode.
2. Open the DMG and drag **Hey Mint** onto **Applications**. (If you open it straight from the DMG or your
   Downloads folder, Hey Mint offers to move itself into Applications: click **Move to Applications**.)
3. Open **Hey Mint** from Applications. The first time, macOS says *“Apple could not verify ‘Hey Mint’ is free of
   malware…”*. Click **Done**, then:
   - open **System Settings ▸ Privacy & Security**, scroll down to *“Hey Mint” was blocked…*, click **Open Anyway**,
     type your Mac password, and click **Open Anyway** again;
   - on macOS 14, right-click **Hey Mint** in Applications, choose **Open**, then **Open**.

   **This happens once per Mac, never again:** new versions install themselves (see Updates below), so don't
   download the DMG again. Why it asks at all: Hey Mint is free and open source, and Apple only lets an app skip this
   question if its developer pays Apple a yearly fee for notarization.

**Way 2: one line in Terminal (no question from macOS at all).** Open Terminal, paste this and press Return:

```bash
curl -fsSL https://raw.githubusercontent.com/shivatmax/hey-mint/main/packaging/install.sh | bash
```

It downloads the latest release, checks its checksum, puts Hey Mint in Applications, approves it with macOS (it
removes the “downloaded from the internet” mark from the copy it installs, exactly what **Open Anyway** does) and
opens it. The script is [`packaging/install.sh`](packaging/install.sh), short enough to read first; it is also at
`https://hey-mint.pages.dev/install.sh` (some networks block `pages.dev`, so GitHub comes first).

**Then, either way:**

1. The welcome window takes it from there (about two minutes): your name, what to call Mint and where it lives
   (in the notch or floating); then **Connect**, where you open Google AI Studio, create a free
   [Gemini API key](https://aistudio.google.com/apikey) and paste it, and Mint checks it with Google before saving
   it; then a voice, each permission with why it's needed and an Allow button (Microphone is the one it needs),
   your shortcuts, and a tour of what Mint can do. It's in the menu bar as **Welcome tour…** any time later.
2. The OpenAI and TypeSafe (Jev) keys are optional: add them in **Settings ▸ Accounts & keys**, or hold
   <kbd>⌥</kbd> while opening Hey Mint to change keys.

<p align="center">
  <img src="docs/media/onboarding.gif" alt="The welcome window: Mint says hi, asks your name, and you pick a voice" width="560">
</p>

Your keys, settings, memory and skills live in `~/Library/Application Support/Hey Mint` (some feature files,
such as the clipboard history and the Google Meet browser profile, are in `~/Library/Application Support/Mint`).
Updating or reinstalling the app keeps them.

### Updates

The downloaded app updates itself, so you approve it with macOS only once: an update is downloaded by Hey Mint
itself (not by a browser), so macOS never asks about it, and it keeps your Microphone, Accessibility and Screen
Recording permissions. At launch and every 12 hours it asks GitHub for the latest release. A newer
version is downloaded in the background and checked before anything changes: its checksum must match the
release's `latest.json`, its `.sha256` file and GitHub's own digest, and it must be signed by the same certificate
as the app you have. It is installed when the Mac has been left alone for about ten minutes: the new app takes
the old one's place, the old one goes to the Trash, and Mint reopens. Your data and permissions stay.

- **Settings ▸ Updates & Help** shows your version, turns **Update automatically** on or off, and has
  **Check for updates** and **Install** buttons. You can also ask Mint to “check for updates”.
- **Beta channel:** set `"update_channel": "beta"` in the settings file (Settings ▸ Updates & Help ▸ The settings
  file ▸ Edit…) to get pre-releases too. The default is `"stable"`.
- A build from source (below) does not update itself: `git pull`, then `./install.sh` again.

### Build from source

Requirements: a recent macOS on Apple silicon or Intel, Python 3.11+ (`python3`), the Xcode command line tools
(`xcode-select --install`), and a [Gemini API key](https://aistudio.google.com/apikey).

```sh
git clone https://github.com/shivatmax/hey-mint.git
cd hey-mint
./set-key.sh          # paste your Gemini key (stored in .env, readable only by you)
./install.sh          # builds ~/Applications/Mint.app
open ~/Applications/Mint.app
```

macOS asks for the **Microphone** first. Grant **Accessibility** from the menu bar icon so
Mint can click and type. Screen Recording, Automation, Calendars, Reminders and Files and
Folders are asked for the first time a feature needs them.

Optional keys in `.env`:

| Key | For |
|---|---|
| `TYPESAFE_API_KEY` | Jev, the fast chooser (which button, which skill, which memory), and the `desktop` tool |
| `GEMINI_API_KEY_2` | A second Gemini key: when one hits its limit, the other takes over |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` or `OPENROUTER_API_KEY` | Other models for the background agents (Gemini Flash without one) |
| `EXA_API_KEY` or `TAVILY_API_KEY` | Better web search (DuckDuckGo is used without one) |

Then say **“Hey Mint”**, or press <kbd>⌘J</kbd> and type. For hands-free use only by you,
train your voice once: menu bar ▸ **Train my voice…** (two minutes).

Personal settings live in files that are ignored by git: copy `custom.example.json` to
`custom.json` for your accounts, Slack workspaces, aliases and routines.

## How it works

```
 you ──voice──▶ "Hey Mint" detector (on device) ──▶ voice lock (only your voice)
                                                        │
                                                        ▼
                         Gemini Live  ◀── listens, talks, plans, calls tools
                             │
          ┌──────────────────┼───────────────────────────┐
          ▼                  ▼                           ▼
  104 tools on the Mac   Jev (fast choices from     Agents (Gemini Flash, Codex)
   Accessibility, menus,  real lists: controls,      research, code, documents,
   AppleScript, files,    skills, memories, apps)    websites; work as a team
   browser, Vision OCR
          │
          ▼
   the orb, captions, marks and critters (Core Animation, hidden from screen capture)
```

- **Direct routes first.** Menus, the accessibility tree, AppleScript and web APIs before
  pixels; the pointer moves only when nothing else can do the job.
- **On device where it matters.** The wake word, the voice lock and speaker checks run
  locally; nothing leaves the Mac while Mint is asleep.
- **Learns.** Tasks that took many steps or needed your correction become skills, ranked
  by how often they work.
- **Remembers well.** The memory keeps separate stores, each with its own job:
  - facts about you, which are replaced (not piled up) when they change, remember what they used to
    say, and are tidied once a day;
  - a journal of what happened when;
  - plans that outlive a restart;
  - an opt-in, text-only timeline of what was on screen.
- **Cheap to watch.** Videos become a transcript (the site's captions when there are any) and about
  12 keyframes on one contact sheet: about 6k tokens for a 15-minute talk instead of about 180k.

How the pieces fit together is in [docs/architecture.md](docs/architecture.md); every command and
tool is in [docs/usage.md](docs/usage.md); working on Mint is in [docs/development.md](docs/development.md).

## Reliability

`bench/reliability/` holds 70 realistic, long tasks (files, documents, spreadsheets, Reminders, Notes, Calendar, Mail drafts, the web, clipboard, video, memory, agents), each with its own complications - typos, locked files, pages that fail, two- and four-turn follow-ups - set up in a sandbox, sent to the running app and checked by the end state, not by what Mint says. The latest full run passed 67 of 70; the three misses were fixed since. Run it with `bench/reliability/run.py` (see its README).

## Privacy and safety

- Never enters passwords or card numbers, never fills password fields, never stores
  secrets in files, skills or memory.
- Never sends email for you (drafts only); messages, posts and forms go out only when you asked. With email
  control on, the only mail Mint sends is its answer to a request you emailed it, back to the verified sender.
- “Delete” means the Trash; files are backed up before being overwritten; private folders
  (keys, keychains, browser profiles, Mail, Messages) are off limits.
- Invisible in screen shares by default (“Mint, be visible” to show it in a Google Meet).
- Dictation listens only while you hold the key; meeting and screen recordings start only when you
  press record (a window or area is shown first and needs your yes).
- Trackers read local files only: the browser's partial download, a Claude session's own log, a
  terminal tab's running process.
- “Stop” halts everything at once.

## Project layout

```
mint/
  app/         startup, the Gemini Live session, lifecycle
  core/        configuration, preferences, Jev, shortcuts
  voice/       audio engine, wake word, voice lock, enrolment
  knowledge/   conversation, memory, learned skills
  tools/       everything Gemini can call (apps, files, web, browser, clipboard…)
  screen/      accessibility, grounding, text recognition, vision
  ui/          the orb, chat, settings, marks, critters, effects
  agents/      background agents, Codex, teams
  resources/   example skills
launcher/      Mint.app's launcher, the low-memory Mint Ear, the screen-control engine, the meeting
               recorder (MintRecorder) and the screen recorder (MintScreen) - all Swift
packaging/     building the downloadable app and DMG
guide/         the illustrated guide (static site)
docs/          architecture, usage and development handbook
tests/         offline checks (pytest)
scripts/       training the wake word model
models/        the "Hey Mint" wake word model
```

## Development

The full handbook is **[docs/development.md](docs/development.md)**: running from a checkout,
where new code goes, tests, the guide, building the Mac app, and releasing.

### Get going

```sh
git clone https://github.com/shivatmax/hey-mint.git && cd hey-mint
make setup      # .venv with the dependencies, pytest and ruff
./set-key.sh    # your Gemini key, saved in .env (never committed)
make demo       # the animation tour, no API key needed
make run        # run Mint from the checkout, logs in the terminal
```

Handy while working:

```sh
.venv/bin/python -m mint --tool get_status '{}'   # run one tool directly, no Gemini
.venv/bin/python -m mint --text                   # type to Mint instead of speaking
.venv/bin/python -m mint --say "open Notes"       # send a request to the running Mint
tail -f ~/Library/Logs/Mint/mint.log              # what Mint is doing
```

From a checkout, macOS asks for Microphone and Accessibility on behalf of your terminal. To
try your changes as the real menu bar app, run `./install.sh` and open `~/Applications/Mint.app`
again after each change. It keeps its own data, so your everyday Mint is untouched.

### Check your changes

```sh
make lint       # ruff (CI runs this)
make test       # offline checks (CI runs this)
make test-all   # also network, AppleScript and file checks
```

### Build the Mac app

```sh
make app        # dist/Hey Mint.app and dist/Hey-Mint-<version>-arm64.dmg
```

One command builds a self-contained app: a bundled Python, every library, the models and
Mint's code. People who install the DMG need nothing else. It needs an Apple silicon Mac,
the Xcode command line tools and `brew install portaudio`. Set `SIGN_IDENTITY` and
`NOTARY_PROFILE` to sign with a Developer ID and notarize ([details](docs/development.md#signing-and-notarization)).

### Release a version

Bump `version` in `pyproject.toml`, add a `CHANGELOG.md` entry, then push a tag:

```sh
git commit -am "Release 0.2.0"
git tag -a v0.2.0 -m "Hey Mint 0.2.0"
git push origin main v0.2.0
```

GitHub Actions builds the app on an Apple silicon runner and publishes it on the
[Releases](https://github.com/shivatmax/hey-mint/releases) page with install steps, in about
five minutes.

| Workflow | Runs on | Does |
|---|---|---|
| [CI](.github/workflows/ci.yml) | every push and pull request | lint and offline tests on macOS, Python 3.11 to 3.13 |
| [Release](.github/workflows/release.yml) | a `v*` tag | builds, signs (if set up) and publishes the DMG |
| [Guide](.github/workflows/cloudflare.yml) | changes to `guide/` on `main` | deploys [hey-mint.pages.dev](https://hey-mint.pages.dev/) (needs the Cloudflare secrets) |

## Roadmap

What's planned next, roughly in order:

- **Mint's hooks as a Claude Code plugin,** installable with `/plugin`.
- **A wider accuracy suite:** recorded examples for routing and the guard, next to the test-reading one.

Want one of these sooner, or something else? [Open an issue](https://github.com/shivatmax/hey-mint/issues/new/choose).

## Contributing

Issues and pull requests are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md) and
[docs/development.md](docs/development.md), and see the [code of conduct](CODE_OF_CONDUCT.md).
Security issues: [SECURITY.md](SECURITY.md).

## License

Copyright © 2026 shivatmax (github.com/shivatmax). Hey Mint is free software under the
[GNU General Public License v3.0 or later](LICENSE): use it, study it, share it and change it. If you
distribute a modified version, it must stay open source under the same licence.

- **Building Hey Mint into a closed-source product?** A commercial licence is available. Open an issue or
  get in touch through [github.com/shivatmax](https://github.com/shivatmax).
- **The name, logo and orb character** are not covered by the GPL. See [TRADEMARKS.md](TRADEMARKS.md);
  forks need their own name.
- **Contributing** needs a one-time [Contributor License Agreement](CLA.md) (a bot asks on your first
  pull request). See [CONTRIBUTING.md](CONTRIBUTING.md).
- **Privacy:** what stays on your Mac and what goes where. See [PRIVACY.md](PRIVACY.md).

Built with [Gemini Live](https://ai.google.dev/), [openWakeWord](https://github.com/dscripka/openWakeWord),
[3D-Speaker CAM++](https://github.com/modelscope/3D-Speaker) via
[sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx), [PyObjC](https://pyobjc.readthedocs.io/),
and Apple's Accessibility, Vision and Core Animation frameworks. Hey Mint is an independent
project, not affiliated with Apple, Google or OpenAI.
