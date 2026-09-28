# Developing Hey Mint

Everything you need to work on Hey Mint: running it from a checkout, where new code goes,
testing, building the downloadable Mac app, and cutting a release. For how the pieces fit
together, read [architecture.md](architecture.md) first; for what users can say, [usage.md](usage.md).

- [Set up](#set-up)
- [Three ways Mint runs](#three-ways-mint-runs)
- [The everyday loop](#the-everyday-loop)
- [Where new code goes](#where-new-code-goes)
- [Tests and lint](#tests-and-lint)
- [The guide website](#the-guide-website)
- [Building the Mac app](#building-the-mac-app)
- [Releasing a new version](#releasing-a-new-version)
- [Troubleshooting](#troubleshooting)

## Set up

You need macOS 14.2 or later, Python 3.11+ as `python3`, the Xcode command line tools
(`xcode-select --install`), Homebrew's `portaudio` (`brew install portaudio`), and a free
[Gemini API key](https://aistudio.google.com/apikey).

```sh
git clone https://github.com/shivatmax/hey-mint.git
cd hey-mint
make setup        # .venv with the dependencies, plus pytest and ruff
./set-key.sh      # paste your Gemini key; it is checked and saved in .env (mode 600)
make demo         # the animation tour: a quick way to see the UI works (no key needed)
make engine       # optional: the screen-control engine for the desktop tool (needs a TypeSafe key)
```

`make help` lists every command.

## Three ways Mint runs

The same code runs in three shapes. They keep their data apart, so you can develop without
touching a copy of Mint you use every day.

| | From the checkout | Installed dev app | Downloaded app |
|---|---|---|---|
| Start it with | `make run` | `./install.sh`, then open `~/Applications/Mint.app` | the DMG from Releases, or `make app` |
| Code runs from | this folder | a copy in `~/Library/Application Support/Mint` | a copy in `~/Library/Application Support/Hey Mint` |
| Python | `.venv` in the checkout | a venv `install.sh` creates | bundled inside the app |
| Keys | `.env` in the checkout | `.env` in its data folder | asked for on first launch, `.env` in its data folder |
| Bundle id (permissions) | your terminal's | `local.jarvis` | `io.github.shivatmax.heymint` |
| Good for | fast edits, reading logs live | testing the real app, Mint Ear, login item | testing what users download |

macOS grants Microphone and Accessibility per app. From a checkout, **your terminal** is
the app that needs them; the installed and downloaded apps each ask for their own.

Only run one Mint at a time: two copies would both answer "Hey Mint".

## The everyday loop

1. Edit code in `mint/`.
2. Try it:
   - `make run` starts Mint in the terminal (hands-free, logs printed as it goes). Add
     flags with `.venv/bin/python -m mint --hands-free --verbose` to log every tool call.
   - `.venv/bin/python -m mint --text` lets you type instead of speak.
   - `.venv/bin/python -m mint --tool get_status '{}'` runs one tool directly, without
     Gemini, and prints the result. The quickest way to test a tool.
   - `.venv/bin/python -m mint --say "open Notes"` sends a request to the Mint that is
     already running, as if you had said it.
3. To try it as the real app, re-run `./install.sh` (it quits the running Mint and copies
   your code into the app's data folder), then `open ~/Applications/Mint.app`. Your memory,
   skills and settings stay.
4. Logs: `~/Library/Logs/Mint/mint.log` (`tail -f` it while you test).

Your own settings files are ignored by git: `.env` (keys), `custom.json` (accounts, Slack
workspaces, aliases; copy `custom.example.json`), `settings.json`, memory, skills you teach
Mint, and voice recordings. Keep them out of commits.

## Where new code goes

The packages, and what belongs in each, are listed in [architecture.md](architecture.md#packages).
Import with absolute names (`from mint.ui import motion`), like the rest of the code.

**A new tool Gemini can call.** Add its declaration to `declarations()` in
`mint/tools/extra.py` and its handler to `HANDLERS` in the same file. A handler takes the
arguments dict and returns a short string: that string is what Gemini reads back, so say
plainly what happened. Bigger families of tools get their own module in `mint/tools/` and
are registered in `mint/tools/registry.py`. Then try it with `--tool NAME '{...}'`.

**Something on screen.** `mint/ui/`: `hud.py` and `orb.py` for the orb, `effects.py` and
`flourishes.py` for small reactions to tool calls, `emotes.py` for expressions, `motion.py`
for paths and tricks, `critters.py` for agent creatures, `marks.py` for boxes and highlights.
All of it is Core Animation through PyObjC. `make demo` replays the tour so you can check animations without talking to Mint.

**An example skill.** Skills are Markdown how-tos Mint reads before a task. The ones shipped
with Mint live in `mint/resources/skills/<category>/<app>/<name>.md`; they are copied to a
user's `skills/` folder on first run. Follow the two examples: front matter (`title`, `when`,
`apps`), then `## Steps` naming real tools, then `## Notes`. Keep them general. Nothing that
only works on one person's setup.

**A background agent.** `mint/agents/`: `registry.py` holds the roster, `runtime.py` the tool
loop, `orchestrator.py` the tools Gemini uses to delegate.

**The launcher and Mint Ear** (Swift) are in `launcher/`. `install.sh` and `make app` both
compile them; there is no Xcode project.

**The screen-control engine** behind the `desktop` tool is a Swift package in `launcher/engine`
(adapted from jev-use; its [README](../launcher/engine/README.md) explains how Mint drives it).
`install.sh` and `make app` build it into the app as `Contents/MacOS/MintEngine`. For `make run`
from a checkout, build it once with `make engine` (it lands in `build/MintEngine`). It needs a
`TYPESAFE_API_KEY`; without one, Mint hides the `desktop` tool and uses `ui_act`. `launcher/ear/Packaged.swift` is the code that only
runs in the downloaded app (first-launch setup and the key dialog).

**The wake word model** (`models/hey_mint*`) is trained with `scripts/train_hey_mint.py`.

## Tests and lint

```sh
make lint        # ruff: syntax errors and undefined names
make test        # offline checks: imports of every module, autopilot rules, wake word features
make test-all    # also the checks that use the network, AppleScript and ~/Documents/Mint
```

CI runs `make lint` and `make test` on macOS for every push and pull request. The checks
are scripts in `tests/checks/` that print PASS/FAIL lines; `tests/test_checks.py` runs them
under pytest. Add a check next to the code you changed when you can.

## The guide website

The illustrated guide at [hey-mint.pages.dev](https://hey-mint.pages.dev/) is plain HTML in
`guide/`. Edit the page sources in `guide/src/*.html`, then:

```sh
make guide                                  # rebuilds guide/*.html, the sitemap and the search index
python3 guide/serve.py                      # preview at http://localhost:8765
```

The videos are recorded from the real app: `guide/make_scenes.py` plays each scene on a blank
desktop and records the whole screen at full resolution (the agent scene is a real agent run), and
`guide/encode_scenes.py` cuts sharp crops out of those recordings into `guide/media/` (needs `ffmpeg`).
`python3 guide/serve.py` previews the site with the same clean URLs as Cloudflare.

Changes to `guide/` on `main` are deployed to Cloudflare Pages by
`.github/workflows/cloudflare.yml` when the repository has the `CLOUDFLARE_API_TOKEN` and
`CLOUDFLARE_ACCOUNT_ID` secrets. Without them the job skips; deploy by hand with:

```sh
rsync -a --exclude '*.py' --exclude 'src/' guide/ /tmp/hey-mint-site/
npx wrangler pages deploy /tmp/hey-mint-site --project-name hey-mint --branch main
```

## Building the Mac app

```sh
make app
```

This builds `dist/Hey Mint.app` and `dist/Hey-Mint-<version>-arm64.dmg` (plus a `.sha256`)
that anyone with an Apple silicon Mac can install: no Python, Homebrew or Xcode needed on
their side. The first build takes a few minutes; later ones reuse `build/cache`.

What `packaging/build_app.sh` does:

1. Downloads a standalone Python 3.12 (checksum verified) and installs `requirements.txt` into it.
2. Copies `portaudio` into the app and relinks PyAudio to it (with `delocate`).
3. Fetches the wake word feature models and the voice lock model (checksum verified).
4. Trims the runtime (tests, headers, pip, unused standard library).
5. Copies `mint/`, the models and the example configs into `Contents/Resources/app`.
6. Compiles the Swift launcher and the screen-control engine, draws the icon and writes
   `Info.plist` (with the permission prompts' wording).
7. Signs every binary, then the engine, then the app, then makes the DMG.

Build requirements: an Apple silicon Mac, `python3` 3.11+, the Xcode command line tools,
`brew install portaudio`, and `git` (the build id comes from the current commit). The version
comes from `pyproject.toml`; `VERSION=0.2.0 make app` overrides it.

**What the built app does on first launch.** It copies its code and models into
`~/Library/Application Support/Hey Mint`, points `.venv` there at the bundled Python, and
asks for the API keys (only the Gemini key is required). After an update it copies the new
code over; keys, memory, skills and settings stay. Hold <kbd>⌥</kbd> while opening it to
change the keys.

**Testing a build like a new user.** Quit Mint, move `~/Library/Application Support/Hey Mint`
aside, then open `dist/Hey Mint.app`. Put the folder back afterwards to keep your data.

### Signing and notarization

By default the app is signed ad hoc. That works, but people downloading it must click
**Open Anyway** in System Settings ▸ Privacy & Security once. To avoid that, sign with a
Developer ID (Apple Developer Program) and notarize:

```sh
xcrun notarytool store-credentials heymint --apple-id you@example.com --team-id TEAMID
SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)" NOTARY_PROFILE=heymint make app
```

With a Developer ID the hardened runtime is on, with the exceptions an embedded Python needs
(`packaging/entitlements.plist`).

## Releasing a new version

Releases are built by GitHub, not on your Mac:

1. Put the new version in `pyproject.toml` (`version = "0.2.0"`) and describe the changes at
   the top of `CHANGELOG.md`.
2. Commit, then tag and push:

   ```sh
   git commit -am "Release 0.2.0"
   git tag -a v0.2.0 -m "Hey Mint 0.2.0"
   git push origin main v0.2.0
   ```

3. `.github/workflows/release.yml` builds the app on an Apple silicon runner (about five
   minutes) and publishes a GitHub Release with the DMG, its checksum, install steps and
   notes generated from the merged pull requests.

The **latest release** link in the README always points at the newest one. Running the
workflow by hand (Actions ▸ Release ▸ Run workflow) builds the app without publishing:
a dry run.

To have CI sign and notarize, add these repository secrets:

| Secret | What it is |
|---|---|
| `MACOS_CERT_P12` | Your Developer ID Application certificate exported as .p12, base64-encoded (`base64 -i cert.p12 \| pbcopy`) |
| `MACOS_CERT_PASSWORD` | The password you gave the .p12 |
| `MACOS_SIGN_IDENTITY` | `Developer ID Application: Your Name (TEAMID)` |
| `APPLE_ID`, `APPLE_TEAM_ID`, `APPLE_APP_PASSWORD` | For notarization; the password is an [app-specific password](https://support.apple.com/102654) |

If a release build fails, the error lines are shown as annotations on the workflow run's
summary page.

## Troubleshooting

| Problem | Fix |
|---|---|
| Mint can't click or type | Accessibility isn't granted to the app that runs it (your terminal when using `make run`). Grant it in System Settings ▸ Privacy & Security ▸ Accessibility. After rebuilding, macOS sometimes needs the switch turned off and on again. |
| Mint doesn't hear you | Microphone permission, then the input device in Mint's settings. `mint.log` says which microphone it opened. |
| `make app` stops at portaudio | `brew install portaudio` |
| `make app`: "Build on an Apple silicon Mac" | The bundled app is arm64 only; build on an M-series Mac, or use `./install.sh` on Intel. |
| The wake word check is skipped in `make test` | Its models are downloaded by `./install.sh`; run it once. |
| The downloaded app won't open | Not notarized: System Settings ▸ Privacy & Security ▸ Open Anyway. |
| Start over with a clean app | `./install.sh --remove` removes the dev app, its data and the login item. For the downloaded app, delete it and `~/Library/Application Support/Hey Mint`. |
