"""Offline model + tiny runner helpers so agents can be exercised with no API key.

FakeLlm(script=[...]) replays scripted turns in order. Each item is either:
  - str                       -> model replies with that text
  - ("call", name, {args})    -> model emits a function call
With no script (MODEL=fake) it echoes the latest user text.
"""

from __future__ import annotations

import uuid
from typing import Any, AsyncGenerator

from google.adk.models import BaseLlm, LlmRequest, LlmResponse
from google.genai import types
from pydantic import Field


class FakeLlm(BaseLlm):
    model: str = "fake"
    script: list[Any] = Field(default_factory=list)
    calls: int = 0
    last_prompt: str = ""  # all text the model saw on its latest call (for assertions)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        self.calls += 1
        self.last_prompt = " ".join(p.text or "" for c in llm_request.contents or [] for p in c.parts or [])
        if self.script:
            step = self.script.pop(0)
        else:
            step = self._auto(llm_request)
        if isinstance(step, tuple) and step[0] == "call":
            _, name, args = step
            part = types.Part(function_call=types.FunctionCall(id=f"fc-{uuid.uuid4().hex[:8]}", name=name, args=args))
        else:
            part = types.Part(text=str(step))
        yield LlmResponse(content=types.Content(role="model", parts=[part]))

    @staticmethod
    def _auto(req: LlmRequest):
        """Echo the latest user text — enough to smoke-test any wiring offline."""
        for c in reversed(req.contents or []):
            if c.role == "user":
                user = " ".join(p.text for p in c.parts or [] if getattr(p, "text", None))
                if user:
                    return f"[fake] {user[:200]}"
        return "[fake] ok"


async def ask(agent, text: str, *, user_id: str = "u1", session_id: str | None = None, runner=None):
    """Send one message to an agent; return (final_text, events, runner, session_id)."""
    from google.adk.runners import InMemoryRunner

    runner = runner or InMemoryRunner(agent=agent, app_name="test_app")
    if session_id is None:
        s = await runner.session_service.create_session(app_name=runner.app_name, user_id=user_id)
        session_id = s.id
    events = []
    msg = types.Content(role="user", parts=[types.Part(text=text)])
    async for ev in runner.run_async(user_id=user_id, session_id=session_id, new_message=msg):
        events.append(ev)
    texts = [
        p.text
        for ev in events
        if ev.content and ev.author != "user"
        for p in ev.content.parts or []
        if getattr(p, "text", None)
    ]
    return (texts[-1] if texts else ""), events, runner, session_id

