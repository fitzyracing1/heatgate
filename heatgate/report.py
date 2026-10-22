"""Record heat samples to a JSONL file and summarize them (used by the GitHub Action)."""
import json, os, signal, time
from . import readings, estimates_enabled
from .app import energy_wh, watts_per_core

def _cpu_jiffies(path="/proc/stat"):
    """(busy, total) jiffies across all CPUs, or None."""
    try:
        with open(path) as f:
            v = [int(x) for x in f.readline().split()[1:]]
        idle = v[3] + (v[4] if len(v) > 4 else 0)
        total = sum(v[:8])  # exclude guest cols (already counted in user/nice)
        return total - idle, total
    except (OSError, ValueError, IndexError):
        return None

def _clk_tck():
    try:
        return os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError, AttributeError):
        return 100

def sample_once(stat_path="/proc/stat"):
    est = estimates_enabled()
    r = readings(interval=0.2, estimates=est)
    j = _cpu_jiffies(stat_path)
    row = {"t": time.time(), "mode": r["mode"], "temp_c": r["temp_c"],
           "cpu": r["cpu"] if est else (None if r["cpu_pct"] is None else r["cpu_pct"] / 100.0),
           "cores": r["cores"], "busy_jiffies": j[0] if j else None, "total_jiffies": j[1] if j else None}
    if est:
        row.update({"heat": r["heat"], "load1": r["load1"]})
    return row

def sample(out, interval=5.0, count=None):
    """Append one JSON line per sample to `out` until SIGTERM/SIGINT (or `count` samples)."""
    stop = {"v": False}
    def h(*_): stop["v"] = True
    signal.signal(signal.SIGTERM, h); signal.signal(signal.SIGINT, h)
    n = 0
    with open(out, "a") as f:
        while not stop["v"]:
            f.write(json.dumps(sample_once()) + "\n"); f.flush(); n += 1
            if count is not None and n >= count:
                break
            end = time.time() + max(0.0, interval - 0.2)
            while not stop["v"] and time.time() < end:
                time.sleep(min(0.2, end - time.time()) if end > time.time() else 0)
        f.write(json.dumps(sample_once()) + "\n")  # final sample for accurate deltas
    return 0

def summarize(rows, clk_tck=None):
    rows = [r for r in rows if "t" in r]
    if not rows:
        return None
    clk = clk_tck or _clk_tck()
    hs = [r["heat"] for r in rows if r.get("heat") is not None]
    ts = [r["temp_c"] for r in rows if r.get("temp_c") is not None]
    first, last = rows[0], rows[-1]
    s = {"mode": "estimates" if hs else "readings", "samples": len(rows), "cores": last.get("cores"),
         "duration_s": round(last["t"] - first["t"], 1),
         "temp_avg_c": round(sum(ts) / len(ts), 1) if ts else None, "temp_peak_c": round(max(ts), 1) if ts else None,
         "cpu_minutes": None, "cpu_util": None}
    if hs:
        s.update({"heat_avg": round(sum(hs) / len(hs), 3), "heat_peak": round(max(hs), 3)})
    if first.get("busy_jiffies") is not None and last.get("busy_jiffies") is not None:
        busy = last["busy_jiffies"] - first["busy_jiffies"]
        tot = last["total_jiffies"] - first["total_jiffies"]
        s["cpu_minutes"] = round(busy / clk / 60.0, 3)
        s["cpu_util"] = round(busy / tot, 3) if tot > 0 else None
    return s

def add_energy(s, wpc=None):
    """ESTIMATE energy from machine-wide CPU-minutes (all processes on the box)."""
    if s is None or s.get("mode") != "estimates":
        return s
    w = watts_per_core(wpc)
    s["watts_per_core"] = w
    s["est_energy_wh"] = None if s["cpu_minutes"] is None else round(energy_wh(s["cpu_minutes"] * 60, w), 5)
    return s

def load(path):
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    return rows

def markdown(s, title="Heat report"):
    if s is None:
        return f"### {title}\n\nNo heat samples recorded.\n"
    def fmt(v, spec): return "n/a" if v is None else format(v, spec)
    est = s.get("mode") == "estimates"
    return (f"### {title}\n\n| metric | value |\n|---|---|\n"
            f"| mode | {s.get('mode', 'readings')} |\n"
            f"| avg temp (sensor) | {'no readings' if s.get('temp_avg_c') is None else format(s['temp_avg_c'], '.1f') + ' C'} |\n"
            f"| peak temp (sensor) | {'no readings' if s.get('temp_peak_c') is None else format(s['temp_peak_c'], '.1f') + ' C'} |\n"
            + (f"| avg heat (ESTIMATE) | {fmt(s['heat_avg'], '.2f')} |\n| peak heat (ESTIMATE) | {fmt(s['heat_peak'], '.2f')} |\n"
               if est else "") +
            f"| duration | {fmt(s['duration_s'], '.1f')} s |\n| CPU-minutes | {fmt(s['cpu_minutes'], '.2f')} |\n"
            f"| CPU utilization | {'n/a' if s['cpu_util'] is None else format(s['cpu_util']*100, '.0f') + '%'} |\n"
            f"| cores | {s['cores']} |\n| samples | {s['samples']} |\n"
            + (f"| est. energy (ESTIMATE) | {fmt(s['est_energy_wh'], '.4f')} Wh @ {s['watts_per_core']:g} W/core |\n"
               if "est_energy_wh" in s else "") + "\n"
            + ("_heat = max(load1/cores, CPU busy fraction) is an ESTIMATE; energy is an ESTIMATE: CPU-seconds x "
               "watts-per-core, not a power-meter reading. By [heatgate](https://github.com/fitzyracing1/heatgate)._\n" if est else
               "_Readings only: sensor temperatures and /proc/stat CPU time. Estimates (heat proxy, energy) are off; "
               "set input `estimates: true`. By [heatgate](https://github.com/fitzyracing1/heatgate)._\n"))

def report(path, summary=None, github_output=None, json_out=None, title="Heat report", wpc=None):
    s = add_energy(summarize(load(path)) if os.path.exists(path) else None, wpc)
    md = markdown(s, title)
    print(md)
    if summary:
        with open(summary, "a") as f: f.write(md + "\n")
    if github_output and s:
        with open(github_output, "a") as f:
            for k, v in s.items():
                f.write(f"{k.replace('_', '-')}={'' if v is None else v}\n")
    if json_out:
        with open(json_out, "w") as f: json.dump(s, f, indent=2)
    return 0
