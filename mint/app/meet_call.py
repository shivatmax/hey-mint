"""Google Meet calls with Mint: "start a meet" (by voice, or from Telegram) and Mint makes a Google Meet, joins it,
shares the Mac's screen, and sends the link to the user's phone. The user joins from anywhere and talks with Mint
as on a phone call, watching the screen while Mint works.

How (everything local and free - no server, no API key, no audio driver):
  - A separate Chrome profile (~/Library/Application Support/Mint/meet-chrome) signed in to the user's Google
    account once, by the user (`google_meet action=setup`). Mint never types a password.
  - Mint drives that Chrome over --remote-debugging-pipe (file descriptors 3 and 4 of the child: no network port
    any other program could reach), and adds INJECT to the Meet page before Meet's own scripts run:
      * getUserMedia hands Meet a synthetic microphone that plays Mint's voice (and a camera card "Mint");
      * the call's audio is tapped where Meet plays it (every AudioNode connected to the speakers goes through a
        tee; remote WebRTC tracks and media elements as the fallback), mixed to 16 kHz PCM and handed to Mint
        through a CDP binding. The Mac's speakers stay silent (--mute-audio).
  - In the session (session.meet_*), the call is the microphone: Mint's own mic is ignored, the voice lock and wake
    word are skipped, Mint never falls asleep, and its voice goes to the call instead of the speakers.
The technique (synthetic mic, tapping WebAudio and WebRTC) is the one open-source meeting bots use; this is Mint's
own code.
"""

from __future__ import annotations

import base64
import fcntl
import json
import logging
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("mint.meet")

PROFILE = Path.home() / "Library/Application Support/Mint/meet-chrome"
BROWSERS = ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser")
HOSTS = ["meet.google.com"]
BINDING = "__mintMeetOut"
NEW_MEETING = "https://meet.google.com/new?hl=en"
SIGN_IN = "https://accounts.google.com/ServiceLogin?hl=en&continue=https%3A%2F%2Fmeet.google.com%2F%3Fhl%3Den"
NOBODY_AFTER = 600          # nobody joined in 10 minutes: leave
ALONE_AFTER = 180           # everyone left 3 minutes ago: leave
KNOCK_WAIT = 90             # someone asking to join, unanswered: deny
SIGN_IN_WAIT = 300          # the first call: time to sign in to Google in the Meet window
VOICE_RATE = 24000          # Gemini Live speaks 24 kHz 16-bit mono


class MeetError(Exception):
    pass


# --- the page script ---------------------------------------------------------------------------------------------

INJECT = r"""
(function (CFG) {
  'use strict';
  if (window.__mintMeet || window.top !== window || !CFG.hosts.includes(location.hostname)) return;
  const send = (m) => { try { window[CFG.binding](JSON.stringify(m)); } catch (e) { /* not bound yet */ } };
  const log = (t) => send({ t: 'log', m: String(t).slice(0, 300) });
  const AC = window.AudioContext || window.webkitAudioContext;
  const connect = AudioNode.prototype.connect, disconnect = AudioNode.prototype.disconnect;
  const S = { ctx: null, voice: null, mic: null, mixA: null, mixB: null, meterA: null, until: 0, sources: new Set(),
              lastA: 0, carry: new Float32Array(0), seen: new Set(), canvas: null, cam: null, shareTrack: null,
              share: 'off', taps: 0, peakA: 0, peakB: 0, pcs: [], chatSeen: new Set(), chatReady: false,
              mine: [] };
  const ours = (n) => !!S.ctx && !!n && n.context === S.ctx;

  const WORKLET = `registerProcessor('mint-tap', class extends AudioWorkletProcessor {
    constructor() { super(); this.buf = new Float32Array(4800); this.n = 0; }
    process(inputs) {
      const ch = inputs[0] && inputs[0][0];
      if (ch) for (let i = 0; i < ch.length; i++) {
        this.buf[this.n++] = ch[i];
        if (this.n === this.buf.length) { this.port.postMessage(this.buf.slice(0)); this.n = 0; }
      }
      return true;
    }
  });`;

  function b64(buffer) {
    const u8 = new Uint8Array(buffer);
    let s = '';
    for (let i = 0; i < u8.length; i += 0x8000) s += String.fromCharCode.apply(null, u8.subarray(i, i + 0x8000));
    return btoa(s);
  }

  // 48 kHz float -> 16 kHz 16-bit (the mean of each three samples), the remainder kept for the next block
  function feed(f32) {
    const all = new Float32Array(S.carry.length + f32.length);
    all.set(S.carry); all.set(f32, S.carry.length);
    const n = Math.floor(all.length / 3);
    const out = new Int16Array(n);
    for (let i = 0; i < n; i++) {
      const v = (all[3 * i] + all[3 * i + 1] + all[3 * i + 2]) / 3;
      out[i] = Math.max(-32768, Math.min(32767, Math.round(v * 32767)));
    }
    S.carry = all.slice(n * 3);
    if (n) send({ t: 'pcm', d: b64(out.buffer) });
  }

  async function tap(ctx, from, sink) {
    try {
      const url = URL.createObjectURL(new Blob([WORKLET], { type: 'application/javascript' }));
      await ctx.audioWorklet.addModule(url);
      const node = new AudioWorkletNode(ctx, 'mint-tap', { numberOfInputs: 1, numberOfOutputs: 1,
                                                           channelCount: 1, channelCountMode: 'explicit' });
      node.port.onmessage = (e) => feed(e.data);
      connect.call(from, node); connect.call(node, sink);
      log('capture: worklet');
    } catch (err) {
      const sp = ctx.createScriptProcessor(4096, 1, 1);
      sp.onaudioprocess = (e) => feed(e.inputBuffer.getChannelData(0));
      connect.call(from, sp); connect.call(sp, sink);
      log('capture: script processor (' + err + ')');
    }
  }

  function ensure() {
    if (S.ctx) return S.ctx;
    const ctx = new AC({ sampleRate: 48000 });
    S.ctx = ctx;
    S.mic = ctx.createMediaStreamDestination();
    S.voice = ctx.createGain(); connect.call(S.voice, S.mic);
    S.mixA = ctx.createGain(); S.mixB = ctx.createGain();
    const all = ctx.createGain();
    connect.call(S.mixA, all); connect.call(S.mixB, all);
    S.meterA = ctx.createAnalyser(); S.meterA.fftSize = 1024; connect.call(S.mixA, S.meterA);
    S.meterB = ctx.createAnalyser(); S.meterB.fftSize = 1024; connect.call(S.mixB, S.meterB);
    const sink = ctx.createGain(); sink.gain.value = 0; connect.call(sink, ctx.destination);
    tap(ctx, all, sink);
    setInterval(watch, 250);
    const resume = () => { if (ctx.state !== 'running') ctx.resume().catch(() => {}); };
    ['pointerdown', 'keydown', 'click'].forEach((ev) => window.addEventListener(ev, resume, true));
    resume();
    return ctx;
  }

  // Where Meet plays the call (A): it decodes the audio itself and plays it through its own AudioContexts, so every
  // connection to the speakers goes through a tee that also feeds Mint. The fallback (B): remote WebRTC tracks and
  // playing media elements, heard only while A is silent, so nothing is heard twice.
  function addA(stream) {
    ensure();
    connect.call(S.ctx.createMediaStreamSource(stream), S.mixA);
    S.taps += 1;
    log('call audio: page audio ' + S.taps);
  }

  function addB(track, element) {
    if (!track || track.kind !== 'audio' || S.seen.has(track.id)) return;
    S.seen.add(track.id);
    ensure();
    const ms = new MediaStream([track]);
    if (!element) {        // Chrome pulls a remote track through WebAudio only while an element plays it too
      const el = new Audio(); el.__mint = true; el.muted = true; el.srcObject = ms; el.play().catch(() => {});
    }
    connect.call(S.ctx.createMediaStreamSource(ms), S.mixB);
    log('call audio: ' + (element ? 'element' : 'webrtc') + ' track');
  }

  function level(meter) {
    const d = new Float32Array(meter.fftSize);
    meter.getFloatTimeDomainData(d);
    let s = 0;
    for (let i = 0; i < d.length; i++) s += d[i] * d[i];
    return Math.sqrt(s / d.length);
  }

  // Both ways in are heard. (Hearing the fallback only while the page's audio was quiet lost the user: Meet's own
  // sounds kept that side "busy" while their voice came in on a WebRTC track - 6 Oct.) Where Meet plays a track
  // through WebAudio, the same voice comes twice, near together: louder, still clear.
  function watch() {
    const a = level(S.meterA), b = level(S.meterB);
    S.peakA = Math.max(S.peakA * 0.9, a); S.peakB = Math.max(S.peakB * 0.9, b);
    document.querySelectorAll('audio, video').forEach((el) => {
      if (el.__mint || el.paused || !(el.srcObject instanceof MediaStream)) return;
      el.srcObject.getAudioTracks().forEach((t) => addB(t, el));
    });
  }

  AudioNode.prototype.connect = function (target, ...rest) {
    if (!(target instanceof AudioDestinationNode) || ours(this)) return connect.call(this, target, ...rest);
    const c = this.context;
    try {
      if (!c.__mintTee) {
        const tee = c.createGain();
        const out = c.createMediaStreamDestination();     // (none on an OfflineAudioContext: plain connect below)
        connect.call(tee, c.destination); connect.call(tee, out);
        c.__mintTee = tee;
        addA(out.stream);
      }
      connect.call(this, c.__mintTee, ...rest);
      return target;
    } catch (e) {
      return connect.call(this, target, ...rest);
    }
  };
  AudioNode.prototype.disconnect = function (...args) {
    if (args[0] instanceof AudioDestinationNode && !ours(this) && this.context.__mintTee) args[0] = this.context.__mintTee;
    return disconnect.apply(this, args);
  };

  const PC = window.RTCPeerConnection;
  if (PC) {
    class MintPeerConnection extends PC {
      constructor(...args) {
        super(...args);
        S.pcs.push(this); window.__mintPcs = S.pcs;
        if (!(args[0] && args[0].mintFarEnd)) this.addEventListener('track', (e) => addB(e.track));
      }
    }
    window.RTCPeerConnection = MintPeerConnection;
    if (window.webkitRTCPeerConnection) window.webkitRTCPeerConnection = MintPeerConnection;
  }

  // Mint's microphone (its voice) and camera (a card that glows while Mint talks)
  function micTrack() { ensure(); return S.mic.stream.getAudioTracks()[0].clone(); }

  function draw() {
    const g = S.canvas.getContext('2d');
    const t = performance.now() / 1000;
    const talking = S.ctx && S.ctx.currentTime < S.until;
    g.fillStyle = '#0d1714'; g.fillRect(0, 0, 640, 360);
    const r = 62 + (talking ? 10 * (0.5 + 0.5 * Math.sin(t * 11)) : 3 * Math.sin(t * 2));
    const glow = g.createRadialGradient(320, 150, 10, 320, 150, r * 1.9);
    glow.addColorStop(0, 'rgba(92, 230, 176, 0.55)'); glow.addColorStop(1, 'rgba(92, 230, 176, 0)');
    g.fillStyle = glow; g.beginPath(); g.arc(320, 150, r * 1.9, 0, Math.PI * 2); g.fill();
    g.fillStyle = '#5ce6b0'; g.beginPath(); g.arc(320, 150, r, 0, Math.PI * 2); g.fill();
    g.fillStyle = '#0d1714'; g.beginPath(); g.arc(300, 140, 7, 0, Math.PI * 2); g.arc(340, 140, 7, 0, Math.PI * 2); g.fill();
    g.textAlign = 'center'; g.fillStyle = '#ffffff'; g.font = '600 34px -apple-system, Helvetica, sans-serif';
    g.fillText(CFG.name, 320, 285);
    g.fillStyle = 'rgba(255,255,255,0.6)'; g.font = '18px -apple-system, Helvetica, sans-serif';
    g.fillText(CFG.tagline, 320, 318);
  }

  function camTrack() {
    if (!S.canvas) {
      S.canvas = document.createElement('canvas'); S.canvas.width = 640; S.canvas.height = 360;
      draw(); setInterval(draw, 80);
      S.cam = S.canvas.captureStream(12);
    }
    return S.cam.getVideoTracks()[0].clone();
  }

  const md = navigator.mediaDevices;
  if (md) {
    const device = (kind, label) => ({ deviceId: 'mint-' + kind, kind, label, groupId: 'mint',
      toJSON() { return { deviceId: this.deviceId, kind: this.kind, label: this.label, groupId: this.groupId }; } });
    md.getUserMedia = async (c) => {
      const out = new MediaStream();
      if (c && c.audio) out.addTrack(micTrack());
      if (c && c.video) out.addTrack(camTrack());
      return out;
    };
    md.enumerateDevices = async () => [device('audioinput', CFG.name + ' voice'), device('videoinput', CFG.name),
                                       device('audiooutput', 'Default')];
    const gdm = md.getDisplayMedia && md.getDisplayMedia.bind(md);
    if (gdm) md.getDisplayMedia = async (c) => {
      S.share = 'asking';
      try {
        const s = await gdm(c);
        S.shareTrack = s.getVideoTracks()[0] || null; S.share = 'on';
        return s;
      } catch (e) { S.share = 'error: ' + e.name; throw e; }
    };
  }

  // Meet's controls, by their accessible names (English: the page is opened with hl=en)
  const WANT = {
    join: [/^(join now|join|ask to join)$/i],
    leave: [/^leave call$/i],
    share: [/^(share screen|present now|present)$/i],
    screen: [/^(your entire screen|entire screen)$/i],
    unshare: [/^(stop presenting|stop sharing)$/i],
    chat: [/^chat with everyone/i, /^(in-call messages|chat)$/i],
    admit: [/^admit$/i, /^admit (?!all\b)(?!\d)\S/i],
    // Meet's newer waiting room: a green "Admit 1 guest" pill (6 Oct) opens the list; "Admit all" takes everyone.
    admitpill: [/admit \d+ (guest|guests|person|people)$/i, /^admit \d+/i],
    admitall: [/(^|\W)admit all$/i],
    deny: [/^(deny|deny entry)$/i],
    dismiss: [/^(got it|dismiss|no thanks|not now)$/i],
    justleave: [/^just leave the (call|meeting)$/i],
  };
  const words = (s) => (s || '').replace(/\s+/g, ' ').trim();
  function visible(el) {
    const r = el.getBoundingClientRect();
    return r.width > 2 && r.height > 2 && r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth
      && getComputedStyle(el).visibility !== 'hidden';
  }
  function find(kind) {
    const res = WANT[kind] || [];
    for (const el of document.querySelectorAll('button, [role="button"], [role="menuitem"], [role="menuitemradio"]')) {
      const names = [el.getAttribute('aria-label'), el.getAttribute('data-tooltip'), el.textContent].map(words);
      if (!names.some((n) => n && res.some((re) => re.test(n))) || !visible(el)) continue;
      if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
      const r = el.getBoundingClientRect();
      return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
    }
    return null;
  }
  function people() {
    for (const el of document.querySelectorAll('[aria-label]')) {
      const l = el.getAttribute('aria-label') || '';
      const m = l.match(/^(?:people|show everyone|participants)\D{0,24}(\d+)/i) || l.match(/\b(\d+)\s+(?:participants|people)\b/i);
      if (m) return +m[1];
    }
    for (const el of document.querySelectorAll('button[aria-label]')) {
      if (!/^(people|show everyone)/i.test(el.getAttribute('aria-label') || '')) continue;
      const m = words(el.textContent).match(/(\d+)/);
      if (m) return +m[1];
    }
    return null;
  }
  function knock() {
    if (!find('admit')) return '';
    const btn = [...document.querySelectorAll('button, [role="button"]')].find((el) => /^admit$/i.test(words(el.textContent)) && visible(el));
    let box = btn && (btn.closest('[role="dialog"], [role="alertdialog"], [role="alert"]') || btn.parentElement?.parentElement?.parentElement);
    // The name, without Meet's button words and icon names ("Admitmore_vertMore actions" - 6 Oct)
    const text = words((box ? box.innerText || box.textContent : '')
      .replace(/[a-z]+(_[a-z]+)+/g, ' ')
      .replace(/\b(admit all|admit|deny entry|deny|view all|view|more actions|waiting to join|visible to hosts|\d+ unconfirmed users?|someone wants to join this call|wants to join( this call)?|ask(ing)? to join)\b/gi, ' ')
      .replace(/\(\d+\)/g, ' '));
    return text.slice(0, 80) || 'Someone';
  }

  window.__mintMeet = {
    play(d, rate) {
      const ctx = ensure();
      const bin = atob(d);
      const n = bin.length >> 1;
      if (!n) return;
      const buf = ctx.createBuffer(1, n, rate);
      const ch = buf.getChannelData(0);
      for (let i = 0; i < n; i++) {
        let v = bin.charCodeAt(2 * i) | (bin.charCodeAt(2 * i + 1) << 8);
        if (v >= 32768) v -= 65536;
        ch[i] = v / 32768;
      }
      const src = ctx.createBufferSource();
      src.buffer = buf; connect.call(src, S.voice);
      const at = Math.max(ctx.currentTime + 0.04, S.until);
      src.start(at); S.until = at + buf.duration;
      S.sources.add(src); src.onended = () => S.sources.delete(src);
    },
    clear() {
      for (const s of S.sources) { s.onended = null; try { s.stop(); } catch (e) { /* not started */ } }
      S.sources.clear(); S.until = 0;
    },
    find,
    // Meet's chat (the "In-call messages" panel): messages are div[data-message-id]; the box is "Send a message".
    // chat() -> the new ones since the last look (Mint's own left out); the first look only marks what's there.
    chat() {
      const box = document.querySelector('textarea[aria-label="Send a message"], textarea[placeholder="Send a message"]');
      const out = [];
      for (const el of document.querySelectorAll('div[data-message-id]')) {
        const id = el.getAttribute('data-message-id');
        if (S.chatSeen.has(id)) continue;
        S.chatSeen.add(id);
        const body = el.querySelector('[jsname="dTKtvb"]') || el;
        const text = words(body.innerText || body.textContent);
        if (!text || !S.chatReady) continue;
        // Mint's own (each shows twice: a pending copy, then the sent one - both are skipped)
        if (S.mine.some((m) => m.text === text && performance.now() - m.at < 600000)) continue;
        out.push(text);
      }
      S.chatReady = true;
      return { open: !!box, messages: out };
    },
    chatBox() {
      const box = document.querySelector('textarea[aria-label="Send a message"], textarea[placeholder="Send a message"]');
      if (!box) return false;
      box.focus();
      return document.activeElement === box;
    },
    sent(text) { S.mine.push({ text: words(text), at: performance.now() }); if (S.mine.length > 50) S.mine.shift(); },
    // What the connection carries (for the log): audio sent and received, and the loudest incoming level.
    async stats() {
      const out = { sent: 0, received: 0, level: 0, inbound: 0 };
      for (const pc of S.pcs) {
        if (pc.connectionState === 'closed') continue;
        try {
          (await pc.getStats()).forEach((r) => {
            if (r.kind !== 'audio') return;
            if (r.type === 'outbound-rtp') out.sent += r.bytesSent || 0;
            if (r.type === 'inbound-rtp') {
              out.received += r.bytesReceived || 0; out.inbound += 1;
              out.level = Math.max(out.level, r.audioLevel || 0);
            }
            if (r.type === 'media-source' && r.audioLevel !== undefined) out.micLevel = Math.max(out.micLevel || 0, r.audioLevel);
          });
        } catch (e) { /* closed meanwhile */ }
      }
      return out;
    },
    state() {
      const inCall = !!find('leave');
      const body = inCall ? '' : words(document.body ? document.body.innerText : '').slice(0, 4000);
      const live = S.shareTrack && S.shareTrack.readyState === 'live';
      return {
        url: location.href, host: location.hostname,
        code: (location.pathname.match(/^\/([a-z]{3,4}-[a-z]{4}-[a-z]{3,4})\b/) || [])[1] || '',
        inCall, canJoin: !!find('join'), people: inCall ? people() : null, knock: inCall ? knock() : '',
        waiting: inCall && !!(find('admitpill') || find('admitall')),
        share: live ? 'on' : (S.share === 'on' ? 'off' : S.share),
        ended: !inCall && /(you left the meeting|meeting has ended|been removed from the meeting|return to home screen|call ended)/i.test(body),
        blocked: !inCall && /(you can't join this video call|you can’t join this video call|not allowed to join)/i.test(body),
        signIn: /^(accounts|workspace)\.google\.com$/.test(location.hostname) || (!inCall && /\bsign in\b/i.test(body) && !find('join')),
        audio: S.ctx ? S.ctx.state : 'none', taps: S.taps, talking: !!S.ctx && S.ctx.currentTime < S.until,
        levels: [Math.round(S.peakA * 1000) / 1000, Math.round(S.peakB * 1000) / 1000],
      };
    },
  };
})
"""


def inject_source(name: str = "Mint", tagline: str = "AI assistant · on your Mac", hosts=None) -> str:
    cfg = {"hosts": list(hosts or HOSTS), "binding": BINDING, "name": name, "tagline": tagline}
    return INJECT.strip() + "(" + json.dumps(cfg) + ");"


# --- Chrome over a pipe ------------------------------------------------------------------------------------------


class CDPError(Exception):
    pass


class Pipe:
    """Chrome DevTools Protocol over --remote-debugging-pipe: NUL-separated JSON on two file descriptors."""

    def __init__(self, read_fd: int, write_fd: int) -> None:
        self._rfd, self._wfd = read_fd, write_fd
        self._next = 0
        self._lock = threading.Lock()
        self._waiting: dict[int, list] = {}
        self._handlers: dict[str, list] = {}
        self._out: queue.Queue = queue.Queue()
        self.closed = threading.Event()
        threading.Thread(target=self._read_loop, name="meet-cdp-read", daemon=True).start()
        threading.Thread(target=self._write_loop, name="meet-cdp-write", daemon=True).start()

    def on(self, method: str, fn) -> None:
        self._handlers.setdefault(method, []).append(fn)

    def _ident(self) -> int:
        with self._lock:
            self._next += 1
            return self._next

    def call(self, method: str, params: dict | None = None, session: str | None = None, timeout: float = 15.0):
        ident = self._ident()
        slot = [threading.Event(), None]
        with self._lock:
            self._waiting[ident] = slot
        self._queue(ident, method, params, session)
        if not slot[0].wait(timeout):
            with self._lock:
                self._waiting.pop(ident, None)
            raise CDPError(f"{method}: no answer from Chrome")
        reply = slot[1]
        if reply is None:
            raise CDPError(f"{method}: Chrome closed")
        if "error" in reply:
            raise CDPError(f"{method}: {reply['error'].get('message')}")
        return reply.get("result") or {}

    def post(self, method: str, params: dict | None = None, session: str | None = None) -> None:
        """Fire and forget (the answer is dropped)."""
        self._queue(self._ident(), method, params, session)

    def _queue(self, ident, method, params, session) -> None:
        if self.closed.is_set():
            raise CDPError("Chrome closed")
        message = {"id": ident, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        self._out.put(json.dumps(message).encode() + b"\0")

    def _write_loop(self) -> None:
        while not self.closed.is_set():
            data = self._out.get()
            if data is None:
                break
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(self._wfd, view):]
            except OSError:
                break
        self._close()

    def _read_loop(self) -> None:
        buf = bytearray()
        while True:
            try:
                chunk = os.read(self._rfd, 1 << 16)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while True:
                end = buf.find(b"\0")
                if end < 0:
                    break
                raw, buf = bytes(buf[:end]), buf[end + 1:]
                try:
                    self._dispatch(json.loads(raw))
                except Exception:
                    log.debug("cdp message", exc_info=True)
        self._close()

    def _dispatch(self, message: dict) -> None:
        if "id" in message:
            with self._lock:
                slot = self._waiting.pop(message["id"], None)
            if slot is not None:
                slot[1] = message
                slot[0].set()
            return
        for fn in self._handlers.get(message.get("method", ""), ()):
            try:
                fn(message.get("params") or {}, message.get("sessionId"))
            except Exception:
                log.exception("cdp event %s", message.get("method"))

    def _close(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        self._out.put(None)
        with self._lock:
            waiting, self._waiting = self._waiting, {}
        for slot in waiting.values():
            slot[0].set()
        for fd in (self._rfd, self._wfd):
            try:
                os.close(fd)
            except OSError:
                pass


def browser() -> str:
    for path in BROWSERS:
        path = os.path.expanduser(path)
        if os.path.exists(path):
            return path
    return ""


def _flags(share: str) -> list[str]:
    flags = ["--no-first-run", "--no-default-browser-check", "--lang=en-US", "--window-size=1180,800",
             "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
             "--disable-backgrounding-occluded-windows", "--autoplay-policy=no-user-gesture-required",
             "--mute-audio"]
    if share:
        # Chrome's own share picker, answered by the flag (macOS's system picker would wait for a click).
        flags.append("--disable-features=UseSCContentSharingPicker")
    if share == "screen":
        flags.append("--auto-select-screen-capture-source")    # the whole screen, no click
    elif share:
        flags.append(f"--auto-select-window-capture-source-by-title={share}")
    return flags


def launch(url: str, cdp: bool = True, share: str = "screen", profile: Path = PROFILE,
           headless: bool = False, extra: list[str] | None = None):
    """Start the Meet Chrome. With cdp: (process, Pipe) driven over fds 3/4; without: (process, None) - for signing
    in, an ordinary window nobody drives."""
    path = browser()
    if not path:
        raise MeetError("Google Chrome isn't installed (Google Meet calls use it). Install it from google.com/chrome.")
    profile.mkdir(parents=True, exist_ok=True)
    args = [path, f"--user-data-dir={profile}", *(_flags(share) if cdp else
                                                  ["--no-first-run", "--no-default-browser-check", "--lang=en-US"])]
    if headless:
        args.append("--headless=new")
    args += list(extra or [])
    if not cdp:
        return spawn(args + [url]), None
    to_chrome_r, to_chrome_w = os.pipe()
    from_chrome_r, from_chrome_w = os.pipe()
    # Out of the way of 3 and 4 before the child gets them there.
    hi_r = fcntl.fcntl(to_chrome_r, fcntl.F_DUPFD_CLOEXEC, 20)
    hi_w = fcntl.fcntl(from_chrome_w, fcntl.F_DUPFD_CLOEXEC, 20)
    os.close(to_chrome_r)
    os.close(from_chrome_w)
    try:
        proc = spawn(args + ["--remote-debugging-pipe", url], {3: hi_r, 4: hi_w})
    finally:
        os.close(hi_r)
        os.close(hi_w)
    return proc, Pipe(from_chrome_r, to_chrome_w)


class Spawned:
    """A child started by spawn(): the few Popen methods the call uses."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self.returncode is None:
            try:
                done, status = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                done, status = self.pid, 0
            if done:
                self.returncode = os.waitstatus_to_exitcode(status) if status else 0
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        end = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if end is not None and time.monotonic() > end:
                raise subprocess.TimeoutExpired("chrome", timeout)
            time.sleep(0.1)
        return self.returncode

    def terminate(self) -> None:
        self._signal(15)

    def kill(self) -> None:
        self._signal(9)

    def _signal(self, number: int) -> None:
        if self.poll() is None:
            try:
                os.kill(self.pid, number)
            except ProcessLookupError:
                pass


def spawn(args: list[str], fds: dict[int, int] | None = None) -> Spawned:
    """Start Chrome as its own app in macOS's eyes: the "responsible process" for privacy (camera, microphone,
    screen recording) is Chrome, not Mint. Started as Mint's child it was Mint's - and when Chrome looked at the
    cameras, macOS killed it (Mint declares no camera use; seen 6 Oct, SIGABRT "Namespace TCC"). Chrome does the
    same for its own helpers (responsibility_spawnattrs_setdisclaim). `fds`: child fd -> our fd (the CDP pipe)."""
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    attr = ctypes.c_void_p()
    actions = ctypes.c_void_p()
    libc.posix_spawnattr_init(ctypes.byref(attr))
    libc.posix_spawn_file_actions_init(ctypes.byref(actions))
    try:
        libc.responsibility_spawnattrs_setdisclaim(ctypes.byref(attr), 1)
        POSIX_SPAWN_SETSID, POSIX_SPAWN_CLOEXEC_DEFAULT = 0x0400, 0x4000
        libc.posix_spawnattr_setflags(ctypes.byref(attr), ctypes.c_short(POSIX_SPAWN_SETSID |
                                                                         POSIX_SPAWN_CLOEXEC_DEFAULT))
        for fd in (0, 1, 2):                    # quiet, and nothing of Mint's leaks in (CLOEXEC_DEFAULT)
            libc.posix_spawn_file_actions_addopen(ctypes.byref(actions), fd, b"/dev/null",
                                                  os.O_RDWR, 0)
        for target, source in (fds or {}).items():
            libc.posix_spawn_file_actions_adddup2(ctypes.byref(actions), source, target)
        argv = (ctypes.c_char_p * (len(args) + 1))(*[a.encode() for a in args], None)
        env = [f"{k}={v}".encode() for k, v in os.environ.items()]
        envp = (ctypes.c_char_p * (len(env) + 1))(*env, None)
        pid = ctypes.c_int()
        error = libc.posix_spawn(ctypes.byref(pid), args[0].encode(), ctypes.byref(actions), ctypes.byref(attr),
                                 argv, envp)
        if error:
            raise MeetError(f"Couldn't start Chrome ({os.strerror(error)}).")
        return Spawned(pid.value)
    finally:
        libc.posix_spawn_file_actions_destroy(ctypes.byref(actions))
        libc.posix_spawnattr_destroy(ctypes.byref(attr))


# --- a call ------------------------------------------------------------------------------------------------------


class Page:
    """The one tab Mint drives."""

    def __init__(self, pipe: Pipe) -> None:
        self.pipe = pipe
        target = None
        for _ in range(40):
            infos = pipe.call("Target.getTargets").get("targetInfos") or []
            target = next((t for t in infos if t.get("type") == "page"), None)
            if target:
                break
            time.sleep(0.25)
        if target is None:
            target = {"targetId": pipe.call("Target.createTarget", {"url": "about:blank"})["targetId"]}
        self.target = target["targetId"]
        self.sid = pipe.call("Target.attachToTarget", {"targetId": self.target, "flatten": True})["sessionId"]

    def call(self, method: str, params: dict | None = None, timeout: float = 15.0):
        return self.pipe.call(method, params, self.sid, timeout)

    def post(self, method: str, params: dict | None = None) -> None:
        self.pipe.post(method, params, self.sid)

    def prepare(self, source: str) -> None:
        self.call("Page.enable")
        self.call("Runtime.enable")
        self.call("Page.setBypassCSP", {"enabled": True})      # the worklet module is a blob: URL
        self.call("Runtime.addBinding", {"name": BINDING})
        self.call("Page.addScriptToEvaluateOnNewDocument", {"source": source})

    def go(self, url: str) -> None:
        self.call("Page.navigate", {"url": url})

    def js(self, expression: str, timeout: float = 8.0):
        r = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True},
                      timeout)
        if r.get("exceptionDetails"):
            raise CDPError(str(r["exceptionDetails"].get("text") or r["exceptionDetails"])[:200])
        return (r.get("result") or {}).get("value")

    def state(self) -> dict:
        try:
            st = self.js("typeof __mintMeet === 'object' ? __mintMeet.state() : "
                         "({url: location.href, host: location.hostname, missing: true, insecure: "
                         "/(browser or app may not be secure|couldn.t sign you in)/i.test("
                         "document.body ? document.body.innerText.slice(0, 3000) : '')})") or {}
        except CDPError:
            return {}
        if st.get("missing") and st.get("host") in ("accounts.google.com", "workspace.google.com"):
            st["signIn"] = True                       # Google's sign-in (the page script is only on Meet)
        return st

    def click(self, what: str) -> bool:
        """A real click (trusted, so it counts as the user's gesture - screen sharing needs one)."""
        try:
            at = self.js(f"typeof __mintMeet === 'object' ? __mintMeet.find({json.dumps(what)}) : null")
        except CDPError:
            return False
        if not at:
            return False
        x, y = float(at["x"]), float(at["y"])
        self.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        self.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left",
                                               "clickCount": 1})
        self.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left",
                                               "clickCount": 1})
        return True


class Call:
    def __init__(self, share: str = "screen", asked_from: str = "voice") -> None:
        self.share = share
        self.asked_from = asked_from
        self.proc = None
        self.pipe: Pipe | None = None
        self.page: Page | None = None
        self.link = ""
        self.status = "starting"
        self.error = ""
        self.started = time.time()
        self.joined_at = 0.0          # the user came in
        self.alone_since = 0.0
        self.heard_at = 0.0
        self.ready = threading.Event()
        self.over = threading.Event()
        self.live = False             # audio flows between the call and Mint
        self.knocks: dict[str, dict] = {}
        self.auto_admitted = False
        self._chat_opened = 0.0
        self._told_admit = 0.0
        self._chat_lock = threading.Lock()
        self.chunks = self.loud = 0       # call audio that reached Mint (and how much of it was someone talking)
        self.sharing = False
        self.card_id = None
        self.note = ""                # what to tell the user while the call waits for them (the sign-in)
        self._caffeinate = None
        self._lock = threading.Lock()

    # --- audio ------------------------------------------------------------------------------------------------

    def _binding(self, params: dict, session: str | None) -> None:
        if params.get("name") != BINDING:
            return
        try:
            message = json.loads(params.get("payload") or "{}")
        except ValueError:
            return
        kind = message.get("t")
        if kind == "pcm":
            if not self.live:
                return
            pcm = base64.b64decode(message.get("d") or "")
            self.chunks += 1
            if _loud(pcm):
                self.heard_at = time.time()
                self.loud += 1
            mint = _mint()
            if mint is not None:
                mint.meet_audio(pcm)
        elif kind == "log":
            _trace("page: %s", message.get("m"))

    def speak(self, pcm: bytes) -> None:
        """Mint's voice into the call (any thread)."""
        if self.page is None or not self.live:
            return
        try:
            self.page.post("Runtime.evaluate", {"expression": f"__mintMeet.play('{base64.b64encode(pcm).decode()}',"
                                                              f"{VOICE_RATE})"})
        except CDPError:
            pass

    def clear(self) -> None:
        if self.page is not None:
            try:
                self.page.post("Runtime.evaluate", {"expression": "typeof __mintMeet==='object'&&__mintMeet.clear()"})
            except CDPError:
                pass

    # --- the run ------------------------------------------------------------------------------------------------

    def run(self) -> None:
        try:
            self._start()
        except Exception as error:
            if not isinstance(error, (MeetError, CDPError)):
                log.exception("meet start")
            answered = self.ready.is_set()      # (during the sign-in: the request was already answered)
            self.error = str(error) or error.__class__.__name__
            _trace("couldn't start: %s", self.error)
            self.status = "failed"
            self.ready.set()
            self._close(f"couldn't start: {self.error}", quiet=True)
            if answered:
                _tell("The Google Meet didn't start", self.error)
            return
        self.ready.set()
        try:
            self._watch()
        except Exception:
            log.exception("meet watch")
            self._close("something went wrong")

    def _start(self) -> None:
        if _locked():
            raise MeetError("The Mac is locked, so there's no screen to share. Unlock it first (I can't type your "
                            "password).")
        _wait_unlocked()
        self.proc, self.pipe = launch("about:blank", share=self.share)
        self.pipe.on("Runtime.bindingCalled", self._binding)
        self.page = Page(self.pipe)
        self.page.prepare(inject_source())
        self.page.go(NEW_MEETING)
        self.status = "creating"
        st = self._until(lambda s: s.get("code") or s.get("signIn"), 35, "Google Meet didn't open a new meeting.")
        late = False
        if not st.get("code"):
            st, late = self._sign_in(), True
        self.link = "https://meet.google.com/" + st["code"]
        _trace("made %s - joining", self.link)
        self._caffeinate = subprocess.Popen(["/usr/bin/caffeinate", "-dimsu", "-w", str(self.proc.pid)],
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.status = "joining"
        self._until(lambda s: s.get("canJoin") or s.get("inCall"), 30, "Meet's Join button didn't appear.",
                    dismiss=True)
        for _ in range(3):
            if self.page.state().get("inCall"):
                break
            self.page.click("join")
            try:
                self._until(lambda s: s.get("inCall"), 12, "", dismiss=True)
            except MeetError:
                continue
        if not self.page.state().get("inCall"):
            raise MeetError("Mint couldn't get into the call (Meet didn't let it join).")
        self.status = "in call"
        self.live = True
        mint = _mint()
        if mint is not None:
            mint.meet_begin(self)
        if self.share:
            self._share()
        self.page.click("dismiss")
        self.page.click("chat")                      # the chat panel stays open: messages there are requests too
        self._chat_opened = time.time()
        _hide_app(self.proc.pid)
        _trace("in %s (sharing: %s)", self.link, self.sharing)
        if late:
            # The request was answered while the user signed in: tell them the call is ready (Mint's voice is
            # the call's now, so a notification and Telegram, not speech).
            _telegram_card(self)
            _notify("Mint is in the Google Meet", "Join from your phone: " + self.link)

    def _sign_in(self) -> dict:
        """The first call: Mint's Meet profile has no Google account yet, so Meet sent the window to Google's
        sign-in. It comes to the front, the user signs in there (Mint never types a password), Google sends it
        back to a new meeting, and the call carries on - no second request."""
        if self.asked_from == "telegram" and _away():
            raise MeetError("This first call needs a one-time Google sign-in in Mint's Meet window at the Mac. "
                            "Next time you're at it, say \"start a Google Meet\" and sign in when the window asks; "
                            "after that, calls start from anywhere.")
        self.status = "signing in"
        self.note = ("Google's sign-in is open in Mint's Meet window - needed only this first time. Mint fills in "
                     "its address; type the password there (Mint never does) and approve on your phone if Google "
                     "asks. The meeting starts by itself right after.")
        _show_app(self.proc.pid)
        self.ready.set()                        # the request is answered now; the call goes on after the sign-in
        _trace("waiting for the Google sign-in")
        end = time.monotonic() + SIGN_IN_WAIT
        nudged = 0.0
        prefilled = False
        while time.monotonic() < end:
            if self.over.is_set() or self.pipe.closed.is_set() or self.proc.poll() is not None:
                raise MeetError("The Meet window was closed before signing in.")
            st = self.page.state()
            if st.get("code"):
                self.status = "joining"
                return st
            if st.get("insecure"):
                return self._sign_in_plain(end)
            if st.get("host") == "accounts.google.com" and not prefilled:
                prefilled = self._prefill_account()
            if st.get("host") == "meet.google.com" and not st.get("signIn") and time.monotonic() - nudged > 8:
                nudged = time.monotonic()        # signed in, but on Meet's home page: make the meeting
                self.page.go(NEW_MEETING)
            time.sleep(1.0)
        raise MeetError("Nobody signed in to Google within 5 minutes, so there's no meeting. Ask again when ready.")

    def _prefill_account(self) -> bool:
        """Google's "Email or phone" box: Mint's own address goes in (Settings ▸ Email control, or meet_account) and
        Next is pressed. The password is the user's to type - never Mint's."""
        from mint.core import prefs
        address = str(prefs.get("meet_account") or prefs.get("email_address") or "").strip()
        if "@" not in address:
            return True
        try:
            ready = self.page.js("(() => { const i = document.querySelector('input[type=email]');"
                                 " if (!i || i.offsetParent === null) return false; i.focus(); i.select();"
                                 " return document.activeElement === i; })()")
            if not ready:
                return False
            self.page.call("Input.insertText", {"text": address})
            time.sleep(0.3)
            key = {"key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13}
            self.page.call("Input.dispatchKeyEvent", {"type": "keyDown", "text": "\r", **key})   # (text: submits)
            self.page.call("Input.dispatchKeyEvent", {"type": "keyUp", **key})
            _trace("filled in %s on Google's sign-in", address)
        except Exception:
            log.debug("prefill the sign-in", exc_info=True)
            return False
        return True

    def _sign_in_plain(self, end: float) -> dict:
        """Google refused the sign-in in a window Mint drives: sign in in an ordinary window of the same profile
        instead (nothing drives it), then carry on once the user closes it."""
        _trace("Google wants an ordinary window to sign in")
        self.pipe.post("Browser.close")
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.note = ("Google wanted an ordinary window for the sign-in, so Mint opened one. Sign in there; Mint "
                     "closes it as soon as you're in and starts the meeting.")
        _wait_unlocked()
        plain, _ = launch(SIGN_IN, cdp=False)
        _show_app(plain.pid)
        while plain.poll() is None:
            if time.monotonic() > end or self.over.is_set():
                plain.terminate()
                raise MeetError("Nobody signed in to Google within 5 minutes, so there's no meeting.")
            if signed_in():
                _trace("signed in to Google - closing the sign-in window")
                time.sleep(3)                   # let Chrome finish the page it is on
                plain.terminate()               # (a clean quit: Chrome saves the sign-in)
                try:
                    plain.wait(15)
                except subprocess.TimeoutExpired:
                    plain.kill()
                break
            time.sleep(2.0)
        _wait_unlocked()
        self.proc, self.pipe = launch("about:blank", share=self.share)
        self.pipe.on("Runtime.bindingCalled", self._binding)
        self.page = Page(self.pipe)
        self.page.prepare(inject_source())
        self.page.go(NEW_MEETING)
        st = self._until(lambda s: s.get("code") or s.get("signIn"), 35, "Google Meet didn't open a new meeting.")
        if not st.get("code"):
            raise MeetError("The Meet window still isn't signed in to Google.")
        self.status = "joining"
        return st

    def _share(self) -> bool:
        """Meet's Share screen; Chrome's picker is answered by the launch flag (the whole screen)."""
        self.error = ""
        for _ in range(2):
            if not self.page.click("share"):
                time.sleep(1.5)
                continue
            st: dict = {}
            end = time.monotonic() + 12
            while time.monotonic() < end:
                time.sleep(0.5)
                st = self.page.state()
                share = str(st.get("share") or "")
                if share == "on" or share.startswith("error"):
                    break
                self.page.click("screen")            # an older Meet asks first: "Your entire screen"
            share = str(st.get("share") or "")
            if share == "on":
                self.sharing = True
                return True
            if share.startswith("error"):
                self.error = f"screen sharing didn't start ({share})"
                break
        self.error = self.error or "Meet's Share screen button didn't work"
        _trace("%s", self.error)
        return False

    def unshare(self) -> bool:
        if self.page is None:
            return False
        ok = self.page.click("unshare")
        if ok:
            self.sharing = False
        return ok

    def _until(self, test, seconds: float, failure: str, dismiss: bool = False) -> dict:
        end = time.monotonic() + seconds
        st: dict = {}
        while time.monotonic() < end:
            if self.over.is_set() or (self.pipe is not None and self.pipe.closed.is_set()):
                raise MeetError("Chrome closed")
            st = self.page.state()
            if st and test(st):
                return st
            if dismiss:
                self.page.click("dismiss")
            time.sleep(0.5)
        raise MeetError(failure or "timed out")

    def _watch(self) -> None:
        from mint.core import prefs
        max_minutes = float(prefs.get("meet_max_minutes") or 120)
        misses = 0
        last_dismiss = 0.0
        while not self.over.is_set():
            time.sleep(1.0)
            if self.proc.poll() is not None or self.pipe.closed.is_set():
                _trace("Chrome stopped (exit %s, pipe %s)", self.proc.poll(),
                       "closed" if self.pipe.closed.is_set() else "open")
                self._close("the Meet window was closed")
                return
            st = self.page.state()
            now = time.time()
            if not st.get("inCall"):
                misses += 1
                if st.get("ended") or misses >= 6:
                    self._close("the call ended")
                    return
                continue
            misses = 0
            if now - last_dismiss > 5:
                last_dismiss = now
                self.page.click("dismiss")
            if now - getattr(self, "_told_audio", 0) > 30:
                self._told_audio = now
                try:
                    net = self.page.js("__mintMeet.stats()")
                except CDPError:
                    net = None
                _trace("call audio so far: %d chunks, %d with voice; levels page/webrtc %s; network %s; people %s",
                       self.chunks, self.loud, st.get("levels"), net, st.get("people"))
            people = st.get("people")
            here = (people or 0) >= 2 or (people is None and now - self.heard_at < 2)
            if here and not self.joined_at:
                self.joined_at = now
                self._joined()
            if self.joined_at:
                if people is not None and people <= 1:
                    self.alone_since = self.alone_since or now
                    if now - self.alone_since > ALONE_AFTER:
                        self._close("everyone left")
                        return
                else:
                    self.alone_since = 0.0
            elif people is not None and now - self.started > NOBODY_AFTER:
                self._close("nobody joined in 10 minutes")
                return
            if now - self.started > max_minutes * 60:
                self._close(f"the {int(max_minutes)}-minute limit")
                return
            if self.sharing and st.get("share") != "on":
                self.sharing = False
            if st.get("knock") or st.get("waiting"):
                self._let_in(st, now)
            self._chat_poll(now)
            for key, k in list(self.knocks.items()):
                if k["answer"] is None and now - k["at"] > KNOCK_WAIT:
                    self._admit(key, False)

    # --- Meet's chat: another way to ask (and Mint answers there too) --------------------------------------------

    def _chat_poll(self, now: float) -> None:
        try:
            got = self.page.js("__mintMeet.chat()") or {}
        except CDPError:
            return
        if not got.get("open"):
            if now - self._chat_opened > 15:      # the panel closed (or never opened): open it again
                self._chat_opened = now
                self.page.click("dismiss")
                self.page.click("chat")
            return
        for text in got.get("messages") or []:
            _trace("chat: %s", text[:120])
            mint = _mint()
            if mint is not None:
                mint.meet_chat(text[:2000])

    def chat_send(self, text: str) -> bool:
        """Mint's words into the call's chat (any thread)."""
        text = " ".join(str(text or "").split())[:1500]
        if not text or self.page is None or not self.live:
            return False
        with self._chat_lock:
            try:
                if not self.page.js("__mintMeet.chatBox()"):
                    self.page.click("chat")
                    time.sleep(1.0)
                    if not self.page.js("__mintMeet.chatBox()"):
                        return False
                self.page.js(f"__mintMeet.sent({json.dumps(text)})")
                self.page.call("Input.insertText", {"text": text})
                key = {"key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13}
                self.page.call("Input.dispatchKeyEvent", {"type": "keyDown", "text": "\r", **key})
                self.page.call("Input.dispatchKeyEvent", {"type": "keyUp", **key})
                return True
            except CDPError:
                return False

    def _joined(self) -> None:
        _trace("the user joined")
        mint = _mint()
        if mint is not None:
            mint.meet_note("(Mint note - not the user: the user just joined your Google Meet call - from their phone "
                           "or another computer. They hear you through the call" +
                           (" and watch the Mac's whole screen, which you are sharing" if self.sharing else "") +
                           ". Greet them in one short sentence. For the rest of the call, talk as on a phone call: "
                           "short and natural; say what you are doing on the Mac while you do it.)")
        _telegram_card(self)

    # --- people asking to join ------------------------------------------------------------------------------------

    def _let_in(self, st: dict, now: float) -> None:
        """Someone is in the waiting room. "everyone" (the default): in at once. Otherwise each is asked about
        (_knock: the first one, or Telegram)."""
        from mint.core import prefs
        who = st.get("knock") or "a guest"
        if str(prefs.get("meet_admit") or "everyone") == "everyone":
            if self.page.click("admitall") or self.page.click("admit"):
                time.sleep(0.6)
                self.page.click("admitall")          # (a confirmation: "Admit all" again)
                if now - self._told_admit > 10:
                    self._told_admit = now
                    _trace("let in: %s", who)
                    _tell("Let into the Google Meet", who)
            else:
                self.page.click("admitpill")         # opens the list with the Admit buttons
            return
        if st.get("knock"):
            self._knock(st["knock"])
        else:
            self.page.click("admitpill")

    def _knock(self, who: str) -> None:
        key = str(abs(hash(who)) % 100000)
        if key in self.knocks:
            return
        self.knocks[key] = {"who": who, "at": time.time(), "answer": None}
        _trace("someone asks to join: %s", who)
        if self._first_guest():
            # Mint's own Google account made the call, so the user (signed in as themselves) has to ask to join.
            # The link went only to them (Telegram, email, or said at the Mac) and Meet codes can't be guessed:
            # the first person to ask before anyone is in is them. Anyone after that needs the user's yes.
            self.auto_admitted = True
            _trace("letting in: %s", self._admit(key, True))
            _tell("Let into the Google Meet", who)
            return
        _telegram_knock(self, key, who)

    def _first_guest(self) -> bool:
        """Let this one in without asking? meet_admit: "everyone" (the link is the key: only the user gets it, and
        Meet codes can't be guessed), "first" (the first before anyone is in), or "ask" (Telegram, every time)."""
        from mint.core import prefs
        mode = str(prefs.get("meet_admit") or "everyone")
        if mode == "everyone":
            return True
        return (mode == "first" and not self.auto_admitted and not self.joined_at
                and time.time() - self.started < NOBODY_AFTER)

    def _admit(self, key: str, yes: bool) -> str:
        k = self.knocks.get(key)
        if k is None or k["answer"] is not None:
            return "That request is already answered."
        k["answer"] = yes
        if self.page is not None and self.page.click("admit" if yes else "deny"):
            return "Let in." if yes else "Turned away."
        return "They're no longer asking."

    # --- the end ------------------------------------------------------------------------------------------------

    def end(self, why: str = "you ended it") -> None:
        self._close(why)

    def _close(self, why: str, quiet: bool = False) -> None:
        with self._lock:
            if self.over.is_set():
                return
            self.over.set()
        self.live = False
        _trace("closing (%s)", why)
        mint = _mint()
        if mint is not None:
            mint.meet_end()
        if self.page is not None and not self.pipe.closed.is_set():
            try:
                if self.page.click("leave"):
                    time.sleep(1.0)
                    self.page.click("justleave")
                    time.sleep(0.5)
                self.pipe.call("Browser.close", timeout=4)
            except Exception:
                pass
        if self.proc is not None:
            try:
                self.proc.wait(4)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        if self._caffeinate is not None:
            self._caffeinate.terminate()
        self.status = "ended"
        global _call
        with _calls_lock:
            if _call is self:
                _call = None
        if not quiet:
            _telegram_ended(self, why)

    def minutes(self) -> int:
        return int((time.time() - self.started) // 60)

    def describe(self) -> str:
        if self.status == "signing in":
            return self.note
        if self.status in ("starting", "creating", "joining"):
            return f"Starting a Google Meet ({self.status})."
        bits = [f"In a Google Meet at {self.link} for {self.minutes()} min"]
        bits.append("sharing the whole screen" if self.sharing else "not sharing the screen")
        bits.append("the user is in the call" if self.joined_at and not self.alone_since else
                    "waiting for the user to join")
        return ", ".join(bits) + "."


# --- one call at a time --------------------------------------------------------------------------------------------

_call: Call | None = None
_calls_lock = threading.Lock()


def current() -> Call | None:
    return _call


def start(share: str | None = None, asked_from: str = "voice", wait: float = 45.0) -> str:
    global _call
    from mint.core import prefs
    with _calls_lock:
        if _call is not None and not _call.over.is_set():
            return "Already in a call: " + _call.describe() + (f" Link: {_call.link}" if _call.link else "")
        if share is None:
            share = "screen" if str(prefs.get("meet_share") or "screen") != "off" else ""
        call = _call = Call(share, asked_from)
    threading.Thread(target=call.run, name="meet-call", daemon=True).start()
    if not call.ready.wait(wait):
        return ("Still joining the Google Meet - it's taking a while. I'll send the link to Telegram as soon as "
                "I'm in.")
    if call.status == "failed":
        return f"Couldn't start the Google Meet: {call.error}"
    if call.status == "signing in":
        return call.note
    _telegram_card(call)
    if call.live:
        _notify("Mint is in the Google Meet", "Join from your phone: " + call.link)
    shared = ("I'm sharing the Mac's whole screen." if call.sharing else
              f"I couldn't share the screen ({call.error}); check that Chrome may record the screen (System "
              "Settings ▸ Privacy & Security ▸ Screen Recording).")
    tg = " The link is on Telegram too." if _telegram_ready() else ""
    return (f"Mint is in a Google Meet: {call.link} . {shared}{tg} Tell the user to join from their phone with the "
            "link; Mint lets the first person who asks to join in.")


def end(why: str = "you ended it") -> str:
    call = _call
    if call is None:
        return "There's no Google Meet call going on."
    minutes = call.minutes()
    threading.Thread(target=call.end, args=(why,), name="meet-end", daemon=True).start()
    return f"Leaving the Google Meet (after {minutes} min)."


def setup() -> str:
    """Once: a window of the Meet Chrome to sign in to Google. Mint doesn't type in it."""
    if _call is not None:
        return "A call is going on; end it first."
    try:
        launch(SIGN_IN, cdp=False)
    except MeetError as error:
        return str(error)
    return ("Opened Mint's Google Meet window (its own Chrome profile). Sign in to your Google account there - "
            "you type the password, not Mint - then close that window. After that, \"start a Google Meet\" works, "
            "from here or from Telegram.")


def google_meet(args: dict) -> str:
    action = str(args.get("action") or "status").lower()
    if action == "start":
        if not _asked_for_call():
            return ("Not started: the user's words didn't ask for a call. Only start a Google Meet when the user "
                    "asks for one.")
        return start()
    if action == "end":
        return end()
    if action == "setup":
        # Already signed in: say so, don't open the sign-in window again (the setup guide's rule:
        # never open a setup that is already done unless the user insists).
        if signed_in() and not args.get("again"):
            return ("ALREADY SET UP - nothing opened: Mint's Google Meet window is signed in. Say so in one short "
                    "sentence; open the sign-in again only if the user asks for a different account (again=true).")
        return setup()
    call = _call
    if action in ("share", "unshare"):
        if call is None or call.page is None or not call.live:
            return "There's no Google Meet call going on."
        if action == "unshare":
            return "Stopped sharing the screen." if call.unshare() else "The screen wasn't being shared."
        if call.sharing:
            return "Already sharing the whole screen."
        return "Sharing the screen again." if call._share() else f"Couldn't share the screen: {call.error}"
    return call.describe() if call is not None else "There's no Google Meet call going on."


_WANTS = re.compile(r"\b(?:start|begin|open|create|make|launch|host|have|join me|call me|get me)\b[\w\s'-]{0,30}?"
                    r"\b(?:google meet|g ?meet|meet call|meet|video call)\b", re.I)
_NOT = re.compile(r"\b(?:end|stop|leave|quit|cancel|close|don'?t|do not|never|set ?up|status|is the|are you)\b", re.I)


def wants_call(text: str) -> bool:
    """The user's words ask to start a call ("start a Google Meet", "call me on Meet and share my screen"). The
    session starts it itself rather than leave it to the model: it answered from an earlier attempt once."""
    t = " ".join(str(text or "").lower().replace("’", "'").split())
    return bool(t) and len(t.split()) <= 16 and bool(_WANTS.search(t)) and not _NOT.search(t)


_ENDS = re.compile(r"\b(?:end|stop|leave|quit|close|finish|hang up|drop|exit|disconnect)\b[\w\s'-]{0,20}?"
                   r"\b(?:google meet|g ?meet|meet|call|meeting)\b|\bhang up\b", re.I)


def wants_end(text: str) -> bool:
    """The user's words end the call that's on ("end the Google Meet", "leave the call", "hang up"). Like
    wants_call, the session does it itself (6 Oct: the model clicked around Chrome instead)."""
    t = " ".join(str(text or "").lower().replace("’", "'").split())
    return _call is not None and bool(t) and len(t.split()) <= 12 and bool(_ENDS.search(t)) \
        and not re.search(r"\b(don'?t|do not|never|when|after|before)\b", t)


END_NOTE = ("(Mint note - not the user: Mint itself is leaving the Google Meet the user asked to end. Don't call "
            "google_meet or click anything - say only \"Leaving the call.\")")


START_NOTE = ("(Mint note - not the user: Mint itself is starting the Google Meet the user just asked for; it takes "
              "a few seconds. Don't call google_meet and don't answer from any earlier attempt - say only "
              "\"Starting the Google Meet.\" The result comes in another note.)")


def started_note(said: str) -> str:
    return (f"(Mint note - not the user: the Google Meet the user asked for: {said} Tell the user that in one or two "
            "short sentences.)")


def _asked_for_call() -> bool:
    try:
        from mint.app import live
        request = (live.request() or "").lower()
    except Exception:
        return True
    if not request:
        return True                     # a typed command or Telegram's /meet: the user's own
    from mint.tools.harness import _asked
    return any(_asked(request, w) for w in ("meet", "call", "video", "join me", "screen share", "share my screen",
                                            "share the screen"))


# --- helpers -------------------------------------------------------------------------------------------------------


def _trace(text: str, *args) -> None:
    """A step of the call, in Mint's log (mint.log) and its console."""
    words = text % args if args else text
    log.info("meet: %s", words)
    print(f"  {time.strftime('%H:%M:%S')} [meet] {words}", flush=True)


def _mint():
    try:
        from mint.tools.extra import _live_mint
        return _live_mint()
    except Exception:
        return None


def _loud(pcm: bytes) -> bool:
    try:
        import numpy as np
        a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        return bool(a.size) and float(np.sqrt(np.mean(a * a))) / 32768 > 0.02
    except Exception:
        return False


def signed_in(profile: Path = PROFILE) -> bool:
    """The Meet profile holds a Google sign-in (cookie names only - never their values)."""
    import shutil
    import sqlite3
    import tempfile
    db = profile / "Default" / "Cookies"
    if not db.exists():
        return False
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "c.db"                  # (Chrome holds the file open)
        try:
            shutil.copyfile(db, copy)
            with sqlite3.connect(copy) as con:
                n = con.execute("select count(*) from cookies where host_key in ('.google.com', 'accounts.google.com')"
                                " and name in ('SID', '__Secure-1PSID')").fetchone()[0]
        except (OSError, sqlite3.Error):
            return False
    return n > 0


def _wait_unlocked(profile: Path = PROFILE, seconds: float = 15.0) -> None:
    """A Chrome that just quit can hold the profile for a moment; a new one started then hands over to it and exits."""
    lock = profile / "SingletonLock"                # a link to "<host>-<pid>"
    end = time.monotonic() + seconds
    while os.path.lexists(lock) and time.monotonic() < end:
        try:
            os.kill(int(os.readlink(lock).rsplit("-", 1)[1]), 0)
        except (OSError, ValueError, IndexError):
            return                                  # left behind by a Chrome that's gone: Chrome clears it itself
        time.sleep(0.5)


def _locked() -> bool:
    try:
        import Quartz
        return bool((Quartz.CGSessionCopyCurrentDictionary() or {}).get("CGSSessionScreenIsLocked"))
    except Exception:
        return False


def _notify(title: str, words: str) -> None:
    try:
        from mint.tools.everyday import notify
        notify(title, words)
    except Exception:
        log.debug("meet notification", exc_info=True)


def _tell(title: str, words: str) -> None:
    """News about a call after its request was answered: a notification, and Telegram."""
    _notify(title, words)
    b = _bridge()
    if b is not None:
        try:
            b.reply(f"📹 {title}: {words}")
        except Exception:
            log.debug("meet telegram", exc_info=True)


def _away() -> bool:
    try:
        from mint.app.telegram_agents import _away as away
        return away()
    except Exception:
        return False


def _show_app(pid: int, front: bool = True) -> None:
    """The Meet window to the front (to sign in), or back behind the user's apps."""
    try:
        from AppKit import NSApplicationActivateIgnoringOtherApps, NSRunningApplication
        from PyObjCTools import AppHelper
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app is not None and front:
            AppHelper.callAfter(app.activateWithOptions_, NSApplicationActivateIgnoringOtherApps)
    except Exception:
        log.debug("show meet chrome", exc_info=True)


def _hide_app(pid: int) -> None:
    """The Meet window out of sight: on a whole-screen share it would show the call inside itself."""
    try:
        from AppKit import NSRunningApplication
        from PyObjCTools import AppHelper
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app is not None:
            AppHelper.callAfter(app.hide)
    except Exception:
        log.debug("hide meet chrome", exc_info=True)


# --- Telegram --------------------------------------------------------------------------------------------------------


def _bridge():
    try:
        from mint.app import telegram
        b = telegram.bridge
        return b if b.bot is not None and b._state.get("user_id") else None
    except Exception:
        return None


def _telegram_ready() -> bool:
    return _bridge() is not None


def _markup(call: Call) -> dict:
    rows = [[{"text": "📞 Join the call", "url": call.link}]] if call.link else []
    rows.append([{"text": "🖥 Stop sharing" if call.sharing else "🖥 Share screen",
                  "callback_data": "mt:unshare" if call.sharing else "mt:share"},
                 {"text": "⏹ End call", "callback_data": "mt:end"}])
    return {"inline_keyboard": rows}


def _card_text(call: Call) -> str:
    from mint.app.telegram import esc
    who = "You're in the call with Mint." if call.joined_at else "Mint is waiting for you in the call."
    share = "Sharing your Mac's whole screen." if call.sharing else "Not sharing the screen."
    return (f"📹 <b>Google Meet with Mint</b>\n{esc(who)} {esc(share)}\n<code>{esc(call.link)}</code>\n\n"
            "<i>Join with your own Google account and ask to join: Mint lets the first person in. (Signed in as "
            "Mint's own account? Pick “Join here too”, not “Switch here”.) Mint leaves when you do.</i>")


def _telegram_card(call: Call) -> None:
    b = _bridge()
    if b is None or not call.link:
        return
    try:
        if call.card_id:
            b._call("editMessageText", patient=False, chat_id=b._chat(), message_id=call.card_id,
                    text=_card_text(call), parse_mode="HTML", reply_markup=_markup(call),
                    link_preview_options={"is_disabled": True})
            return
        sent = b.html(_card_text(call), markup=_markup(call))
        call.card_id = (sent or {}).get("message_id")
    except Exception:
        log.debug("meet telegram card", exc_info=True)


def _telegram_ended(call: Call, why: str) -> None:
    b = _bridge()
    if b is None or not call.card_id:
        return
    from mint.app.telegram import esc
    try:
        b._call("editMessageText", patient=False, chat_id=b._chat(), message_id=call.card_id,
                text=f"📹 <b>Google Meet with Mint</b> - ended after {call.minutes()} min ({esc(why)}).",
                parse_mode="HTML", reply_markup={"inline_keyboard": []})
    except Exception:
        log.debug("meet telegram end", exc_info=True)


def _telegram_knock(call: Call, key: str, who: str) -> None:
    b = _bridge()
    if b is None:
        return
    from mint.app.telegram import esc, keys
    try:
        b.html(f"🚪 <b>Someone asks to join your Mint call</b>\n{esc(who)}\n\n<i>They'd see your screen. Turned away "
               f"in {KNOCK_WAIT} s unless you let them in.</i>",
               markup=keys([("✅ Let in", f"mt:admit:{key}"), ("⛔ Turn away", f"mt:deny:{key}")]))
    except Exception:
        log.debug("meet telegram knock", exc_info=True)


def telegram_command(bridge, rest: str) -> None:
    """/meet, /meet end (from the paired user)."""
    word = rest.strip().lower()
    if word in ("end", "stop", "leave", "off"):
        bridge.reply(end())
        return
    if word in ("status", "?"):
        call = _call
        bridge.reply(call.describe() if call else "No call going on. /meet starts one.")
        return
    if _call is not None and not _call.over.is_set():
        if _call.link:
            _telegram_card(_call)
        else:
            bridge.reply(_call.describe())
        return
    bridge.reply("📹 Starting a Google Meet - Mint joins it and shares the screen. A few seconds…")
    said = start(asked_from="telegram")
    if not (_call and _call.link and _call.live):
        bridge.reply(said)


def telegram_plan(bridge, arg: str):
    """(toast, action or None, used label) for the call's buttons (mt:...)."""
    call = _call
    act, _, key = arg.partition(":")
    if act == "start":
        return "📹 Starting…", lambda: telegram_command(bridge, ""), ""
    if call is None or call.over.is_set():
        return "That call is over.", None, ""
    if act == "end":
        return "⏹ Leaving the call…", lambda: end(), ""
    if act == "unshare":
        return "🖥 Stopping the share…", lambda: (call.unshare(), _telegram_card(call)), ""
    if act == "share":
        return "🖥 Sharing the screen…", lambda: (call._share(), _telegram_card(call)), ""
    if act in ("admit", "deny"):
        yes = act == "admit"
        return ("✅ Letting them in…" if yes else "⛔ Turning them away…"), lambda: call._admit(key, yes), \
            ("✅ Let in" if yes else "⛔ Turned away")
    return "That button no longer works.", None, ""


PROMPT = """Google Meet calls: google_meet. "start a Google Meet", "call me on Meet", "join me in a meet and share \
my screen" -> action=start: Mint makes a meeting with the user's Google account, joins it, shares the Mac's whole \
screen and sends the link to Telegram; the user joins from their phone and talks with you there - you hear them \
through the call and your voice goes to it. "end the call", "leave the meet" -> end; "stop sharing" -> unshare; \
"share the screen again" -> share; "is the call on?" -> status. The very first call opens Google's sign-in in Mint's \
Meet window: tell the user to sign in there (they type the password - never you); the meeting then starts by itself. \
"set up Google Meet" -> setup (the same sign-in, ahead of time). Start a call only when the user asks for one. EVERY time the user asks to start a call, call google_meet \
action=start - never answer from an earlier attempt (an old "sign in" or failure is out of date: the user may have \
signed in since)."""


def declarations():
    from google.genai import types
    return [types.FunctionDeclaration(
        name="google_meet",
        description=("Google Meet calls with Mint: start one (Mint creates and joins a Meet, shares the Mac's screen, "
                     "and sends the link to the user's phone, so they talk with Mint by voice from anywhere), end "
                     "it, stop or restart the screen share, tell its status, or set it up once. Always call it for "
                     "a new request; earlier results are out of date."),
        parameters=types.Schema(type=types.Type.OBJECT, properties={
            "action": types.Schema(type=types.Type.STRING,
                                   enum=["start", "end", "status", "share", "unshare", "setup"],
                                   description="start / end the call, share / unshare the screen, status, setup "
                                               "(sign in to Google once)"),
            "again": types.Schema(type=types.Type.BOOLEAN,
                                  description="setup only: sign in again even though already signed in (another "
                                              "account) - only when the user asks")},
            required=["action"]))]


HANDLERS = {"google_meet": google_meet}
