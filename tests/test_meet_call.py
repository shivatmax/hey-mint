"""Google Meet calls with Mint: the page script (call audio in, Mint's voice out), Chrome over a pipe, the session's
routing while a call is on, the listening rule in a call, and the Telegram buttons."""
import asyncio
import base64
import json
import os
import tempfile
import threading
import time
import types
from pathlib import Path

import numpy as np
import pytest

try:
    from mint.app import meet_call
    from mint.voice import listening
except ImportError:
    from mint import listening, meet_call


def test_page_script_is_configured():
    src = meet_call.inject_source(name="Mint", hosts=["meet.google.com"])
    assert src.startswith("(function (CFG)") and src.endswith(");")
    cfg = json.loads(src[src.rindex("})(") + 3:-2])
    assert cfg["hosts"] == ["meet.google.com"] and cfg["binding"] == meet_call.BINDING


def test_pipe_frames_and_answers():
    """NUL-separated JSON both ways; answers matched by id; events to their handlers."""
    to_chrome_r, to_chrome_w = os.pipe()
    from_chrome_r, from_chrome_w = os.pipe()
    pipe = meet_call.Pipe(from_chrome_r, to_chrome_w)
    events = []
    pipe.on("Runtime.bindingCalled", lambda p, s: events.append((p, s)))

    def chrome():
        buf = b""
        while True:
            chunk = os.read(to_chrome_r, 4096)
            if not chunk:
                return
            buf += chunk
            while b"\0" in buf:
                raw, buf = buf.split(b"\0", 1)
                msg = json.loads(raw)
                reply = {"id": msg["id"], "result": {"echo": msg["method"], "sid": msg.get("sessionId")}}
                if msg["method"] == "Bad.method":
                    reply = {"id": msg["id"], "error": {"message": "nope"}}
                event = {"method": "Runtime.bindingCalled", "params": {"name": "x"}, "sessionId": "S"}
                os.write(from_chrome_w, json.dumps(event).encode() + b"\0" + json.dumps(reply).encode() + b"\0")
    threading.Thread(target=chrome, daemon=True).start()
    assert pipe.call("Target.getTargets") == {"echo": "Target.getTargets", "sid": None}
    assert pipe.call("Page.enable", session="S")["sid"] == "S"
    with pytest.raises(meet_call.CDPError, match="nope"):
        pipe.call("Bad.method")
    assert events and events[0] == ({"name": "x"}, "S")
    os.close(from_chrome_w)                   # Chrome gone: waiting calls end, new ones fail
    assert pipe.closed.wait(2)
    with pytest.raises(meet_call.CDPError):
        pipe.call("Page.enable", timeout=1)


@pytest.mark.parametrize("text,said,verdict", [
    ("open my calendar", "", "act"), ("what's on the screen", "", "act"), ("yes", "Shall I open it?", "act"),
    ("can you", "", "wait"), ("I want to", "", "wait"), ("hmm", "", "wait")])
def test_in_a_call_everything_is_for_mint_unless_unfinished(text, said, verdict):
    assert listening.quick(text, "call", said) == verdict


def test_only_asked_calls_start(monkeypatch):
    try:
        from mint.app import live
    except ImportError:
        from mint import live
    monkeypatch.setattr(meet_call, "start", lambda **k: "STARTED")
    monkeypatch.setattr(live, "request", lambda: "summarise this web page for me")
    assert meet_call.google_meet({"action": "start"}).startswith("Not started")
    monkeypatch.setattr(live, "request", lambda: "start a google meet and share my screen")
    assert meet_call.google_meet({"action": "start"}) == "STARTED"
    monkeypatch.setattr(live, "request", lambda: "don't call anyone")
    assert meet_call.google_meet({"action": "start"}).startswith("Not started")


def test_no_call_answers():
    assert meet_call.current() is None
    assert "no Google Meet" in meet_call.google_meet({"action": "status"})
    assert "no Google Meet" in meet_call.google_meet({"action": "unshare"})
    assert meet_call.telegram_plan(None, "end")[1] is None
    assert meet_call.telegram_plan(None, "admit:123")[1] is None


def test_knocks_are_answered_once(monkeypatch):
    try:
        from mint.core import prefs
    except ImportError:
        from mint import prefs
    real = prefs.get
    monkeypatch.setattr(prefs, "get", lambda key: "ask" if key == "meet_admit" else real(key))
    call = meet_call.Call(share="")
    clicked = []
    call.page = types.SimpleNamespace(click=lambda what: clicked.append(what) or True)
    monkeypatch.setattr(meet_call, "_telegram_knock", lambda *a: None)
    call._knock("Sam Doe wants to join")
    call._knock("Sam Doe wants to join")            # the same request, seen again on the next poll
    (key,) = call.knocks
    assert call._admit(key, False) == "Turned away." and clicked == ["deny"]
    assert call._admit(key, True).startswith("That request is already")


# --- the session while a call is on ------------------------------------------------------------------------------


def _session():
    try:
        from mint.app import session
    except ImportError:
        from mint import session
    return session


class _Window:
    def __init__(self):
        self.sounds = []

    def sound(self, now, loud):
        self.sounds.append(loud)


def test_call_audio_goes_to_the_model_and_the_mac_mic_does_not():
    session = _session()
    q = asyncio.Queue(maxsize=10)
    stub = types.SimpleNamespace(paused=False, asleep=False, meet=object(), out_queue=q, _window=_Window(),
                                 audio=types.SimpleNamespace(playing=False), ui=types.SimpleNamespace(
                                     set_level=lambda level: None), _last_voice=0.0)
    loud = (np.sin(np.arange(1600) / 5) * 8000).astype(np.int16).tobytes()

    async def run():
        await session.Mint._on_meet_audio(stub, loud)
        await session.Mint._on_audio(stub, loud)         # the Mac's microphone during the call: dropped
    asyncio.run(run())
    assert q.qsize() == 1 and stub._window.sounds == [True] and stub._last_voice > 0


def test_mints_voice_goes_to_the_call():
    session = _session()
    said, cleared = [], []
    call = types.SimpleNamespace(speak=said.append, clear=lambda: cleared.append(1))
    stub = types.SimpleNamespace(meet=call, ui=types.SimpleNamespace(set_level=lambda level: None),
                                 audio_in=asyncio.Queue(), audio=types.SimpleNamespace())
    session.Mint._on_play(stub, b"\x01\x00" * 100)
    session.Mint._flush_playback(stub)
    assert said == [b"\x01\x00" * 100] and cleared == [1]


# --- the page script in a real (headless) Chrome, on a local page: no Google, nothing on screen --------------------

FAKE = """<!doctype html><html><body><button aria-label="Leave call">Leave</button><script>
window.__t = {};
window.toneA = (hz) => { const c = new AudioContext(); const o = c.createOscillator(); o.frequency.value = hz;
  const g = c.createGain(); g.gain.value = 0.3; o.connect(g); g.connect(c.destination); o.start(); __t.a = o; };
window.stopA = () => __t.a.stop();
window.micLevel = async () => {
  if (!__t.mic) { const s = await navigator.mediaDevices.getUserMedia({audio: true, video: true});
    const c = new AudioContext(); const an = c.createAnalyser(); an.fftSize = 2048;
    c.createMediaStreamSource(s).connect(an); __t.mic = an; __t.vid = s.getVideoTracks().length; }
  const d = new Float32Array(2048); __t.mic.getFloatTimeDomainData(d); let x = 0; for (const v of d) x += v * v;
  return Math.sqrt(x / d.length);
};
window.toneB = async (hz) => {
  const far = new RTCPeerConnection({mintFarEnd: true}), near = new RTCPeerConnection();
  far.onicecandidate = (e) => e.candidate && near.addIceCandidate(e.candidate);
  near.onicecandidate = (e) => e.candidate && far.addIceCandidate(e.candidate);
  const c = new AudioContext(), o = c.createOscillator(), d = c.createMediaStreamDestination(), g = c.createGain();
  o.frequency.value = hz; g.gain.value = 0.3; o.connect(g); g.connect(d); o.start();
  far.addTrack(d.stream.getAudioTracks()[0], d.stream);
  const offer = await far.createOffer(); await far.setLocalDescription(offer); await near.setRemoteDescription(offer);
  const answer = await near.createAnswer(); await near.setLocalDescription(answer); await far.setRemoteDescription(answer);
  __t.b = [far, near];
};
</script></body></html>"""


@pytest.mark.skipif(not meet_call.browser() or bool(os.environ.get("CI")), reason="needs Chrome (not on CI)")
def test_the_bridge_in_chrome(tmp_path):
    page_file = tmp_path / "call.html"
    page_file.write_text(FAKE)
    heard: list[bytes] = []
    proc, pipe = meet_call.launch("about:blank", share="", profile=Path(tempfile.mkdtemp()), headless=True)
    try:
        def binding(params, session):
            message = json.loads(params.get("payload") or "{}")
            if message.get("t") == "pcm":
                heard.append(base64.b64decode(message["d"]))
        pipe.on("Runtime.bindingCalled", binding)
        page = meet_call.Page(pipe)
        page.prepare(meet_call.inject_source(hosts=[""]))
        page.go(page_file.as_uri())
        time.sleep(1.5)

        def tone():
            a = np.frombuffer(b"".join(heard[-8:]), np.int16).astype(np.float32) / 32768
            if not a.size:
                return 0.0, 0
            spectrum = np.abs(np.fft.rfft(a))
            return float(np.sqrt(np.mean(a * a))), round(float(np.fft.rfftfreq(a.size, 1 / 16000)[spectrum.argmax()]))

        page.js("toneA(440)")                        # the call, played the way Meet plays it
        time.sleep(2)
        level, hz = tone()
        assert level > 0.05 and abs(hz - 440) <= 5
        page.js("stopA()")
        time.sleep(6)                                # the fallback opens once the page's audio is quiet
        page.js("toneB(660)")                        # a remote WebRTC track
        time.sleep(4)
        level, hz = tone()
        assert level > 0.05 and abs(hz - 660) <= 5

        assert page.js("micLevel()") == 0            # Meet's microphone: silent until Mint talks
        t = np.arange(24000) / 24000
        voice = (0.4 * np.sin(2 * np.pi * 300 * t) * 32767).astype(np.int16).tobytes()
        for i in range(0, len(voice), 4800):
            page.post("Runtime.evaluate", {"expression": f"__mintMeet.play('{base64.b64encode(voice[i:i + 4800]).decode()}',"
                                                         "24000)"})
        time.sleep(0.5)
        assert page.js("micLevel()") > 0.1 and page.js("__t.vid") == 1
        page.js("__mintMeet.clear()")                # Mint interrupted: its voice stops at once
        time.sleep(0.3)
        assert page.js("micLevel()") < 0.01
        assert page.state()["inCall"] is True and page.js("__mintMeet.find('leave')")
    finally:
        try:
            pipe.call("Browser.close", timeout=4)
        except Exception:
            pass
        try:
            proc.wait(5)
        except Exception:
            proc.kill()


def test_first_call_waits_for_the_sign_in_then_carries_on(monkeypatch):
    """The Meet profile isn't signed in: the request is answered at once ("sign in there"), the window comes to the
    front, and the meeting is made as soon as Google sends the window back - no second request."""
    states = iter([{"host": "accounts.google.com", "signIn": True, "missing": True}] * 2 +
                  [{"host": "meet.google.com"}] +
                  [{"host": "meet.google.com", "code": "abc-defg-hij"}])
    went = []
    call = meet_call.Call(share="")
    call.page = types.SimpleNamespace(state=lambda: next(states), go=went.append)
    call.pipe = types.SimpleNamespace(closed=threading.Event())
    call.proc = types.SimpleNamespace(poll=lambda: None, pid=1)
    shown = []
    monkeypatch.setattr(meet_call, "_show_app", lambda pid, front=True: shown.append(pid))
    monkeypatch.setattr(meet_call.time, "sleep", lambda s: None)
    monkeypatch.setattr(meet_call, "_away", lambda: False)
    st = call._sign_in()
    assert st["code"] == "abc-defg-hij" and call.ready.is_set() and "sign" in call.note.lower()
    assert shown == [1] and went == [meet_call.NEW_MEETING]       # signed in on Meet's home page: a new meeting


def test_first_call_from_telegram_while_away_says_why(monkeypatch):
    call = meet_call.Call(share="", asked_from="telegram")
    monkeypatch.setattr(meet_call, "_away", lambda: True)
    with pytest.raises(meet_call.MeetError, match="one-time Google sign-in"):
        call._sign_in()


@pytest.mark.parametrize("text,wants", [
    ("Start a Google Meet.", True), ("start a google meet and share my screen", True), ("call me on meet", True),
    ("hey mint can you start a video call with me", True), ("end the google meet", False),
    ("set up google meet", False), ("is the meet on?", False), ("open my meeting notes", False),
    ("start the meeting recording", False), ("don't start a google meet", False)])
def test_a_call_request_is_recognised(text, wants):
    """The session starts these itself (the model once answered "sign in first" from an earlier attempt)."""
    assert meet_call.wants_call(text) is wants
    try:
        from mint.app import instant
    except ImportError:
        from mint import instant
    assert (instant.match(text) or ("",))[0] == ("google_meet" if wants else (instant.match(text) or ("",))[0])


@pytest.mark.parametrize("mode", ["first", "everyone"])
def test_who_is_let_in(monkeypatch, mode):
    """Mint's own account made the call, so the user, joining as themselves, asks to join. "everyone" (the default:
    the link is the key) lets all in; "first" lets the first in and asks on Telegram about later ones."""
    try:
        from mint.core import prefs
    except ImportError:
        from mint import prefs
    real = prefs.get
    monkeypatch.setattr(prefs, "get", lambda key: mode if key == "meet_admit" else real(key))
    call = meet_call.Call(share="")
    clicked, asked = [], []
    call.page = types.SimpleNamespace(click=lambda what: clicked.append(what) or True)
    monkeypatch.setattr(meet_call, "_telegram_knock", lambda c, key, who: asked.append(who))
    monkeypatch.setattr(meet_call, "_tell", lambda *a: None)
    call._knock("Alex wants to join")
    assert clicked == ["admit"] and asked == []
    call._knock("Someone else wants to join")
    if mode == "first":
        assert clicked == ["admit"] and asked == ["Someone else wants to join"]
    else:
        assert clicked == ["admit", "admit"] and asked == []


CHAT = """<!doctype html><html><body><button aria-label="Leave call">Leave</button>
<textarea aria-label="Send a message"></textarea><div id="log"><div data-message-id="m0"><div jsname="dTKtvb">
<div>said before Mint joined</div></div></div></div><script>
window.say = (id, text) => { const d = document.createElement('div'); d.setAttribute('data-message-id', id);
  d.innerHTML = '<div jsname="dTKtvb"><div></div></div>'; d.querySelector('div div').textContent = text;
  document.getElementById('log').appendChild(d); };
</script></body></html>"""


@pytest.mark.skipif(not meet_call.browser() or bool(os.environ.get("CI")), reason="needs Chrome (not on CI)")
def test_meet_chat_is_read_once_and_mints_own_lines_are_skipped(tmp_path):
    """6 Oct: Mint's reply showed twice (a pending copy, then the sent one) and Mint answered itself in a loop."""
    page_file = tmp_path / "chat.html"
    page_file.write_text(CHAT)
    proc, pipe = meet_call.launch("about:blank", share="", profile=Path(tempfile.mkdtemp()), headless=True)
    try:
        page = meet_call.Page(pipe)
        page.prepare(meet_call.inject_source(hosts=[""]))
        page.go(page_file.as_uri())
        time.sleep(1.0)
        assert page.js("__mintMeet.chat()") == {"open": True, "messages": []}     # what was there: not requests
        page.js("say('m1', 'what is on my screen?')")
        assert page.js("__mintMeet.chat()")["messages"] == ["what is on my screen?"]
        assert page.js("__mintMeet.chat()")["messages"] == []                       # read once
        page.js("__mintMeet.sent('It is your editor.')")
        page.js("say('pending', 'It is your editor.'); say('m2', 'It is your editor.'); say('m3', 'thanks')")
        assert page.js("__mintMeet.chat()")["messages"] == ["thanks"]
        assert page.js("__mintMeet.chatBox()") is True
    finally:
        try:
            pipe.call("Browser.close", timeout=4)
        except Exception:
            pass
        try:
            proc.wait(5)
        except Exception:
            proc.kill()
