"""Configuration + multi-model routing. Everything is swappable from .env, no code edits.

Each agent asks for a *role*; each role maps to an ordered FALLBACK CHAIN of models.
If the first model errors (429 quota, 5xx, timeout, malformed output) the next one
answers — see lh.healing.FallbackLlm.

  MODEL_FAST      intake / lookups      default: gemini-flash-lite-latest -> gemini-flash-latest
  MODEL           planning / chat       default: gemini-flash-latest -> gemini-flash-lite-latest
  MODEL_SMART     exceptions / risk     default: gemini-pro-latest -> gemini-flash-latest
  MODEL_REVIEWER  plan critic           default: Claude if ANTHROPIC_API_KEY is set, else MODEL_SMART

Values are comma-separated. Plain names = Gemini; `provider/model` = LiteLLM
(e.g. anthropic/claude-sonnet-4-6, openai/gpt-4o, ollama_chat/llama3.2); `fake` = offline.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env", override=False)
os.environ.setdefault("PYTHONUTF8", "1")  # LiteLLM cp1252 pitfall on Windows

DATA_DIR = Path(os.getenv("LH_DATA_DIR", ROOT / "data"))
DB_PATH = Path(os.getenv("LH_DB_PATH", DATA_DIR / "logistics.db"))

# Dispatches above this cost (INR) need explicit human approval before booking.
APPROVAL_LIMIT_INR = float(os.getenv("APPROVAL_LIMIT_INR", "25000"))

DEFAULT_CHAINS = {
    "fast": "gemini-3.5-flash-lite,gemini-3.8-flash",
    "default": "gemini-3.8-flash,gemini-3.5-flash-lite",
    "smart": "gemini-3.8-flash,gemini-3.5-flash-lite",
}


def chain(role: str = "default") -> list[str]:
    env = {"fast": "MODEL_FAST", "default": "MODEL", "smart": "MODEL_SMART", "reviewer": "MODEL_REVIEWER"}[role]
    raw = os.getenv(env)
    if not raw and role == "reviewer":
        # Cross-vendor critic = genuinely multi-model: a second model family checks Gemini's plan.
        if os.getenv("ANTHROPIC_API_KEY"):
            raw = "anthropic/claude-sonnet-4-6," + ",".join(chain("smart"))
        else:
            return chain("smart")
    if not raw and os.getenv("MODEL") == "fake":
        raw = "fake"
    raw = raw or DEFAULT_CHAINS[role]
    return [m.strip() for m in raw.split(",") if m.strip()]


def is_gemini(role: str = "default") -> bool:
    return chain(role)[0].startswith("gemini")


def model(role: str = "default"):
    """Return something `Agent(model=...)` accepts for this role."""
    names = chain(role)
    if names == ["fake"]:
        from lh.testing import FakeLlm

        return FakeLlm()
    from lh.healing import FallbackLlm

    return FallbackLlm(model=names[0], chain=names)
