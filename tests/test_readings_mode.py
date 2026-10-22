"""Readings-only is the default: gate on real sensor temps; no sensor -> no readings, no throttle."""
import io, json, os, sys, unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock
import heatgate
from heatgate import Gate, workers, readings, estimates_enabled, cli, mcp, report

OFF = {"HEATGATE_ESTIMATES": ""}
HOT = [{"source": "hwmon", "name": "coretemp", "temp_c": 92.0, "wifi": False}]

class Mode(unittest.TestCase):
    def test_flag_env(self):
        with mock.patch.dict(os.environ, OFF):
            self.assertFalse(estimates_enabled()); self.assertTrue(estimates_enabled(True))
        with mock.patch.dict(os.environ, {"HEATGATE_ESTIMATES": "1"}):
            self.assertTrue(estimates_enabled()); self.assertFalse(estimates_enabled(False))

    def test_readings_default_no_sensor(self):
        with mock.patch.dict(os.environ, OFF), mock.patch.object(heatgate, "sensors", return_value=[]), \
             mock.patch.object(heatgate, "thermal", return_value=None):
            r = readings(0.01)
        self.assertEqual(r["mode"], "readings"); self.assertTrue(r["no_readings"])
        self.assertNotIn("heat", r); self.assertNotIn("load1", r)

    def test_gate_no_sensor_never_throttles(self):
        with mock.patch.dict(os.environ, OFF), mock.patch.object(heatgate, "sensors", return_value=[]), \
             mock.patch.object(heatgate, "thermal", return_value=None):
            self.assertEqual(Gate(_sleep=lambda s: self.fail("slept")).wait(), 0.0)
            self.assertEqual(workers(8), 8)

    def test_gate_temp_hysteresis(self):
        temps = iter([92.0, 90.0, 80.0, 74.0])
        with mock.patch.dict(os.environ, OFF), \
             mock.patch.object(heatgate, "sensor_temp", side_effect=lambda: next(temps)):
            g = Gate(temp_high=85, temp_low=75, poll=1, _sleep=lambda s: None)
            self.assertEqual(g.wait(), 2)  # hot at 92; still >=75 at 90, 80; resumes at 74

    def test_gate_router_temp(self):
        rt = mock.Mock(); rt.kw = {"band": (40, 70)}; rt.status.return_value = {"temp_c": 75.0}
        with mock.patch.dict(os.environ, OFF), mock.patch.object(heatgate, "sensor_temp", return_value=None):
            g = Gate(router=rt, max_wait=2, poll=1, _sleep=lambda s: None)
            self.assertEqual(g.wait(), 2)  # router 75 >= 70 -> held until max_wait

    def test_workers_temp(self):
        with mock.patch.dict(os.environ, OFF), mock.patch.object(heatgate, "sensor_temp", return_value=80.0):
            self.assertEqual(workers(9, 1, temp_high=85, temp_low=75), 5)

class Cli(unittest.TestCase):
    def test_status_no_readings(self):
        with mock.patch.dict(os.environ, OFF), mock.patch.object(heatgate, "sensors", return_value=[]), \
             mock.patch.object(heatgate, "thermal", return_value=None):
            out = io.StringIO()
            with redirect_stdout(out):
                cli.main(["status"])
        self.assertIn("mode=readings", out.getvalue()); self.assertIn("no readings", out.getvalue())
        self.assertNotIn("heat=", out.getvalue())

    def test_run_readings_pauses_on_temp_and_omits_estimates(self):
        seq = iter([95.0] * 3)
        def fake(interval=0, estimates=None):
            t = next(seq, 60.0)
            return {"mode": "readings", "temp_c": t, "cpu_pct": 50.0, "cores": 1, "sensors": [], "no_readings": False}
        import tempfile; j = tempfile.mktemp(suffix=".json")
        with mock.patch.object(cli, "readings", side_effect=fake), redirect_stderr(io.StringIO()) as err:
            rc = cli.run([sys.executable, "-c", "import time; time.sleep(1.2)"], 0.8, 0.5, 0.1, json_out=j)
        self.assertEqual(rc, 0)
        s = json.load(open(j))
        self.assertEqual(s["mode"], "readings"); self.assertGreaterEqual(s["pauses"], 1)
        self.assertNotIn("est_energy_wh", s); self.assertNotIn("heat_avg", s)
        self.assertEqual(s["temp_max_c"], 95.0); self.assertIn("PAUSE temp=95.0C", err.getvalue())

class Mcp(unittest.TestCase):
    def test_tools_readings(self):
        with mock.patch.dict(os.environ, OFF), mock.patch.object(heatgate, "sensors", return_value=HOT):
            r = mcp.call("heat_status", {})
            self.assertEqual(r["mode"], "readings"); self.assertEqual(r["temp_c"], 92.0)
            p = mcp.call("should_pause", {})
            self.assertTrue(p["pause"]); self.assertEqual(p["mode"], "readings")
            self.assertTrue(mcp.call("energy_estimate", {"cpu_seconds": 3600})["disabled"])
            self.assertIn("est_energy_wh", mcp.call("energy_estimate", {"cpu_seconds": 3600, "estimates": True}))

class Report(unittest.TestCase):
    def test_summary_readings(self):
        rows = [{"t": 0, "temp_c": 50.0, "cores": 2, "busy_jiffies": 0, "total_jiffies": 0},
                {"t": 10, "temp_c": 60.0, "cores": 2, "busy_jiffies": 100, "total_jiffies": 200}]
        s = report.add_energy(report.summarize(rows))
        self.assertEqual(s["mode"], "readings"); self.assertEqual(s["temp_peak_c"], 60.0)
        self.assertNotIn("heat_avg", s); self.assertNotIn("est_energy_wh", s)
        self.assertIn("Readings only", report.markdown(s))

if __name__ == "__main__":
    unittest.main()
