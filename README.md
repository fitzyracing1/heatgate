# heatgate

> **electron-flow heat throttle**: pauses heavy work when the CPU is hot and resumes it when it cools.

```
pipx install heatgate        # or: pip install heatgate
```

A tiny, zero-dependency Python throttle that slows work down when the machine is "hot".

**Why load instead of temperature?** Cloud VMs usually hide thermal sensors (`/sys/class/thermal` is empty,
`psutil.sensors_temperatures()` returns nothing). What a VM *can* see is how busy its CPUs are. Busy CPUs are
what make the host chip hot, so heatgate uses CPU load as a stand-in for heat.

## How heat is computed

```
heat = max(load1 / cores, cpu_utilization [, (temp_C - 40) / 50 if a sensor exists])
```

- `load1` comes from `/proc/loadavg` (falls back to `os.getloadavg()`).
- `cpu_utilization` is the busy fraction from two `/proc/stat` samples taken 0.2 s apart.
- If `/sys/class/thermal/thermal_zone*/temp` or psutil sensors ever show up, they are added in. If not, they are skipped.

0.0 means idle and 1.0 means every core is saturated.

## Install

```
pip install heatgate     # or: pipx install heatgate / uvx heatgate status
pip install git+https://github.com/fitzyracing1/heatgate   # from source
```

## Demo

In a real run wrapping a CPU-saturating job, `heatgate run --high 0.8 --low 0.5` went through **12 pauses and
12 resumes**. It paused at about **100% load** and resumed at about **20%**, so the job ran in a duty cycle and
the box never stayed pinned.

## Use it from any AI

heatgate includes a zero-dependency **stdio MCP server**: `heatgate mcp`. It exposes these tools:

| tool | what it does |
|---|---|
| `heat_status` | heat, load1, cores, cpu, temp_c |
| `should_pause` | `{pause: heat >= high}` |
| `recommended_workers` | worker count scaled down by heat |
| `run_throttled(cmd)` | runs a shell command under `heatgate run`. **Disabled unless `HEATGATE_ALLOW_RUN=1`** |

**Claude Desktop** (`claude_desktop_config.json`) and **Cursor** (`~/.cursor/mcp.json` or `.cursor/mcp.json`) use the same shape:

```json
{
  "mcpServers": {
    "heatgate": {
      "command": "uvx",
      "args": ["heatgate", "mcp"],
      "env": { "HEATGATE_ALLOW_RUN": "0" }
    }
  }
}
```

Set `"HEATGATE_ALLOW_RUN": "1"` only if you want the AI to be able to launch commands. With pipx, use
`"command": "pipx", "args": ["run", "heatgate", "mcp"]`. If heatgate is already installed, use `"command": "heatgate", "args": ["mcp"]`.

**Grok / Grok Bot**: add a custom MCP server (stdio) with command `uvx heatgate mcp` (or `pipx run heatgate mcp`).
Add env `HEATGATE_ALLOW_RUN=1` only if you want it to run jobs.

**ChatGPT and other MCP clients**: any client that can launch a local stdio MCP server works with the same
command, `uvx heatgate mcp`. Clients that only accept remote (HTTP) servers need a stdio-to-HTTP bridge,
for example `npx -y supergateway --stdio "uvx heatgate mcp"`.

**Agents without MCP**: load [`skills/heatgate/SKILL.md`](skills/heatgate/SKILL.md), a plain instructions file that
covers install, checking heat, and wrapping heavy jobs.

## Python API

```python
import heatgate

heatgate.heat()                      # -> 0.0..1.0+
with heatgate.Gate(high=0.8, low=0.5):   # blocks while heat >= 0.8, until it drops below 0.5
    do_work()

for batch in batches:
    heatgate.pace(high=0.8, low=0.5, max_sleep=5)  # no sleep below low, up to max_sleep at/above high
    process(batch)

@heatgate.throttled(high=0.8, low=0.5)
def step(): ...

from concurrent.futures import ProcessPoolExecutor
with ProcessPoolExecutor(heatgate.workers(max_workers=8)) as ex:  # fewer workers when hot
    ...
```

## CLI

```
heatgate status            # heat=0.10 load1=0.78 cores=8 load/core=0.10 cpu=1% temp=n/a (hidden)
heatgate status --json
heatgate run [--high 0.8] [--low 0.5] [--poll 1] -- <cmd...>
heatgate mcp               # stdio MCP server
```

`run` starts the command in its own process group. When heat reaches `high`, it sends SIGSTOP to the whole group.
When heat falls below `low`, it sends SIGCONT. Each transition is logged to stderr, and the run ends with a summary
of pause/resume counts and heat min/avg/max. Ctrl-C and SIGTERM are passed on to the child, which is resumed first.

Note: if the child is the only thing loading the box, it will cycle: it runs until the box is hot, gets paused,
the box cools, and it resumes. The effect is a duty cycle. If you want fewer transitions, widen the gap between
`high` and `low` or raise `--poll`.

## Wrapping the marslog heat training

The marslog repo has a training entrypoint (`marslog/heat.py`, run with `python -m marslog.heat train`).
heatgate does not change it. To run it under heatgate:

```
cd marslog && heatgate run --high 0.8 --low 0.5 -- python -m marslog.heat train
```

## License
MIT

## Plans

This project is free, and its license stays as is. Support it through [GitHub Sponsors](https://github.com/sponsors/fitzyracing1):

- **Free**: use the code, community support.
- **Pro ($49/month)**: priority issues and support.
- **Team ($199/month)**: priority support for your team plus early access.
