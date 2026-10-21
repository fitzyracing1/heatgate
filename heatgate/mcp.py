"""Minimal zero-dependency stdio MCP server for heatgate (JSON-RPC 2.0, newline-delimited)."""
import json, os, subprocess, sys
from . import __version__, readings, workers

PROTOCOL = "2025-06-18"
HIGH = {"type": "number", "description": "Pause threshold (0..1, default 0.8)"}
LOW = {"type": "number", "description": "Resume threshold (0..1, default 0.5)"}
TOOLS = [
    {"name": "heat_status", "description": "Current machine heat (CPU load as heat proxy): heat, load1, cores, cpu, temp_c.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "should_pause", "description": "Whether heavy work should pause now (heat >= high).",
     "inputSchema": {"type": "object", "properties": {"high": HIGH}}},
    {"name": "recommended_workers", "description": "Worker count scaled down as heat rises.",
     "inputSchema": {"type": "object", "properties": {"max_workers": {"type": "integer"}, "min_workers": {"type": "integer"},
                                                      "high": HIGH, "low": LOW}}},
    {"name": "run_throttled", "description": "Run a shell command under heatgate (SIGSTOP when hot, SIGCONT when cool). "
     "Disabled unless the server was started with HEATGATE_ALLOW_RUN=1.",
     "inputSchema": {"type": "object", "required": ["cmd"], "properties": {
         "cmd": {"type": "string", "description": "Command line to run"}, "high": HIGH, "low": LOW,
         "timeout": {"type": "number", "description": "Seconds before giving up (default 3600)"}}}},
]

def call(name, args):
    if name == "heat_status":
        return readings()
    if name == "should_pause":
        r = readings(); high = float(args.get("high", 0.8))
        return {"pause": r["heat"] >= high, "heat": r["heat"], "high": high}
    if name == "recommended_workers":
        return {"workers": workers(args.get("max_workers"), int(args.get("min_workers", 1)),
                                   float(args.get("high", 0.8)), float(args.get("low", 0.5)))}
    if name == "run_throttled":
        if os.environ.get("HEATGATE_ALLOW_RUN") != "1":
            raise PermissionError("run_throttled is disabled; restart the MCP server with env HEATGATE_ALLOW_RUN=1 to opt in")
        cmd = [sys.executable, "-m", "heatgate", "run", "--high", str(args.get("high", 0.8)),
               "--low", str(args.get("low", 0.5)), "--", "sh", "-c", args["cmd"]]
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
