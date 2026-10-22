import json, subprocess, sys, unittest
import heatgate
from heatgate import cli

class T(unittest.TestCase):
    def test_readings(self):
        r = heatgate.readings(0.05)
        self.assertGreaterEqual(r["heat"], 0.0); self.assertIn("load1", r)
    def test_thermal_missing(self):
        self.assertIsNone(heatgate.thermal("/nonexistent"))
    def test_pace(self):
        s = []
        self.assertEqual(heatgate.pace(_heat=lambda: 0.1, _sleep=s.append), 0.0)
        self.assertAlmostEqual(heatgate.pace(0.8, 0.5, 4, _heat=lambda: 0.65, _sleep=s.append), 2.0)
        self.assertEqual(heatgate.pace(0.8, 0.5, 4, _heat=lambda: 2.0, _sleep=s.append), 4.0)
    def test_gate_hysteresis(self):
        vals = iter([0.9, 0.9, 0.7, 0.4]); slept = []
        with heatgate.Gate(0.8, 0.5, poll=1, _heat=lambda: next(vals), _sleep=slept.append) as g:
            pass
        self.assertEqual(len(slept), 2); self.assertEqual(g.waited, 2)
    def test_gate_cool(self):
        with heatgate.Gate(_heat=lambda: 0.1, _sleep=lambda s: 1/0) as g:
            self.assertEqual(g.waited, 0)
    def test_decorator(self):
        @heatgate.throttled(high=100, low=99)
        def f(x): return x * 2
        self.assertEqual(f(3), 6)
    def test_workers(self):
        self.assertEqual(heatgate.workers(8, _heat=lambda: 0.1), 8)
        self.assertEqual(heatgate.workers(8, _heat=lambda: 0.9), 1)
        self.assertTrue(1 < heatgate.workers(8, _heat=lambda: 0.65) < 8)
    def test_cli_status(self):
        self.assertEqual(cli.main(["status"]), 0)
    def test_cli_run(self):
        self.assertEqual(cli.run([sys.executable, "-c", "pass"], 100, 99, 0.2), 0)
        self.assertEqual(cli.run([sys.executable, "-c", "import sys; sys.exit(3)"], 100, 99, 0.2), 3)
    def test_cli_pause_resume(self):
        # high=0 forces a pause; low=-1 never resumes until exit -> use flipping heat
        import itertools; seq = itertools.chain([1.0], itertools.repeat(0.0))
        orig = cli.readings
        cli.readings = lambda interval=0, **k: {"heat": next(seq), "load1": 0, "cpu": 0}
        try:
            self.assertEqual(cli.run([sys.executable, "-c", "import time; time.sleep(1)"], 0.8, 0.5, 0.1, estimates=True), 0)
        finally:
            cli.readings = orig

if __name__ == "__main__":
    unittest.main()


class TestMCP(unittest.TestCase):
    def rpc(self, lines):
        import io
        from heatgate.mcp import serve
        out = io.StringIO()
        serve(io.StringIO("\n".join(json.dumps(l) for l in lines) + "\n"), out)
        return [json.loads(l) for l in out.getvalue().splitlines()]

    def test_flow(self):
        import os
        os.environ.pop("HEATGATE_ALLOW_RUN", None)
        r = self.rpc([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                      {"jsonrpc": "2.0", "method": "notifications/initialized"},
                      {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                      {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "heat_status", "arguments": {}}},
                      {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "run_throttled", "arguments": {"cmd": "true"}}}])
        self.assertEqual(len(r), 4)
        self.assertEqual({t["name"] for t in r[1]["result"]["tools"]},
                         {"heat_status", "should_pause", "recommended_workers", "run_throttled", "app_heat", "energy_estimate", "grid_status", "router_status", "call_log", "pace_status"})
        self.assertIn("heat", r[2]["result"]["structuredContent"])
        self.assertTrue(r[3]["result"]["isError"])

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
