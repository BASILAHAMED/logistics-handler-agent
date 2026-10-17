"""Guardrails + observability callbacks."""

from __future__ import annotations

import logging
import os
import re
import time

from google.adk.models import LlmRequest, LlmResponse
from google.genai import types

log = logging.getLogger("lh")
if not log.handlers:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(levelname)s %(message)s")

_blocked = re.compile(
    r"ignore (all|previous|prior|your) (instructions|rules)|reveal (your|the) (system )?prompt|\bapi[_ -]?key\b"
    r"|drop table|delete (all|every) (shipments|records)",
    re.I,
)


def _last_user_text(req: LlmRequest) -> str:
    for c in reversed(req.contents or []):
        if c.role == "user":
            t = " ".join(p.text for p in (c.parts or []) if getattr(p, "text", None))
            if t:
                return t
    return ""


def input_guardrail(callback_context, llm_request: LlmRequest):
    """Block prompt-injection / destructive requests before they reach any model."""
    text = _last_user_text(llm_request)
    if text and _blocked.search(text):
        log.warning("guardrail blocked: %r", text[:120])
        return LlmResponse(content=types.Content(role="model", parts=[types.Part(
            text="I can't help with that. I can track shipments, plan dispatches, check stock or handle delays.")]))
    return None


def log_tool_start(tool, args: dict, tool_context):
    tool_context.state[f"temp:t0_{tool.name}"] = time.perf_counter()
    log.info("tool -> %s %s", tool.name, {k: v for k, v in args.items()})
    return None


def log_tool_end(tool, args: dict, tool_context, tool_response):
    t0 = tool_context.state.get(f"temp:t0_{tool.name}")
    ms = (time.perf_counter() - t0) * 1000 if t0 else -1
    status = tool_response.get("status") if isinstance(tool_response, dict) else "ok"
    log.info("tool <- %s status=%s %.0fms", tool.name, status, ms)
    return None
