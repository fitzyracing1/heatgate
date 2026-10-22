"""Router / Wi-Fi temperature (opt-in, best-effort, zero-dep, fail open).

Sources, each only when asked:
  * local sysfs: /sys/class/thermal + /sys/class/hwmon, incl. Wi-Fi PHY hwmon (iwlwifi, ath10k/11k/12k,
    mt76, brcmfmac, rtw88/89...). Useful when heatgate runs ON a Linux/OpenWrt router, or for the local
    Wi-Fi chip.
  * user command: e.g. `ssh root@192.168.1.1 cat /sys/class/thermal/thermal_zone0/temp`; the first
    number printed is parsed (values > 1000 treated as millidegrees).
  * OpenWrt ubus JSON-RPC (`http://<router>/ubus`, the rpcd endpoint LuCI uses): login with env
    HEATGATE_ROUTER_USER / HEATGATE_ROUTER_PASS, then `file read` of thermal/hwmon temp files
    (needs rpcd-mod-file and an ACL allowing read of those paths). Creds are read from env only.
Nothing scans the network. No temperature -> temp n/a, router stress None (contributes nothing).
"""
import glob, json, os, re, shlex, subprocess, urllib.request

WIFI_DRIVERS = ("iwlwifi", "ath9k", "ath10k", "ath11k", "ath12k", "mt76", "mt79", "mt7", "brcmfmac",
                "rtw88", "rtw89", "rtl8", "mwlwifi", "qcom_wifi")
NULL_SESSION = "0" * 32

def _read_c(path):
    try:
        with open(path) as f:
            v = float(f.read().strip())
        return v / 1000.0 if abs(v) > 200 else v
    except (OSError, ValueError):
        return None

def _ok(t):
    return t is not None and -40.0 < t < 150.0

def _is_wifi(name, devpath):
    n = (name or "").lower()
    return n.startswith(WIFI_DRIVERS) or "ieee80211" in devpath or ("/phy" in devpath and "/net/" in devpath)

def sysfs_temps(thermal="/sys/class/thermal", hwmon="/sys/class/hwmon"):
    """-> list of {source, name, temp_c, wifi}."""
    out = []
    for z in sorted(glob.glob(os.path.join(thermal, "thermal_zone*"))):
        t = _read_c(os.path.join(z, "temp"))
        if _ok(t):
            try:
                kind = open(os.path.join(z, "type")).read().strip()
            except OSError:
                kind = os.path.basename(z)
            out.append({"source": "thermal", "name": kind, "temp_c": t, "wifi": _is_wifi(kind, "")})
    for h in sorted(glob.glob(os.path.join(hwmon, "hwmon*"))):
        try:
            name = open(os.path.join(h, "name")).read().strip()
        except OSError:
            name = os.path.basename(h)
        dev = os.path.realpath(os.path.join(h, "device")) if os.path.exists(os.path.join(h, "device")) else ""
        if os.path.isdir(os.path.join(h, "device", "ieee80211")):
            dev += "/ieee80211"
        for p in sorted(glob.glob(os.path.join(h, "temp*_input"))):
            t = _read_c(p)
            if _ok(t):
                out.append({"source": "hwmon", "name": name, "temp_c": t, "wifi": _is_wifi(name, dev)})
    return out

_NUM = re.compile(r"-?\d+(?:\.\d+)?")

def parse_temp(text):
    """First number in text -> degrees C (millidegrees if > 200)."""
    m = _NUM.search(text or "")
    if not m:
        return None
    v = float(m.group())
    v = v / 1000.0 if abs(v) > 200 else v
    return v if _ok(v) else None

def cmd_temp(cmd, timeout=15):
    """Run a user-supplied command (no shell) and parse its temperature."""
    args = shlex.split(cmd) if isinstance(cmd, str) else list(cmd)
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return parse_temp(r.stdout)

UBUS_PATHS = ["/sys/class/thermal/thermal_zone%d/temp" % i for i in range(4)] + \
             ["/sys/class/hwmon/hwmon%d/temp1_input" % i for i in range(6)]

def _ubus(url, sid, obj, method, params, timeout):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "call", "params": [sid, obj, method, params]})
    req = urllib.request.Request(url, body.encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        res = json.loads(r.read().decode())["result"]
    return res[1] if res and res[0] == 0 and len(res) > 1 else None

def ubus_temps(url, user=None, password=None, timeout=5):
    """OpenWrt ubus JSON-RPC. Creds from args or env HEATGATE_ROUTER_USER/PASS. -> list of temps."""
    user = user or os.environ.get("HEATGATE_ROUTER_USER", "root")
    password = password if password is not None else os.environ.get("HEATGATE_ROUTER_PASS", "")
    if not url.rstrip("/").endswith("/ubus"):
        url = url.rstrip("/") + "/ubus"
    login = _ubus(url, NULL_SESSION, "session", "login", {"username": user, "password": password}, timeout)
    if not login or "ubus_rpc_session" not in login:
        raise PermissionError("ubus login failed")
    sid, out = login["ubus_rpc_session"], []
    for p in UBUS_PATHS:
        try:
            d = _ubus(url, sid, "file", "read", {"path": p}, timeout)
        except Exception:
            d = None
        t = parse_temp(d.get("data")) if d else None
        if t is not None:
            out.append({"source": "ubus", "name": p, "temp_c": t, "wifi": False})
    return out

def temp_stress(t, band=(40.0, 70.0)):
    if t is None:
        return None
    lo, hi = band
    return max(0.0, min(1.0, (t - lo) / max(hi - lo, 1e-9)))

def router_status(cmd=None, url=None, local="wifi", band=(40.0, 70.0)):
    """Fail open: errors are collected, never raised. router_temp_stress is None if no temperature."""
    cmd = cmd or os.environ.get("HEATGATE_ROUTER_CMD")
    url = url or os.environ.get("HEATGATE_ROUTER_URL")
    sensors, errors = [], {}
    if local:  # "wifi": only Wi-Fi chip sensors; "all"/True: every sensor (heatgate running ON the router)
        try:
            sensors += [s for s in sysfs_temps() if local != "wifi" or s["wifi"]]
        except Exception as e:
            errors["sysfs"] = repr(e)
    if cmd:
        try:
            t = cmd_temp(cmd)
            if t is None:
                errors["cmd"] = "no temperature in output"
            else:
                sensors.append({"source": "cmd", "name": "router-cmd", "temp_c": t, "wifi": False})
        except Exception as e:
            errors["cmd"] = repr(e)
    if url:
        try:
            got = ubus_temps(url)
            sensors += got
            if not got:
                errors["ubus"] = "no readable temperature files (rpcd-mod-file/ACL?)"
        except Exception as e:
            errors["ubus"] = repr(e)
    temps = [s["temp_c"] for s in sensors]
    wifi = [s["temp_c"] for s in sensors if s["wifi"]]
    t = max(temps) if temps else None
    return {"temp_c": t, "wifi_temp_c": max(wifi) if wifi else None, "band": list(band),
            "router_temp_stress": temp_stress(t, band), "sensors": sensors, "errors": errors}

class Router:
    """Callable config: Router(cmd=...)() -> router_temp_stress or None (fail open)."""
    def __init__(self, cmd=None, url=None, local="wifi", band=(40.0, 70.0), weight=1.0):
        self.kw = dict(cmd=cmd, url=url, local=local, band=band)
        self.weight = weight
    def status(self):
        return router_status(**self.kw)
    def __call__(self):
        try:
            return self.status()["router_temp_stress"]
        except Exception:
            return None
