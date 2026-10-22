import io, json, unittest
from contextlib import redirect_stdout
from unittest import mock
import heatgate
from heatgate import grid, cli, mcp

ZIP = {"places": [{"place name": "Town", "latitude": "41.6", "longitude": "-70.9", "state abbreviation": "MA"}]}
METEO = {"current": {"time": "2026-01-01T00:00", "temperature_2m": -5.0}}
ISO = [{"data": {"actual": [{"Mw": 9000, "BeginDate": "a"}, {"Mw": 12000, "BeginDate": "b"}],
                 "forecast": [{"Mw": 15000}]}}]

def fake(url, data=None, headers=None, timeout=10, ttl=None):
    if "zippopotam" in url: return ZIP
    if "open-meteo" in url: return METEO
    if "iso-ne" in url: return ISO
    raise OSError("no net")

class T(unittest.TestCase):
    def setUp(self): grid._cache.clear()
    def test_stress_math(self):
        self.assertEqual(grid.temp_stress(15), 0.0)
        self.assertAlmostEqual(grid.temp_stress(-5), 1.0)
        self.assertAlmostEqual(grid.temp_stress(31.5), 0.5)
        self.assertAlmostEqual(grid.demand_stress(0.85), 0.5)
        self.assertEqual(grid.demand_stress(0.5), 0.0)
        self.assertEqual(grid.effective_heat(0.3, None), 0.3)
        self.assertAlmostEqual(grid.effective_heat(0.3, 0.9, 0.5), 0.45)
    @mock.patch.object(grid, "_fetch", side_effect=fake)
    def test_status_zip(self, _):
        d = grid.grid_status("02740")
        self.assertEqual(d["state"], "MA"); self.assertEqual(d["temp_c"], -5.0)
        self.assertAlmostEqual(d["demand"]["ratio"], 0.8)
        self.assertAlmostEqual(d["grid_stress"], 1.0)
    @mock.patch.object(grid, "_fetch", side_effect=OSError("down"))
    def test_fail_open(self, _):
        d = grid.grid_status("02740")
        self.assertIsNone(d["grid_stress"]); self.assertTrue(d["errors"])
        g = heatgate.Gate(_heat=lambda: 0.1, grid=grid.Grid(zip_code="02740"), _sleep=lambda s: None)
        self.assertEqual(g.wait(), 0.0)
    @mock.patch.object(grid, "_fetch", side_effect=fake)
    def test_outside_ne_no_demand(self, f):
        d = grid.grid_status(lat=30.0, lon=-97.0)
        self.assertIsNone(d["demand"]); self.assertAlmostEqual(d["grid_stress"], 1.0)
        self.assertFalse(any("iso-ne" in c.args[0] for c in f.call_args_list))
    def test_cache(self):
        calls = []
        class R(io.BytesIO):
            def __enter__(s): return s
            def __exit__(s, *a): pass
        def op(req, timeout=0):
            calls.append(1); return R(b'{"x":1}')
        with mock.patch("urllib.request.urlopen", op):
            grid._fetch("http://e/1"); grid._fetch("http://e/1")
        self.assertEqual(len(calls), 1)
    def test_gate_grid_blocks(self):
        seq = iter([0.9, 0.9, 0.1])
        g = heatgate.Gate(_heat=lambda: 0.1, grid=mock.Mock(weight=1.0, side_effect=lambda: next(seq)),
                          poll=1, _sleep=lambda s: None)
        self.assertEqual(g.wait(), 1)
    @mock.patch.object(grid, "_fetch", side_effect=fake)
    def test_cli_and_mcp(self, _):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(["grid", "--zip", "02740", "--json"]), 0)
        self.assertAlmostEqual(json.loads(buf.getvalue())["grid_stress"], 1.0)
        self.assertIn("grid_status", [t["name"] for t in mcp.TOOLS])
        self.assertEqual(mcp.call("grid_status", {"zip": "02740"})["temp_c"], -5.0)
    @mock.patch.object(grid, "_fetch", side_effect=fake)
    def test_eia_optional(self, _):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(grid.eia_demand())

if __name__ == "__main__": unittest.main()

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
