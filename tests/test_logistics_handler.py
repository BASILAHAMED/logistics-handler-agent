"""Comprehensive test suite for Logistics Handler.

Includes:
1. Offline agent tests (via FakeLlm)
2. MCP server tool unit tests
3. Geo / Routing API self-healing tests
4. Feedback loop persistence tests
5. Database self-repair test
"""

import pytest
import os
import sqlite3
from unittest.mock import patch

from lh import db, geo, healing, feedback
from lh.mcp_server import list_shipments, get_shipment, check_inventory, find_stock, quote_carriers
from lh.testing import FakeLlm, ask


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    test_db = tmp_path / "test_logistics.db"
    os.environ["LH_DB_PATH"] = str(test_db)
    db.reset(test_db)
    yield
    if test_db.exists():
        test_db.unlink()


# ------------------------------------------------------------------ MCP Tool Tests

def test_mcp_shipment_queries():
    res = list_shipments(status="IN_TRANSIT")
    assert res["status"] == "success"
    assert res["count"] > 0

    s = get_shipment("SHP-1001")
    assert s["status"] == "success"
    assert s["shipment"]["id"] == "SHP-1001"
    assert s["shipment"]["origin_wh"] == "WH-CHN"


def test_mcp_inventory_and_quotes():
    inv = check_inventory(sku="SKU-BAG-01", warehouse="WH-CHN")
    assert inv["status"] == "success"
    assert inv["items"][0]["qty"] > 0

    stock = find_stock("SKU-LAP-14", 10)
    assert stock["status"] == "success"
    assert len(stock["warehouses"]) > 0

    quotes = quote_carriers(distance_km=350.0, sku="SKU-BAG-01", qty=50, deadline_hours=24.0)
    assert quotes["status"] == "success"
    assert len(opts := quotes["options"]) > 0
    assert opts[0]["meets_deadline"] is True


# ------------------------------------------------------------------ Geo & Self-Healing Tests

def test_geocode_and_road_distance():
    loc = geo.geocode("WH-CHN")
    assert loc["status"] == "success"
    assert loc["lat"] > 0

    dist = geo.road_distance("WH-CHN", "Bengaluru")
    assert dist["status"] == "success"
    assert dist["distance_km"] > 300


def test_weather_risk_resilience():
    w = geo.weather_risk("Chennai")
    assert w["status"] == "success"
    assert w["overall_risk"] in ("LOW", "MEDIUM", "HIGH", "UNKNOWN")


def test_db_self_repair(tmp_path):
    corrupt_db = tmp_path / "corrupt.db"
    corrupt_db.write_bytes(b"NOT A SQLITE FILE")
    con = db.connect(corrupt_db)
    assert con is not None
    # Verify it re-seeded tables
    row = con.execute("SELECT COUNT(*) FROM warehouses").fetchone()
    assert row[0] > 0


# ------------------------------------------------------------------ Feedback Loop Test

def test_feedback_loop_recording():
    res = feedback.record_feedback(2, "Avoid EcoFreight for fragile goods", topic="carrier choice")
    assert res["status"] == "success"
    assert "EcoFreight" in res["lesson_learned"]

    ls = feedback.lessons()
    assert len(ls) > 0
    assert "EcoFreight" in ls[0]


# ------------------------------------------------------------------ Agent Offline Integration Test

@pytest.mark.asyncio
async def test_agent_loads_and_runs():
    os.environ["MODEL"] = "fake"
    from agents.logistics_handler.agent import root_agent

    root_agent.model = FakeLlm(script=[
        "I can assist with logistics operations. What shipment or stock would you like to check?"
    ])

    text, events, runner, sid = await ask(root_agent, "Hello")
    assert "logistics operations" in text
