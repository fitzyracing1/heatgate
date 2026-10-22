"""Minimal zero-dependency stdio MCP server for heatgate (JSON-RPC 2.0, newline-delimited)."""
import json, os, subprocess, sys
from . import __version__, readings, workers, estimates_enabled
from .app import app_readings, energy_estimate

PROTOCOL = "2025-06-18"
HIGH = {"type": "number", "description": "Pause threshold (0..1, default 0.8)"}
LOW = {"type": "number", "description": "Resume threshold (0..1, default 0.5)"}
EST = {"type": "boolean", "description": "Opt in to ESTIMATES (load-as-heat, energy Wh, stress scores). "
       "Default: readings-only unless env HEATGATE_ESTIMATES=1"}
THI = {"type": "number", "description": "Readings mode: pause at this sensor temp C (default 85)"}
TLO = {"type": "number", "description": "Readings mode: resume below this C (default 75)"}
TOOLS = [
    {"name": "heat_status", "description": "Current measured readings + mode. Readings-only (default): real sensor temps "
     "(temp_c, sensors), raw cpu_pct; no_readings=true when no sensor. With estimates: also load-as-heat proxy `heat`.",
     "inputSchema": {"type": "object", "properties": {"estimates": EST}}},
    {"name": "should_pause", "description": "Whether heavy work should pause now: sensor temp >= temp_high "
     "(readings mode; never pauses with no sensor) or heat >= high (estimates).",
     "inputSchema": {"type": "object", "properties": {"high": HIGH, "temp_high": THI, "estimates": EST}}},
    {"name": "recommended_workers", "description": "Worker count scaled down as heat rises.",
     "inputSchema": {"type": "object", "properties": {"max_workers": {"type": "integer"}, "min_workers": {"type": "integer"},
                                                      "high": HIGH, "low": LOW, "temp_high": THI, "temp_low": TLO,
                                                      "estimates": EST}}},
    {"name": "app_heat", "description": "Heat of one process tree: busy cores / total cores over a short interval, "
     "plus cumulative CPU-seconds. Falls back to machine heat (source='machine') where per-process CPU is unavailable.",
     "inputSchema": {"type": "object", "required": ["pid"], "properties": {
         "pid": {"type": "integer"}, "interval": {"type": "number", "description": "Seconds to measure (default 0.5)"}}}},
    {"name": "energy_estimate", "description": "(Opt-in: estimates=true or HEATGATE_ESTIMATES=1) ESTIMATED watt-hours from CPU-seconds (or a pid's cumulative CPU) "
     "x watts-per-core (default env HEATGATE_WATTS_PER_CORE or 10 W). Not a power-meter reading.",
     "inputSchema": {"type": "object", "properties": {
         "cpu_seconds": {"type": "number"}, "pid": {"type": "integer"}, "watts_per_core": {"type": "number"}, "estimates": EST}}},
    {"name": "run_throttled", "description": "Run a shell command under heatgate (SIGSTOP when hot, SIGCONT when cool). "
     "Disabled unless the server was started with HEATGATE_ALLOW_RUN=1.",
     "inputSchema": {"type": "object", "required": ["cmd"], "properties": {
         "cmd": {"type": "string", "description": "Command line to run"}, "high": HIGH, "low": LOW,
         "timeout": {"type": "number", "description": "Seconds before giving up (default 3600)"},
         "temp_high": THI, "temp_low": TLO, "estimates": EST}}},
    {"name": "grid_status", "description": "Outside temperature (Open-Meteo) and power-grid stress (ISO-NE keyless; "
     "EIA if EIA_API_KEY set) as grid_stress 0..1. Fails open (null fields) on network errors.",
     "inputSchema": {"type": "object", "properties": {
         "zip": {"type": "string", "description": "US ZIP code"}, "lat": {"type": "number"}, "lon": {"type": "number"},
         "comfort_lo": {"type": "number", "description": "default 10 C"},
         "comfort_hi": {"type": "number", "description": "default 24 C"}, "estimates": EST}}},
]

TOOLS.append({"name": "router_status", "description": "Router / Wi-Fi chip temperature as router_temp_stress 0..1 "
     "over a band (default 40-70 C). Sources: local Wi-Fi hwmon, env HEATGATE_ROUTER_CMD, OpenWrt ubus "
     "HEATGATE_ROUTER_URL (+USER/PASS env). Null when no temperature is available (fails open).",
     "inputSchema": {"type": "object", "properties": {
         "band_lo": {"type": "number", "description": "default 40 C"},
         "band_hi": {"type": "number", "description": "default 70 C"},
         "on_router": {"type": "boolean", "description": "use all local sensors (server runs on the router)"},
         "estimates": EST}}})

TOOLS.append({"name": "call_log", "description": "Summarize a heatgate AI-call JSONL log (count, provider-reported "
              "tokens, avg/p95 duration, time held and why) plus the last N records. Path defaults to $HEATGATE_CALL_LOG.",
              "inputSchema": {"type": "object", "properties": {"path": {"type": "string"},
                                                               "last": {"type": "integer", "description": "records to return (default 10)"}}}})
TOOLS.append({"name": "pace_status", "description": "Whether the next AI call would be held now: call lock busy, "
              "measured temperature >= temp_high (readings-only), recent measured latency above latency_threshold.",
              "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "lock": {"type": "string"},
                                                               "temp_high": THI, "latency_threshold": {"type": "number"},
                                                               "window": {"type": "integer"}}}})

def _calls_tool(name, args):
    from . import calls
    path = args.get("path") or calls.DEFAULT_LOG
    recs = calls.load(path) if os.path.exists(path) else []
    if name == "call_log":
        return {"path": path, "summary": calls.summarize(recs), "last": recs[-int(args.get("last", 10)):]}
    import fcntl
    lock = args.get("lock") or os.environ.get("HEATGATE_CALL_LOCK") or os.path.join(
        os.environ.get("TMPDIR", "/tmp"), f"heatgate-call-{os.getuid()}.lock")
    busy = False
    if os.path.exists(lock):
        with open(lock, "a") as lf:
            busy = calls._locked(lf)
    from . import sensor_temp
    t, th = sensor_temp(), float(args.get("temp_high", 85))
    w = int(args.get("window", 5)); durs = [r["duration_s"] for r in recs[-w:] if isinstance(r.get("duration_s"), (int, float))]
    lat = sum(durs) / len(durs) if durs else None
    lt = args.get("latency_threshold")
    reasons = (["lock"] if busy else []) + (["heat"] if t is not None and t >= th else []) + \
              (["latency"] if lt is not None and lat is not None and lat > float(lt) else [])
    return {"mode": "readings", "would_hold": bool(reasons), "reasons": reasons, "lock_busy": busy, "temp_c": t,
            "temp_high": th, "recent_latency_s": lat, "latency_threshold_s": lt, "path": path}

def call(name, args):
    if name in ("call_log", "pace_status"):
        return _calls_tool(name, args)
    if name == "router_status":
        from .router import router_status
        d = router_status(local="all" if args.get("on_router") else "wifi",
                          band=(float(args.get("band_lo", 40)), float(args.get("band_hi", 70))))
        if not estimates_enabled(args.get("estimates")):
            d.pop("router_temp_stress", None); d["mode"] = "readings"
        return d
    if name == "grid_status":
        from .grid import grid_status
        d = grid_status(args.get("zip"), args.get("lat"), args.get("lon"),
                        (float(args.get("comfort_lo", 10)), float(args.get("comfort_hi", 24))))
        if not estimates_enabled(args.get("estimates")):
            for k in ("temp_stress", "demand_stress", "grid_stress"):
                d.pop(k, None)
            if d.get("demand"):
                d["demand"].pop("ratio", None)
            d["mode"] = "readings"
        return d
    est = estimates_enabled(args.get("estimates"))
    if name == "heat_status":
        return readings(estimates=est)
    if name == "should_pause":
        if not est:
            r = readings(estimates=False); th = float(args.get("temp_high", 85))
            return {"mode": "readings", "pause": bool(r["temp_c"] is not None and r["temp_c"] >= th),
                    "temp_c": r["temp_c"], "temp_high": th, "no_readings": r["temp_c"] is None}
        r = readings(estimates=True); high = float(args.get("high", 0.8))
        return {"mode": "estimates", "pause": r["heat"] >= high, "heat": r["heat"], "high": high}
    if name == "recommended_workers":
        return {"mode": "estimates" if est else "readings",
                "workers": workers(args.get("max_workers"), int(args.get("min_workers", 1)),
                                   float(args.get("high", 0.8)), float(args.get("low", 0.5)), estimates=est,
                                   temp_high=float(args.get("temp_high", 85)), temp_low=float(args.get("temp_low", 75)))}
    if name == "app_heat":
        return app_readings(int(args["pid"]), float(args.get("interval", 0.5)))
    if name == "energy_estimate":
        if not est:
            return {"mode": "readings", "disabled": True,
                    "note": "energy Wh is an estimate; pass estimates=true or set HEATGATE_ESTIMATES=1"}
        return energy_estimate(args.get("cpu_seconds"), args.get("pid"), args.get("watts_per_core"))
    if name == "run_throttled":
        if os.environ.get("HEATGATE_ALLOW_RUN") != "1":
            raise PermissionError("run_throttled is disabled; restart the MCP server with env HEATGATE_ALLOW_RUN=1 to opt in")
        cmd = [sys.executable, "-m", "heatgate", "run", "--high", str(args.get("high", 0.8)),
               "--low", str(args.get("low", 0.5))] + (["--estimates"] if est else []) + \
              ["--temp-high", str(args.get("temp_high", 85)), "--temp-low", str(args.get("temp_low", 75)),
               "--", "sh", "-c", args["cmd"]]
        env = dict(os.environ)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        p = subprocess.run(cmd, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL,
                           timeout=float(args.get("timeout", 3600)))
        return {"returncode": p.returncode, "stdout": p.stdout[-20000:], "stderr": p.stderr[-20000:]}
    raise KeyError(f"unknown tool: {name}")

def handle(msg):
    m, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if mid is None:
        return None  # notification
    if m == "initialize":
        res = {"protocolVersion": params.get("protocolVersion", PROTOCOL), "capabilities": {"tools": {}},
               "serverInfo": {"name": "heatgate", "version": __version__}}
    elif m == "ping":
        res = {}
    elif m == "tools/list":
        res = {"tools": TOOLS}
    elif m == "tools/call":
        try:
            out = call(params.get("name"), params.get("arguments") or {})
            res = {"content": [{"type": "text", "text": json.dumps(out)}], "structuredContent": out, "isError": False}
        except Exception as e:
            res = {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {m}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": res}

def serve(inp=sys.stdin, out=sys.stdout):
    for line in inp:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            resp = handle(msg)
        if resp is not None:
            out.write(json.dumps(resp) + "\n"); out.flush()
    return 0


# ---- Streamable HTTP transport (stdlib only) ----------------------------------------------
# POST /mcp with a JSON-RPC message (or batch); replies application/json. Notifications -> 202.
# Every request must carry `Authorization: Bearer <token>` (or use /mcp/<token> with path_token=True).

def make_http_server(host="127.0.0.1", port=8765, token=None, path_token=False, quiet=False):
    import hmac, secrets, uuid
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    token = token or os.environ.get("HEATGATE_TOKEN") or secrets.token_urlsafe(32)
    sessions = set()

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, fmt, *a):
            if not quiet:
                sys.stderr.write("[heatgate-http] %s %s\n" % (self.address_string(), fmt % a))
        def _send(self, code, body=None, headers=None):
            data = b"" if body is None else json.dumps(body).encode()
            self.send_response(code)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            if body is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers(); self.wfile.write(data)
        def _authed(self):
            path = self.path.split("?", 1)[0].rstrip("/")
            if path == "/mcp":
                got = self.headers.get("Authorization", "")
                ok = got.startswith("Bearer ") and hmac.compare_digest(got[7:].strip(), token)
            elif path_token and path.startswith("/mcp/"):
                ok = hmac.compare_digest(path[5:], token)
            else:
                self._send(404, {"error": "not found"}); return False
            if not ok:
                self._send(401, {"error": "missing or invalid bearer token"},
                           {"WWW-Authenticate": 'Bearer realm="heatgate"'})
            return ok
        def do_GET(self):  # no server-initiated SSE stream
            if self._authed():
                self._send(405, {"error": "use POST"}, {"Allow": "POST, DELETE"})
        def do_DELETE(self):
            if self._authed():
                sessions.discard(self.headers.get("Mcp-Session-Id")); self._send(204)
        def do_POST(self):
            if not self._authed():
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                msg = json.loads(self.rfile.read(n) or b"null")
            except ValueError:
                return self._send(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            msgs = msg if isinstance(msg, list) else [msg]
            if not all(isinstance(m, dict) for m in msgs):
                return self._send(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}})
            hdr = {}
            if any(m.get("method") == "initialize" for m in msgs):
                sid = uuid.uuid4().hex; sessions.add(sid); hdr["Mcp-Session-Id"] = sid
            out = [r for r in (handle(m) for m in msgs) if r is not None]
            if not out:
                return self._send(202, None, hdr)
            self._send(200, out if isinstance(msg, list) else out[0], hdr)

    srv = ThreadingHTTPServer((host, port), H)
    srv.token = token
    return srv

def serve_http(host="127.0.0.1", port=8765, token=None, path_token=False):
    srv = make_http_server(host, port, token, path_token)
    src = "HEATGATE_TOKEN" if (token or os.environ.get("HEATGATE_TOKEN")) else "generated"
    print(f"[heatgate] MCP Streamable HTTP on http://{srv.server_address[0]}:{srv.server_address[1]}/mcp", file=sys.stderr)
    if src == "generated":
        print(f"[heatgate] bearer token (shown once; set HEATGATE_TOKEN to choose your own): {srv.token}", file=sys.stderr)
    print(f"[heatgate] run_throttled is {'ENABLED' if os.environ.get('HEATGATE_ALLOW_RUN') == '1' else 'disabled'}", file=sys.stderr, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0
