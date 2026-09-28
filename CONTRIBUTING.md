# Contributing to Hey Mint

Thanks for helping! Bug reports, ideas, docs fixes and code are all welcome.

The developer handbook, [docs/development.md](docs/development.md), covers everything below
in more depth: the ways Mint runs, where new code goes, testing, building the Mac app and
releasing.

## Getting set up

```sh
git clone https://github.com/shivatmax/hey-mint.git && cd hey-mint
make setup                        # .venv with dependencies, pytest and ruff
./set-key.sh                      # your Gemini key goes into .env (never commit it)
make demo                         # the animation tour, no key needed for the UI
make run                          # Mint from the checkout, logs in the terminal
```

To test as the real app: `./install.sh && open ~/Applications/Mint.app`. It runs from a copy
in `~/Library/Application Support/Mint`, so re-run `./install.sh` after changing code.
Logs: `~/Library/Logs/Mint/mint.log`.

`.venv/bin/python -m mint --tool NAME '{...}'` runs a single tool without Gemini: the fastest
way to try a tool you are working on.

## Making a change

1. Open an issue first for anything big, so we can agree on the approach.
2. Fork, then branch from `main`.
3. Put code in the package that matches its job ([architecture](docs/architecture.md#packages));
   new tools go in `mint/tools/` ([how](docs/development.md#where-new-code-goes)).
4. `make lint` and `make test` must pass (CI runs both on macOS).
5. Open a pull request describing what changed and how you tried it. Screen recordings are
   great for anything visual.

## Before you open a pull request

- Keep changes focused; one feature or fix per PR.
- Match the surrounding style: plain names, short docstrings that say *why*.
- If you change what users see, update `docs/usage.md` and the guide (`guide/src/*.html`, then
  `make guide`). New videos: `guide/make_scenes.py` records them, `guide/encode_scenes.py` cuts them.
- If you touch packaging or the launcher, check `make app` still builds and the app opens.
- Add a line under "Unreleased" in `CHANGELOG.md` for anything users will notice.
- Never commit keys, `.env`, `custom.json`, `settings.json`, memory, voice recordings or logs.
  They are in `.gitignore`; please keep it that way.
- Safety rules are features: no entering passwords, no sending without being asked, Trash
  instead of delete. PRs that weaken them will not be merged.

## Releases

Maintainers release by bumping the version in `pyproject.toml`, updating `CHANGELOG.md` and
pushing a `vX.Y.Z` tag; GitHub Actions builds the DMG and publishes it
([details](docs/development.md#releasing-a-new-version)).

## Reporting bugs

Use the bug report template. Include your macOS version, what you said or typed, what you
expected, and the relevant lines from `mint.log` (remove anything personal first).

## License

By contributing you agree that your contributions are licensed under the
[GPL-3.0](LICENSE), the same license as the project.
