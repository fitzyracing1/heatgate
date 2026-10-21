---
name: heatgate
description: Check machine heat (CPU load as a heat proxy) and run heavy jobs throttled so they pause when the box is hot and resume when it cools. Use before/while running builds, training, test suites, or batch jobs.
---

# heatgate

Zero-dependency Python tool (3.8+). Heat = max(load1/cores, CPU utilization[, scaled temp]); 0.0 idle, 1.0 saturated.

## Install
```
pipx install heatgate      # or: pip install heatgate  /  uvx heatgate status
```

## Check heat
```
heatgate status            # heat=0.10 load1=0.78 cores=8 ... temp=n/a (hidden)
heatgate status --json     # machine-readable; read the "heat" field
```
Rule of thumb: heat >= 0.8 is hot (wait or reduce parallelism); < 0.5 is cool.

## Wrap heavy jobs
```
heatgate run --high 0.8 --low 0.5 -- <command...>
```
SIGSTOPs the whole job when heat >= high, SIGCONTs below low, and logs PAUSE/RESUME plus a summary to stderr. The exit code is the child's.

Pick parallelism: `python -c "import heatgate; print(heatgate.workers(max_workers=8))"` then e.g. `make -j<N>`.

In Python code:
```python
import heatgate
with heatgate.Gate(high=0.8, low=0.5): do_work()
heatgate.pace()                          # in loops
@heatgate.throttled()                    # per call
```

## As an MCP server
`heatgate mcp` (stdio) exposes heat_status, should_pause, recommended_workers, run_throttled.
run_throttled only works when the server is started with env HEATGATE_ALLOW_RUN=1.

## Agent guidance
- Run `heatgate status --json` before starting long or parallel work.
- Prefer `heatgate run -- ...` for anything expected to take more than about a minute or to saturate the CPU.
- Don't kill a job that heatgate paused; it resumes on its own once the box cools.
