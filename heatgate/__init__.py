"""heatgate: load-based throttle. CPU load is a stand-in for chip heat."""
import functools, glob, os, time

__version__ = "0.1.0"
__all__ = ["heat", "readings", "Gate", "pace", "throttled", "workers", "app_heat", "app_readings",
           "cpu_seconds", "energy_estimate", "energy_wh"]

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

def readings(interval=0.2):
    load1 = _loadavg()
    cpu = cpu_percent(interval)
    t = thermal()
    load_n = load1 / _cores()
    parts = [load_n] + ([cpu] if cpu is not None else [])
    if t is not None:  # map 40C..90C -> 0..1
        parts.append(max(0.0, (t - 40.0) / 50.0))
    return {"load1": load1, "cores": _cores(), "load_norm": load_n,
            "cpu": cpu, "temp_c": t, "heat": max(parts)}

def heat(interval=0.2):
    """Normalized heat: max(load1/cores, cpu utilization[, temp scale])."""
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
    def __init__(self, high=0.8, low=0.5, poll=1.0, timeout=None, pid=None, _heat=None, _sleep=time.sleep):
        """pid: gate on that process tree's heat (busy cores / cores) instead of the whole machine."""
        self.high, self.low, self.poll, self.timeout, self.pid = high, low, poll, timeout, pid
        if _heat is None and pid is not None:
            _heat = lambda: app_heat(pid, 0.2)
        self._heat = _heat or heat
        self._sleep = _sleep
        self.waited = 0.0
    def wait(self):
        if self._heat() < self.high:
            return 0.0
        start = time.monotonic()
        while self._heat() >= self.low:
            if self.timeout is not None and time.monotonic() - start >= self.timeout:
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

def workers(max_workers=None, min_workers=1, high=0.8, low=0.5, _heat=None):
    """Worker count scaled down linearly as heat rises from low to high."""
    mx = max_workers or _cores()
    h = heat() if _heat is None else _heat()
    if h <= low:
        return mx
    if h >= high:
        return min_workers
    frac = (high - h) / (high - low)
    return max(min_workers, int(round(min_workers + frac * (mx - min_workers))))

from .app import app_heat, app_readings, cpu_seconds, energy_estimate, energy_wh  # noqa: E402
