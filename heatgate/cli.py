import argparse, json, os, signal, subprocess, sys, time
from . import readings

def log(msg):
    print(time.strftime("%Y-%m-%dT%H:%M:%S%z"), "[heatgate]", msg, file=sys.stderr, flush=True)

def run(cmd, high, low, poll, own_pid=True):
    # own process group so SIGSTOP hits the whole tree
    p = subprocess.Popen(cmd, start_new_session=True)
    paused, pauses, resumes, samples = False, 0, 0, []
    log(f"started pid={p.pid} high={high} low={low}: {' '.join(cmd)}")
    def fwd(sig, _):
        try:
            os.killpg(p.pid, signal.SIGCONT); os.killpg(p.pid, sig)
        except ProcessLookupError:
            pass
    signal.signal(signal.SIGINT, fwd); signal.signal(signal.SIGTERM, fwd)
    while p.poll() is None:
        r = readings(interval=min(poll, 0.5))
        h = r["heat"]
        # while paused, the child adds no load; judge on load1 only for low side
        samples.append(h)
        try:
            if not paused and h >= high:
                os.killpg(p.pid, signal.SIGSTOP); paused = True; pauses += 1
                log(f"PAUSE heat={h:.2f} load1={r['load1']:.2f} cpu={r['cpu'] or 0:.2f}")
            elif paused and h < low:
                os.killpg(p.pid, signal.SIGCONT); paused = False; resumes += 1
                log(f"RESUME heat={h:.2f} load1={r['load1']:.2f} cpu={r['cpu'] or 0:.2f}")
        except ProcessLookupError:
            break
        time.sleep(max(0.05, poll - 0.5))
    rc = p.wait()
    if samples:
        log(f"exit rc={rc} pauses={pauses} resumes={resumes} heat min/avg/max="
            f"{min(samples):.2f}/{sum(samples)/len(samples):.2f}/{max(samples):.2f}")
    return rc

def main(argv=None):
    ap = argparse.ArgumentParser(prog="heatgate", description="Load-based throttle (load as heat proxy).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status"); s.add_argument("--json", action="store_true")
    r = sub.add_parser("run"); r.add_argument("--high", type=float, default=0.8)
    r.add_argument("--low", type=float, default=0.5); r.add_argument("--poll", type=float, default=1.0)
    r.add_argument("command", nargs=argparse.REMAINDER)
    sub.add_parser("mcp", help="run a stdio MCP server")
    sp = sub.add_parser("sample", help="append heat samples (JSONL) to a file until SIGTERM")
    sp.add_argument("--out", required=True); sp.add_argument("--interval", type=float, default=5.0)
    sp.add_argument("--count", type=int, default=None)
    rp = sub.add_parser("report", help="summarize a sample file (markdown; optional GitHub outputs/JSON)")
    rp.add_argument("file"); rp.add_argument("--summary", help="append markdown here (e.g. $GITHUB_STEP_SUMMARY)")
    rp.add_argument("--github-output", help="write key=value outputs here (e.g. $GITHUB_OUTPUT)")
    rp.add_argument("--json-out"); rp.add_argument("--title", default="Heat report")
    a = ap.parse_args(argv)
    if a.cmd == "mcp":
        from .mcp import serve
        return serve()
    if a.cmd == "sample":
        from .report import sample
        return sample(a.out, a.interval, a.count)
    if a.cmd == "report":
        from .report import report
        return report(a.file, a.summary, a.github_output, a.json_out, a.title)
    if a.cmd == "status":
        d = readings()
        if a.json:
            print(json.dumps(d))
        else:
            t = "n/a (hidden)" if d["temp_c"] is None else f"{d['temp_c']:.1f}C"
            print(f"heat={d['heat']:.2f} load1={d['load1']:.2f} cores={d['cores']} "
                  f"load/core={d['load_norm']:.2f} cpu={(d['cpu'] or 0)*100:.0f}% temp={t}")
        return 0
    cmd = a.command[1:] if a.command[:1] == ["--"] else a.command
    if not cmd:
        ap.error("run needs a command after --")
    return run(cmd, a.high, a.low, a.poll)

if __name__ == "__main__":
    sys.exit(main())
