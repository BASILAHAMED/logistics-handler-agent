"""Live external API tools (ADK function tools). Each one self-heals through a provider chain:

  geocode        OpenStreetMap Nominatim  -> Open-Meteo geocoding -> built-in city table
  road_distance  OSRM road routing        -> haversine x 1.3 road factor (offline)
  weather_risk   Open-Meteo forecast      -> "unknown" with advice to re-check

Results carry `source` and `degraded` so the agent can be honest about data quality.
"""

from __future__ import annotations

import json
import math
import os
import urllib.parse
import urllib.request

from lh.healing import HEALTH, resilient

UA = {"User-Agent": "logistics-handler/1.0 (Google ADK demo)"}
TIMEOUT = float(os.getenv("HTTP_TIMEOUT_S", "8"))
# Set to a comma list (e.g. "osrm,nominatim") to simulate outages in demos: shows self-healing live.
SIMULATE_DOWN = {s.strip() for s in os.getenv("SIMULATE_OUTAGE", "").split(",") if s.strip()}

CITIES = {
    "chennai": (13.0827, 80.2707), "bengaluru": (12.9716, 77.5946), "bangalore": (12.9716, 77.5946),
    "mumbai": (19.0760, 72.8777), "delhi": (28.6139, 77.2090), "new delhi": (28.6139, 77.2090),
    "hyderabad": (17.3850, 78.4867), "kolkata": (22.5726, 88.3639), "pune": (18.5204, 73.8567),
    "ahmedabad": (23.0225, 72.5714), "jaipur": (26.9124, 75.7873), "kochi": (9.9312, 76.2673),
    "coimbatore": (11.0168, 76.9558), "madurai": (9.9252, 78.1198), "visakhapatnam": (17.6868, 83.2185),
    "vizag": (17.6868, 83.2185), "vijayawada": (16.5062, 80.6480), "mysuru": (12.2958, 76.6394),
    "mysore": (12.2958, 76.6394), "trichy": (10.7905, 78.7047), "tiruchirappalli": (10.7905, 78.7047),
    "lucknow": (26.8467, 80.9462), "nagpur": (21.1458, 79.0882), "surat": (21.1702, 72.8311),
    "indore": (22.7196, 75.8577), "bhopal": (23.2599, 77.4126), "patna": (25.5941, 85.1376),
    "chandigarh": (30.7333, 76.7794), "guwahati": (26.1445, 91.7362), "bhubaneswar": (20.2961, 85.8245),
    "thiruvananthapuram": (8.5241, 76.9366), "goa": (15.2993, 74.1240), "salem": (11.6643, 78.1460),
    "vellore": (12.9165, 79.1325), "hosur": (12.7409, 77.8253), "pondicherry": (11.9416, 79.8083),
}
WAREHOUSE_CITY = {"WH-CHN": "chennai", "WH-BLR": "bengaluru", "WH-MUM": "mumbai", "WH-DEL": "delhi",
                  "WH-HYD": "hyderabad", "WH-KOL": "kolkata"}


def _get(url: str, provider: str) -> dict:
    if provider in SIMULATE_DOWN:
        raise ConnectionError(f"simulated outage of {provider}")
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def _nominatim(place: str):
    q = urllib.parse.urlencode({"q": place, "format": "json", "limit": 1, "countrycodes": "in"})
    d = _get(f"https://nominatim.openstreetmap.org/search?{q}", "nominatim")
    if not d:
        raise LookupError(f"nominatim: no match for {place!r}")
    return {"lat": float(d[0]["lat"]), "lon": float(d[0]["lon"]), "label": d[0]["display_name"][:120]}


def _openmeteo_geo(place: str):
    q = urllib.parse.urlencode({"name": place.split(",")[0], "count": 1, "countryCode": "IN"})
    d = _get(f"https://geocoding-api.open-meteo.com/v1/search?{q}", "openmeteo-geo").get("results")
    if not d:
        raise LookupError(f"open-meteo: no match for {place!r}")
    return {"lat": d[0]["latitude"], "lon": d[0]["longitude"], "label": f"{d[0]['name']}, {d[0].get('admin1', '')}"}


def _table(place: str):
    key = WAREHOUSE_CITY.get(place.strip().upper(), place.split(",")[0].strip().lower())
    if key not in CITIES:
        raise LookupError(f"{place!r} not in offline table")
    lat, lon = CITIES[key]
    return {"lat": lat, "lon": lon, "label": key.title()}


def _coords(place: str) -> dict:
    if place.strip().upper() in WAREHOUSE_CITY:  # warehouse codes resolve locally, no API needed
        return {"result": _table(place), "source": "warehouse-registry", "degraded": False, "errors": []}
    return resilient([("nominatim", lambda: _nominatim(place)), ("openmeteo-geo", lambda: _openmeteo_geo(place)),
                      ("offline-city-table", lambda: _table(place))], retries=2, backoff=0.3)


def geocode(place: str) -> dict:
    """Latitude/longitude for a city, address or warehouse code (e.g. 'WH-CHN', 'Hosur', 'Andheri East, Mumbai')."""
    try:
        r = _coords(place)
    except RuntimeError as e:
        return {"status": "error", "error": str(e), "hint": "use a bigger nearby city name"}
    return {"status": "success", **r["result"], "source": r["source"], "degraded": r["degraded"]}


def _haversine(a, b) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def _osrm(a, b):
    d = _get(f"https://router.project-osrm.org/route/v1/driving/{a['lon']},{a['lat']};{b['lon']},{b['lat']}?overview=false", "osrm")
    if d.get("code") != "Ok":
        raise RuntimeError(f"osrm: {d.get('code')}")
    r = d["routes"][0]
    return {"distance_km": round(r["distance"] / 1000, 1), "drive_hours": round(r["duration"] / 3600, 1)}


def _estimate(a, b):
    km = _haversine(a, b) * 1.3
    return {"distance_km": round(km, 1), "drive_hours": round(km / 50, 1)}


def road_distance(origin: str, destination: str) -> dict:
    """Road distance (km) and pure driving time between two places or warehouse codes.
    Use distance_km as input to quote_carriers."""
    try:
        a, b = _coords(origin), _coords(destination)
        r = resilient([("osrm", lambda: _osrm(a["result"], b["result"])),
                       ("haversine-estimate", lambda: _estimate(a["result"], b["result"]))], retries=2, backoff=0.3)
    except RuntimeError as e:
        return {"status": "error", "error": str(e), "hint": "check the place names; try the nearest big city"}
    degraded = r["degraded"] or a["degraded"] or b["degraded"]
    return {"status": "success", "origin": a["result"]["label"], "destination": b["result"]["label"], **r["result"],
            "source": r["source"], "degraded": degraded,
            "note": "estimated from straight-line distance x1.3 (routing API unavailable)" if r["degraded"] else ""}


WMO_SEVERE = {65: "heavy rain", 67: "freezing rain", 75: "heavy snow", 82: "violent rain showers", 95: "thunderstorm",
              96: "thunderstorm with hail", 99: "severe thunderstorm with hail"}


def _forecast(c):
    q = urllib.parse.urlencode({"latitude": c["lat"], "longitude": c["lon"], "forecast_days": 3, "timezone": "Asia/Kolkata",
                                "daily": "weather_code,precipitation_sum,wind_speed_10m_max"})
    d = _get(f"https://api.open-meteo.com/v1/forecast?{q}", "open-meteo")["daily"]
    days = []
    for i, day in enumerate(d["time"]):
        rain, wind, code = d["precipitation_sum"][i] or 0, d["wind_speed_10m_max"][i] or 0, d["weather_code"][i]
        level = "HIGH" if rain >= 50 or wind >= 60 or code in (82, 95, 96, 99) else "MEDIUM" if rain >= 15 or wind >= 40 or code in WMO_SEVERE else "LOW"
        days.append({"date": day, "rain_mm": rain, "wind_kmh": wind, "condition": WMO_SEVERE.get(code, f"wmo-{code}"), "risk": level})
    return days


def weather_risk(place: str) -> dict:
    """3-day weather risk (LOW/MEDIUM/HIGH) for a place on the route: rain, wind, storms."""
    try:
        c = _coords(place)["result"]
        r = resilient([("open-meteo", lambda: _forecast(c))], retries=2, backoff=0.4)
    except RuntimeError as e:
        HEALTH.fail("weather", e, threshold=99)
        return {"status": "success", "place": place, "overall_risk": "UNKNOWN", "degraded": True,
                "note": "weather service unreachable; treat as MEDIUM risk and re-check before dispatch"}
    days = r["result"]
    overall = "HIGH" if any(d["risk"] == "HIGH" for d in days) else "MEDIUM" if any(d["risk"] == "MEDIUM" for d in days) else "LOW"
    return {"status": "success", "place": c["label"], "overall_risk": overall, "days": days, "source": r["source"], "degraded": False}


def system_health() -> dict:
    """Self-healing report: failures caught, automatic recoveries, open circuit breakers, recent events."""
    return {"status": "success", **HEALTH.snapshot(), "simulated_outages": sorted(SIMULATE_DOWN)}
