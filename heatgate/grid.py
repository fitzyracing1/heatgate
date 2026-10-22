"""Grid mode: throttle on outside temperature and power-grid stress (free public data, no key by default).

Sources (all optional, all fail open -> None):
  * ZIP -> lat/lon: zippopotam.us (no key)
  * Temperature:    Open-Meteo forecast API `current=temperature_2m` (no key)
  * Grid demand:    ISO New England public ISO Express chart endpoint (no key; New England only,
                    undocumented website feed, may change). Optional EIA-930 hourly demand with env
                    EIA_API_KEY (+ --ba / HEATGATE_GRID_BA, default ISNE).
"""
import json, os, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

UA = "heatgate-grid/1 (+https://github.com/fitzyracing1/heatgate)"
NE_STATES = {"CT", "MA", "ME", "NH", "RI", "VT"}
_cache = {}
CACHE_TTL = float(os.environ.get("HEATGATE_GRID_TTL", 600))

def _fetch(url, data=None, headers=None, timeout=10, ttl=None):
    key = (url, data)
    ttl = CACHE_TTL if ttl is None else ttl
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    h = {"User-Agent": UA, "Accept": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data.encode() if data else None, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        val = json.loads(r.read().decode("utf-8"))
    _cache[key] = (time.time(), val)
    return val

def geocode_zip(zip_code, country="us"):
    """-> (lat, lon, place, state_abbr) or None."""
    try:
        d = _fetch(f"https://api.zippopotam.us/{country}/{urllib.parse.quote(str(zip_code))}", ttl=86400)
        p = d["places"][0]
        return float(p["latitude"]), float(p["longitude"]), p.get("place name"), p.get("state abbreviation")
    except Exception:
        return None

def temperature(lat, lon):
    """Current 2 m temperature in C from Open-Meteo, or None."""
    try:
        d = _fetch("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(
            {"latitude": lat, "longitude": lon, "current": "temperature_2m"}))
        return {"temp_c": float(d["current"]["temperature_2m"]), "time_utc": d["current"].get("time")}
    except Exception:
        return None

def in_new_england(lat, lon):
    return 40.9 <= lat <= 47.5 and -73.8 <= lon <= -66.8

def isone_demand(now=None):
    """Latest ISO-NE 5-min system load vs peak of the last ~2 days (actual + day-ahead forecast)."""
    try:
        now = now or datetime.now(timezone.utc)
        est = now - timedelta(hours=5)
        fmt = lambda d: d.strftime("%m/%d/%Y")
        form = urllib.parse.urlencode({
            "_nstmp_startDate": fmt(est - timedelta(days=1)), "_nstmp_endDate": fmt(est),
            "_nstmp_twodays": "true", "_nstmp_requestType": "systemload", "_nstmp_forecast": "true",
            "_nstmp_actual": "true", "_nstmp_cleared": "false", "_nstmp_priorDay": "false",
            "_nstmp_inclPumpLoad": "true", "_nstmp_inclBtmPv": "true"})
        d = _fetch("https://www.iso-ne.com/ws/wsclient", data=form,
                   headers={"X-Requested-With": "XMLHttpRequest", "User-Agent": "Mozilla/5.0 " + UA})
        data = d[0]["data"]
        actual = [x for x in data.get("actual") or [] if x.get("Mw") is not None]
        if not actual:
            return None
        last = actual[-1]
        vals = [float(x["Mw"]) for x in actual] + [float(x["Mw"]) for x in data.get("forecast") or []
                                                   if x.get("Mw") is not None]
        peak = max(vals)
        return {"source": "iso-ne", "load_mw": float(last["Mw"]), "peak_mw": peak,
                "ratio": float(last["Mw"]) / peak if peak else None, "time": last.get("BeginDate")}
    except Exception:
        return None

def eia_demand(ba="ISNE", key=None):
    """EIA-930 hourly demand (type D) vs peak of the last 48 h. Needs a free key (env EIA_API_KEY)."""
    key = key or os.environ.get("EIA_API_KEY")
    if not key:
        return None
    try:
        q = urllib.parse.urlencode([("api_key", key), ("frequency", "hourly"), ("data[0]", "value"),
                                    ("facets[respondent][]", ba), ("facets[type][]", "D"),
                                    ("sort[0][column]", "period"), ("sort[0][direction]", "desc"),
                                    ("length", "48")])
        rows = _fetch("https://api.eia.gov/v2/electricity/rto/region-data/data/?" + q)["response"]["data"]
        vals = [float(r["value"]) for r in rows if r.get("value") is not None]
        if not vals:
            return None
        return {"source": f"eia:{ba}", "load_mw": vals[0], "peak_mw": max(vals),
                "ratio": vals[0] / max(vals), "time": rows[0].get("period")}
    except Exception:
        return None

def clamp(x):
    return max(0.0, min(1.0, x))

def temp_stress(t, comfort=(10.0, 24.0), span=15.0):
    """0 inside the comfort band; rises linearly to 1 at `span` C beyond either edge."""
    if t is None:
        return None
    lo, hi = comfort
    if t < lo:
        return clamp((lo - t) / span)
    if t > hi:
        return clamp((t - hi) / span)
    return 0.0

def demand_stress(ratio, floor=0.7):
    """0 at <= floor x recent peak, 1 at the peak."""
    if ratio is None:
        return None
    return clamp((ratio - floor) / (1.0 - floor))

def grid_status(zip_code=None, lat=None, lon=None, comfort=(10.0, 24.0), source="auto", ba=None):
    """Never raises. grid_stress = max(temp_stress, demand_stress) over whatever is available, else None."""
    out = {"zip": zip_code, "lat": lat, "lon": lon, "place": None, "state": None, "temp_c": None,
           "temp_stress": None, "demand": None, "demand_stress": None, "grid_stress": None,
           "comfort_c": list(comfort), "errors": []}
    try:
        if zip_code is not None:
            g = geocode_zip(zip_code)
            if g is None:
                out["errors"].append("geocode failed")
            else:
                out["lat"], out["lon"], out["place"], out["state"] = g
        if out["lat"] is not None and out["lon"] is not None:
            t = temperature(out["lat"], out["lon"])
            if t is None:
                out["errors"].append("open-meteo failed")
            else:
                out["temp_c"], out["temp_time_utc"] = t["temp_c"], t["time_utc"]
                out["temp_stress"] = temp_stress(t["temp_c"], comfort)
        dem = None
        ne = out["state"] in NE_STATES if out["state"] else (
            out["lat"] is not None and in_new_england(out["lat"], out["lon"]))
        if source in ("eia",) or (source == "auto" and os.environ.get("EIA_API_KEY")):
            dem = eia_demand(ba or os.environ.get("HEATGATE_GRID_BA", "ISNE"))
        if dem is None and (source == "isone" or (source == "auto" and ne)):
            dem = isone_demand()
            if dem is None:
                out["errors"].append("iso-ne failed")
        if dem:
            out["demand"] = dem
            out["demand_stress"] = demand_stress(dem["ratio"])
        parts = [x for x in (out["temp_stress"], out["demand_stress"]) if x is not None]
        out["grid_stress"] = max(parts) if parts else None
    except Exception as e:  # fail open
        out["errors"].append(f"{type(e).__name__}: {e}")
    return out

def effective_heat(local, grid_stress, weight=1.0):
    """effective = max(local_heat, weight * grid_stress); grid ignored when unknown."""
    if grid_stress is None:
        return local
    return max(local, weight * grid_stress)

class Grid:
    """Callable config: Grid(zip_code='02740')() -> grid_stress or None (cached, fail open)."""
    def __init__(self, zip_code=None, lat=None, lon=None, weight=1.0, comfort=(10.0, 24.0), source="auto", ba=None):
        self.kw = dict(zip_code=zip_code, lat=lat, lon=lon, comfort=comfort, source=source, ba=ba)
        self.weight = weight
    def status(self):
        return grid_status(**self.kw)
    def __call__(self):
        return self.status()["grid_stress"]
