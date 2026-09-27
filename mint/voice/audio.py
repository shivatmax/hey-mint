"""Microphone capture and speaker playback.

Note on echo: there is no acoustic echo cancellation here. On a MacBook's built-in
speakers the model will hear its own voice and interrupt itself. Use headphones,
or set MINT_PUSH_TO_TALK=1 so the microphone is only open while you hold the key.
"""

from __future__ import annotations

import asyncio
import logging

import pyaudio

from mint.core import config

log = logging.getLogger("mint.voice.audio")

FORMAT = pyaudio.paInt16


class Audio:
    def __init__(self) -> None:
        self._pya = pyaudio.PyAudio()
        self._in_stream = None
        self._out_stream = None
        # Set while the mic should be ignored (half-duplex, during playback).
        self.muted = False
        # Set while audio is actually coming out of the speakers.
        self.playing = False

    # --- capture -------------------------------------------------------------

    async def listen(self, handler) -> None:
        """Read the mic forever, handing every PCM chunk to `handler`.

        The handler decides where the audio goes: to the local wake word model
        while asleep, or to the Live session while awake. It is awaited, so it
        must stay fast.
        """
        info = self._pya.get_default_input_device_info()
        self._in_stream = await asyncio.to_thread(
            self._pya.open,
            format=FORMAT,
            channels=config.CHANNELS,
            rate=config.SEND_SAMPLE_RATE,
            input=True,
            input_device_index=info["index"],
            frames_per_buffer=config.CHUNK_SIZE,
        )
        log.info("microphone: %s", info.get("name", "default"))
        while True:
            data = await asyncio.to_thread(
                self._in_stream.read, config.CHUNK_SIZE, exception_on_overflow=False
            )
            if self.muted:
                continue
            await handler(data)

    # --- playback ------------------------------------------------------------

    async def play(self, in_queue: asyncio.Queue, on_idle=None, on_chunk=None) -> None:
        """Play PCM chunks as they arrive.

        `on_chunk` sees each chunk as it starts playing (for the level meter).
        `on_idle` is called once playback has drained, which is how the session
        knows the assistant has stopped speaking.
        """
        self._out_stream = await asyncio.to_thread(
            self._pya.open,
            format=FORMAT,
            channels=config.CHANNELS,
            rate=config.RECEIVE_SAMPLE_RATE,
            output=True,
        )
        while True:
            chunk = await in_queue.get()
            self.playing = True
            if on_chunk is not None:
                on_chunk(chunk)
            await asyncio.to_thread(self._out_stream.write, chunk)
            if in_queue.empty():
                # Give the stream a moment to finish the last buffer before
                # declaring silence; otherwise half-duplex unmutes too early and
                # the microphone catches the tail of the sentence.
                await asyncio.sleep(0.5)
                if in_queue.empty():
                    self.playing = False
                    if on_idle is not None:
                        on_idle()

    def close(self) -> None:
        for stream in (self._in_stream, self._out_stream):
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
        self._pya.terminate()
