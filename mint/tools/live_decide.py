"""Web-step decisions by a Gemini Live model, when Jev isn't there.

Live models carry far more quota than the regular ones (a per-minute budget each, no tight daily cap), and Mint
already has them. They don't answer in text, only speech - but they call tools: the decision comes back as the
arguments of a `decide` call, in about 0.7 s, and no audio is made (the session is closed at the call).

- A fresh session per decision: a kept session resends its whole history each turn, which eats the per-minute
  budget. The next session is opened while the browser carries out the current step, so the ~1 s connect is hidden.
- A model the conversation isn't on, the least used this minute; the conversation's own only with plenty of
  room this minute; never a thinking model (it won't call the tool).
  Its tokens are counted in live_models, so the voice pool sees them.
- At most one decider session at a time, so the user's own voice session is never crowded out. A warm session
  nobody uses for 30 s is closed.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

log = logging.getLogger("mint.tools.live_decide")

WARM_FOR = 30.0               # a pre-opened session nobody used for this long is closed
SYSTEM = ("You choose the next step of a browser task. You never speak. You answer every message by calling the "
          "decide function exactly once, with the fields it asks for.")


def _declaration():
    from google.genai import types
    S = types.Type
    return types.FunctionDeclaration(
        name="decide", description="The next browser step.",
        parameters=types.Schema(type=S.OBJECT, required=["not_done_yet", "operation"], properties={
            "not_done_yet": types.Schema(type=S.ARRAY, items=types.Schema(type=S.STRING),
                                         description="Each part of the goal not yet visibly done."),
            "operation": types.Schema(type=S.STRING, description="One key of OPERATIONS."),
            "target": types.Schema(type=S.STRING, description="One index from TARGETS[operation], or empty."),
            "confidence": types.Schema(type=S.NUMBER, description="0 to 1.")}))


SHARE = 0.4                   # the conversation's own model only while under this share of its minute's budget
PROMPT_TOKENS = 4000          # about one decision


def pick_model() -> str | None:
    """A Live model for decisions, or None (the regular models decide then):
    - not a thinking model (it won't call decide: it refuses or stays silent);
    - not benched or resting; the least used one this minute;
    - the conversation's own model only when no other is free and it has plenty of room this minute (its turns are
      counted in live_models in this same process), so the user's voice is never slowed."""
    try:
        from mint.core import config
        from mint.voice import live_models
        now = time.time()
        models = [m for m in live_models.pool() if "thinking" not in m and live_models.benched(m, now) <= 0]
        others = [m for m in models if m != config.MODEL]
        if others:
            return min(others, key=lambda m: live_models.used(m))
        if config.MODEL in models and live_models.used(config.MODEL) + PROMPT_TOKENS < \
                SHARE * live_models._tpm_limit():
            return config.MODEL
        return None
    except Exception:
        log.debug("no Live model for decisions", exc_info=True)
        return None


class Decider:
    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.lock = threading.Lock()               # one decision (and so one Live session) at a time
        self.warm: asyncio.Task | None = None
        self.warm_since = 0.0
        self.down_until = 0.0

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self.loop is None:
            self.loop = asyncio.new_event_loop()
            threading.Thread(target=self.loop.run_forever, name="live-decide", daemon=True).start()
        return self.loop

    async def _open(self, model: str):
        from google.genai import types

        from mint.core import llm
        config = types.LiveConnectConfig(response_modalities=["AUDIO"], system_instruction=SYSTEM,
                                         tools=[types.Tool(function_declarations=[_declaration()])])
        manager = llm.client().aio.live.connect(model=model, config=config)
        session = await manager.__aenter__()
        return manager, session, model

    async def _close(self, opened) -> None:
        try:
            await opened[0].__aexit__(None, None, None)
        except Exception:
            pass

    def _warm_next(self) -> None:
        model = pick_model()
        if model is None:
            return
        self.warm = asyncio.ensure_future(self._open(model))
        self.warm_since = time.monotonic()
        self.loop.call_later(WARM_FOR, self._expire, self.warm)

    def _expire(self, task: asyncio.Task) -> None:
        if self.warm is task:                      # still unused
            self.warm = None
            task.add_done_callback(lambda t: None if t.cancelled() or t.exception() else
                                   asyncio.ensure_future(self._close(t.result())))

    async def _ask(self, prompt: str, timeout: float):
        from google.genai import types

        from mint.voice import live_models
        task, self.warm = self.warm, None
        if task is None:
            model = pick_model()
            if model is None:
                raise RuntimeError("no Live model free for decisions")
            task = asyncio.ensure_future(self._open(model))
        opened = await asyncio.wait_for(task, 6)
        manager, session, model = opened
        counted = [False]
        try:
            await session.send_client_content(turns=types.Content(role="user", parts=[types.Part(text=prompt)]),
                                              turn_complete=True)

            async def first_call():
                async for message in session.receive():
                    usage = getattr(message, "usage_metadata", None)
                    if usage is not None and getattr(usage, "prompt_token_count", None) and not counted[0]:
                        live_models.tokens(model, int(usage.prompt_token_count))
                        counted[0] = True
                    if message.tool_call and message.tool_call.function_calls:
                        return dict(message.tool_call.function_calls[0].args or {})
                    server = message.server_content
                    if server is not None and server.turn_complete:
                        return None
                return None
            answer = await asyncio.wait_for(first_call(), timeout)
        finally:
            await self._close(opened)
            self._warm_next()                      # ready for the next step while this one is carried out
        if not counted[0]:
            live_models.tokens(model, len(prompt) // 3)          # no usage reported: a fair estimate
        if answer is None:
            raise RuntimeError(f"{model} answered without a decision")
        return answer, model

    def decide(self, prompt: str, timeout: float = 8.0) -> tuple[dict, str]:
        """-> (the decide call's arguments, the model). Raises when no Live model could answer."""
        if time.monotonic() < self.down_until:
            raise RuntimeError("Live decisions resting after a failure")
        with self.lock:
            loop = self._ensure_loop()
            future = asyncio.run_coroutine_threadsafe(self._ask(prompt, timeout), loop)
            try:
                return future.result(timeout + 8)
            except Exception as error:
                future.cancel()
                self.down_until = time.monotonic() + 60
                raise RuntimeError(f"Live decision failed: {str(error)[:160]}") from None

    def close(self) -> None:
        if self.loop is None:
            return
        task, self.warm = self.warm, None
        if task is not None:
            async def shut():
                try:
                    await self._close(await task)
                except Exception:
                    pass
            asyncio.run_coroutine_threadsafe(shut(), self.loop)


decider = Decider()
