# Changelog

## Unreleased

- A developer handbook ([docs/development.md](docs/development.md)): running from a checkout,
  where new code goes, testing, building the Mac app and releasing.
- Fix: Mint no longer refuses to start without Desktop Voice (jev-use). It is optional now: when it
  is missing, the `desktop` tool is hidden and Mint uses its own `ui_act`. This made the 0.1.1 app
  unusable on Macs without it.
- `run.sh` no longer needs the optional Desktop Voice app.

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
