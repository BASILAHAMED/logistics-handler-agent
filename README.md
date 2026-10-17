# Logistics Handler 📦

An autonomous AI operations desk for supply chain logistics, built for the **Agent Wars** competition.

## Features (Competition Focus)
- **Multi-Agent Coordination**: Root agent delegates to tracking, planning, recovery, and auditing specialists.
- **System of Record (MCP)**: 10 tools managing shipments, inventory, and carriers via Model Context Protocol.
- **Self-Healing (Production Grade)**:
  - **Model Failover**: `FallbackLlm` chain ensures uptime even if Gemini quotas are hit.
  - **API Resilience**: Geocoding and routing tools automatically fall back to secondary providers or offline estimates.
  - **Reflection**: Tool and model retry plugins for error recovery.
- **Dual Feedback Loops**:
  - **Reviewer Critique**: Every plan is audited by a separate model (`plan_reviewer`) before execution.
  - **User Learning**: `record_feedback` persists user corrections into the agent's instructions.
- **Human-in-the-Loop**: Large dispatches (>₹25,000) pause for user confirmation.

## Quick Start
1. **Setup**:
   ```bash
   uv sync
   cp .env.example .env # Add your GOOGLE_API_KEY
   ```
2. **Run Tests** (Offline, safe for competition speed):
   ```bash
   uv run pytest
   ```
3. **Launch UI**:
   ```bash
   uv run adk web agents
   ```

## Competition Readiness
- **Multi-Model**: Configured to use **Gemini 3.8 Flash** for speed and **Claude** (via LiteLLM) for auditing.
- **First Come, First Served**: Built on a modular kit for rapid deployment to Cloud Run.
