"""Scheduled temperature checker for YOUR OWN listed devices (no discovery, no scanning).

Config (JSON; TOML too on Python 3.11+ via tomllib):
  {"devices": [{"name": "this-box", "source": "local", "threshold_c": 75},
               {"name": "laptop-wifi", "source": "local-wifi", "threshold_c": 70},
               {"name": "openwrt", "source": "ubus", "url": "http://192.168.1.1/ubus", "threshold_c": 70},
               {"name": "nas", "source": "cmd", "cmd": "ssh nas cat /sys/class/thermal/thermal_zone0/temp"}]}
Credentials for ubus come from env HEATGATE_ROUTER_USER / HEATGATE_ROUTER_PASS only (or per-device
user_env / pass_env naming other env vars). Never put passwords in the config.
"""
import html, json, os, sys, time
from . import router as R

DEFAULT_THRESHOLD = 70.0

def load_config(path):
    with open(path, "rb") as f:
        raw = f.read()
    if path.endswith(".toml"):
        import tomllib  # Python 3.11+
        cfg = tomllib.loads(raw.decode())
    else:
        cfg = json.loads(raw.decode())
    devs = cfg.get("devices") or []
    for d in devs:
        if any(k in d for k in ("password", "pass", "token")):
            raise ValueError(f"device {d.get('name')}: put credentials in env vars, not the config")
    return devs

def read_device(d):
    """-> {name, source, temp_c, threshold_c, status: hot|ok|n/a, error}. Never raises."""
    src = d.get("source", "local")
    thr = float(d.get("threshold_c", DEFAULT_THRESHOLD))
    t, err = None, None
    try:
        if src in ("local", "local-wifi"):
            s = [x for x in R.sysfs_temps() if src == "local" or x["wifi"]]
            t = max(x["temp_c"] for x in s) if s else None
        elif src == "cmd":
            t = R.cmd_temp(d["cmd"])
        elif src == "ubus":
            user = os.environ.get(d.get("user_env", "HEATGATE_ROUTER_USER"))
            pw = os.environ.get(d.get("pass_env", "HEATGATE_ROUTER_PASS"), "")
            s = R.ubus_temps(d["url"], user, pw)
            t = max(x["temp_c"] for x in s) if s else None
        else:
            err = f"unknown source {src!r}"
    except Exception as e:
        err = repr(e)
    status = "n/a" if t is None else ("hot" if t >= thr else "ok")
    return {"name": d.get("name", src), "source": src, "temp_c": t, "threshold_c": thr,
            "status": status, "error": err}

def write_page(outdir, results, ts):
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "status.json"), "w") as f:
        json.dump({"time": ts, "devices": results}, f, indent=2)
    color = {"hot": "#c62828", "ok": "#2e7d32", "n/a": "#757575"}
    rows = "".join(
        f"<tr><td>{html.escape(r['name'])}</td><td>{html.escape(r['source'])}</td>"
        f"<td>{'n/a' if r['temp_c'] is None else '%.1f &deg;C' % r['temp_c']}</td>"
        f"<td>{r['threshold_c']:g} &deg;C</td><td style='color:{color[r['status']]};font-weight:bold'>"
        f"{r['status']}</td><td>{html.escape(r['error'] or '')}</td></tr>" for r in results)
    with open(os.path.join(outdir, "index.html"), "w") as f:
        f.write("<!doctype html><meta charset=utf-8><title>heatgate device temps</title>"
                "<style>body{font-family:sans-serif;margin:2em}td,th{padding:.3em .8em;border-bottom:1px solid #ddd}</style>"
                f"<h1>Device temperatures</h1><p>Updated {html.escape(ts)}</p><table><tr><th>Device</th>"
                f"<th>Source</th><th>Temp</th><th>Threshold</th><th>Status</th><th>Error</th></tr>{rows}</table>")

def check(devices, log_path=None, page_dir=None):
    ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    results = [read_device(d) for d in devices]
    if log_path:
        with open(log_path, "a") as f:
            for r in results:
                f.write(json.dumps(dict(r, time=ts)) + "\n")
    if page_dir:
        write_page(page_dir, results, ts)
    return ts, results

def watch(config, interval=300.0, once=False, log_path=None, page_dir=None, _sleep=time.sleep):
    """Exit code 0 = all ok/n/a, 2 = at least one device hot (last round)."""
    devices = load_config(config)
    while True:
        ts, results = check(devices, log_path, page_dir)
        hot = False
        for r in results:
            t = "n/a" if r["temp_c"] is None else f"{r['temp_c']:.1f}C"
            line = f"{ts} {r['name']} [{r['source']}] temp={t} threshold={r['threshold_c']:g}C {r['status']}"
            if r["error"]:
                line += f" error={r['error']}"
            if r["status"] == "hot":
                hot = True; line = "WARNING HOT " + line
            print(line, file=sys.stderr if r["status"] == "hot" else sys.stdout, flush=True)
        if once:
            return 2 if hot else 0
        _sleep(interval)
