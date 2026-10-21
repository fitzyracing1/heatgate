import json, os, tempfile, unittest
from heatgate import cli, report

class R(unittest.TestCase):
    def test_summarize(self):
        rows = [{"t": 0, "heat": 0.2, "cores": 4, "busy_jiffies": 0, "total_jiffies": 0},
                {"t": 60, "heat": 0.6, "cores": 4, "busy_jiffies": 12000, "total_jiffies": 24000}]
        s = report.summarize(rows, clk_tck=100)
        self.assertEqual(s["duration_s"], 60); self.assertEqual(s["heat_peak"], 0.6)
        self.assertAlmostEqual(s["heat_avg"], 0.4); self.assertEqual(s["cpu_minutes"], 2.0)
        self.assertEqual(s["cpu_util"], 0.5); self.assertEqual(s["cores"], 4)
    def test_empty(self):
        self.assertIsNone(report.summarize([]))
        self.assertIn("No heat samples", report.markdown(None))
    def test_cli_sample_report(self):
        d = tempfile.mkdtemp(); f = os.path.join(d, "h.jsonl")
        self.assertEqual(cli.main(["sample", "--out", f, "--interval", "0.3", "--count", "2"]), 0)
        self.assertGreaterEqual(len(report.load(f)), 3)
        summ, out, js = (os.path.join(d, n) for n in ("s.md", "o.txt", "r.json"))
        self.assertEqual(cli.main(["report", f, "--summary", summ, "--github-output", out, "--json-out", js]), 0)
        self.assertIn("peak heat", open(summ).read())
        self.assertIn("heat-peak=", open(out).read())
        self.assertIn("cpu_minutes", json.load(open(js)))

if __name__ == "__main__":
    unittest.main()
