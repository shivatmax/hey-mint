"""Entry point: `python -m mint`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from mint.core import config

SAY_NOTIFICATION = "local.mint.say"
LOG_DIR = Path.home() / "Library" / "Logs" / "Mint"


def _parse() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mint",
        description="Voice control for your Mac: Gemini Live reasons and talks, Jev drives the screen.",
    )
    parser.add_argument("--hands-free", "-w", action="store_true",
                        help="Wait for the wake word instead of streaming constantly. "
                             "Nothing is sent anywhere while asleep.")
    parser.add_argument("--wake-word", default=config.WAKE_WORD, choices=config.WAKE_WORDS,
                        help="Which wake word to listen for.")
    parser.add_argument("--wake-threshold", type=float, default=config.WAKE_THRESHOLD,
                        help="0-1. Lower triggers more easily. Default 0.5.")
    parser.add_argument("--sleep-after", type=float, default=config.SLEEP_AFTER,
                        help="Seconds of quiet before going back to sleep. Default 12.")
    parser.add_argument("--half-duplex", action="store_true",
                        help="Close the mic while Mint speaks (no interrupting). Default is automatic: "
                             "open when echo cancellation is available.")
    parser.add_argument("--no-ui", "--no-menu-bar", dest="no_ui", action="store_true",
                        help="No menu bar item and no HUD; terminal only.")
    parser.add_argument("--no-hud", action="store_true", help="Menu bar item only, no floating HUD.")
    parser.add_argument("--text", action="store_true",
                        help="Type instead of speaking. Mint still replies with audio.")
    parser.add_argument("--say", metavar="TEXT",
                        help="Send a request to the Mint that is already running, then exit.")
    parser.add_argument("--voice", default=None, help="Prebuilt voice, e.g. Zephyr, Puck, Charon, Kore.")
    parser.add_argument("--model", default=None, help="Override the Live model id.")
    parser.add_argument("--allow-shell", action="store_true", help="Enable the run_shell tool.")
    parser.add_argument("--allow-search", action="store_true",
                        help="Enable Google Search grounding. Needs a billed key.")
    parser.add_argument("--grant", action="store_true",
                        help="Ask macOS for Accessibility permission and show what to do next.")
    parser.add_argument("--verbose", action="store_true", help="Log every tool call and reconnect.")
    parser.add_argument("--keys", nargs="+", help=argparse.SUPPRESS)   # test aid: post key presses
    parser.add_argument("--ax", metavar="APP", help=argparse.SUPPRESS)  # test aid: accessibility census
    parser.add_argument("--script", metavar="JSON", help=argparse.SUPPRESS)  # test aid: [[tool, args], ...]
    parser.add_argument("--demo", action="store_true",
                        help="Play a tour of the HUD animations and effects, without Gemini, then quit.")
    parser.add_argument("--tool", nargs=2, metavar=("NAME", "JSON"),
                        help="Run one tool directly, without Gemini, print its result and exit. "
                             "Run through the app to use its permissions: "
                             "open -n ~/Applications/Mint.app --args --tool NAME '{...}'")
    return parser.parse_args()


# --- helper commands -------------------------------------------------------------

def _say(text: str) -> int:
    """Post a request to the running instance over a distributed notification."""
    from Foundation import NSDistributedNotificationCenter

    NSDistributedNotificationCenter.defaultCenter().postNotificationName_object_userInfo_deliverImmediately_(
        SAY_NOTIFICATION, text, None, True)
    return 0


def _host_app() -> str:
    """Name of the .app that ultimately launched this process.

    The immediate parent is the shell, so walk up the tree until a bundle
    appears. That bundle is what owns the Accessibility grant.
    """
    pid = os.getppid()
    for _ in range(12):
        probe = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                               capture_output=True, text=True, check=False)
        parts = probe.stdout.strip().split(None, 1)
        if len(parts) != 2:
            break
        parent, command = parts
        bundles = [name for name in re.findall(r"/([^/]+)\.app/", command) if name != "Python"]
        if bundles:
            return bundles[0]
        try:
            pid = int(parent)
        except ValueError:
            break
        if pid <= 1:
            break
    return "your terminal app"


def _grant() -> int:
    from mint.tools import fastinput

    host = _host_app()
    if fastinput.has_accessibility():
        print(f"Accessibility is already granted to {host}. Nothing to do.")
        return 0
    print(f"Asking macOS for Accessibility on behalf of: {host}\n")
    fastinput.request_accessibility()
    print(
        "A system dialog should have appeared.\n"
        "  1. Click 'Open System Settings'.\n"
        f"  2. Find {host} in the list - it is there now, because it just asked - and turn it ON.\n"
        f"  3. Quit {host} completely (Cmd-Q) and reopen it.\n"
        "  4. Check:  ./run.sh --grant\n")
    return 0


def _run_tool(name: str, raw: str) -> int:
    """Test harness: one tool, no model. Deterministic, and it runs with the
    permissions of whatever launched it - through Mint.app, Mint's own."""
    import json

    if not (sys.stdin and sys.stdin.isatty()):
        _log_to_file()
    os.environ["MINT_DEBUG"] = "1"   # show each internal step
    from mint.tools import registry as tools

    started = time.monotonic()
    result, image = asyncio.run(tools.dispatch(name, json.loads(raw or "{}")))
    print(f"==== tool {name} ({time.monotonic() - started:.2f}s): {result}"
          + (" [+ image]" if image else ""), flush=True)
    return 0


def _run_script(path: str) -> int:
    """Test aid: run several tools in order in ONE process, e.g.
    [["open_app", {"name": "ChatGPT"}], ["sleep", 2], ["type_text", {"text": "hi"}]].
    One process matters: each separate Mint.app launch hands activation to
    another app when it quits, which moved focus mid-test."""
    import json

    if not (sys.stdin and sys.stdin.isatty()):
        _log_to_file()
    os.environ["MINT_DEBUG"] = "1"
    from mint.tools import registry as tools

    for name, arguments in json.loads(Path(path).read_text()):
        if name == "sleep":
            time.sleep(float(arguments))
            continue
        if name == "request":           # what the user "said", for tools that check the request
            from mint.app import live
            live.typed(str(arguments))
            continue
        started = time.monotonic()
        result, image = asyncio.run(tools.dispatch(name, arguments or {}))
        print(f"==== script {name} ({time.monotonic() - started:.2f}s): {result}"
              + (" [+ image]" if image else ""), flush=True)
    return 0


def _post_keys(specs: list[str]) -> int:
    """Test aid: press keys in order, e.g. `cmd+j t i m e return`, with a
    short pause between. Run through Mint.app so it has Accessibility."""
    from mint.tools import fastinput

    if not (sys.stdin and sys.stdin.isatty()):
        _log_to_file()
    pid = None
    for spec in specs:
        if spec.startswith("sleep="):
            time.sleep(float(spec[6:]))
            continue
        if spec.startswith("pid="):          # later keys go straight to this process
            pid = int(spec[4:])
            continue
        *mods, key = spec.split("+")
        print(f"==== keys {spec}: {fastinput.press_key(key, mods or None, pid=pid)}", flush=True)
        time.sleep(0.12)
    return 0


def _ensure_desktop_voice() -> None:
    """Make Desktop Voice an invisible engine. It is started on demand by the
    desktop bridge, the first time a screen action needs it - not at launch."""
    subprocess.run(["defaults", "write", "local.jev-use", "Headless", "-bool", "YES"], check=False)


def _log_to_file() -> Path:
    """When there is no terminal (the app bundle), send all output to a log file."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / "mint.log"
    stream = open(path, "a", buffering=1)
    sys.stdout = sys.stderr = stream
    print(f"\n==== Mint started {time.strftime('%Y-%m-%d %H:%M:%S')} ====", flush=True)
    return path


# --- main ------------------------------------------------------------------------

def main() -> int:
    args = _parse()

    if args.say:
        return _say(args.say)
    if args.grant:
        return _grant()
    if args.tool:
        return _run_tool(*args.tool)
    if args.keys:
        return _post_keys(args.keys)
    if args.script:
        return _run_script(args.script)
    if args.ax:
        if not (sys.stdin and sys.stdin.isatty()):
            _log_to_file()
        from mint.screen import axkit
        print("==== ax\n" + axkit.dump(args.ax), flush=True)
        return 0
    if args.demo:
        if not (sys.stdin and sys.stdin.isatty()):
            _log_to_file()
        from mint.app import demo
        return demo.main()

    interactive = sys.stdin is not None and sys.stdin.isatty()
    log_path = None if interactive else _log_to_file()
    # `kill -USR1 <pid>` writes every thread's stack to the log: for a hang.
    import faulthandler
    import signal as _signal
    faulthandler.register(_signal.SIGUSR1, file=sys.stderr, all_threads=True)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    if args.voice:
        config.VOICE = args.voice
    if args.model:
        config.MODEL = args.model
    if args.allow_shell:
        config.ALLOW_SHELL = True
    if args.allow_search:
        config.ALLOW_SEARCH = True

    problems = config.preflight()
    if problems:
        print("Cannot start:\n", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}\n", file=sys.stderr)
        return 1

    _ensure_desktop_voice()

    # The on-device text recognizer takes ~26s to load the first time and ~0.1s
    # after; load it now so the first click_text does not stall.
    from mint.screen import ocr
    threading.Thread(target=ocr.warm_up, name="ocr-warm-up", daemon=True).start()

    from mint.tools import fastinput
    if not args.text and not fastinput.has_accessibility():
        print("Note: no Accessibility permission, so instant typing, scrolling and key presses\n"
              "will not work. Run ./run.sh --grant to fix. Everything else works.\n",
              file=sys.stderr)

    from mint.app.session import Mint

    hands_free = args.hands_free and not args.text
    use_ui = not args.no_ui

    if not use_ui:
        mint = Mint(text_mode=args.text, hands_free=hands_free, wake_word=args.wake_word,
                        wake_threshold=args.wake_threshold, sleep_after=args.sleep_after,
                        half_duplex=True if args.half_duplex else None)
        try:
            asyncio.run(mint.run())
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\nStopped.")
        finally:
            mint.close()
        return 0

    # With the menu bar and HUD, Cocoa owns the main thread (it will run nowhere
    # else) and the session runs on a worker thread beside it.
    from mint.ui import presence as ui

    # Started by Mint Ear (Mint.app)? Take the hand-over first: it holds what the
    # microphone heard while this app was not loaded (mint/app/ear.py).
    from mint.app import ear
    ear_link = ear.connect()
    presence = ui.Presence(wake_word=args.wake_word, hands_free=hands_free,
                           show_hud=not args.no_hud,
                           log_path=str(log_path) if log_path else None)
    mint = Mint(text_mode=args.text, hands_free=hands_free, wake_word=args.wake_word,
                    wake_threshold=args.wake_threshold, sleep_after=args.sleep_after,
                    half_duplex=True if args.half_duplex else None, ui=presence)
    mint.ear = ear_link

    def submit(coroutine) -> None:
        """Run a coroutine on the session's loop from the Cocoa thread, and
        report its failure - run_coroutine_threadsafe otherwise swallows it."""
        if mint.loop is None:
            coroutine.close()
            print("  [ignored: the session is not running yet]", flush=True)
            return
        future = asyncio.run_coroutine_threadsafe(coroutine, mint.loop)

        def report(done):
            if done.exception() is not None:
                print(f"  [request failed: {done.exception()!r}]", flush=True)
        future.add_done_callback(report)

    def in_session(coroutine_factory):
        def run():
            submit(coroutine_factory())
        return run

    presence.on("wake", in_session(lambda: mint.wake_up("menu bar")))
    presence.on("grant", fastinput.request_accessibility)

    # The console (⌘J) is for typing. It does not open the microphone: in
    # testing, room audio picked up while the console was open was answered
    # as a request. "Hey Mint" still works as usual.
    presence.on("submit", lambda text: submit(mint.inject_text(text)))

    async def dismiss():
        mint.dismiss()
    presence.on("dismiss", in_session(dismiss))
    presence.on("sleep", in_session(dismiss))     # the moon button and "Go to sleep"
    presence.on("stop", in_session(lambda: mint.stop_everything("button")))
    presence.on("new_session", in_session(lambda: mint.new_session("button")))

    async def compact():
        result = await mint.compact("button")
        if result.startswith(("There is nothing", "Could not")):
            presence.chat_note(result)
    presence.on("compact", in_session(compact))

    from mint.core import prefs

    def setting_changed(key, value):
        if key == "mic":
            mint.set_paused(not value)
        elif key == "voice":
            mint.set_voice(bool(value))
            presence.refresh()
        elif key == "shortcuts":
            from PyObjCTools import AppHelper
            AppHelper.callAfter(presence.register_shortcuts)
        elif key == "listen_while_working":
            mint.listen_while_working = bool(value)
        elif key in ("voice_lock", "wake_phrase", "wake_models"):
            mint.refresh_voice_lock()             # reloads the wake word models too
        elif key in ("theme", "position", "face"):
            presence.refresh()
        elif key in ("input_device", "output_device", "echo_cancellation"):
            mint.apply_audio_settings()
        elif key == "assistant_name":
            rename_later()
        elif key in ("voice_name", "speaking_style", "reply_language", "personality"):
            revoice_later()                   # applies at a reconnect, once Mint is quiet

    # A new voice or speaking style (Settings, or set_voice): once the typing
    # stops, reconnect when Mint is quiet; the conversation carries on.
    revoice_timer = [None]

    def revoice_later():
        if revoice_timer[0] is not None:
            revoice_timer[0].cancel()
        revoice_timer[0] = threading.Timer(1.5, revoice_now)
        revoice_timer[0].start()

    def revoice_now():
        from mint.tools import extra as extra_tools
        print(f"  [voice setting: {prefs.get('voice_name') or config.VOICE}"
              + (f", style '{prefs.get('speaking_style')}'" if prefs.get("speaking_style") else "") + "]", flush=True)
        extra_tools.schedule_voice_reconnect(mint)

    # A new name: wait until the typing stops, then build "Hey <name>" if it
    # does not exist yet, and start a fresh conversation under the new name.
    rename_timer = [None]

    def rename_later():
        if rename_timer[0] is not None:
            rename_timer[0].cancel()
        rename_timer[0] = threading.Timer(2.0, rename_now)
        rename_timer[0].start()

    def rename_now():
        from mint.voice import wake
        name = prefs.name()
        print(f"  [assistant renamed: {name}]", flush=True)
        presence.refresh()
        if not wake.ready(name):
            presence.action(f"Setting up “Hey {name}”…")
            try:
                report = wake.train_name(name, progress=presence.action)
                print(f"  [wake word ready: {report}]", flush=True)
                presence.action(f"“Hey {name}” is ready - retrain your voice to make it yours")
            except Exception as error:
                print(f"  [could not set up 'Hey {name}': {error}; 'Hey Mint' still works]", flush=True)
                presence.action(f"Could not set up “Hey {name}” - “Hey Mint” still works")
        mint.refresh_voice_lock()           # reloads the wake model for the name
        restart = getattr(mint, "new_session", None)
        if restart is not None and mint.loop is not None:
            asyncio.run_coroutine_threadsafe(restart("rename"), mint.loop)

    prefs.on_change(setting_changed)
    if not prefs.get("mic"):
        mint.set_paused(True)

    def train_voice():
        """Guided enrolment: the window on the main thread, the audio from the session."""
        from PyObjCTools import AppHelper
        from mint.voice import enroll

        if mint.enroller is not None:
            return
        if not prefs.get("mic"):
            prefs.set("mic", True)              # enrolment needs the microphone

        def start():
            enroller = None

            def cancel():
                if enroller is not None:
                    enroller.cancel()
                mint.enroller = None
                print("  [voice training cancelled]", flush=True)

            window = enroll.EnrollWindow(on_cancel=cancel)
            window.show()

            def done(ok, message):
                mint.enroller = None
                mint.refresh_voice_lock()
                AppHelper.callAfter(window.done, ok, message)

            enroller = enroll.Enroller(
                on_update=lambda *a: AppHelper.callAfter(window.update, *a),
                on_done=done,
                on_level=lambda level: AppHelper.callAfter(window.level, level))
            mint.enroller = enroller
            enroller.start()
            print("  [voice training started]", flush=True)

        AppHelper.callAfter(start)

    def forget_voice():
        from mint.voice import voicelock
        voicelock.lock.forget()
        mint.refresh_voice_lock()
        print("  [voiceprint deleted]", flush=True)

    settings_window = [None]

    def open_settings():
        from PyObjCTools import AppHelper
        from mint.ui.settings import SettingsWindow

        def show():
            if settings_window[0] is None:
                settings_window[0] = SettingsWindow({
                    "train_voice": lambda: presence.fire("train_voice"),
                    "forget_voice": lambda: presence.fire("forget_voice"),
                    "audio_status": lambda: ((f"{mint._mic_lent_to} has the microphone - "
                                              if mint._mic_lent_to else "") + getattr(mint.audio, "status", "")),
                })
            settings_window[0].show()
        AppHelper.callAfter(show)

    presence.on("open_settings", open_settings)

    # Meeting notes from the menu bar: one click starts a silent recording of the call and your mic.
    def meeting(action: str):
        from mint.tools import meetings

        def run():
            result = (meetings.start() if action == "start" else meetings.start(video=True) if action == "video"
                      else meetings.stop())
            print(f"  [meeting {action}] {result}", flush=True)
            presence.action(("● Recording the meeting" if meetings.is_recording() else result)[:90])
        if action == "folder":
            subprocess.run(["open", str(meetings.meetings_root())], check=False)
            return
        threading.Thread(target=run, daemon=True, name=f"meeting-{action}").start()
    presence.on("meeting_start", lambda *_: meeting("start"))
    presence.on("meeting_stop", lambda *_: meeting("stop"))
    presence.on("meeting_video", lambda *_: meeting("video"))
    presence.on("meeting_folder", lambda *_: meeting("folder"))
    presence.on("train_voice", train_voice)
    presence.on("forget_voice", forget_voice)

    def customise():
        from mint.core import custom
        if not custom.PATH.exists():
            example = custom.PATH.with_name("custom.example.json")
            custom.PATH.write_text(example.read_text() if example.exists() else "{}\n")
        subprocess.run(["open", "-e", str(custom.PATH)], check=False)

    presence.on("customise", customise)

    def forget():
        from mint.knowledge.conversation import memory
        memory.clear()
        print("  [memory cleared]", flush=True)

    presence.on("forget", forget)
    from mint.app import power
    presence.on("quit", lambda *_: power.quit_fully(reason="menu"))

    def listen_for_say():
        """`mint --say "…"` from anywhere lands here."""
        from Foundation import NSDistributedNotificationCenter, NSOperationQueue

        def received(note):
            text = note.object()
            if text:
                print(f"  [request: {text}]", flush=True)
                submit(mint.inject_text(str(text)))

        # Keep the token: if it is garbage-collected, the observer goes with it.
        observers.append(
            NSDistributedNotificationCenter.defaultCenter().addObserverForName_object_queue_usingBlock_(
                SAY_NOTIFICATION, None, NSOperationQueue.mainQueue(), received))

        def debug(note):
            """Test probe: log what the chat holds."""
            import AppKit
            hud = presence.hud
            if hud is None or not getattr(hud, "_built", False):
                return
            chat = hud.chat
            print(f"  [debug: chat={chat.is_open} active={AppKit.NSApp.isActive()} "
                  f"key={chat.window.isKeyWindow()} holds={chat.holds_keyboard()} "
                  f"text={chat.field_text()!r} rows={len(chat._rows)} orb={hud.orb_center()}]", flush=True)

        observers.append(
            NSDistributedNotificationCenter.defaultCenter().addObserverForName_object_queue_usingBlock_(
                "local.mint.debug", None, NSOperationQueue.mainQueue(), debug))

    observers: list = []

    def worker():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(mint.run())
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        except SystemExit:
            presence.set_state("offline", "stopped - see the log")
            return
        except Exception as error:
            logging.getLogger("mint").error("session ended: %s", error)
            presence.set_state("offline", str(error)[:60])
            return
        finally:
            mint.close()
        ui.quit_app()

    threading.Thread(target=worker, name="mint-session", daemon=True).start()

    # Logout and `killall Mint` send SIGTERM. Quit properly instead of dying on
    # the spot, so the conversation still gets folded into memory.
    import signal
    signal.signal(signal.SIGTERM, lambda *_: ui.quit_app())

    def build():
        presence.build()
        listen_for_say()
        # Our orb and menu are up: Mint Ear (the launcher) hides its own and
        # passes on any `mint --say` that came while this app was starting.
        from Foundation import NSDistributedNotificationCenter
        NSDistributedNotificationCenter.defaultCenter().postNotificationName_object_userInfo_deliverImmediately_(
            "local.mint.ready", None, None, True)
        if ear_link is not None:
            ear_link.send("READY")
        reason = ear_link.reason if ear_link is not None else os.environ.get("MINT_EAR_REASON", "")
        if reason == "console":
            presence.fire("console")
        elif reason == "settings":
            presence.fire("open_settings")
        # NSApplication's terminate exits the process directly, so a `finally`
        # never runs; this notification fires on every way of quitting (menu,
        # logout, SIGTERM) and is where memory gets its last summary.
        import AppKit
        from Foundation import NSNotificationCenter
        observers.append(NSNotificationCenter.defaultCenter().addObserverForName_object_queue_usingBlock_(
            AppKit.NSApplicationWillTerminateNotification, None, None, lambda note: mint.close()))
        # First run: the welcome window (name, voice, permissions, shortcuts, a tour).
        # Later runs still ask for Accessibility once if it is missing, like macOS apps do.
        from mint.ui import onboarding
        if onboarding.needed():
            onboarding.onboarding.show()
        elif not fastinput.has_accessibility():
            fastinput.request_accessibility()

    try:
        ui.run_cocoa(build)
    except KeyboardInterrupt:
        pass
    finally:
        mint.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
