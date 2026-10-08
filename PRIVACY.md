# Privacy

Hey Mint runs on your Mac. There is no Hey Mint server, no account and no analytics: the Project itself
collects nothing. What leaves your Mac goes straight from your Mac to the AI services **you** choose, using
**your own** API keys, under those services' terms.

## Stays on your Mac

- **Asleep:** the wake word ("Hey Mint"), the voice lock and speaker checks run on your Mac. While Mint is
  asleep, no audio leaves it.
- **Your data:** memory, learned skills, settings, chat history, clipboard history, dictation history,
  meeting notes and recordings are saved as local files (in `~/Library/Application Support/Mint` and
  your Mint folder). Delete them any time.
- **Passwords and keys** you ask Mint to keep go into the macOS Keychain. They are never kept in the
  clipboard history, memory or logs.
- **Text recognition** of screenshots and documents uses Apple's Vision framework on your Mac.

## Sent to services you set up

| When | What is sent | To |
|---|---|---|
| Mint is awake (after the wake word, a shortcut or a typed request) | Your voice and words, and screenshots or text from the screen when a task needs them | Google Gemini API |
| Dictation | The audio you hold the key for | Google Gemini API |
| Meeting notes, video watching and editing, translations, documents | The recorded audio, video frames or captions, and the document text being worked on | Google Gemini API |
| Background agents and Codex (optional) | The task and the files or pages it works on | OpenAI |
| Quick choices and the screen-control engine (optional) | The request, and the names of the controls in the app in front (no screenshots) | TypeSafe Jev |
| Telegram remote control (optional; off until you add your own bot token and switch it on) | The requests and voice notes you send your bot, Mint's steps and replies, and screenshots or files from those requests | Telegram (your own bot, through the Bot API; voice notes are transcribed by the Google Gemini API) |
| Email control (optional; off until you set it up) | Mint reads new mail in the address you give it (only messages from senders you allowed, with the subject prefix) and sends its replies to them | Your mail provider (IMAP / SMTP, or Apple Mail) |
| Google Meet calls (optional; only when you ask for a call) | Mint's voice, and your screen while it is shared, to the people in the call | Google Meet (in a separate Chrome window) |
| Downloading a video (when you ask) | Requests for the page and the video, as a browser would; for a signed-in video, your Chrome's cookies for that one site go to that site (kept in a private file, deleted after) | The video's own site |
| Watching or editing a web video | The video's address | The video site (via yt-dlp) |
| Checking for updates (the downloaded app, at launch and every 12 hours; off with Settings ▸ Automatic updates) | A request for the latest release, with no data about you (GitHub sees your IP address, as with any download) | GitHub |

Read each provider's terms: Google's for the Gemini API, OpenAI's and TypeSafe's. Some free tiers
may use your data to improve their models. Paid tiers usually don't.

## You are in control

- "Stop" halts everything at once. "Go to sleep" stops listening until the wake word.
- Meeting and screen recordings start only when you press record.
- Dictation listens only while you hold the key.
- Mint is invisible in screen shares unless you ask it to show.
- Never sends email on its own (drafts only). The one exception is email control, which you set up: Mint
  replies by email only to the allowed senders who wrote to it. Messages, posts and forms go out only when you
  asked.
- Watching your coding agents (Claude Code, Codex) reads their own session logs on your Mac; nothing from them
  is sent anywhere, apart from what you ask about out loud.

## Contact

Questions or concerns: open an issue, or contact the Maintainer through
[github.com/shivatmax](https://github.com/shivatmax).

*Last updated: 7 October 2026.*
