"""The voices the live model can speak in, and a short spoken preview of each.

Gemini Live speaks in 30 prebuilt voices (VOICES in extra_tools: name -> tone).
The Voices API lists about a thousand more (en-in-advisor-1 and friends), but
those are for the TTS models only: tested on gemini-3.8-live, any of them -
or a made-up name - is accepted without error and spoken in the default voice
(Zephyr). So only these 30 are offered.

preview(voice, style) says one sentence in that voice through the chosen
speaker. The clip is made once by a short live session and cached, so the
second listen is instant and free.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import wave

from . import config

log = logging.getLogger("mint.voices")

# Google does not label them; this is how they sound (AI Studio's grouping).
GENDER = {
    **dict.fromkeys(("Achernar", "Aoede", "Autonoe", "Callirrhoe", "Despina", "Erinome", "Gacrux", "Kore",
                     "Laomedeia", "Leda", "Pulcherrima", "Sulafat", "Vindemiatrix", "Zephyr"), "female"),
    **dict.fromkeys(("Achird", "Algenib", "Algieba", "Alnilam", "Charon", "Enceladus", "Fenrir", "Iapetus",
                     "Orus", "Puck", "Rasalgethi", "Sadachbia", "Sadaltager", "Schedar", "Umbriel",
                     "Zubenelgenubi"), "male"),
}
CACHE = config.PROJECT_ROOT / "voice" / "previews"
SAMPLE = "Hi! This is how I would sound when we talk. Pick me if you like it."
RATE = 24000                     # the live model's output: 24 kHz, 16-bit mono


def catalog() -> list[tuple[str, str, str]]:
    """[(name, gender, tone)]: women first, then men, each alphabetical."""
    from .extra_tools import VOICES
    order = {"female": 0, "male": 1}
    return sorted(((n, GENDER.get(n, ""), t) for n, t in VOICES.items()),
                  key=lambda v: (order.get(v[1], 2), v[0]))


def label(name: str) -> str:
    from .extra_tools import VOICES
    gender = GENDER.get(name, "")
    return f"{name} - {gender + ', ' if gender else ''}{VOICES.get(name, '')}"


def _path(voice: str, style: str):
    key = hashlib.sha1(f"{voice}|{style.strip().lower()}".encode()).hexdigest()[:10]
    return CACHE / f"{voice}-{key}.wav"


async def _record(voice: str, style: str) -> bytes:
    from google.genai import types

    from .session import _client
    instruction = ("Read the user's text aloud exactly as written. Say nothing else."
                   + (f" Speaking style: {style}." if style else ""))
    settings = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=types.Content(parts=[types.Part(text=instruction)], role="user"),
        speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))))
    pcm = b""
    async with _client().aio.live.connect(model=config.MODEL, config=settings) as session:
        await session.send_realtime_input(text=SAMPLE)
        async for message in session.receive():
            content = message.server_content
            if content and content.model_turn:
                pcm += b"".join(p.inline_data.data for p in content.model_turn.parts
                                if p.inline_data and p.inline_data.data)
            if content and content.turn_complete:
                break
    return pcm


def clip(voice: str, style: str = "") -> str:
    """Path of the cached preview WAV, recording it first if needed (~3 s)."""
    path = _path(voice, style)
    if not path.exists():
        pcm = asyncio.run(asyncio.wait_for(_record(voice, style), 20))
        if not pcm:
            raise RuntimeError("no audio came back")
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(RATE)
            out.writeframes(pcm)
    return str(path)


_playing = []                    # NSSound must stay referenced while it plays


def play(path: str, device_uid: str = "") -> None:
    import AppKit
    sound = AppKit.NSSound.alloc().initWithContentsOfFile_byReference_(path, True)
    if sound is None:
        return
    if device_uid:
        sound.setPlaybackDeviceIdentifier_(device_uid)
    _playing[:] = [sound]
    sound.play()


def preview(voice: str, style: str = "", done=None) -> None:
    """Record (or reuse) and play the preview off the main thread.
    done(error_or_None) is called on the main thread when it starts playing."""
    from PyObjCTools import AppHelper

    from . import prefs

    def work():
        error = None
        try:
            path = clip(voice, style)
            AppHelper.callAfter(play, path, prefs.get("output_device") or "")
        except Exception as problem:            # network, quota, a bad voice name
            log.warning("voice preview failed: %s", problem)
            error = str(problem) or type(problem).__name__
        if done is not None:
            AppHelper.callAfter(done, error)
    threading.Thread(target=work, daemon=True, name="voice-preview").start()
