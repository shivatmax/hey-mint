# Security policy

Hey Mint can see your screen, press keys and read files, so security reports matter a lot.

## Reporting a vulnerability

Please **do not open a public issue**. Report it privately:
[Security ▸ Report a vulnerability](https://github.com/shivatmax/hey-mint/security/advisories/new).
Include what an attacker could do, how to reproduce it, and the version you tested (Settings ▸ Updates & Help
shows it, or the commit for a build from source).
You will get a reply within a week.

## Supported versions

Security fixes go into the latest release, currently **0.5.x**. Older versions don't get fixes; the downloaded
app updates itself (see [Updates](README.md#updates)).

## What counts

Anything that lets Mint be made to act against its safety rules: typing into password
fields, sending messages or email without being asked, deleting instead of trashing,
reading private folders (keys, keychains, browser profiles, Mail, Messages), leaking keys
from `.env`, prompt injection from web pages, documents or emails that leads to such actions,
an emailed or Telegram request accepted from someone who isn't allowed, or the voice lock
accepting someone else's voice.

## What leaves your Mac

There is no Hey Mint server, no account and no analytics. Everything below goes straight from your Mac to
the service, with **your own** keys or accounts, and only when the feature is in use. While Mint is asleep, no
audio leaves the Mac: the wake word and the voice lock run on it.

- **Google Gemini** (your Gemini key; required). The Live voice models get your voice and words once Mint is
  awake (after the wake word, a shortcut or a typed request), screenshots or screen text when a task needs them,
  and the memory facts relevant to the conversation. Gemini's text models handle dictation, meeting notes, video,
  translations, documents and other background work, with the audio, frames or text involved.
- **Other AI providers, only if you add their key** (Settings ▸ Accounts & keys, Settings ▸ Models & agents):
  OpenAI (background agents and Codex), Anthropic, OpenRouter, Groq, xAI or a custom OpenAI-compatible endpoint
  get the task you hand an agent and the files or pages it works on. Ollama runs on your Mac.
- **TypeSafe Jev** (only with a TypeSafe key). Short requests and lists to choose from: the names of controls in
  the app in front, apps, skills, memories, on-screen text, and a few lines of the conversation (for example, to
  tell whether words heard without the wake word were meant for Mint). No screenshots.
- **Telegram** (only if you add your own bot token and switch it on). Requests and voice notes you send your bot,
  and Mint's steps, replies, screenshots and files for those requests, through Telegram's Bot API. Voice notes
  are transcribed by Gemini.
- **Your mail provider** (only if you set up email control). Mint reads the mailbox it watches over IMAP (or
  through Apple Mail) and sends its answers over SMTP, straight to your provider; nothing in between. The app
  password is kept in the macOS Keychain. Only messages whose subject starts with the prefix are read past their
  headers.
- **Google Meet** (only during a call you ask for). Mint's own Chrome profile joins the meeting; the Mac's screen
  is shared to the call, Mint's voice goes into it, and the call's audio and chat come back to Mint (and to Gemini
  as its microphone). You sign in to Google in that profile yourself; Mint never types the password.
- **GitHub** (the downloaded app's updater, at launch and every 12 hours; off with Settings ▸ Updates & Help).
  It asks GitHub's API for the latest release, reads its `latest.json`, and downloads the DMG and its `.sha256`.
  No data about you is sent (GitHub sees your IP address, as with any download).
- **The web, when a task needs it.** Searches (DuckDuckGo, or Exa or Tavily with your key), the pages and videos
  you ask about (videos through yt-dlp), and the daily briefing's weather (wttr.in) and headlines (Google News).

## What stays on your Mac

- **Keys:** `.env` in the data folder (mode 600). Passwords and secrets you ask Mint to keep, and the email app
  password, go into the macOS Keychain.
- **The data folder:** `~/Library/Application Support/Hey Mint` for the downloaded app,
  `~/Library/Application Support/Mint` for a build from source. It holds settings (`settings.json`), memory
  (`memory/`), learned skills (`skills/`), conversation history (`history.jsonl`, `summary.md`), and your voice
  print for the voice lock (`voiceprint.json`, with the enrolment recordings in `voice/`).
- **Feature files** in `~/Library/Application Support/Mint` either way: clipboard history, file backups made
  before Mint changes a file, the guard's log, the Telegram and email request log (`remote.log`, never a
  password), connectors, and the Google Meet Chrome profile.
- **Logs:** `~/Library/Logs/Mint/` (`mint.log`, and `tools.log` with every tool call and its result).
- **Meeting notes, recordings and files Mint makes:** your Mint folder (`~/Documents/Mint` unless you chose
  another).

Text recognition of screenshots and documents uses Apple's Vision framework on your Mac. More detail, per
feature: [PRIVACY.md](PRIVACY.md).
