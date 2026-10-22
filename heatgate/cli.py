import argparse, json, os, signal, subprocess, sys, time
from . import readings
from .app import app_readings, cpu_seconds, energy_wh, watts_per_core

def log(msg):
    print(time.strftime("%Y-%m-%dT%H:%M:%S%z"), "[heatgate]", msg, file=sys.stderr, flush=True)

def _children_cpu():
    try:
        import resource
        u = resource.getrusage(resource.RUSAGE_CHILDREN)
        return u.ru_utime + u.ru_stime
    except Exception:
        return None

def run(cmd, high, low, poll, own_pid=True, gate="machine", energy=False, wpc=None, json_out=None, grid=None, router=None, max_wait=None,
        estimates=False, temp_high=85.0, temp_low=75.0):
    """Run cmd; SIGSTOP its group when hot, SIGCONT when cool.

    gate='machine' judges whole-machine heat; gate='app' judges only this command's process tree.
    Always reports the app's CPU-seconds and ESTIMATED energy (CPU-s x watts-per-core)."""
    wpc = watts_per_core(wpc)
    base = _children_cpu()
    t0 = time.monotonic()
    # own process group so SIGSTOP hits the whole tree
    p = subprocess.Popen(cmd, start_new_session=True)
    suppress_until, paused_at = 0.0, None
    paused, pauses, resumes, samples, app_samples, temps, cpus = False, 0, 0, [], [], [], []
    paused_s, saved_cpu_s, rate, last_cpu, last_t, app_cpu = 0.0, 0.0, 0.0, 0.0, time.monotonic(), 0.0
    log(f"started pid={p.pid} mode={'estimates' if estimates else 'readings'} "
        + (f"high={high} low={low} gate={gate}" if estimates else f"temp_high={temp_high:g}C temp_low={temp_low:g}C") + f": {' '.join(cmd)}")
    def fwd(sig, _):
        try:
            os.killpg(p.pid, signal.SIGCONT); os.killpg(p.pid, sig)
        except ProcessLookupError:
            pass
    signal.signal(signal.SIGINT, fwd); signal.signal(signal.SIGTERM, fwd)
    while p.poll() is None:
        r = readings(interval=min(poll, 0.5), estimates=estimates)
        now = time.monotonic(); dt = max(now - last_t, 1e-6)
        c = cpu_seconds(p.pid)
        if c is not None:
            app_cpu = max(app_cpu, c)
            ah = max(0.0, c - last_cpu) / dt / (r.get("cores") or os.cpu_count() or 1)
            if not paused:
                rate = max(0.0, c - last_cpu) / dt  # app CPU-seconds per second while running
            last_cpu = c
        else:
            ah = None
        if paused:
            paused_s += dt; saved_cpu_s += rate * dt
        last_t = now
        if ah is not None:
            app_samples.append(ah)
        hi, lo = high, low
        if estimates:
            h = r["heat"]
            g = ah if (gate == "app" and ah is not None) else h
            gs = grid() if grid is not None else None  # cached; None on any failure (fail open)
            rs = router() if router is not None else None  # None when no temperature (fail open)
            ext = max([0.0] + ([grid.weight * gs] if gs is not None else []) + ([router.weight * rs] if rs is not None else []))
            g = max(g, ext)
            cool = max(h if gate == "app" else g, ext)
            desc = (f"router_stress={rs if rs is None else round(rs, 2)} grid_stress={gs if gs is None else round(gs, 2)} "
                    f"{gate}_heat={g:.2f} heat={h:.2f} load1={r['load1']:.2f} cpu={r['cpu'] or 0:.2f}")
            samples.append(h)
        else:  # readings-only: real temperatures vs --temp-high/--temp-low; no reading -> never pause
            hi, lo = 0.0, -(temp_high - temp_low)
            m, parts = [], []
            if r["temp_c"] is not None:
                m.append(r["temp_c"] - temp_high); parts.append(f"temp={r['temp_c']:.1f}C")
                temps.append(r["temp_c"])
            if router is not None:
                try:
                    st = router.status()
                    if st["temp_c"] is not None:
                        m.append(st["temp_c"] - router.kw["band"][1]); parts.append(f"router_temp={st['temp_c']:.1f}C")
                except Exception:
                    pass
            if r["cpu_pct"] is not None:
                cpus.append(r["cpu_pct"])
            g = max(m) if m else float("-inf")
            cool = g
            desc = (" ".join(parts) or "no readings") + f" cpu={r['cpu_pct'] if r['cpu_pct'] is not None else 'n/a'}%"
        if suppress_until and now < suppress_until:
            g = min(g, hi - 1e-9)  # grace period after a max-wait forced resume
        try:
            if not paused and g >= hi:
                os.killpg(p.pid, signal.SIGSTOP); paused = True; pauses += 1; paused_at = now
                log(f"PAUSE {desc}")
            elif paused and max_wait is not None and now - paused_at >= max_wait:
                os.killpg(p.pid, signal.SIGCONT); paused = False; resumes += 1
                suppress_until = now + max_wait
                log(f"WARNING still hot after --max-wait {max_wait:g}s ({desc}); resuming, "
                    f"no re-pause for {max_wait:g}s")
            elif paused and cool < lo:
                # while paused the app is ~0, so resume on machine readings cooling below low
                os.killpg(p.pid, signal.SIGCONT); paused = False; resumes += 1
                log(f"RESUME {desc}")
        except ProcessLookupError:
            break
        time.sleep(max(0.05, poll - 0.5))
    rc = p.wait()
    wall = time.monotonic() - t0
    after = _children_cpu()
    if base is not None and after is not None:  # exact: rusage of the reaped tree
        app_cpu = max(app_cpu, after - base)
    s = {"mode": "estimates" if estimates else "readings", "rc": rc, "wall_s": round(wall, 2),
         "pauses": pauses, "resumes": resumes, "paused_s": round(paused_s, 2),
         "app_cpu_seconds": round(app_cpu, 3)}
    if not estimates:
        rnd = lambda v: None if v is None else round(v, 1)
        s.update({"temp_min_c": rnd(min(temps)) if temps else None, "temp_max_c": rnd(max(temps)) if temps else None,
                  "temp_avg_c": rnd(sum(temps) / len(temps)) if temps else None,
                  "cpu_pct_avg": rnd(sum(cpus) / len(cpus)) if cpus else None, "cpu_pct_max": rnd(max(cpus)) if cpus else None,
                  "no_readings": not temps})
        log(f"exit rc={rc} pauses={pauses} resumes={resumes} app_cpu={app_cpu:.2f}s wall={wall:.2f}s "
            + (f"temp min/avg/max={s['temp_min_c']}/{s['temp_avg_c']}/{s['temp_max_c']}C" if temps
               else "temp=no readings (not throttled)")
            + (f" cpu avg/max={s['cpu_pct_avg']}/{s['cpu_pct_max']}%" if cpus else ""))
        if energy:
            log("energy Wh is an estimate; enable with --estimates or HEATGATE_ESTIMATES=1")
    else:
        s.update({
         "app_heat_avg": round(sum(app_samples) / len(app_samples), 3) if app_samples else None,
         "app_heat_peak": round(max(app_samples), 3) if app_samples else None,
         "heat_min": round(min(samples), 3) if samples else None,
         "heat_avg": round(sum(samples) / len(samples), 3) if samples else None,
         "heat_max": round(max(samples), 3) if samples else None,
         "watts_per_core": wpc, "est_energy_wh": round(energy_wh(app_cpu, wpc), 6),
         "est_saved_wh": round(energy_wh(saved_cpu_s, wpc), 6), "estimate": True})
        if samples:
            log(f"exit rc={rc} pauses={pauses} resumes={resumes} heat min/avg/max="
                f"{s['heat_min']:.2f}/{s['heat_avg']:.2f}/{s['heat_max']:.2f}")
        log(f"app cpu={app_cpu:.2f}s wall={wall:.2f}s app_heat avg/peak="
            f"{s['app_heat_avg'] if s['app_heat_avg'] is not None else 'n/a'}/{s['app_heat_peak'] if s['app_heat_peak'] is not None else 'n/a'} "
            f"| ESTIMATE energy={s['est_energy_wh']:.4f} Wh saved_by_pauses={s['est_saved_wh']:.4f} Wh (paused {paused_s:.1f}s) @ {wpc:g} W/core")
        if energy:
            print(f"[heatgate] ESTIMATED energy for this call: {s['est_energy_wh']:.4f} Wh "
                  f"({app_cpu:.2f} CPU-s x {wpc:g} W/core / 3600; est. saved by pauses {s['est_saved_wh']:.4f} Wh)",
                  file=sys.stderr, flush=True)
    if json_out:
        with open(json_out, "w") as f: json.dump(s, f, indent=2)
    return rc

def main(argv=None):
    ap = argparse.ArgumentParser(prog="heatgate", description="Throttle on real temperature readings "
                                 "(estimates such as load-as-heat are opt-in with --estimates).")
    def est_arg(x):
        x.add_argument("--estimates", action="store_true", default=None,
                       help="opt in to ESTIMATES: load-as-heat proxy, energy Wh, grid_stress score (env HEATGATE_ESTIMATES=1)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status"); s.add_argument("--json", action="store_true")
    s.add_argument("--pid", type=int, help="report heat of this process tree instead of the whole machine")
    s.add_argument("--watts-per-core", type=float, default=None); est_arg(s)
    r = sub.add_parser("run"); est_arg(r)
    r.add_argument("--temp-high", type=float, default=85.0, help="readings mode: pause at this sensor temp C (default 85)")
    r.add_argument("--temp-low", type=float, default=75.0, help="readings mode: resume below this C (default 75)")
    r.add_argument("--high", type=float, default=0.8)
    r.add_argument("--low", type=float, default=0.5); r.add_argument("--poll", type=float, default=1.0)
    r.add_argument("--gate", choices=["machine", "app"], default="machine",
                   help="pause on whole-machine heat (default) or on this command's own heat")
    r.add_argument("--energy", action="store_true", help="print an ESTIMATED Wh line for this call")
    r.add_argument("--watts-per-core", type=float, default=None,
                   help="ESTIMATE factor (default env HEATGATE_WATTS_PER_CORE or 10)")
    r.add_argument("--json-out", help="write the run summary as JSON here")
    def grid_args(x):
        x.add_argument("--zip"); x.add_argument("--lat", type=float); x.add_argument("--lon", type=float)
        x.add_argument("--comfort", default="10,24", help="comfort band in C, lo,hi (default 10,24)")
        x.add_argument("--grid-source", choices=["auto", "isone", "eia", "none"], default="auto")
        x.add_argument("--ba", help="EIA balancing authority (with EIA_API_KEY), default ISNE")
    r.add_argument("--grid", action="store_true", help="also pause on outside temp / grid stress (needs --zip or --lat/--lon)")
    r.add_argument("--grid-weight", type=float, default=1.0)
    r.add_argument("--max-wait", type=float, default=None,
                   help="max seconds to stay paused; then resume with a warning (default: wait forever)")
    def router_args(x):
        x.add_argument("--router-cmd", help="command printing a temperature, e.g. 'ssh root@192.168.1.1 cat "
                       "/sys/class/thermal/thermal_zone0/temp' (env HEATGATE_ROUTER_CMD)")
        x.add_argument("--router-url", help="OpenWrt ubus URL, e.g. http://192.168.1.1/ubus "
                       "(creds env HEATGATE_ROUTER_USER/HEATGATE_ROUTER_PASS; env HEATGATE_ROUTER_URL)")
        x.add_argument("--on-router", action="store_true", help="use ALL local sysfs sensors (heatgate runs on the router)")
        x.add_argument("--no-local-wifi", action="store_true", help="skip local Wi-Fi chip hwmon sensors")
        x.add_argument("--router-band", default="40,70", help="temp band in C mapped to stress 0..1 (default 40,70)")
    r.add_argument("--router", action="store_true", help="also pause on router / Wi-Fi chip temperature")
    r.add_argument("--router-weight", type=float, default=1.0)
    router_args(r)
    grid_args(r)
    r.add_argument("command", nargs=argparse.REMAINDER)
    gp = sub.add_parser("grid", help="outside temperature + power-grid stress (free public data, no key)")
    grid_args(gp); gp.add_argument("--json", action="store_true"); est_arg(gp)
    rr = sub.add_parser("router", help="router / Wi-Fi chip temperature (opt-in sources; fails open)")
    router_args(rr); rr.add_argument("--json", action="store_true"); est_arg(rr)
    w = sub.add_parser("watch", help="check temps of YOUR listed devices (no scanning); exit 2 if any hot")
    w.add_argument("--config", required=True, help="devices.json (or .toml on Python 3.11+)")
    w.add_argument("--interval", type=float, default=300.0); w.add_argument("--once", action="store_true")
    w.add_argument("--log", help="append JSONL readings here"); w.add_argument("--page-dir", help="write status.json + index.html here")
    m = sub.add_parser("mcp", help="run an MCP server (stdio, or Streamable HTTP with --http)")
    m.add_argument("--http", action="store_true", help="serve MCP Streamable HTTP at /mcp (bearer token required)")
    m.add_argument("--host", default="127.0.0.1"); m.add_argument("--port", type=int, default=8765)
    m.add_argument("--path-token", action="store_true",
                   help="also accept the token as /mcp/<token> for clients that cannot send headers")
    sp = sub.add_parser("sample", help="append heat samples (JSONL) to a file until SIGTERM")
    sp.add_argument("--out", required=True); sp.add_argument("--interval", type=float, default=5.0)
    sp.add_argument("--count", type=int, default=None)
    rp = sub.add_parser("report", help="summarize a sample file (markdown; optional GitHub outputs/JSON)")
    rp.add_argument("file"); rp.add_argument("--summary", help="append markdown here (e.g. $GITHUB_STEP_SUMMARY)")
    rp.add_argument("--github-output", help="write key=value outputs here (e.g. $GITHUB_OUTPUT)")
    rp.add_argument("--json-out"); rp.add_argument("--title", default="Heat report")
    rp.add_argument("--watts-per-core", type=float, default=None)
    a = ap.parse_args(argv)
    from . import estimates_enabled
    est = estimates_enabled(getattr(a, "estimates", None))
    if a.cmd == "mcp":
        if a.http:
            from .mcp import serve_http
            return serve_http(a.host, a.port, path_token=a.path_token)
        from .mcp import serve
        return serve()
    if a.cmd == "watch":
        from .watch import watch
        return watch(a.config, a.interval, a.once, a.log, a.page_dir)
    if a.cmd == "sample":
        from .report import sample
        return sample(a.out, a.interval, a.count)
    if a.cmd == "report":
        from .report import report
        return report(a.file, a.summary, a.github_output, a.json_out, a.title, a.watts_per_core)
    if a.cmd in ("grid", "run"):
        comfort = tuple(float(x) for x in a.comfort.split(","))
        gkw = dict(zip_code=a.zip, lat=a.lat, lon=a.lon, comfort=comfort, source=a.grid_source, ba=a.ba)
    if a.cmd in ("router", "run"):
        rkw = dict(cmd=a.router_cmd, url=a.router_url,
                   local=False if a.no_local_wifi else ("all" if a.on_router else "wifi"),
                   band=tuple(float(x) for x in a.router_band.split(",")))
    if a.cmd == "router":
        from .router import router_status
        d = router_status(**rkw)
        if not est:
            d.pop("router_temp_stress", None)
            d["hot"] = None if d["temp_c"] is None else d["temp_c"] >= d["band"][1]
            d["mode"] = "readings"
        if a.json:
            print(json.dumps(d)); return 0
        f = lambda v, n=1: "n/a" if v is None else f"{v:.{n}f}"
        for x in d["sensors"]:
            print(f"sensor {x['source']}:{x['name']} {x['temp_c']:.1f}C" + (" (wifi)" if x["wifi"] else ""))
        c = lambda v: "n/a" if v is None else f"{v:.1f}C"
        tail = (f"band={d['band'][0]:g}..{d['band'][1]:g}C router_temp_stress={f(d['router_temp_stress'], 2)} (ESTIMATE)" if est
                else f"hot_at={d['band'][1]:g}C status=" + ("no readings" if d["hot"] is None else "hot" if d["hot"] else "ok"))
        print(f"router_temp={c(d['temp_c'])} wifi_temp={c(d['wifi_temp_c'])} {tail}" + (f"  errors={d['errors']}" if d["errors"] else ""))
        return 0
    if a.cmd == "grid":
        if a.zip is None and (a.lat is None or a.lon is None):
            ap.error("grid needs --zip or --lat and --lon")
        from .grid import grid_status
        d = grid_status(**gkw)
        if not est:
            for k in ("temp_stress", "demand_stress", "grid_stress"):
                d.pop(k, None)
            if d.get("demand"):
                d["demand"].pop("ratio", None)
            d["mode"] = "readings"
        if a.json:
            print(json.dumps(d)); return 0
        f = lambda v, n=2: "n/a" if v is None else f"{v:.{n}f}"
        where = f"{d['place']}, {d['state']}" if d["place"] else "(lat/lon)"
        print(f"location={where} lat={f(d['lat'],4)} lon={f(d['lon'],4)}")
        print(f"outside_temp={f(d['temp_c'],1)}C" + (f" comfort={comfort[0]:g}..{comfort[1]:g}C temp_stress={f(d['temp_stress'])} (ESTIMATE)" if est else ""))
        dm = d["demand"]
        if dm:
            print(f"demand[{dm['source']}]={dm['load_mw']:.0f} MW recent_peak={dm['peak_mw']:.0f} MW at {dm['time']}"
                  + (f" ratio={dm['ratio']:.2f} demand_stress={f(d['demand_stress'])} (ESTIMATE)" if est else ""))
        else:
            print("demand=n/a (no keyless feed for this area, or fetch failed)")
        if est:
            print(f"grid_stress={f(d['grid_stress'])} (ESTIMATE)")
        if d["errors"]:
            print(f"errors={d['errors']}")
        return 0
    if a.cmd == "status" and a.pid:
        d = app_readings(a.pid)
        if d["cpu_seconds"] is not None and est:
            d.update({"watts_per_core": watts_per_core(a.watts_per_core),
                      "est_energy_wh": round(energy_wh(d["cpu_seconds"], a.watts_per_core), 6), "estimate": True})
        if a.json:
            print(json.dumps(d))
        elif d["source"] == "machine":
            print(f"pid {a.pid}: per-process CPU unavailable here; machine heat={d['app_heat']:.2f}")
        else:
            print(f"pid={a.pid} busy_cores={d['app_cores']:.2f}/{d['cores']} cpu_total={d['cpu_seconds']:.2f}s"
                  + (f" app_heat={d['app_heat']:.2f} est_energy={d['est_energy_wh']:.4f} Wh (ESTIMATE)" if est else ""))
        return 0
    if a.cmd == "status":
        d = readings(estimates=est)
        if a.json:
            print(json.dumps(d))
        elif not est:
            for x in d["sensors"]:
                print(f"sensor {x['source']}:{x['name']} {x['temp_c']:.1f}C")
            t = "no readings (no temperature sensor exposed; not throttling)" if d["temp_c"] is None else f"{d['temp_c']:.1f}C"
            cpu = "n/a" if d["cpu_pct"] is None else f"{d['cpu_pct']:.0f}%"
            print(f"mode=readings temp={t} cpu={cpu} cores={d['cores']}")
        else:
            t = "n/a (hidden)" if d["temp_c"] is None else f"{d['temp_c']:.1f}C"
            print(f"mode=estimates heat={d['heat']:.2f} (ESTIMATE) load1={d['load1']:.2f} cores={d['cores']} "
                  f"load/core={d['load_norm']:.2f} cpu={(d['cpu'] or 0)*100:.0f}% temp={t}")
        return 0
    cmd = a.command[1:] if a.command[:1] == ["--"] else a.command
    grid = None
    if a.grid and a.grid_source != "none":
        if a.zip is None and (a.lat is None or a.lon is None):
            ap.error("--grid needs --zip or --lat and --lon")
        from .grid import Grid
        grid = Grid(weight=a.grid_weight, **gkw)
    router = None
    if a.router:
        from .router import Router
        router = Router(weight=a.router_weight, **rkw)
    if not cmd:
        ap.error("run needs a command after --")
    return run(cmd, a.high, a.low, a.poll, gate=a.gate, energy=a.energy,
               wpc=a.watts_per_core, json_out=a.json_out, grid=grid,
               router=router, max_wait=a.max_wait,
               estimates=est, temp_high=a.temp_high, temp_low=a.temp_low)

if __name__ == "__main__":
    sys.exit(main())
