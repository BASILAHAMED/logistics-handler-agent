"""Self-healing layer. Four mechanisms, all visible through the `system_health` tool:

1. FallbackLlm         - every agent's model is a chain; on 429/5xx/timeout/empty output the
                         next model answers, and a failing model is benched (circuit breaker).
2. resilient()         - every external API call retries with exponential backoff, then falls
                         through to the next provider (live API -> secondary API -> offline estimate),
                         and marks the answer `degraded` so agents can tell the user.
3. ToolRetryPlugin     - ADK ReflectAndRetryToolPlugin: when a tool raises OR returns
                         {"status": "error"}, the model gets the error back with guidance,
                         reflects, fixes its arguments and retries (max 2) instead of crashing.
4. ReflectAndRetryModelPlugin - re-prompts the model when it emits a malformed function call.
(+ lh.db self-repairs a missing/corrupt database by re-seeding it.)
"""

from __future__ import annotations

import logging
import os
import time
from collections import deque
from typing import Any, AsyncGenerator, Callable

from google.adk.models import BaseLlm, LlmRequest, LlmResponse
from google.adk.plugins import ReflectAndRetryToolPlugin
from pydantic import Field, PrivateAttr

log = logging.getLogger("lh.healing")
if not log.handlers:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(levelname)s %(message)s")

# ------------------------------------------------------------------ health registry


class Health:
    """Process-wide record of failures, recoveries and open circuits (shown to judges/users)."""

    def __init__(self):
        self.events: deque[dict] = deque(maxlen=50)
        self.fail_count: dict[str, int] = {}
        self.open_until: dict[str, float] = {}
        self.stats = {"recoveries": 0, "failures": 0, "calls": 0}

    def ok(self, name: str, recovered_from: str | None = None):
        self.stats["calls"] += 1
        self.fail_count[name] = 0
        self.open_until.pop(name, None)
        if recovered_from:
            self.stats["recoveries"] += 1
            self.events.append({"t": time.strftime("%H:%M:%S"), "kind": "recovered", "via": name, "after": recovered_from})

    def fail(self, name: str, err: Exception | str, *, threshold: int = 2, cooldown: float = 60):
        self.stats["failures"] += 1
        n = self.fail_count.get(name, 0) + 1
        self.fail_count[name] = n
        if n >= threshold:
            self.open_until[name] = time.time() + cooldown
        self.events.append({"t": time.strftime("%H:%M:%S"), "kind": "failure", "component": name, "error": str(err)[:160]})
        log.warning("self-heal: %s failed (%s): %s", name, n, str(err)[:200])

    def is_open(self, name: str) -> bool:
        return self.open_until.get(name, 0) > time.time()

    def snapshot(self) -> dict:
        return {
            "stats": dict(self.stats),
            "open_circuits": {k: round(v - time.time()) for k, v in self.open_until.items() if v > time.time()},
            "recent_events": list(self.events)[-10:],
        }

    def reset(self):
        self.__init__()


HEALTH = Health()

# ------------------------------------------------------------------ resilient API calls


def resilient(providers: list[tuple[str, Callable[[], Any]]], *, retries: int = 2, backoff: float = 0.4) -> dict:
    """Try providers in order; each gets `retries` attempts with exponential backoff.

    Returns {"result", "source", "degraded", "errors"}. Providers whose circuit is open are
    skipped (but still tried last if everything else fails).
    """
    ordered = [p for p in providers if not HEALTH.is_open(p[0])] + [p for p in providers if HEALTH.is_open(p[0])]
    errors: list[str] = []
    for idx, (name, fn) in enumerate(ordered):
        for attempt in range(retries):
            try:
                result = fn()
                HEALTH.ok(name, recovered_from=errors[-1].split(":")[0] if errors else None)
                return {"result": result, "source": name, "degraded": idx > 0, "errors": errors}
            except Exception as e:  # noqa: BLE001 - any provider failure triggers healing
                errors.append(f"{name}: {type(e).__name__}: {str(e)[:120]}")
                HEALTH.fail(name, e)
                if attempt + 1 < retries:
                    time.sleep(backoff * (2**attempt))
    raise RuntimeError("all providers failed: " + " | ".join(errors))


# ------------------------------------------------------------------ model fallback


def _build_llm(name: str) -> BaseLlm:
    if name == "fake":
        from lh.testing import FakeLlm

        return FakeLlm()
    if "/" in name:
        from google.adk.models.lite_llm import LiteLlm

        return LiteLlm(model=name)
    from google.adk.models import LLMRegistry

    return LLMRegistry.new_llm(name)


class FallbackLlm(BaseLlm):
    """A model that is really an ordered chain of models with failover + circuit breaking.

    `model` is the primary's name so ADK's built-in tools (google_search) see a Gemini model.
    """

    model: str
    chain: list[str] = Field(default_factory=list)
    _llms: dict[str, BaseLlm] = PrivateAttr(default_factory=dict)

    @classmethod
    def from_llms(cls, llms: dict[str, BaseLlm]) -> "FallbackLlm":
        """Build from ready-made model objects (used by tests to simulate outages)."""
        names = list(llms)
        inst = cls(model=names[0], chain=names)
        inst._llms = dict(llms)
        return inst

    def _llm(self, name: str) -> BaseLlm:
        if name not in self._llms:
            self._llms[name] = _build_llm(name)
        return self._llms[name]

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        names = self.chain or [self.model]
        order = [n for n in names if not HEALTH.is_open(f"model:{n}")] + [n for n in names if HEALTH.is_open(f"model:{n}")]
        last_err: Exception | None = None
        failed: str | None = None
        for name in order:
            key = f"model:{name}"
            req = llm_request.model_copy(deep=True)
            req.model = name
            yielded = False
            try:
                async for resp in self._llm(name).generate_content_async(req, stream=stream):
                    if not yielded and resp.error_code and not resp.content:
                        raise RuntimeError(f"{resp.error_code}: {resp.error_message}")
                    yielded = True
                    yield resp
                if not yielded:
                    raise RuntimeError("empty response")
                HEALTH.ok(key, recovered_from=failed)
                return
            except Exception as e:  # noqa: BLE001
                if yielded:  # already streamed partial output; can't transparently switch
                    raise
                last_err = e
                failed = key
                HEALTH.fail(key, e, threshold=1, cooldown=float(os.getenv("MODEL_COOLDOWN_S", "90")))
        raise RuntimeError(f"all models failed ({', '.join(order)}): {last_err}")


# ------------------------------------------------------------------ tool reflect & retry


class ToolRetryPlugin(ReflectAndRetryToolPlugin):
    """Also treat {"status": "error"} results as failures so the model reflects and retries."""

    def __init__(self, max_retries: int = 2):
        super().__init__(name="lh_tool_retry", max_retries=max_retries, throw_exception_if_retry_exceeded=False)

    async def extract_error_from_result(self, *, tool, tool_args, tool_context, result):
        if isinstance(result, dict) and result.get("status") == "error" and result.get("retryable", True):
            HEALTH.fail(f"tool:{tool.name}", result.get("error", "error"), threshold=99)
            return result
        return None


def plugins() -> list:
    from google.adk.plugins import ReflectAndRetryModelPlugin

    return [ToolRetryPlugin(max_retries=2), ReflectAndRetryModelPlugin(max_retries=2, throw_exception_if_retry_exceeded=False)]
