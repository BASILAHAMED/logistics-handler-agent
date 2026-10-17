"""Logistics MCP server — the system of record, exposed over the Model Context Protocol.

Any MCP client (our ADK agents, Claude Desktop, Cursor, MCP Inspector) can use it:
    uv run python -m lh.mcp_server            # stdio transport (what the agents spawn)
    uv run python -m lh.mcp_server --http     # streamable-http on :8765/mcp

Every tool returns {"status": "success", ...} or {"status": "error", "error", "hint"}; error
results trigger the agents' reflect-and-retry self-healing (lh.healing.ToolRetryPlugin).
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta

from mcp.server.mcpserver import MCPServer

from lh import db

logging.basicConfig(stream=sys.stderr, level=logging.WARNING)  # stdout is the MCP channel

mcp = MCPServer(
    "logistics",
    instructions="Logistics system of record: shipments, warehouses, stock, carriers, quotes, incidents.",
)

STATUSES = ("CREATED", "IN_TRANSIT", "DELAYED", "EXCEPTION", "DELIVERED", "CANCELLED")


def _err(error: str, hint: str = "", retryable: bool = True) -> dict:
    return {"status": "error", "error": error, "hint": hint, "retryable": retryable}


def _ship_or_err(con, shipment_id: str):
    sid = (shipment_id or "").strip().upper()
    if sid and not sid.startswith("SHP-") and sid.isdigit():
        sid = f"SHP-{sid}"
    r = con.execute("SELECT * FROM shipments WHERE id=?", (sid,)).fetchone()
    if not r:
        ids = [x[0] for x in con.execute("SELECT id FROM shipments ORDER BY id DESC LIMIT 10")]
        return None, _err(f"shipment {shipment_id!r} not found", f"known ids include {ids}; ids look like SHP-1001")
    return dict(r), None


# ------------------------------------------------------------------ shipments


@mcp.tool()
def list_shipments(status: str = "", customer: str = "", limit: int = 20) -> dict:
    """List shipments, newest first. Filter by status (CREATED, IN_TRANSIT, DELAYED, EXCEPTION,
    DELIVERED, CANCELLED; comma-separated allowed) and/or customer name substring."""
    q, args = "SELECT id,order_ref,customer,origin_wh,destination,sku,qty,carrier_id,status,eta,deadline,last_location FROM shipments WHERE 1=1", []
    if status:
        sts = [s.strip().upper() for s in status.split(",") if s.strip()]
        bad = [s for s in sts if s not in STATUSES]
        if bad:
            return _err(f"unknown status {bad}", f"use one of {STATUSES}")
        q += f" AND status IN ({','.join('?' * len(sts))})"
        args += sts
    if customer:
        q += " AND customer LIKE ?"
        args.append(f"%{customer}%")
    q += " ORDER BY created_at DESC LIMIT ?"
    args.append(max(1, min(int(limit), 100)))
    with db.connect() as con:
        out = db.rows(con.execute(q, args))
    return {"status": "success", "count": len(out), "shipments": out}


@mcp.tool()
def get_shipment(shipment_id: str) -> dict:
    """Full detail for one shipment: route, carrier, ETA vs deadline, event history and incidents."""
    with db.connect() as con:
        s, err = _ship_or_err(con, shipment_id)
        if err:
            return err
        s["events"] = db.rows(con.execute("SELECT at,status,note FROM shipment_events WHERE shipment_id=? ORDER BY id", (s["id"],)))
        s["incidents"] = db.rows(con.execute("SELECT id,kind,description,action,at,resolved FROM incidents WHERE shipment_id=?", (s["id"],)))
        c = con.execute("SELECT name,mode,reliability FROM carriers WHERE id=?", (s["carrier_id"],)).fetchone()
        s["carrier"] = dict(c) if c else None
    if s["eta"] and s["deadline"]:
        s["late_by_hours"] = round(max(0.0, (datetime.fromisoformat(s["eta"]) - datetime.fromisoformat(s["deadline"])).total_seconds() / 3600), 1)
    s["at_risk"] = s["status"] in ("DELAYED", "EXCEPTION") or bool(s.get("late_by_hours"))
    return {"status": "success", "shipment": s}


@mcp.tool()
def update_shipment_status(shipment_id: str, status: str, note: str, location: str = "", eta_hours_from_now: float = -1) -> dict:
    """Move a shipment to a new status with a note (and optional new location / ETA in hours from now)."""
    status = status.strip().upper()
    if status not in STATUSES:
        return _err(f"unknown status {status!r}", f"use one of {STATUSES}")
    with db.connect() as con:
        s, err = _ship_or_err(con, shipment_id)
        if err:
            return err
        now = db.now_iso()
        eta = s["eta"]
        if eta_hours_from_now >= 0:
            eta = (datetime.now() + timedelta(hours=eta_hours_from_now)).isoformat(timespec="minutes")
        con.execute("UPDATE shipments SET status=?, updated_at=?, eta=?, last_location=COALESCE(NULLIF(?,''), last_location) WHERE id=?",
                    (status, now, eta, location, s["id"]))
        con.execute("INSERT INTO shipment_events(shipment_id,at,status,note) VALUES(?,?,?,?)", (s["id"], now, status, note))
    return {"status": "success", "shipment_id": s["id"], "new_status": status, "eta": eta}


@mcp.tool()
def create_shipment(order_ref: str, customer: str, origin_wh: str, destination: str, sku: str, qty: int,
                    carrier_id: str, distance_km: float, cost_inr: float, eta_hours: float, deadline_hours: float) -> dict:
    """Book a shipment: validates stock + carrier capacity, reserves stock, creates the record.
    Use values from quote_carriers. High-value bookings are gated by human approval in the agent."""
    with db.connect() as con:
        sku_row = con.execute("SELECT * FROM skus WHERE sku=?", (sku.upper(),)).fetchone()
        if not sku_row:
            return _err(f"unknown sku {sku!r}", "call check_inventory() to list SKUs")
        inv = con.execute("SELECT qty FROM inventory WHERE warehouse=? AND sku=?", (origin_wh.upper(), sku.upper())).fetchone()
        if not inv or inv[0] < qty:
            return _err(f"{origin_wh} has {inv[0] if inv else 0} of {sku}, need {qty}", "call find_stock(sku, qty) and pick another warehouse")
        car = con.execute("SELECT * FROM carriers WHERE id=? AND active=1", (carrier_id.upper(),)).fetchone()
        if not car:
            return _err(f"unknown/inactive carrier {carrier_id!r}", "call quote_carriers() and use its carrier_id")
        weight = round(sku_row["weight_kg"] * qty, 1)
        if weight > car["max_kg"]:
            return _err(f"{car['name']} max load is {car['max_kg']}kg, shipment is {weight}kg", "pick a higher-capacity carrier")
        if sku_row["cold_chain"] and not car["cold_chain"]:
            return _err(f"{sku} needs cold chain; {car['name']} has none", "use CR-COLD or CR-AIR")
        n = datetime.now()
        new_id = "SHP-" + str(1 + max(int(x[0][4:]) for x in con.execute("SELECT id FROM shipments")))
        con.execute("UPDATE inventory SET qty=qty-? WHERE warehouse=? AND sku=?", (qty, origin_wh.upper(), sku.upper()))
        con.execute("INSERT INTO shipments VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            new_id, order_ref, customer, origin_wh.upper(), destination, sku.upper(), qty, weight, car["id"], "CREATED",
            round(cost_inr, 2), distance_km, (n + timedelta(hours=deadline_hours)).isoformat(timespec="minutes"),
            (n + timedelta(hours=eta_hours)).isoformat(timespec="minutes"), db.now_iso(), db.now_iso(), f"{origin_wh.upper()} dock"))
        con.execute("INSERT INTO shipment_events(shipment_id,at,status,note) VALUES(?,?,?,?)", (new_id, db.now_iso(), "CREATED", f"booked with {car['name']}"))
        left = con.execute("SELECT qty, reorder_level FROM inventory WHERE warehouse=? AND sku=?", (origin_wh.upper(), sku.upper())).fetchone()
    return {"status": "success", "shipment_id": new_id, "weight_kg": weight, "carrier": car["name"],
            "stock_left": left[0], "reorder_needed": left[0] < left[1]}


@mcp.tool()
def reassign_carrier(shipment_id: str, carrier_id: str, reason: str, new_eta_hours: float, extra_cost_inr: float = 0) -> dict:
    """Recovery action: switch an at-risk shipment to another carrier, update ETA and cost."""
    with db.connect() as con:
        s, err = _ship_or_err(con, shipment_id)
        if err:
            return err
        car = con.execute("SELECT * FROM carriers WHERE id=? AND active=1", (carrier_id.upper(),)).fetchone()
        if not car:
            return _err(f"unknown carrier {carrier_id!r}", "call list_carriers()")
        if s["weight_kg"] > car["max_kg"]:
            return _err(f"{car['name']} max load {car['max_kg']}kg < {s['weight_kg']}kg", "pick a higher-capacity carrier")
        eta = (datetime.now() + timedelta(hours=new_eta_hours)).isoformat(timespec="minutes")
        con.execute("UPDATE shipments SET carrier_id=?, eta=?, cost_inr=cost_inr+?, status='IN_TRANSIT', updated_at=? WHERE id=?",
                    (car["id"], eta, extra_cost_inr, db.now_iso(), s["id"]))
        con.execute("INSERT INTO shipment_events(shipment_id,at,status,note) VALUES(?,?,?,?)",
                    (s["id"], db.now_iso(), "IN_TRANSIT", f"reassigned to {car['name']}: {reason}"))
        con.execute("UPDATE incidents SET resolved=1, action=? WHERE shipment_id=? AND resolved=0", (f"reassigned to {car['id']}", s["id"]))
    return {"status": "success", "shipment_id": s["id"], "carrier": car["name"], "new_eta": eta, "deadline": s["deadline"]}


@mcp.tool()
def log_incident(shipment_id: str, kind: str, description: str, action: str) -> dict:
    """Record an incident (delay, breakdown, weather, damage, customs...) and the action taken."""
    with db.connect() as con:
        s, err = _ship_or_err(con, shipment_id)
        if err:
            return err
        cur = con.execute("INSERT INTO incidents(shipment_id,kind,description,action,at) VALUES(?,?,?,?,?)",
                          (s["id"], kind, description, action, db.now_iso()))
    return {"status": "success", "incident_id": cur.lastrowid}


# ------------------------------------------------------------------ stock


@mcp.tool()
def check_inventory(sku: str = "", warehouse: str = "") -> dict:
    """Stock levels by warehouse and SKU (both filters optional). Flags items below reorder level."""
    q = ("SELECT i.warehouse, w.city, i.sku, s.name, i.qty, i.reorder_level, (i.qty < i.reorder_level) AS low "
         "FROM inventory i JOIN skus s ON s.sku=i.sku JOIN warehouses w ON w.code=i.warehouse WHERE 1=1")
    args = []
    if sku:
        q += " AND (i.sku=? OR s.name LIKE ?)"
        args += [sku.upper(), f"%{sku}%"]
    if warehouse:
        q += " AND (i.warehouse=? OR w.city LIKE ?)"
        args += [warehouse.upper(), f"%{warehouse}%"]
    with db.connect() as con:
        out = db.rows(con.execute(q + " ORDER BY i.warehouse, i.sku", args))
        skus = db.rows(con.execute("SELECT sku, name, weight_kg, fragile, cold_chain FROM skus"))
    if not out:
        return _err("no matching stock", f"valid SKUs: {[s['sku'] for s in skus]}; warehouses WH-CHN, WH-BLR, WH-MUM, WH-DEL, WH-HYD, WH-KOL")
    return {"status": "success", "items": out, "low_stock": [r for r in out if r["low"]]}


@mcp.tool()
def find_stock(sku: str, qty: int) -> dict:
    """Warehouses that can fulfil `qty` units of `sku`, with coordinates (for distance calc)."""
    with db.connect() as con:
        s = con.execute("SELECT * FROM skus WHERE sku=? OR name LIKE ?", (sku.upper(), f"%{sku}%")).fetchone()
        if not s:
            names = db.rows(con.execute("SELECT sku, name FROM skus"))
            return _err(f"unknown sku {sku!r}", f"valid SKUs: {names}")
        out = db.rows(con.execute(
            "SELECT i.warehouse, w.city, w.lat, w.lon, i.qty FROM inventory i JOIN warehouses w ON w.code=i.warehouse "
            "WHERE i.sku=? AND i.qty>=? ORDER BY i.qty DESC", (s["sku"], qty)))
    return {"status": "success", "sku": s["sku"], "name": s["name"], "unit_weight_kg": s["weight_kg"],
            "total_weight_kg": round(s["weight_kg"] * qty, 1), "cold_chain": bool(s["cold_chain"]),
            "fragile": bool(s["fragile"]), "warehouses": out,
            "note": "" if out else "no single warehouse has enough stock: split the order or reduce qty"}


# ------------------------------------------------------------------ carriers


@mcp.tool()
def list_carriers() -> dict:
    """All carriers with mode, rates, speed, reliability, capacity and cold-chain capability."""
    with db.connect() as con:
        return {"status": "success", "carriers": db.rows(con.execute("SELECT * FROM carriers WHERE active=1"))}


@mcp.tool()
def quote_carriers(distance_km: float, sku: str, qty: int, deadline_hours: float) -> dict:
    """Price + ETA for every carrier that can legally carry this load (capacity, cold chain),
    sorted best-first. `meets_deadline` tells you which options are on time."""
    if distance_km <= 0:
        return _err("distance_km must be > 0", "get it from road_distance(origin, destination)")
    with db.connect() as con:
        s = con.execute("SELECT * FROM skus WHERE sku=?", (sku.upper(),)).fetchone()
        if not s:
            return _err(f"unknown sku {sku!r}", "use the sku code from find_stock, e.g. SKU-LAP-14")
        carriers = db.rows(con.execute("SELECT * FROM carriers WHERE active=1"))
    weight = s["weight_kg"] * qty
    opts = []
    for c in carriers:
        if weight > c["max_kg"] or (s["cold_chain"] and not c["cold_chain"]):
            continue
        km = distance_km * (0.8 if c["mode"] == "air" else 1.0)  # air is closer to great-circle
        eta = km / c["speed_kmph"] + (6 if c["mode"] == "air" else 2 if c["mode"] == "rail" else 1)
        cost = c["base_fee"] + c["rate_per_km"] * km + c["rate_per_kg"] * weight
        opts.append({"carrier_id": c["id"], "name": c["name"], "mode": c["mode"], "cost_inr": round(cost),
                     "eta_hours": round(eta, 1), "reliability": c["reliability"], "meets_deadline": eta <= deadline_hours,
                     "slack_hours": round(deadline_hours - eta, 1)})
    if not opts:
        return _err(f"no carrier can carry {weight:.0f}kg of {sku}", "split into smaller shipments", retryable=False)
    opts.sort(key=lambda o: (not o["meets_deadline"], o["cost_inr"] / (o["reliability"] ** 3)))
    return {"status": "success", "weight_kg": round(weight, 1), "options": opts,
            "recommended": next((o for o in opts if o["meets_deadline"]), None)}


def main():
    if "--http" in sys.argv:
        mcp.run("streamable-http", host="0.0.0.0", port=8765)
    else:
        mcp.run("stdio")


if __name__ == "__main__":
    main()
