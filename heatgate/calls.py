"""AI call pacing + logging. Measured values only: wall-clock timings and the token counts the provider returns.
If a provider returns no usage, token fields are null; nothing is estimated."""
import json, math, os, threading, time, uuid

__all__ = ["Pacer", "extract_usage", "summarize", "report", "load", "DEFAULT_LOG"]
DEFAULT_LOG = os.environ.get("HEATGATE_CALL_LOG", "heatgate-calls.jsonl")

def _get(o, *keys):
    for k in keys:
        if o is None:
            return None
        o = o.get(k) if isinstance(o, dict) else getattr(o, k, None)
    return o

def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None

def extract_usage(resp):
    """Token counts as reported by the provider -> {input_tokens, output_tokens, total_tokens, usage_source}.
    Supports OpenAI-compatible `usage` (incl. xAI; prompt/completion or input/output tokens), Anthropic
    `usage.input_tokens/output_tokens`, Gemini `usageMetadata` / `usage_metadata`. Missing -> None."""
    out = {"input_tokens": None, "output_tokens": None, "total_tokens": None, "usage_source": None}
    if resp is None:
        return out
    gm = _get(resp, "usageMetadata") or _get(resp, "usage_metadata")
    if gm is not None:
        f = lambda a, b: _int(_get(gm, a)) if _get(gm, a) is not None else _int(_get(gm, b))
        out.update(input_tokens=f("promptTokenCount", "prompt_token_count"),
                   output_tokens=f("candidatesTokenCount", "candidates_token_count"),
                   total_tokens=f("totalTokenCount", "total_token_count"),
                   usage_source="gemini")
        return out
    u = _get(resp, "usage")
    if u is None:
        u = _get(resp, "message", "usage")  # Anthropic stream `message_start` event
    if u is None:
        return out
    if _get(u, "prompt_tokens") is not None or _get(u, "completion_tokens") is not None:
        out.update(input_tokens=_int(_get(u, "prompt_tokens")), output_tokens=_int(_get(u, "completion_tokens")),
                   total_tokens=_int(_get(u, "total_tokens")), usage_source="openai")
    elif _get(u, "input_tokens") is not None or _get(u, "output_tokens") is not None:
        tot = _int(_get(u, "total_tokens"))  # OpenAI Responses API has total; Anthropic does not
        out.update(input_tokens=_int(_get(u, "input_tokens")), output_tokens=_int(_get(u, "output_tokens")),
                   total_tokens=tot, usage_source="openai" if tot is not None else "anthropic")
    return out

def _merge_usage(acc, new):
    """Streaming: later chunks override earlier ones field by field (only with reported values)."""
    for k in ("input_tokens", "output_tokens", "total_tokens", "usage_source"):
        if new.get(k) is not None:
            acc[k] = new[k]
    return acc

def _finish_total(u):
    # Sum of two provider-reported counts (Anthropic gives no total). Not an estimate; null if either is missing.
    if u["total_tokens"] is None and u["input_tokens"] is not None and u["output_tokens"] is not None:
        u["total_tokens"] = u["input_tokens"] + u["output_tokens"]
    return u

class _Call:
    def __init__(self, pacer, name, model, provider, meta):
        self.p, self.rec = pacer, {"id": uuid.uuid4().hex[:12], "name": name, "model": model, "provider": provider}
        if meta:
            self.rec["meta"] = meta
        self.usage = {"input_tokens": None, "output_tokens": None, "total_tokens": None, "usage_source": None}
        self._t0 = self._ttft = None
    def first_token(self):
        """Mark first byte/token (streaming). Only the first call counts."""
        if self._ttft is None and self._t0 is not None:
            self._ttft = time.monotonic(); self.rec["first_token_ts"] = time.time()
    def set_response(self, resp):
        _merge_usage(self.usage, extract_usage(resp)); return resp
    def set_usage(self, input_tokens=None, output_tokens=None, total_tokens=None, source="manual"):
        _merge_usage(self.usage, {"input_tokens": input_tokens, "output_tokens": output_tokens,
                                  "total_tokens": total_tokens, "usage_source": source})
    def __enter__(self):
        self.held = self.p._acquire()
        self.rec["start_ts"] = time.time(); self._t0 = time.monotonic(); return self
    def __exit__(self, et, ev, tb):
        t1 = time.monotonic()
        dur = t1 - self._t0
        self.rec.update(end_ts=time.time(), duration_s=round(dur, 6),
                        ttft_s=None if self._ttft is None else round(self._ttft - self._t0, 6),
                        ok=et is None and not self.rec.get("returncode"), error=None if et is None else f"{et.__name__}: {ev}",
                        held_s=round(sum(self.held.values()), 6), held={k: round(v, 6) for k, v in self.held.items()})
        self.rec.update(_finish_total(dict(self.usage)))
        self.p._release(dur, self.rec)
        return False

class Pacer:
    """Sequential (concurrency=1 default) pacing + JSONL logging of AI API calls.

    gate: None (off), True (readings-only heatgate.Gate with defaults), a dict of Gate kwargs, or a callable
          returning seconds held. Heat gate reuses Gate (temperature / grid / router) and honours max_wait.
    latency_threshold: seconds; if the mean of the last `latency_window` measured durations exceeds it, hold
          `latency_hold` seconds before the next call (capped by max_wait). Off by default."""
    def __init__(self, log=None, concurrency=1, gate=None, max_wait=None, latency_threshold=None,
                 latency_window=5, latency_hold=5.0, _sleep=time.sleep):
        self.log = log
        self.concurrency = max(1, int(concurrency))
        self._sem = threading.BoundedSemaphore(self.concurrency)
        self._lock = threading.Lock()
        self.gate, self.max_wait = gate, max_wait
        self.latency_threshold, self.latency_window, self.latency_hold = latency_threshold, latency_window, latency_hold
        self._sleep, self.recent, self.records = _sleep, [], []
        self.totals = {"queue": 0.0, "heat": 0.0, "latency": 0.0}
    def _heat_wait(self):
        g = self.gate
        if not g:
            return 0.0
        if callable(g) and not isinstance(g, dict):
            return float(g() or 0.0)
        from . import Gate
        kw = {} if g is True else dict(g)
        kw.setdefault("max_wait", self.max_wait)
        t = time.monotonic(); Gate(**kw).wait()
        return time.monotonic() - t
    def recent_latency(self):
        with self._lock:
            r = self.recent[-self.latency_window:]
        return sum(r) / len(r) if r else None
    def _latency_wait(self):
        if self.latency_threshold is None:
            return 0.0
        lat = self.recent_latency()
        if lat is None or lat <= self.latency_threshold:
            return 0.0
        s = self.latency_hold if self.max_wait is None else min(self.latency_hold, self.max_wait)
        t = time.monotonic(); self._sleep(s)
        return time.monotonic() - t
    def _acquire(self):
        t = time.monotonic(); self._sem.acquire()
        held = {"queue": time.monotonic() - t}
        try:
            held["heat"] = self._heat_wait()
            held["latency"] = self._latency_wait()
        except BaseException:
            self._sem.release(); raise
        return held
    def _release(self, dur, rec):
        try:
            with self._lock:
                self.recent.append(dur); self.recent = self.recent[-max(self.latency_window, 1) * 4:]
                for k, v in rec["held"].items():
                    self.totals[k] = self.totals.get(k, 0.0) + v
                self.records.append(rec)
                if self.log:
                    with open(self.log, "a") as f:
                        f.write(json.dumps(rec) + "\n")
        finally:
            self._sem.release()
    def track(self, name=None, model=None, provider=None, **meta):
        """Context manager: `with pacer.track(model="grok-4") as c: c.set_response(client.create(...))`."""
        return _Call(self, name, model, provider, meta)
    def call(self, fn, *a, _name=None, _model=None, _provider=None, **kw):
        """Run fn(*a, **kw) paced; usage is read from its return value."""
        with self.track(_name or getattr(fn, "__name__", None), _model or kw.get("model"), _provider) as c:
            return c.set_response(fn(*a, **kw))
    def stream(self, iterable_or_fn, *a, _name=None, _model=None, _provider=None, **kw):
        """Wrap a streaming response: yields chunks unchanged, marks first-token time at the first chunk and
        takes usage from any chunk that carries it (OpenAI include_usage, Anthropic message_start/message_delta,
        Gemini usageMetadata). The call slot is held until the stream is exhausted or closed."""
        c = self.track(_name, _model or kw.get("model"), _provider)
        c.__enter__()
        exc = (None, None, None)
        try:
            it = iterable_or_fn(*a, **kw) if callable(iterable_or_fn) else iterable_or_fn
            for chunk in it:
                c.first_token(); c.set_response(chunk)
                yield chunk
        except BaseException as e:
            exc = (type(e), e, e.__traceback__)
            if isinstance(e, GeneratorExit):
                exc = (None, None, None)
            raise
        finally:
            c.__exit__(*exc)
    def status(self):
        return {"concurrency": self.concurrency, "calls": len(self.records), "recent_latency_s": self.recent_latency(),
                "latency_threshold_s": self.latency_threshold, "held_s": dict(self.totals), "log": self.log}

def load(path):
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out

def _pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[max(0, math.ceil(p / 100 * len(xs)) - 1)]  # nearest-rank

def summarize(recs):
    durs = [r["duration_s"] for r in recs if isinstance(r.get("duration_s"), (int, float))]
    ttft = [r["ttft_s"] for r in recs if isinstance(r.get("ttft_s"), (int, float))]
    tok = lambda k: sum(r[k] for r in recs if isinstance(r.get(k), int))
    held = {}
    for r in recs:
        for k, v in (r.get("held") or {}).items():
            held[k] = round(held.get(k, 0.0) + (v or 0.0), 6)
    return {"count": len(recs), "ok": sum(1 for r in recs if r.get("ok")), "errors": sum(1 for r in recs if r.get("ok") is False),
            "input_tokens": tok("input_tokens"), "output_tokens": tok("output_tokens"), "total_tokens": tok("total_tokens"),
            "calls_without_usage": sum(1 for r in recs if r.get("total_tokens") is None and r.get("input_tokens") is None
                                       and r.get("output_tokens") is None),
            "avg_duration_s": round(sum(durs) / len(durs), 6) if durs else None,
            "p95_duration_s": _pct(durs, 95), "avg_ttft_s": round(sum(ttft) / len(ttft), 6) if ttft else None,
            "held_total_s": round(sum(held.values()), 6), "held_by_reason_s": held}

def report(path, as_json=False, out=None):
    import sys
    out = out or sys.stdout
    s = summarize(load(path))
    if as_json:
        print(json.dumps(s), file=out); return 0
    f = lambda v: "n/a" if v is None else f"{v:.3f}s"
    print(f"calls={s['count']} ok={s['ok']} errors={s['errors']}", file=out)
    print(f"tokens: input={s['input_tokens']} output={s['output_tokens']} total={s['total_tokens']} "
          f"(provider-reported; {s['calls_without_usage']} call(s) reported no usage)", file=out)
    print(f"duration: avg={f(s['avg_duration_s'])} p95={f(s['p95_duration_s'])} avg_first_token={f(s['avg_ttft_s'])}", file=out)
    why = ", ".join(f"{k}={v:.3f}s" for k, v in s["held_by_reason_s"].items() if v) or "none"
    print(f"held: total={s['held_total_s']:.3f}s ({why})", file=out)
    return 0

def run_locked(cmd, lock=None, log=None, name=None, gate=None, max_wait=None):
    """Run one shell command at a time across processes (flock on `lock`), logging start/finish to JSONL."""
    import fcntl, subprocess, sys
    lock = lock or os.environ.get("HEATGATE_CALL_LOCK") or os.path.join(
        os.environ.get("TMPDIR", "/tmp"), f"heatgate-call-{os.getuid()}.lock")
    p = Pacer(log=log or DEFAULT_LOG, gate=gate, max_wait=max_wait)
    rc = [None]
    with open(lock, "a") as lf:
        t = time.monotonic()
        print(f"[heatgate] waiting for call lock {lock}", file=sys.stderr) if _locked(lf) else None
        fcntl.flock(lf, fcntl.LOCK_EX)
        q = time.monotonic() - t
        try:
            c = p.track(name or " ".join(cmd)[:200], provider="cmd")
            c.__enter__(); c.held["queue"] += q
            try:
                rc[0] = subprocess.call(cmd)
                c.rec["returncode"] = rc[0]
            except BaseException as e:
                c.__exit__(type(e), e, None); raise
            c.__exit__(None, None, None)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
    return rc[0]

def _locked(lf):
    import fcntl
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB); fcntl.flock(lf, fcntl.LOCK_UN); return False
    except OSError:
        return True
