<div align="center">

# Hey Mint

**An open-source, Jarvis-style voice assistant for macOS.**
Say “Hey Mint” and it talks back, clicks and types in your apps, browses, manages files,
marks things on your screen, and hands long jobs to a family of background agents.

[**Illustrated guide**](https://hey-mint.pages.dev/) ·
[Install](#install) ·
[What it can do](#what-it-can-do) ·
[How it works](#how-it-works) ·
[Development](#development) ·
[Contributing](CONTRIBUTING.md)

![License: GPL-3.0](https://img.shields.io/badge/license-GPL--3.0-2EC4B6)
![Platform: macOS](https://img.shields.io/badge/platform-macOS-172033)
![Python](https://img.shields.io/badge/python-3.11%2B-8B7CFF)
![Voice: Gemini Live](https://img.shields.io/badge/voice-Gemini%20Live-FFB547)

</div>

<br>

https://github.com/user-attachments/assets/26b80d2a-2451-4fef-a194-42d6552577b5

<p align="center">
  <sub>▶ <b>The 1-minute film</b> · unmute for sound · also on <a href="https://youtu.be/mWFT-IIiCaI">YouTube</a></sub>
</p>

<br>

<p align="center">
  <img src="docs/media/talk.gif" alt="Asking Mint to open Slack: captions, the orb turning into the Slack icon, a check mark, and the spoken reply" width="560">
</p>

## What it can do

Mint is a small orb with a face that lives in the corner of your screen. Ask in your own
words, by voice or by typing (<kbd>⌘J</kbd>):

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
| “do a trick”, “show me a heart”, “put on a show” | Expressions, 60 fps tricks, and a critter family that performs |

<p align="center">
  <img src="docs/media/mark-box.gif" alt="Mint boxing the deadline in a document and flying over beside it" width="560">
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
| OpenAI API key (asked for on first launch) | Background agents: research, documents, code |
| The [ChatGPT app](https://openai.com/chatgpt/desktop/) (it brings Codex) | The agent that builds websites and code projects |
| TypeSafe key (asked for on first launch) | Jev: the `desktop` tool (multi-step clicking and typing in any app, by the engine built into Mint) and surer picks of which button, skill or memory is meant |

1. Download the latest **Hey-Mint-…-arm64.dmg** from [Releases](https://github.com/shivatmax/hey-mint/releases/latest)
   (Apple silicon Macs, macOS 14.2 or later). Everything it needs is inside: no Python, Homebrew or Xcode.
2. Open the DMG and drag **Hey Mint** into **Applications**, then open it.
3. First time only: if macOS says it cannot check the app for malware, open **System Settings ▸ Privacy &
   Security** and click **Open Anyway**. (Release builds are not notarized by Apple yet.)
4. Paste your free [Gemini API key](https://aistudio.google.com/apikey); the OpenAI and Jev keys are optional.
   Hold <kbd>⌥</kbd> while opening Hey Mint to change keys later.
5. Allow the **Microphone**, and switch Hey Mint on under **Accessibility** when asked, so it can click and type.

Your keys, settings, memory and skills live in `~/Library/Application Support/Hey Mint`. To update,
replace the app with a newer one; your data stays.

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
| `OPENAI_API_KEY` or `OPENROUTER_API_KEY` | Background agents (GPT-6 Luna) |
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
   88 tools on the Mac   Jev (fast choices from     Agents (GPT-6 Luna, Codex)
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

How the pieces fit together is in [docs/architecture.md](docs/architecture.md); every command and
tool is in [docs/usage.md](docs/usage.md); working on Mint is in [docs/development.md](docs/development.md).

## Privacy and safety

- Never enters passwords or card numbers, never fills password fields, never stores
  secrets in files, skills or memory.
- Never sends email (drafts only); messages, posts and forms go out only when you asked.
- “Delete” means the Trash; files are backed up before being overwritten; private folders
  (keys, keychains, browser profiles, Mail, Messages) are off limits.
- Invisible in screen shares by default (“Mint, be visible” to show it in a Google Meet).
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
launcher/      Mint.app's launcher, the low-memory Mint Ear, and the screen-control engine (Swift)
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
| [CI](.github/workflows/ci.yml) | every push and pull request | lint and offline tests on macOS |
| [Release](.github/workflows/release.yml) | a `v*` tag | builds, signs (if set up) and publishes the DMG |
| [Guide](.github/workflows/cloudflare.yml) | changes to `guide/` on `main` | deploys [hey-mint.pages.dev](https://hey-mint.pages.dev/) (needs the Cloudflare secrets) |

## Contributing

Issues and pull requests are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md) and
[docs/development.md](docs/development.md), and see the [code of conduct](CODE_OF_CONDUCT.md).
Security issues: [SECURITY.md](SECURITY.md).

## License

[GNU General Public License v3.0](LICENSE). You may use, study, share and modify Hey Mint;
if you distribute a modified version, it must stay open source under the same license.

Built with [Gemini Live](https://ai.google.dev/), [openWakeWord](https://github.com/dscripka/openWakeWord),
[3D-Speaker CAM++](https://github.com/modelscope/3D-Speaker) via
[sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx), [PyObjC](https://pyobjc.readthedocs.io/),
and Apple's Accessibility, Vision and Core Animation frameworks. Hey Mint is an independent
project, not affiliated with Apple, Google or OpenAI.
