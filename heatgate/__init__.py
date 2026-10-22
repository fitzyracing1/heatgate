"""heatgate: load-based throttle. CPU load is a stand-in for chip heat."""
import functools, glob, os, time

__version__ = "0.1.0"
__all__ = ["heat", "readings", "Gate", "pace", "throttled", "workers", "app_heat", "app_readings",
           "cpu_seconds", "energy_estimate", "energy_wh", "estimates_enabled", "sensors", "sensor_temp", "temp_margin"]

def _cores():
    return os.cpu_count() or 1

def _loadavg(path="/proc/loadavg"):
    try:
        with open(path) as f:
            return float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        try:
            return os.getloadavg()[0]
        except OSError:
            return 0.0

def _stat(path="/proc/stat"):
    try:
        with open(path) as f:
            v = [int(x) for x in f.readline().split()[1:]]
        idle = v[3] + (v[4] if len(v) > 4 else 0)
        return idle, sum(v)
    except (OSError, ValueError, IndexError):
        return None

def cpu_percent(interval=0.2):
    a = _stat()
    if a is None:
        return None
    time.sleep(interval)
    b = _stat()
    dt = b[1] - a[1]
    return 0.0 if dt <= 0 else 1.0 - (b[0] - a[0]) / dt

def thermal(base="/sys/class/thermal"):
    """Max temp in C from sysfs or psutil, or None when hidden (typical VM)."""
    temps = []
    for p in glob.glob(os.path.join(base, "thermal_zone*/temp")):
        try:
            with open(p) as f:
                temps.append(int(f.read().strip()) / 1000.0)
        except (OSError, ValueError):
            pass
    if not temps:
        try:
            import psutil  # optional
            for entries in (psutil.sensors_temperatures() or {}).values():
                temps += [e.current for e in entries if e.current]
        except Exception:
            pass
    return max(temps) if temps else None

def estimates_enabled(flag=None):
    """Estimates (load-as-heat proxy, energy Wh, grid_stress score) are opt-in: flag or env HEATGATE_ESTIMATES=1."""
    if flag is not None:
        return bool(flag)
    return os.environ.get("HEATGATE_ESTIMATES", "").strip().lower() in ("1", "true", "yes", "on")

def sensors():
    """All real temperature sensors (sysfs thermal zones + hwmon: CPU/GPU/Wi-Fi/...). [] when none."""
    try:
        from .router import sysfs_temps
        return sysfs_temps()
    except Exception:
        return []

def sensor_temp():
    """Max real sensor temperature in C, or None."""
    s = [x["temp_c"] for x in sensors()]
    if not s:
        t = thermal()  # psutil fallback (macOS/Windows if installed)
        return t
    return max(s)

def readings(interval=0.2, estimates=None):
    """Measured readings. Readings-only (default): temp_c from real sensors + raw CPU utilization %.
    With estimates: also load1/load_norm and the load-as-heat proxy `heat`."""
    if not estimates_enabled(estimates):
        cpu = cpu_percent(interval)
        ss = sensors()
        t = max(x["temp_c"] for x in ss) if ss else thermal()
        return {"mode": "readings", "temp_c": t, "sensors": ss,
                "cpu_pct": None if cpu is None else round(cpu * 100, 1), "cores": _cores(),
                "no_readings": t is None}
    load1 = _loadavg()
    cpu = cpu_percent(interval)
    t = thermal()
    load_n = load1 / _cores()
    parts = [load_n] + ([cpu] if cpu is not None else [])
    if t is not None:  # map 40C..90C -> 0..1
        parts.append(max(0.0, (t - 40.0) / 50.0))
    return {"mode": "estimates", "load1": load1, "cores": _cores(), "load_norm": load_n,
            "cpu": cpu, "temp_c": t, "heat": max(parts)}

def temp_margin(temp_high=85.0, router=None):
    """Readings-only gate value: max(sensor_temp - temp_high[, router_temp - router_band_hi]) in C, None if no reading."""
    m = []
    t = sensor_temp()
    if t is not None:
        m.append(t - temp_high)
    if router is not None:
        try:
            st = router.status()
            if st["temp_c"] is not None:
                m.append(st["temp_c"] - router.kw["band"][1])
        except Exception:
            pass
    return max(m) if m else None

def heat(interval=0.2):
    """ESTIMATE (load-as-heat proxy). Normalized heat: max(load1/cores, cpu utilization[, temp scale])."""
    return readings(interval)["heat"]

def pace(high=0.8, low=0.5, max_sleep=5.0, _heat=None, _sleep=time.sleep):
    """Sleep proportionally to how far heat is above `low`. Returns seconds slept."""
    h = heat() if _heat is None else _heat()
    if h <= low:
        return 0.0
    frac = min(1.0, (h - low) / max(high - low, 1e-9))
    s = max_sleep * frac
    _sleep(s)
    return s

class Gate:
    """Block on entry while heat >= high, until it drops below low (hysteresis)."""
    def __init__(self, high=0.8, low=0.5, poll=1.0, timeout=None, pid=None, _heat=None, _sleep=time.sleep, grid=None,
                 router=None, max_wait=None, estimates=None, temp_high=85.0, temp_low=75.0):
        """pid: gate on that process tree's heat (busy cores / cores) instead of the whole machine.
        router: a heatgate.router.Router (or dict); effective also >= weight*router_temp_stress.
        max_wait: stop waiting after this many seconds of polling, log a warning and proceed.
        grid: a heatgate.grid.Grid (or dict of its kwargs); effective = max(heat, weight*grid_stress)."""
        self.high, self.low, self.poll, self.timeout, self.pid = high, low, poll, timeout, pid
        self.estimates = estimates_enabled(estimates) or _heat is not None or pid is not None
        self.max_wait, self._sleep, self.waited = max_wait, _sleep, 0.0
        if not self.estimates:
            # readings-only: gate on real temperatures; hot at temp_high, resume below temp_low; no sensor -> never
            rt = None
            if router is not None:
                from .router import Router
                rt = Router(**router) if isinstance(router, dict) else router
            self.high, self.low = 0.0, -(temp_high - temp_low)
            self._heat = lambda: (lambda m: float("-inf") if m is None else m)(temp_margin(temp_high, rt))
            return
        if _heat is None and pid is not None:
            _heat = lambda: app_heat(pid, 0.2)
        self._heat = _heat or heat
        if grid is not None:
            from .grid import Grid, effective_heat
            g = Grid(**grid) if isinstance(grid, dict) else grid
            local = self._heat
            self._heat = lambda: effective_heat(local(), g(), getattr(g, "weight", 1.0))
        if router is not None:
            from .router import Router
            rt = Router(**router) if isinstance(router, dict) else router
            inner = self._heat
            def _with_router(inner=inner, rt=rt):
                h, rs = inner(), rt()
                return h if rs is None else max(h, getattr(rt, "weight", 1.0) * rs)
            self._heat = _with_router
    def wait(self):
        if self._heat() < self.high:
            return 0.0
        start = time.monotonic()
        while self._heat() >= self.low:
            if self.timeout is not None and time.monotonic() - start >= self.timeout:
                break
            if self.max_wait is not None and self.waited >= self.max_wait:
                import logging
                logging.getLogger("heatgate").warning(
                    "heatgate: still hot after max_wait=%ss; proceeding anyway", self.max_wait)
                break
            self._sleep(self.poll)
            self.waited += self.poll
        return self.waited
    def __enter__(self):
        self.wait(); return self
    def __exit__(self, *exc):
        return False

def throttled(high=0.8, low=0.5, **kw):
    """Decorator: gate each call."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **k):
            Gate(high, low, **kw).wait()
            return fn(*a, **k)
        return wrapper
    return deco

def workers(max_workers=None, min_workers=1, high=0.8, low=0.5, _heat=None, estimates=None,
            temp_high=85.0, temp_low=75.0):
    """Worker count scaled down linearly as heat rises from low to high.
    Readings-only (default): scaled on real sensor temp between temp_low and temp_high; no sensor -> max."""
    mx = max_workers or _cores()
    if _heat is None and not estimates_enabled(estimates):
        t = sensor_temp()
        if t is None:
            return mx
        h, high, low = t, temp_high, temp_low
    else:
        h = heat() if _heat is None else _heat()
    if h <= low:
        return mx
    if h >= high:
        return min_workers
    frac = (high - h) / (high - low)
    return max(min_workers, int(round(min_workers + frac * (mx - min_workers))))

from .app import app_heat, app_readings, cpu_seconds, energy_estimate, energy_wh  # noqa: E402
