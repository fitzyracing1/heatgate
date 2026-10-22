"""Per-app heat: CPU used by one process tree, plus ESTIMATED energy from CPU-seconds.

Linux: reads utime+stime(+cutime+cstime) from /proc/<pid>/stat for the pid and all live descendants.
Other OSes: uses psutil if installed. Otherwise returns None so callers fall back to whole-machine heat.
"""
import os, time

DEFAULT_WATTS_PER_CORE = 10.0  # ESTIMATE: rough active power of one busy core; override per machine

def _clk():
    try:
        return os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError, AttributeError):
        return 100

def _cores():
    return os.cpu_count() or 1

def watts_per_core(value=None):
    """Explicit value > env HEATGATE_WATTS_PER_CORE > DEFAULT_WATTS_PER_CORE (10 W)."""
    if value is not None:
        return float(value)
    try:
        return float(os.environ["HEATGATE_WATTS_PER_CORE"])
    except (KeyError, ValueError):
        return DEFAULT_WATTS_PER_CORE

def _read_stat(pid, proc="/proc"):
    """(ppid, utime+stime+cutime+cstime ticks) for one pid, or None."""
    try:
        with open(os.path.join(proc, str(pid), "stat")) as f:
            s = f.read()
    except OSError:
        return None
    # comm may contain spaces/parens: split after the last ')'
    try:
        rest = s[s.rindex(")") + 2:].split()
        # rest[0]=state(3) rest[1]=ppid(4) ... utime=14 stime=15 cutime=16 cstime=17 -> rest idx 11..14
        return int(rest[1]), sum(int(x) for x in rest[11:15])
    except (ValueError, IndexError):
        return None

def tree_pids(pid, proc="/proc"):
    """pid plus all live descendants (Linux /proc)."""
    kids = {}
    try:
        names = os.listdir(proc)
    except OSError:
        return [pid]
    for n in names:
        if n.isdigit():
            st = _read_stat(n, proc)
            if st:
                kids.setdefault(st[0], []).append(int(n))
    out, todo = [], [int(pid)]
    while todo:
        p = todo.pop()
        if p in out:
            continue
        out.append(p); todo.extend(kids.get(p, []))
    return out

def cpu_seconds(pid, proc="/proc"):
    """Cumulative CPU-seconds (user+sys) of the process tree rooted at pid, or None if unmeasurable.

    Includes cutime/cstime, i.e. children that already exited and were reaped inside the tree."""
    if os.path.isdir(proc):
        if _read_stat(pid, proc) is None:
            return None
        ticks = 0
        for p in tree_pids(pid, proc):
            st = _read_stat(p, proc)
            if st:
                ticks += st[1]
        return ticks / float(_clk())
    try:
        import psutil  # optional, for macOS/Windows
        root = psutil.Process(int(pid))
        total = 0.0
        for pr in [root] + root.children(recursive=True):
            try:
                t = pr.cpu_times(); total += t.user + t.system + getattr(t, "children_user", 0) + getattr(t, "children_system", 0)
            except Exception:
                pass
        return total
    except Exception:
        return None

def app_readings(pid, interval=0.5, _sleep=time.sleep):
    """CPU use of a process tree over `interval`. `app_heat` = busy cores / total cores (0..1).

    Returns source='machine' with whole-machine heat if the tree can't be measured on this OS."""
    a = cpu_seconds(pid); t0 = time.monotonic()
    if a is None:
        from . import readings
        r = readings(interval, estimates=True)  # machine-wide fallback is the load proxy (an estimate)
        return {"pid": pid, "source": "machine", "app_heat": r["heat"], "app_cores": None,
                "cpu_seconds": None, "cores": r["cores"]}
    _sleep(interval)
    b = cpu_seconds(pid); dt = max(time.monotonic() - t0, 1e-6)
    b = a if b is None else b
    busy = max(0.0, b - a) / dt
    return {"pid": pid, "source": "proc" if os.path.isdir("/proc") else "psutil",
            "app_heat": busy / _cores(), "app_cores": busy, "cpu_seconds": b, "cores": _cores()}

def app_heat(pid, interval=0.5):
    return app_readings(pid, interval)["app_heat"]

def energy_wh(cpu_s, wpc=None):
    """ESTIMATE: watt-hours = CPU-seconds x watts-per-core / 3600."""
    return float(cpu_s) * watts_per_core(wpc) / 3600.0

def energy_estimate(cpu_s=None, pid=None, wpc=None):
    if cpu_s is None:
        if pid is None:
            raise ValueError("need cpu_seconds or pid")
        cpu_s = cpu_seconds(pid)
        if cpu_s is None:
            raise ValueError(f"cannot measure pid {pid} on this system")
    w = watts_per_core(wpc)
    return {"estimate": True, "cpu_seconds": round(cpu_s, 3), "watts_per_core": w,
            "est_energy_wh": round(energy_wh(cpu_s, w), 6), "pid": pid,
            "note": "ESTIMATE: CPU-seconds x watts-per-core; not a power-meter reading"}
