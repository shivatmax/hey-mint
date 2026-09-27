# Contributing to Hey Mint

Thanks for helping! Bug reports, ideas, docs fixes and code are all welcome.

## Getting set up

```sh
git clone https://github.com/shivatmax/hey-mint.git && cd hey-mint
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./set-key.sh                      # your Gemini key goes into .env (never commit it)
.venv/bin/python -m mint --demo   # the animation tour, no key needed for the UI
./install.sh && open ~/Applications/Mint.app
```

The installed app runs from `~/Library/Application Support/Mint`; re-run `./install.sh` after
changing code. Logs: `~/Library/Logs/Mint/mint.log`.

## Before you open a pull request

- Keep changes focused; one feature or fix per PR.
- Match the surrounding style: plain names, short docstrings that say *why*.
- `python3 -m compileall -q mint bench guide` must pass (CI runs it).
- If you change what users see, update `GUIDE.md` and the guide (`guide/src/*.html`, then
  `python guide/build.py`). New clips: `guide/make_clips.py` + `guide/encode_clips.py`.
- Never commit keys, `.env`, `custom.json`, `settings.json`, memory, voice recordings or logs.
  They are in `.gitignore`; please keep it that way.
- Safety rules are features: no entering passwords, no sending without being asked, Trash
  instead of delete. PRs that weaken them will not be merged.

## Reporting bugs

Use the bug report template. Include your macOS version, what you said or typed, what you
expected, and the relevant lines from `mint.log` (remove anything personal first).

## License

By contributing you agree that your contributions are licensed under the
[GPL-3.0](LICENSE), the same license as the project.
