import io, json, os, sys, tempfile, unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock
from heatgate import Gate, router as R, watch as W
from heatgate.cli import main

def mkfile(p, s):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w").write(s)

class SysfsTest(unittest.TestCase):
    def test_thermal_and_wifi_hwmon(self):
        d = tempfile.mkdtemp()
        mkfile(f"{d}/t/thermal_zone0/temp", "52000\n"); mkfile(f"{d}/t/thermal_zone0/type", "cpu-thermal")
        mkfile(f"{d}/h/hwmon0/name", "ath10k_hwmon"); mkfile(f"{d}/h/hwmon0/temp1_input", "61000")
        mkfile(f"{d}/h/hwmon1/name", "mt7915_phy0"); mkfile(f"{d}/h/hwmon1/temp1_input", "bad")
        s = R.sysfs_temps(f"{d}/t", f"{d}/h")
        self.assertEqual([(x["name"], x["temp_c"], x["wifi"]) for x in s],
                         [("cpu-thermal", 52.0, False), ("ath10k_hwmon", 61.0, True)])
    def test_missing_dirs(self):
        self.assertEqual(R.sysfs_temps("/nonexistent/a", "/nonexistent/b"), [])

class ParseTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(R.parse_temp("48500\n"), 48.5)
        self.assertEqual(R.parse_temp("temp: 55.5 C"), 55.5)
        self.assertIsNone(R.parse_temp("no number"))
        self.assertIsNone(R.parse_temp("999999"))  # 999.999C out of range
    def test_stress(self):
        self.assertEqual(R.temp_stress(55, (40, 70)), 0.5)
        self.assertEqual(R.temp_stress(30), 0.0); self.assertEqual(R.temp_stress(90), 1.0)
        self.assertIsNone(R.temp_stress(None))

class StatusTest(unittest.TestCase):
    def test_cmd_source(self):
        st = R.router_status(cmd=[sys.executable, "-c", "print(64000)"], local=False)
        self.assertEqual(st["temp_c"], 64.0); self.assertAlmostEqual(st["router_temp_stress"], 0.8)
    def test_fail_open(self):
        st = R.router_status(cmd="/nonexistent/cmd", local=False)
        self.assertIsNone(st["router_temp_stress"]); self.assertIn("cmd", st["errors"])
        self.assertIsNone(R.Router(cmd="/nonexistent/cmd", local=False)())
    def test_local_wifi_filter(self):
        fake = [{"source": "thermal", "name": "x86_pkg", "temp_c": 90.0, "wifi": False},
                {"source": "hwmon", "name": "iwlwifi_1", "temp_c": 45.0, "wifi": True}]
        with mock.patch.object(R, "sysfs_temps", return_value=fake):
            self.assertEqual(R.router_status(local="wifi")["temp_c"], 45.0)
            self.assertEqual(R.router_status(local="all")["temp_c"], 90.0)
    def test_ubus(self):
        calls = []
        def fake(url, sid, obj, method, params, timeout):
            calls.append((sid, obj, method))
            if obj == "session":
                return {"ubus_rpc_session": "abc"}
            return {"data": "57000\n"} if params["path"].endswith("thermal_zone0/temp") else None
        with mock.patch.object(R, "_ubus", side_effect=fake), \
             mock.patch.dict(os.environ, {"HEATGATE_ROUTER_PASS": "pw"}):
            out = R.ubus_temps("http://192.168.1.1")
        self.assertEqual([x["temp_c"] for x in out], [57.0]); self.assertEqual(calls[0][1], "session")
    def test_ubus_login_fail_open(self):
        with mock.patch.object(R, "_ubus", return_value=None):
            st = R.router_status(url="http://r/ubus", local=False)
        self.assertIsNone(st["temp_c"]); self.assertIn("ubus", st["errors"])

class GateTest(unittest.TestCase):
    def test_router_raises_effective(self):
        rt = mock.Mock(return_value=0.9); rt.weight = 1.0
        g = Gate(high=0.8, low=0.5, _heat=lambda: 0.1, router=rt, _sleep=lambda s: None, max_wait=3, poll=1)
        self.assertEqual(g.wait(), 3)  # sustained stress -> proceeds after max_wait
    def test_router_none_contributes_nothing(self):
        rt = mock.Mock(return_value=None); rt.weight = 1.0
        g = Gate(_heat=lambda: 0.1, router=rt, _sleep=lambda s: self.fail("slept"))
        self.assertEqual(g.wait(), 0.0)

class CliTest(unittest.TestCase):
    def test_router_json(self):
        with mock.patch.object(R, "sysfs_temps", return_value=[]):
            buf = io.StringIO()
            with redirect_stdout(buf):
                main(["router", "--json"])
        self.assertIsNone(json.loads(buf.getvalue())["temp_c"])
    def test_run_max_wait_resumes(self):
        with mock.patch("heatgate.cli.readings", return_value={"heat": 0.99, "load1": 9, "cores": 1, "cpu": 1.0}):
            err = io.StringIO()
            with redirect_stderr(err):
                rc = main(["run", "--poll", "0.1", "--max-wait", "0.3", "--", sys.executable, "-c", "import time; time.sleep(1.5)"])
        self.assertEqual(rc, 0); self.assertIn("max-wait", err.getvalue())

class WatchTest(unittest.TestCase):
    def test_watch_once(self):
        d = tempfile.mkdtemp(); cfg = f"{d}/dev.json"
        json.dump({"devices": [
            {"name": "hot", "source": "cmd", "cmd": [sys.executable, "-c", "print(80000)"], "threshold_c": 70},
            {"name": "cool", "source": "cmd", "cmd": [sys.executable, "-c", "print(40)"], "threshold_c": 70},
            {"name": "none", "source": "local-wifi"}]}, open(cfg, "w"))
        with mock.patch.object(R, "sysfs_temps", return_value=[]), redirect_stdout(io.StringIO()), \
             redirect_stderr(io.StringIO()) as err:
            rc = W.watch(cfg, once=True, log_path=f"{d}/log.jsonl", page_dir=f"{d}/page")
        self.assertEqual(rc, 2); self.assertIn("WARNING HOT", err.getvalue())
        rows = [json.loads(l) for l in open(f"{d}/log.jsonl")]
        self.assertEqual([r["status"] for r in rows], ["hot", "ok", "n/a"])
        self.assertIn("hot", open(f"{d}/page/index.html").read())
        self.assertEqual(len(json.load(open(f"{d}/page/status.json"))["devices"]), 3)
    def test_rejects_creds_in_config(self):
        d = tempfile.mkdtemp(); cfg = f"{d}/c.json"
        json.dump({"devices": [{"name": "r", "source": "ubus", "url": "x", "password": "p"}]}, open(cfg, "w"))
        with self.assertRaises(ValueError):
            W.load_config(cfg)

if __name__ == "__main__":
    unittest.main()
