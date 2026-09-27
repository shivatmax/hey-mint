"""Does a typed turn get an answer right after a resumed reconnect?

Session 1: one text turn, keep the resumption handle. Session 2 resumes with
it and sends one text turn with no audio at all (Mint asleep or paused), then
waits. Prints when (if) the reply starts. Uses Mint's real live config.

    python bench/resume_text_probe.py [--no-resume] [--stream-end]
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from google.genai import types  # noqa: E402

from mint import config  # noqa: E402
from mint.session import _client, _live_config  # noqa: E402

RESUME = "--no-resume" not in sys.argv
STREAM_END = "--stream-end" in sys.argv


async def turn(session, text: str, wait: float = 15.0) -> tuple[float | None, list]:
    handle = []
    if STREAM_END:
        await session.send_realtime_input(audio_stream_end=True)
    sent = time.monotonic()
    await session.send_realtime_input(text=text)
    first = None

    async def read():
        nonlocal first
        async for message in session.receive():
            if message.session_resumption_update and message.session_resumption_update.new_handle:
                handle.append(message.session_resumption_update.new_handle)
            content = message.server_content
            if message.tool_call:
                first = first or time.monotonic() - sent
                await session.send_tool_response(function_responses=[
                    types.FunctionResponse(id=c.id, name=c.name, response={"result": "done"})
                    for c in message.tool_call.function_calls])
            if content and content.model_turn:
                first = first or time.monotonic() - sent
            if content and content.turn_complete and first is not None:
                return
    try:
        await asyncio.wait_for(read(), wait)
    except asyncio.TimeoutError:
        pass
    return first, handle


async def main() -> None:
    client = _client()
    async with client.aio.live.connect(model=config.MODEL, config=_live_config()) as s1:
        first, handles = await turn(s1, "Say just: ready.")
        print(f"session 1: reply after {first}")
        await asyncio.sleep(2)
    settings = _live_config()
    if RESUME and handles:
        settings.session_resumption = types.SessionResumptionConfig(handle=handles[-1])
    async with client.aio.live.connect(model=config.MODEL, config=settings) as s2:
        for text in ("What time is it?", "Say just: second."):
            first, _ = await turn(s2, text)
            print(f"session 2 ({'resumed' if RESUME and handles else 'fresh'}"
                  f"{', stream_end' if STREAM_END else ''}): {text!r} -> "
                  + (f"reply after {first:.2f}s" if first else "NO REPLY in 15 s"))


if __name__ == "__main__":
    asyncio.run(main())
