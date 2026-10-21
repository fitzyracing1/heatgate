import subprocess, sys, unittest
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
        cli.readings = lambda interval=0: {"heat": next(seq), "load1": 0, "cpu": 0}
        try:
            self.assertEqual(cli.run([sys.executable, "-c", "import time; time.sleep(1)"], 0.8, 0.5, 0.1), 0)
        finally:
            cli.readings = orig

if __name__ == "__main__":
    unittest.main()
