"""Is the live model really answering? Connects to each model, asks one
question by text, and times the first audio and the full reply.

    python bench/live_model_probe.py [model ...]
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from google.genai import types  # noqa: E402

from mint import config  # noqa: E402
from mint.session import _client  # noqa: E402


async def probe(model: str, voice: str = "Zephyr") -> None:
    settings = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        output_audio_transcription=types.AudioTranscriptionConfig(),
        speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))))
    start = time.monotonic()
    try:
        async with _client().aio.live.connect(model=model, config=settings) as session:
            connected = time.monotonic() - start
            sent = time.monotonic()
            await session.send_realtime_input(text="In one short sentence: which model are you, and what is 17 times 23?")
            first, audio, words = None, 0, []
            async for message in session.receive():
                content = message.server_content
                if content and content.model_turn:
                    for part in content.model_turn.parts:
                        if part.inline_data and part.inline_data.data:
                            first = first or time.monotonic() - sent
                            audio += len(part.inline_data.data)
                if content and content.output_transcription and content.output_transcription.text:
                    words.append(content.output_transcription.text)
                if content and content.turn_complete:
                    break
            print(f"{model:45} voice={voice:10} OK  connect {connected:.2f}s  first audio {first:.2f}s  "
                  f"reply {time.monotonic() - sent:.2f}s  audio {audio / 48000:.1f}s\n    said: {''.join(words).strip()}")
    except Exception as error:
        print(f"{model:45} voice={voice:10} FAILED after {time.monotonic() - start:.2f}s: {str(error)[:160]}")


async def main() -> None:
    models = sys.argv[1:] or [config.MODEL, config.FALLBACK_MODEL]
    for model in models:
        await probe(model)


if __name__ == "__main__":
    asyncio.run(main())
