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
| Watching or editing a web video | The video's address | The video site (via yt-dlp) |

Read each provider's terms: Google's for the Gemini API, OpenAI's and TypeSafe's. Some free tiers
may use your data to improve their models. Paid tiers usually don't.

## You are in control

- "Stop" halts everything at once. "Go to sleep" stops listening until the wake word.
- Meeting and screen recordings start only when you press record.
- Dictation listens only while you hold the key.
- Mint is invisible in screen shares unless you ask it to show.
- Never sends email (drafts only); messages, posts and forms go out only when you asked.

## Contact

Questions or concerns: open an issue, or contact the Maintainer through
[github.com/shivatmax](https://github.com/shivatmax).

*Last updated: 29 September 2026.*
