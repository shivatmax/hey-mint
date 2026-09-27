# The screen-control engine

This is what Mint's `desktop` tool runs on: give it a goal in plain words ("type lo-fi into the
search box and press return") and it reads the app in front through the Accessibility tree,
lets Jev pick the next action from the real controls, performs it, checks what changed, and
repeats until the goal is done. No screenshots.

It comes from [jev-use](https://github.com/savka777/jev-use) by Savva Bojko (MIT licence, see
[LICENSE](LICENSE)), with small changes so it runs inside Mint.

## How Mint uses it

- `install.sh` and `packaging/build_app.sh` build it and put it inside the app as
  `Contents/MacOS/MintEngine`. From a checkout, `make engine` builds `build/MintEngine`.
- Mint starts it the first time a `desktop` goal comes in, as its own child process with
  `-Headless YES`: no window, menu bar item or shortcut of its own, and it uses Mint's
  Accessibility permission. It quits when Mint does.
- Goals arrive on the distributed notification `local.jev-use.command`; "stop" sends
  `local.jev-use.cancel`. Progress and results go to the unified log (subsystem
  `local.jev-use`), which `mint/tools/desktop.py` reads back.
- It needs a TypeSafe key (`TYPESAFE_API_KEY`, from Mint's environment or `.env`). Without one,
  Mint does not offer the `desktop` tool and uses `ui_act` instead.

## Changes from jev-use

- The TypeSafe key is read from the environment first, then Mint's `.env`.
- Headless: no Dock icon even outside an app bundle, and it quits when the Mint process that
  started it (`MINT_PARENT_PID`) has gone.
- Messages in headless mode name Mint, whose permissions it uses.

Run standalone (it also has its own push-to-talk widget), build with
`xcrun swift build -c release` and see the jev-use repository.
