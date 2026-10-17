"""SQLite store behind the MCP server: warehouses, stock, carriers, shipments, incidents, feedback.

Self-repairing: `connect()` creates the schema and seeds demo data if the DB is missing or
empty, and quarantines + rebuilds a corrupt file instead of crashing the agent.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

from lh import config

log = logging.getLogger("lh.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS warehouses(code TEXT PRIMARY KEY, city TEXT, lat REAL, lon REAL, capacity INT);
CREATE TABLE IF NOT EXISTS skus(sku TEXT PRIMARY KEY, name TEXT, weight_kg REAL, fragile INT DEFAULT 0, cold_chain INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS inventory(warehouse TEXT, sku TEXT, qty INT, reorder_level INT, PRIMARY KEY(warehouse, sku));
CREATE TABLE IF NOT EXISTS carriers(id TEXT PRIMARY KEY, name TEXT, mode TEXT, base_fee REAL, rate_per_km REAL,
    rate_per_kg REAL, speed_kmph REAL, reliability REAL, max_kg REAL, cold_chain INT, active INT DEFAULT 1);
CREATE TABLE IF NOT EXISTS shipments(id TEXT PRIMARY KEY, order_ref TEXT, customer TEXT, origin_wh TEXT, destination TEXT,
    sku TEXT, qty INT, weight_kg REAL, carrier_id TEXT, status TEXT, cost_inr REAL, distance_km REAL,
    deadline TEXT, eta TEXT, created_at TEXT, updated_at TEXT, last_location TEXT);
CREATE TABLE IF NOT EXISTS shipment_events(id INTEGER PRIMARY KEY AUTOINCREMENT, shipment_id TEXT, at TEXT, status TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS incidents(id INTEGER PRIMARY KEY AUTOINCREMENT, shipment_id TEXT, kind TEXT, description TEXT,
    action TEXT, at TEXT, resolved INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS feedback(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, rating INT, comment TEXT,
    topic TEXT, shipment_id TEXT, lesson TEXT);
"""

WAREHOUSES = [
    ("WH-CHN", "Chennai", 13.0827, 80.2707, 50000),
    ("WH-BLR", "Bengaluru", 12.9716, 77.5946, 40000),
    ("WH-MUM", "Mumbai", 19.0760, 72.8777, 60000),
    ("WH-DEL", "Delhi", 28.6139, 77.2090, 60000),
    ("WH-HYD", "Hyderabad", 17.3850, 78.4867, 35000),
    ("WH-KOL", "Kolkata", 22.5726, 88.3639, 30000),
]
SKUS = [
    ("SKU-BAG-01", "Cotton tote bag (pack of 10)", 1.5, 0, 0),
    ("SKU-LAP-14", "14in business laptop", 2.2, 1, 0),
    ("SKU-MED-07", "Insulin pens (box of 5)", 0.6, 1, 1),
    ("SKU-DIY-03", "Brass diya set", 3.0, 1, 0),
    ("SKU-TV-55", "55in LED TV", 18.0, 1, 0),
    ("SKU-RICE-25", "Basmati rice 25kg sack", 25.0, 0, 0),
]
INVENTORY = [
    ("WH-CHN", "SKU-BAG-01", 1200, 200), ("WH-CHN", "SKU-LAP-14", 40, 25), ("WH-CHN", "SKU-MED-07", 300, 100),
    ("WH-CHN", "SKU-DIY-03", 500, 100), ("WH-CHN", "SKU-TV-55", 12, 10), ("WH-CHN", "SKU-RICE-25", 800, 150),
    ("WH-BLR", "SKU-BAG-01", 300, 200), ("WH-BLR", "SKU-LAP-14", 220, 25), ("WH-BLR", "SKU-MED-07", 80, 100),
    ("WH-BLR", "SKU-TV-55", 60, 10),
    ("WH-MUM", "SKU-BAG-01", 2000, 200), ("WH-MUM", "SKU-LAP-14", 150, 25), ("WH-MUM", "SKU-TV-55", 90, 10),
    ("WH-MUM", "SKU-RICE-25", 1500, 150), ("WH-MUM", "SKU-MED-07", 400, 100),
    ("WH-DEL", "SKU-BAG-01", 900, 200), ("WH-DEL", "SKU-LAP-14", 300, 25), ("WH-DEL", "SKU-MED-07", 500, 100),
    ("WH-DEL", "SKU-DIY-03", 60, 100), ("WH-DEL", "SKU-TV-55", 110, 10), ("WH-DEL", "SKU-RICE-25", 2000, 150),
    ("WH-HYD", "SKU-LAP-14", 90, 25), ("WH-HYD", "SKU-RICE-25", 600, 150), ("WH-HYD", "SKU-DIY-03", 250, 100),
    ("WH-KOL", "SKU-BAG-01", 400, 200), ("WH-KOL", "SKU-RICE-25", 700, 150), ("WH-KOL", "SKU-TV-55", 8, 10),
]
# id, name, mode, base_fee, rate/km, rate/kg, speed km/h (door-to-door incl. handling), reliability, max_kg, cold_chain
CARRIERS = [
    ("CR-SWIFT", "SwiftRoad Express", "road", 400, 9.0, 4.0, 38, 0.93, 3000, 0),
    ("CR-ECO", "EcoFreight Surface", "road", 250, 5.5, 2.5, 26, 0.88, 10000, 0),
    ("CR-COLD", "PolarChain Reefers", "road-reefer", 900, 14.0, 6.0, 34, 0.95, 2000, 1),
    ("CR-AIR", "SkyHop Air Cargo", "air", 3000, 18.0, 55.0, 160, 0.97, 500, 1),
    ("CR-RAIL", "IndRail Parcel", "rail", 600, 3.0, 1.8, 30, 0.85, 20000, 0),
]


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _iso(d: datetime) -> str:
    return d.isoformat(timespec="minutes")


def _seed(con: sqlite3.Connection):
    con.executemany("INSERT INTO warehouses VALUES(?,?,?,?,?)", WAREHOUSES)
    con.executemany("INSERT INTO skus VALUES(?,?,?,?,?)", SKUS)
    con.executemany("INSERT INTO inventory VALUES(?,?,?,?)", INVENTORY)
    con.executemany("INSERT INTO carriers(id,name,mode,base_fee,rate_per_km,rate_per_kg,speed_kmph,reliability,max_kg,cold_chain) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)", CARRIERS)
    n = _now()
    ships = [
        # id, order, customer, origin, dest, sku, qty, kg, carrier, status, cost, km, deadline, eta, created, location
        ("SHP-1001", "ORD-5001", "Anna Retail", "WH-CHN", "Bengaluru", "SKU-BAG-01", 100, 150, "CR-SWIFT", "IN_TRANSIT",
         5300, 346, n + timedelta(hours=10), n + timedelta(hours=6), n - timedelta(hours=4), "Krishnagiri toll plaza"),
        ("SHP-1002", "ORD-5002", "Vizag Grocers", "WH-HYD", "Visakhapatnam", "SKU-RICE-25", 40, 1000, "CR-ECO", "DELAYED",
         8600, 620, n + timedelta(hours=6), n + timedelta(hours=20), n - timedelta(hours=18), "Vijayawada hub (held: flooding on NH16)"),
        ("SHP-1003", "ORD-5003", "MedPlus Pune", "WH-MUM", "Pune", "SKU-MED-07", 200, 120, "CR-COLD", "IN_TRANSIT",
         5300, 150, n + timedelta(hours=5), n + timedelta(hours=2), n - timedelta(hours=2), "Lonavala"),
        ("SHP-1004", "ORD-5004", "TechMart Kochi", "WH-BLR", "Kochi", "SKU-LAP-14", 30, 66, "CR-SWIFT", "DELIVERED",
         5450, 550, n - timedelta(hours=20), n - timedelta(hours=22), n - timedelta(days=2), "Kochi (delivered, POD signed)"),
        ("SHP-1005", "ORD-5005", "Big Bazaar Jaipur", "WH-DEL", "Jaipur", "SKU-TV-55", 20, 360, "CR-ECO", "EXCEPTION",
         4300, 280, n + timedelta(hours=4), None, n - timedelta(hours=9), "Shahpura (vehicle breakdown)"),
        ("SHP-1006", "ORD-5006", "Diwali Gifts Co", "WH-CHN", "Madurai", "SKU-DIY-03", 150, 450, "CR-RAIL", "CREATED",
         2900, 460, n + timedelta(hours=40), n + timedelta(hours=22), n - timedelta(hours=1), "WH-CHN dock 3"),
    ]
    for s in ships:
        row = list(s)
        for i in (12, 13, 14):
            row[i] = _iso(row[i]) if row[i] else None
        con.execute("INSERT INTO shipments VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row[:15] + [row[14], row[15]])
        con.execute("INSERT INTO shipment_events(shipment_id,at,status,note) VALUES(?,?,?,?)", (s[0], row[14], "CREATED", "booked"))
        if s[9] != "CREATED":
            con.execute("INSERT INTO shipment_events(shipment_id,at,status,note) VALUES(?,?,?,?)", (s[0], _iso(n), s[9], s[15]))
    con.execute("INSERT INTO incidents(shipment_id,kind,description,action,at) VALUES(?,?,?,?,?)",
                ("SHP-1005", "breakdown", "Truck breakdown near Shahpura, 80km before Jaipur", "awaiting decision", _iso(n)))
    con.commit()


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(2):
        try:
            con = sqlite3.connect(path, timeout=10)
            con.row_factory = sqlite3.Row
            con.executescript(SCHEMA)
            if con.execute("SELECT COUNT(*) FROM warehouses").fetchone()[0] == 0:
                _seed(con)
                log.info("seeded demo data into %s", path)
            return con
        except sqlite3.DatabaseError as e:
            # Self-heal: quarantine the corrupt file and rebuild from seed.
            from lh.healing import HEALTH

            HEALTH.fail("database", e, threshold=99)
            try:
                con.close()
            except Exception:  # noqa: BLE001
                pass
            bad = path.with_suffix(f".corrupt-{int(time.time())}")
            path.replace(bad)
            log.warning("database corrupt (%s); moved to %s and rebuilding", e, bad)
            if attempt:
                raise
    raise RuntimeError("unreachable")


def reset(path: Path | str | None = None):
    path = Path(path or config.DB_PATH)
    if path.exists():
        path.unlink()
    connect(path).close()


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def now_iso() -> str:
    return _iso(_now())


def dumps(x) -> str:
    return json.dumps(x, default=str)
