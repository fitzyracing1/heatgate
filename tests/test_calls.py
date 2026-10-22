import io, json, os, subprocess, sys, threading, time
from types import SimpleNamespace as NS
import pytest
from heatgate import calls, mcp
from heatgate.calls import Pacer, extract_usage, summarize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def test_usage_openai_xai():
    u = extract_usage({"usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
    assert (u["input_tokens"], u["output_tokens"], u["total_tokens"], u["usage_source"]) == (10, 5, 15, "openai")

def test_usage_openai_object_and_responses_api():
    assert extract_usage(NS(usage=NS(prompt_tokens=3, completion_tokens=4, total_tokens=7)))["total_tokens"] == 7
    u = extract_usage({"usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}})
    assert u["total_tokens"] == 5 and u["usage_source"] == "openai"

def test_usage_anthropic():
    u = extract_usage(NS(usage=NS(input_tokens=12, output_tokens=8)))
    assert (u["input_tokens"], u["output_tokens"], u["total_tokens"], u["usage_source"]) == (12, 8, None, "anthropic")

def test_usage_gemini_both_casings():
    assert extract_usage({"usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 6, "totalTokenCount": 10}})["total_tokens"] == 10
    u = extract_usage(NS(usage_metadata=NS(prompt_token_count=1, candidates_token_count=2, total_token_count=3)))
    assert (u["input_tokens"], u["usage_source"]) == (1, "gemini")

def test_no_usage_is_null_never_estimated(tmp_path):
    log = tmp_path / "c.jsonl"
    p = Pacer(log=str(log))
    p.call(lambda prompt: {"text": "x" * 500}, "a long prompt " * 50)
    r = json.loads(log.read_text())
    assert r["input_tokens"] is None and r["output_tokens"] is None and r["total_tokens"] is None
    assert r["ok"] and r["duration_s"] >= 0 and r["ttft_s"] is None

def test_anthropic_total_is_sum_of_reported(tmp_path):
    p = Pacer(log=str(tmp_path / "c.jsonl"))
    p.call(lambda: {"usage": {"input_tokens": 3, "output_tokens": 4}})
    assert p.records[0]["total_tokens"] == 7

def test_record_fields_and_error(tmp_path):
    p = Pacer(log=str(tmp_path / "c.jsonl"))
    def boom(model=None):
        raise RuntimeError("503")
    with pytest.raises(RuntimeError):
        p.call(boom, model="grok-4")
    r = p.records[0]
    assert r["ok"] is False and "503" in r["error"] and r["model"] == "grok-4"
    for k in ("start_ts", "end_ts", "duration_s", "held_s", "held", "id"):
        assert k in r

def test_stream_openai_include_usage():
    p = Pacer()
    def gen():
        time.sleep(0.02); yield {"choices": [{"delta": {"content": "hi"}}]}
        time.sleep(0.02); yield {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}
    chunks = list(p.stream(gen, _model="grok-4"))
    r = p.records[0]
    assert len(chunks) == 2 and r["total_tokens"] == 7
    assert 0 < r["ttft_s"] < r["duration_s"]

def test_stream_anthropic_events():
    p = Pacer()
    ev = [{"type": "message_start", "message": {"usage": {"input_tokens": 9, "output_tokens": 1}}},
          {"type": "content_block_delta"}, {"type": "message_delta", "usage": {"output_tokens": 20}}]
    list(p.stream(iter(ev)))
    r = p.records[0]
    assert (r["input_tokens"], r["output_tokens"], r["total_tokens"]) == (9, 20, 29)

def test_stream_gemini_and_early_close():
    p = Pacer()
    g = p.stream(iter([{"usageMetadata": {"promptTokenCount": 1}}, {"x": 1}, {"x": 2}]))
    next(g); g.close()
    assert p.records[0]["ok"] and p.records[0]["input_tokens"] == 1

def test_sequential_default_no_overlap():
    p = Pacer(); active, peak = [0], [0]; lk = threading.Lock()
    def work():
        with lk:
            active[0] += 1; peak[0] = max(peak[0], active[0])
        time.sleep(0.03)
        with lk:
            active[0] -= 1
    ts = [threading.Thread(target=p.call, args=(work,)) for _ in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert peak[0] == 1 and len(p.records) == 4
    assert sum(r["held"]["queue"] for r in p.records) > 0.03

def test_concurrency_configurable():
    p = Pacer(concurrency=2); active, peak = [0], [0]; lk = threading.Lock()
    def work():
        with lk:
            active[0] += 1; peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        with lk:
            active[0] -= 1
    ts = [threading.Thread(target=p.call, args=(work,)) for _ in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert peak[0] == 2

def test_heat_gate_reuses_gate_readings(monkeypatch):
    import heatgate
    temps = iter([90.0, 90.0, 70.0, 70.0])
    monkeypatch.setattr(heatgate, "sensor_temp", lambda: next(temps, 70.0))
    p = Pacer(gate={"poll": 0.01, "temp_high": 85, "temp_low": 75})
    p.call(lambda: None)
    assert p.records[0]["held"]["heat"] > 0

def test_heat_gate_no_sensor_never_holds(monkeypatch):
    import heatgate
    monkeypatch.setattr(heatgate, "sensor_temp", lambda: None)
    p = Pacer(gate=True); p.call(lambda: None)
    assert p.records[0]["held"]["heat"] < 0.05

def test_heat_gate_max_wait(monkeypatch):
    import heatgate
    monkeypatch.setattr(heatgate, "sensor_temp", lambda: 99.0)
    p = Pacer(gate={"poll": 0.01}, max_wait=0.05)
    t = time.monotonic(); p.call(lambda: None)
    assert time.monotonic() - t < 1.0

def test_latency_gate():
    slept = []
    p = Pacer(latency_threshold=0.01, latency_hold=2.0, max_wait=0.5, _sleep=slept.append)
    p.call(lambda: time.sleep(0.03))
    assert slept == []
    p.call(lambda: None)
    assert slept == [0.5]  # capped by max_wait

def test_report_cli(tmp_path, capsys):
    log = tmp_path / "c.jsonl"
    p = Pacer(log=str(log))
    p.call(lambda: {"usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}})
    p.call(lambda: {})
    from heatgate.cli import main
    assert main(["calls", "report", str(log), "--json"]) == 0
    s = json.loads(capsys.readouterr().out)
    assert s["count"] == 2 and s["total_tokens"] == 3 and s["calls_without_usage"] == 1
    assert s["p95_duration_s"] is not None and "queue" in s["held_by_reason_s"]
    main(["calls", "report", str(log)])
    assert "calls=2" in capsys.readouterr().out

def test_summarize_p95():
    recs = [{"duration_s": float(i), "held": {"heat": 1.0}} for i in range(1, 21)]
    s = summarize(recs)
    assert s["p95_duration_s"] == 19.0 and s["held_by_reason_s"]["heat"] == 20.0

def test_call_cli_queues_with_lock(tmp_path):
    lock, log = str(tmp_path / "l"), str(tmp_path / "c.jsonl")
    env = dict(os.environ, PYTHONPATH=ROOT)
    cmd = lambda: subprocess.Popen([sys.executable, "-m", "heatgate", "call", "--lock", lock, "--log", log, "--",
                                    sys.executable, "-c", "import time; time.sleep(0.4)"], env=env)
    a, b = cmd(), cmd()
    assert a.wait() == 0 and b.wait() == 0
    recs = calls.load(log)
    assert len(recs) == 2
    first, second = sorted(recs, key=lambda r: r["start_ts"])
    assert second["start_ts"] >= first["end_ts"] - 0.01
    assert max(r["held"]["queue"] for r in recs) > 0.2

def test_call_cli_nonzero_rc(tmp_path):
    log = str(tmp_path / "c.jsonl")
    rc = subprocess.call([sys.executable, "-m", "heatgate", "call", "--lock", str(tmp_path / "l"), "--log", log, "--",
                          sys.executable, "-c", "raise SystemExit(3)"], env=dict(os.environ, PYTHONPATH=ROOT))
    r = calls.load(log)[0]
    assert rc == 3 and r["returncode"] == 3 and r["ok"] is False

def test_mcp_tools(tmp_path, monkeypatch):
    import heatgate
    monkeypatch.setattr(heatgate, "sensor_temp", lambda: None)
    log = tmp_path / "c.jsonl"
    p = Pacer(log=str(log)); p.call(lambda: time.sleep(0.02))
    names = [t["name"] for t in mcp.TOOLS]
    assert "call_log" in names and "pace_status" in names
    d = mcp.call("call_log", {"path": str(log), "last": 1})
    assert d["summary"]["count"] == 1 and len(d["last"]) == 1
    s = mcp.call("pace_status", {"path": str(log), "lock": str(tmp_path / "nolock"), "latency_threshold": 0.001})
    assert s["would_hold"] and s["reasons"] == ["latency"] and s["temp_c"] is None
