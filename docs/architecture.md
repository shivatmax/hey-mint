# Architecture

How Hey Mint is put together. For what it can do and what to say, see the
[README](../README.md), the [usage reference](usage.md) and the [guide](https://hey-mint.pages.dev/).

## The big picture

```
 microphone ─▶ wake word (on device) ─▶ voice lock ─▶ Gemini Live ─▶ speaker
                                                        │   ▲
                                              tool calls│   │results
                                                        ▼   │
                            mint.tools ── mint.screen ── mint.knowledge ── mint.agents
                                                        │
                                                        ▼
                                  mint.ui: the orb, captions, chat, marks, critters
```

One process, one conversation. **Gemini Live** listens, speaks and decides which tool to
call. **Jev** (a fast chooser) picks from real lists wherever a guess would be risky: which
control to press, which skill fits, which memories matter, which app was meant. Long jobs go
to **agents** that run in the background and report back.

## Packages

| Package | Responsibility | Key modules |
|---|---|---|
| `mint.app` | Startup, the Live session, lifecycle | `main` (entry point, flags), `session` (audio in/out, tool calls, wake/sleep, reconnects), `autopilot` (finishing multi-part requests), `control` (stop), `power` (quit, login item), `ear` (hand-over to the low-memory listener) |
| `mint.core` | Shared configuration and services | `config` (keys, models, the system instruction), `prefs` (settings.json), `custom` (custom.json), `jev`, `hotkeys` |
| `mint.voice` | Listening and speaking | `engine` (AVAudioEngine with voice processing), `wake` + `features` (the "Hey Mint" detector), `voicelock` + `enroll` (speaker verification), `hearing` + `vocab` (words it gets wrong), `voices` |
| `mint.knowledge` | What Mint knows | `conversation` (history and rolling summary), `memory` (one fact per block, Jev recall), `skills` (Markdown how-tos by category), `learner` (writes skills from experience) |
| `mint.tools` | Everything Gemini can call | `registry` (declarations and dispatch), `harness` (files, web, browser, menus, scrolling, AppleScript), `everyday` (calendar, reminders, notes, mail drafts), `clipboard` (screenshots and clipboard), `apps`, `browser_choice`, `workspace`, `work` (long tasks), `extra` (skills, memory, expressions, marks, moving the orb) |
| `mint.screen` | Seeing and acting on the screen | `axkit` (Accessibility), `ground` (finding the control you meant), `ocr` (on-device text recognition), `vision` (screenshots for the model), `pointer` (finding words and marking them) |
| `mint.ui` | Everything drawn | `presence` (menu bar and wiring), `hud` + `orb` (the orb, its face and morphs), `chat`, `settings`, `brain` (skills and memory window), `effects` + `flourishes`, `emotes`, `motion` (60 fps paths and tricks), `marks`, `critters` (agents as creatures), `sharing` (screen-share visibility) |
| `mint.agents` | Background agents | `runtime` (the hub and each agent's tool loop), `registry` (agents.json), `providers` (OpenAI / OpenRouter), `codex` (OpenAI Codex runner), `team` (agents asking agents), `orchestrator` (the tools Gemini uses to delegate) |

`launcher/` holds Mint.app's Swift launcher and **Mint Ear**, which keeps the wake word
running in about 70 MB while the Python process is unloaded, and the **screen-control engine**
(`launcher/engine`, adapted from [jev-use](https://github.com/savka777/jev-use)). The engine
ships inside Mint.app as `MintEngine`; `mint.tools.desktop` starts it as a child of Mint on the
first `desktop` goal, so it shares Mint's Accessibility permission, and it quits with Mint.
It reads the front app's Accessibility tree, lets Jev choose each action, performs it and
checks the result, step by step until the goal is met.

## How a request flows

1. **Wake.** `mint.voice.wake` scores 80 ms frames on device. With a trained voice,
   `mint.voice.voicelock` checks the speaker before any audio leaves the Mac.
2. **Understand.** Audio streams to Gemini Live (`mint.app.session`). The phrase "Hey Mint"
   itself is cut out so it is not transcribed as a word.
3. **Act.** Gemini calls tools (`mint.tools.registry`). Direct routes come first: menus and the
   accessibility tree, AppleScript, the browser's own scripting, file and web APIs. The pointer
   moves only when nothing else can do the job (`mint.screen.ground`), and every action is
   checked by comparing the window before and after.
4. **Show.** `mint.ui` turns each tool call into an orb morph, a caption and a small flourish.
   None of it appears in Mint's own screenshots.
5. **Remember.** Turns go into the conversation history; facts into memory; tasks that took
   many steps or needed correcting become skills (`mint.knowledge.learner`).

## Safety by construction

- Password fields are never typed into, and secrets are refused by the file, memory and skill
  writers.
- Sending, submitting, deleting and quitting need an explicit request, and the same message is
  never sent twice within two minutes.
- AppleScript cannot run shell commands; the shell tool is off unless `MINT_ALLOW_SHELL=1`.
- "Stop" (`mint.app.control`) cancels queued clicks and keys immediately.

## Data on your Mac

The installed app runs from `~/Library/Application Support/Mint`: keys (`.env`, mode 600),
`settings.json`, `custom.json`, `agents.json`, `memory/`, `skills/`, `voice/` (your enrolment)
and the conversation history. Logs go to `~/Library/Logs/Mint/mint.log`. None of this is in
the repository.
