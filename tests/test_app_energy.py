import io, json, os, subprocess, sys, tempfile, threading, time, unittest, urllib.request, urllib.error
import heatgate
from heatgate import app, cli, mcp

BUSY = "import time\nt=time.time()\nwhile time.time()-t<1.5: pass"

class App(unittest.TestCase):
    @unittest.skipUnless(os.path.isdir("/proc"), "Linux /proc")
    def test_tree_cpu_counts_children(self):
        p = subprocess.Popen([sys.executable, "-c", f"import subprocess,sys; subprocess.run([sys.executable,'-c',{BUSY!r}])"])
        time.sleep(1.0)
        self.assertGreaterEqual(len(app.tree_pids(p.pid)), 2)
        r = app.app_readings(p.pid, 0.3)
        p.wait()
        self.assertEqual(r["source"], "proc")
        self.assertGreater(r["app_cores"], 0.5)  # grandchild busy loop is counted
    def test_missing_pid_falls_back(self):
        r = app.app_readings(2 ** 22 + 12345, 0.05)
        self.assertEqual(r["source"], "machine"); self.assertIsNone(r["cpu_seconds"])
    def test_stat_parse_with_spaces(self):
        d = tempfile.mkdtemp(); os.makedirs(os.path.join(d, "7"))
        f = " ".join(["S", "1"] + ["0"] * 9 + ["100", "50", "20", "30"] + ["0"] * 30)
        with open(os.path.join(d, "7", "stat"), "w") as fh: fh.write("7 (a (b) c) " + f)
        self.assertEqual(app._read_stat(7, d), (1, 200))
    def test_energy(self):
        self.assertAlmostEqual(app.energy_wh(3600, 10), 10.0)
        os.environ["HEATGATE_WATTS_PER_CORE"] = "5"
        try:
            self.assertEqual(app.watts_per_core(), 5.0)
            e = app.energy_estimate(cpu_s=720)
            self.assertTrue(e["estimate"]); self.assertAlmostEqual(e["est_energy_wh"], 1.0)
        finally:
            del os.environ["HEATGATE_WATTS_PER_CORE"]
        self.assertEqual(app.watts_per_core(), app.DEFAULT_WATTS_PER_CORE)
        self.assertRaises(ValueError, app.energy_estimate)
    def test_gate_pid(self):
        g = heatgate.Gate(high=0.99, low=0.98, pid=os.getpid(), _sleep=lambda s: 1 / 0)
        self.assertEqual(g.wait(), 0.0)

class Run(unittest.TestCase):
    def test_run_energy_summary(self):
        js = os.path.join(tempfile.mkdtemp(), "r.json")
        rc = cli.main(["run", "--energy", "--watts-per-core", "12", "--poll", "0.3", "--json-out", js,
                       "--", sys.executable, "-c", BUSY])
        self.assertEqual(rc, 0); s = json.load(open(js))
        self.assertGreater(s["app_cpu_seconds"], 1.0)
        self.assertAlmostEqual(s["est_energy_wh"], s["app_cpu_seconds"] * 12 / 3600, places=4)
        self.assertTrue(s["estimate"]); self.assertEqual(s["watts_per_core"], 12)
    def test_run_gate_app_pauses_and_saves(self):
        # app gate: pause once the app is busy, then resume as machine heat (< 2.0) is always below low
        js = os.path.join(tempfile.mkdtemp(), "r.json")
        rc = cli.main(["run", "--gate", "app", "--high", "0.01", "--low", "2.0", "--poll", "0.3",
                       "--json-out", js, "--", sys.executable, "-c", BUSY])
        s = json.load(open(js)); self.assertEqual(rc, 0)
        self.assertGreaterEqual(s["pauses"], 1)
    def test_status_pid(self):
        self.assertEqual(cli.main(["status", "--pid", str(os.getpid()), "--json"]), 0)

class MCP(unittest.TestCase):
    def test_tools(self):
        names = [t["name"] for t in mcp.TOOLS]
        for n in ("app_heat", "energy_estimate", "run_throttled"): self.assertIn(n, names)
        self.assertEqual(mcp.call("app_heat", {"pid": os.getpid(), "interval": 0.05})["pid"], os.getpid())
        self.assertAlmostEqual(mcp.call("energy_estimate", {"cpu_seconds": 360, "watts_per_core": 10})["est_energy_wh"], 1.0)
        self.assertGreaterEqual(mcp.call("energy_estimate", {"pid": os.getpid()})["cpu_seconds"], 0)

class HTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.pop("HEATGATE_ALLOW_RUN", None)
        cls.srv = mcp.make_http_server("127.0.0.1", 0, token="t0k", path_token=True, quiet=True)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]
    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()
    def post(self, body, token="t0k", path="/mcp"):
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if token: h["Authorization"] = "Bearer " + token
        req = urllib.request.Request(self.base + path, json.dumps(body).encode(), h, method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                d = r.read(); return r.status, dict(r.headers), json.loads(d) if d else None
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), None
    def test_auth(self):
        self.assertEqual(self.post({}, token=None)[0], 401)
        self.assertEqual(self.post({}, token="bad")[0], 401)
        self.assertEqual(self.post({}, path="/other")[0], 404)
    def test_flow(self):
        c, h, b = self.post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(c, 200); self.assertIn("Mcp-Session-Id", h); self.assertEqual(b["result"]["serverInfo"]["name"], "heatgate")
        self.assertEqual(self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})[0], 202)
        c, _, b = self.post({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "heat_status", "arguments": {}}})
        self.assertIn("heat", b["result"]["structuredContent"])
        c, _, b = self.post({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "run_throttled", "arguments": {"cmd": "true"}}})
        self.assertTrue(b["result"]["isError"])  # still gated by HEATGATE_ALLOW_RUN
        c, _, b = self.post({"jsonrpc": "2.0", "id": 4, "method": "tools/list"}, token=None, path="/mcp/t0k")
        self.assertEqual(c, 200); self.assertIn("app_heat", [t["name"] for t in b["result"]["tools"]])

if __name__ == "__main__":
    unittest.main()

# These suites exercise the opt-in ESTIMATES mode (load proxy, energy Wh, grid_stress);
# readings-only defaults are covered in test_readings_mode.py.
import os as _os
_prev_est = None
def setUpModule():
    global _prev_est
    _prev_est = _os.environ.get("HEATGATE_ESTIMATES"); _os.environ["HEATGATE_ESTIMATES"] = "1"
def tearDownModule():
    if _prev_est is None:
        _os.environ.pop("HEATGATE_ESTIMATES", None)
    else:
        _os.environ["HEATGATE_ESTIMATES"] = _prev_est
