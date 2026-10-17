"""Logistics Handler — Google ADK multi-agent, multi-model, self-healing logistics desk.

Features for competition submission:
1. Multi-Agent Team: Coordinator + Inventory Tracker + Route Planner + Exception Handler + Plan Reviewer.
2. Tooling: 10 MCP tools (via McpToolset) + Live Geo/Weather APIs + Self-Healing metrics + User Feedback.
3. Multi-Model: Gemini 3.8 Flash / 3.5 Flash Lite + Fallback chains + cross-vendor support.
4. Feedback Loop:
   - Loop 1: Plan Reviewer critiques dispatch plans before execution.
   - Loop 2: User ratings/comments dynamically update instructions (`record_feedback`).
5. Self-Healing: `FallbackLlm` model failover + API `resilient()` backoff + `ToolRetryPlugin`.
"""

from __future__ import annotations

import sys
from google.adk import Agent
from google.adk.apps import App
from google.adk.tools import FunctionTool, McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp.client.stdio import StdioServerParameters

from lh import callbacks, config, feedback, geo, healing

# ------------------------------------------------------------------ MCP Server Toolset

mcp_params = StdioServerParameters(command=sys.executable, args=["-m", "lh.mcp_server"])
mcp_conn = StdioConnectionParams(server_params=mcp_params)
mcp_toolset = McpToolset(connection_params=mcp_conn)

# ------------------------------------------------------------------ Specialized Sub-Agents

tracker_agent = Agent(
    name="inventory_tracker",
    mode="single_turn",
    model=config.model("fast"),
    description="Tracks shipments, verifies stock availability, and checks warehouse inventory.",
    instruction="""You are the Inventory & Tracking Specialist.
Use MCP tools to inspect shipment status, query stock across warehouses, or locate items.
Always report stock levels, location, and warehouse city clearly.""",
    tools=[mcp_toolset],
)

planner_agent = Agent(
    name="route_planner",
    mode="single_turn",
    model=config.model("smart"),
    description="Geocodes locations, calculates road distances, assesses weather risk, and quotes carriers.",
    instruction="""You are the Logistics Route Planner.
1. Geocode cities/warehouses and calculate road distances (`road_distance`).
2. Check 3-day weather risk (`weather_risk`) along the route.
3. Fetch carrier quotes (`quote_carriers`).
4. Select the optimal carrier meeting deadline, cost, and safety/cold-chain constraints.""",
    tools=[
        mcp_toolset,
        FunctionTool(geo.geocode),
        FunctionTool(geo.road_distance),
        FunctionTool(geo.weather_risk),
    ],
)

exception_agent = Agent(
    name="exception_handler",
    mode="single_turn",
    model=config.model("smart"),
    description="Resolves delayed or broken down shipments, reassigns carriers, and logs incidents.",
    instruction="""You are the Incident & Recovery Specialist.
When a shipment is delayed or stuck:
1. Fetch full details (`get_shipment`).
2. Log the incident (`log_incident`).
3. Find an alternative active carrier and reassign (`reassign_carrier`).""",
    tools=[
        mcp_toolset,
        FunctionTool(geo.weather_risk),
    ],
)

reviewer_agent = Agent(
    name="plan_reviewer",
    mode="single_turn",
    model=config.model("reviewer"),
    description="Critiques proposed logistics plans against cost, deadline, weather risk, and historical user feedback.",
    instruction=feedback.with_lessons("""You are the Chief Logistics Auditor (Feedback & Quality Assurance).
Review the proposed shipment or recovery plan against these rules:
1. Cost vs Budget limits (Approval required if > ₹25,000).
2. Deadline feasibility and weather risks along the route.
3. Lessons learned from past user feedback:
{LESSONS}

Respond with either 'APPROVED: <summary>' or 'REJECTED: <reasons to fix>'."""),
)

# ------------------------------------------------------------------ Root Agent

root_agent = Agent(
    name="logistics_handler",
    model=config.model("default"),
    description="Autonomous AI Logistics Operations Desk with multi-agent coordination, MCP, self-healing, and feedback loops.",
    instruction=feedback.with_lessons("""You are Logistics Handler — an autonomous AI operations desk for supply chain logistics.

You handle:
1. Order Dispatching & Route Optimization (route_planner).
2. Shipment Tracking & Inventory Inquiries (inventory_tracker).
3. Exception Recovery & Breakdown Resolution (exception_handler).
4. System Health & Self-Healing Telemetry (`system_health`).
5. Recording User Feedback (`record_feedback`).

Workflow Rules:
- For new shipment orders: Get options from `route_planner`, audit with `plan_reviewer`, then book (`create_shipment`).
- If total cost > ₹25,000, ask the user for explicit confirmation before booking.
- For user feedback or corrections: Call `record_feedback(rating, comment, topic)`.

{LESSONS}"""),
    sub_agents=[tracker_agent, planner_agent, exception_agent, reviewer_agent],
    tools=[
        mcp_toolset,
        FunctionTool(geo.geocode),
        FunctionTool(geo.road_distance),
        FunctionTool(geo.weather_risk),
        FunctionTool(geo.system_health),
        FunctionTool(feedback.record_feedback),
    ],
    before_model_callback=callbacks.input_guardrail,
    before_tool_callback=callbacks.log_tool_start,
    after_tool_callback=callbacks.log_tool_end,
)

app = App(
    name="logistics_handler",
    root_agent=root_agent,
    plugins=healing.plugins(),
)
