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

heatgate includes a zero-dependency MCP server: `heatgate mcp` (stdio) or `heatgate mcp --http` (Streamable HTTP).
It exposes these tools:

| tool | what it does |
|---|---|
| `heat_status` | heat, load1, cores, cpu, temp_c |
| `should_pause` | `{pause: heat >= high}` |
| `recommended_workers` | worker count scaled down by heat |
| `app_heat(pid)` | heat of one process tree: busy cores / cores, plus cumulative CPU-seconds |
| `energy_estimate(cpu_seconds \| pid)` | **ESTIMATED** Wh = CPU-seconds × watts-per-core / 3600 |
| `run_throttled(cmd)` | runs a shell command under `heatgate run`. **Disabled unless `HEATGATE_ALLOW_RUN=1`** |

Set `HEATGATE_ALLOW_RUN=1` only if you want the AI to be able to launch commands. `uvx heatgate mcp` can be swapped
for `pipx run heatgate mcp`, or `heatgate mcp` if it is already installed.

**Grok Build CLI**

```
grok mcp add heatgate -- uvx heatgate mcp
```

**Gemini CLI**: add to `~/.gemini/settings.json` (or project `.gemini/settings.json`), or run
`gemini mcp add -s user heatgate uvx heatgate mcp`:

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

**Claude Code**: commit a `.mcp.json` at the repo root (or run `claude mcp add heatgate -- uvx heatgate mcp`):

```json
{
  "mcpServers": {
    "heatgate": { "command": "uvx", "args": ["heatgate", "mcp"], "env": { "HEATGATE_ALLOW_RUN": "0" } }
  }
}
```

**Claude Desktop**: either install the one-click bundle (`packaging/mcpb/dist/heatgate-<ver>.mcpb`, built by
`packaging/mcpb/build.sh`; double-click it or use Settings > Extensions > Advanced settings > Install Extension…;
it needs `python3` 3.8+ on PATH), or put the same `mcpServers` block as above in `claude_desktop_config.json`.

**Cursor**: the same `mcpServers` block in `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (project).

**ChatGPT desktop**: ChatGPT connects to *remote* MCP servers (developer-mode custom connectors) rather than
launching local stdio commands, so use the HTTP mode plus a tunnel below and add the public `https://…/mcp` URL as
a connector. (Exact menu names change between ChatGPT releases; this path was not tested end-to-end.)

**Agents without MCP**: load [`skills/heatgate/SKILL.md`](skills/heatgate/SKILL.md).

### Remote / web / phone: `heatgate mcp --http` + a tunnel

grok.com, the Grok phone apps (via grok.com connectors) and ChatGPT reach MCP servers from their own servers, so
they need a public HTTPS URL. heatgate ships a stdlib-only MCP **Streamable HTTP** server:

```
export HEATGATE_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"   # or let heatgate generate one
heatgate mcp --http --host 127.0.0.1 --port 8765       # endpoint: http://127.0.0.1:8765/mcp
```

- Every request needs `Authorization: Bearer $HEATGATE_TOKEN`; anything else gets `401`. If `HEATGATE_TOKEN` is
  unset, a random token is generated and printed **once** to stderr.
- `--path-token` also accepts `https://…/mcp/<token>` for clients that can only take a URL (the token is then in
  the URL, so treat that URL as a secret).
- `run_throttled` is still disabled unless the server was started with `HEATGATE_ALLOW_RUN=1`. Think twice before
  enabling it on a public tunnel.
- It binds to 127.0.0.1 by default; the tunnel is what makes it public.

Expose it with a Cloudflare quick tunnel (no account; Streamable HTTP works through it) or ngrok:

```
cloudflared tunnel --url http://localhost:8765       # prints https://<random>.trycloudflare.com
ngrok http 8765                                      # prints https://<random>.ngrok-free.app
```

Check it:

```
curl -s -X POST https://<host>/mcp -H "Authorization: Bearer $HEATGATE_TOKEN" -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}'
```

Then add it to Grok: open **grok.com/connectors** → **New Connector** → **Custom**, enter `https://<host>/mcp` and,
if the dialog offers an auth header, `Authorization: Bearer <token>`. Connectors added on grok.com are available in
the Grok phone apps too. See [xAI: Custom MCP Server Tunneling](https://docs.x.ai/grok/connectors/custom-mcp-tunneling).
If your connector dialog has no header field, start with `--path-token` and use `https://<host>/mcp/<token>`.
heatgate does not implement OAuth. Grok Build can use the same server remotely:
`grok mcp add --transport http heatgate https://<host>/mcp --header "Authorization: Bearer ${HEATGATE_TOKEN}"`.

Quick-tunnel URLs change on every restart, so you have to re-add the connector after a restart. Stop the tunnel when
you're done.

## Per-app heat and energy estimates

`heatgate run` measures the CPU of **its own command's process tree** (Linux: `utime+stime` plus reaped children from
`/proc/<pid>/stat`, with the exact total taken from `getrusage(RUSAGE_CHILDREN)` at exit; macOS/Windows: psutil if
installed; otherwise it falls back to whole-machine heat). Every run ends with a summary like this:

```
[heatgate] app cpu=5.01s wall=6.02s app_heat avg/peak=0.114/0.126 | ESTIMATE energy=0.0139 Wh saved_by_pauses=0.0000 Wh (paused 0.0s) @ 10 W/core
```

- **app_heat** = busy cores used by the app / total cores (0..1).
- **Energy is an ESTIMATE**, not a measurement: `Wh = CPU-seconds × watts-per-core / 3600`. The default is
  **10 W per busy core**, a rough figure for one active modern desktop or server core. Set it per machine with
  `--watts-per-core` or `HEATGATE_WATTS_PER_CORE` (laptop cores are often lower, big server cores higher). Idle,
  GPU, RAM, disk and PSU losses are **not** included.
- **saved_by_pauses** = paused seconds × the app's CPU rate just before the pause × watts-per-core. This is an
  estimate of work that was deferred. It is usually not energy that was avoided altogether, because the work still
  runs after the resume.

```
heatgate run --gate app --high 0.6 --low 0.4 -- make -j     # pause on this app's own heat, not the whole box
heatgate run --json-out run.json -- python train.py          # summary as JSON (est_energy_wh, est_saved_wh, …)
heatgate status --pid 1234                                    # pid=1234 app_heat=0.25 busy_cores=2.00/8 cpu_total=… est_energy=… Wh (ESTIMATE)
```

```python
with heatgate.Gate(high=0.6, low=0.4, pid=worker.pid):   # gate on one process tree
    ...
heatgate.app_heat(pid); heatgate.cpu_seconds(pid); heatgate.energy_estimate(cpu_s=120)
```

### Per-call energy for local AI

Wrap each local-model call to get an estimated Wh figure for it:

```
heatgate run --energy -- ollama run llama3 "Explain hysteresis in one paragraph"
# ... model output ...
# [heatgate] ESTIMATED energy for this call: … Wh (… CPU-s x 10 W/core / 3600; est. saved by pauses … Wh)
```

Caveats: `ollama run` is a client. Inference happens in the `ollama serve` daemon, which is **not** in the wrapped
process tree. To count the inference, measure the server, e.g. `heatgate status --pid $(pgrep -f 'ollama serve')`
before and after the call and take the difference in `cpu_total`, or run the model in-process (llama.cpp
`llama-cli`, which is in the tree). GPU energy is not counted at all.

## Grid mode (outside temperature + power-grid stress)

Pause heavy work when it is very hot/cold outside or the regional grid is near its peak. Free public
data, **no API key by default**, cached 10 min (`HEATGATE_GRID_TTL` seconds), and **fail-open**: any
network error just means the grid is ignored.

```sh
heatgate grid --zip 02740            # example: New Bedford, MA
heatgate grid --lat 41.63 --lon -70.94 --json
heatgate run --grid --zip 02740 --grid-weight 0.8 -- python train.py
```

```python
from heatgate import Gate
from heatgate.grid import Grid
with Gate(high=0.8, low=0.5, grid=Grid(zip_code="02740", weight=0.8)):
    heavy()
```

MCP tool: `grid_status` (`zip` or `lat`/`lon`, optional `comfort_lo`/`comfort_hi`).

| Data | Source | Key |
| --- | --- | --- |
| ZIP -> lat/lon | api.zippopotam.us | none |
| Temperature | Open-Meteo `current=temperature_2m` | none |
| Grid demand (New England only) | ISO New England ISO Express 5-min system load (the public website's chart feed; undocumented, may change) | none |
| Grid demand (any US balancing authority) | EIA-930 hourly demand | optional free key: `EIA_API_KEY`, `--ba` / `HEATGATE_GRID_BA` (default `ISNE`) |

GridStatus and Electricity Maps both require keys and are not used.

Formula:

* `temp_stress` = 0 inside the comfort band (`--comfort 10,24` C), rising linearly to 1 at 15 C beyond either edge.
* `demand_stress` = `clamp((load / recent_peak - 0.7) / 0.3)`; recent peak = max of the last ~2 days of
  actual load and today's forecast (ISO-NE), or of the last 48 h (EIA).
* `grid_stress = max(temp_stress, demand_stress)` over what is available (null if nothing is).
* `effective_heat = max(local_heat, grid_weight * grid_stress)`; `run --grid` pauses/resumes on that.

Outside New England without `EIA_API_KEY`, only temperature is used.

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
heatgate status --pid N    # heat of one process tree
heatgate run [--high 0.8] [--low 0.5] [--poll 1] [--gate machine|app] [--energy] [--watts-per-core 10] [--json-out f] -- <cmd...>
heatgate mcp [--http --host 127.0.0.1 --port 8765 [--path-token]]
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

## Heat in GitHub Actions

This repo is also a composite action. It records heat (max of load1/cores and CPU busy fraction) during a job and
appends a table to the job summary: avg/peak heat, duration, CPU-minutes (from `/proc/stat` deltas, all processes on
the runner), CPU utilization, cores, sample count and an **ESTIMATED** energy figure (machine-wide CPU-seconds × `watts-per-core`
input, default 10). The same values are step outputs (`heat-avg`, `heat-peak`, `duration-s`, `cpu-minutes`, `cpu-util`,
`cores`, `samples`, `est-energy-wh`, `watts-per-core`, `json`). Set `artifact: <name>` to upload the JSON
summary and raw samples.

Wrap one command:

```yaml
- uses: actions/checkout@v4
- uses: fitzyracing1/heatgate@main
  with:
    run: python -m mybot run
    artifact: heat
```

Or sample the whole job:

```yaml
- uses: fitzyracing1/heatgate@main
  with: {mode: start, interval: "5"}
# ... your steps ...
- uses: fitzyracing1/heatgate@main
  if: always()
  with: {mode: stop}
```

The action installs heatgate from its own ref into a venv under `$RUNNER_TEMP` (the `sample`/`report` subcommands
are newer than PyPI 0.1.0). The same subcommands work locally:

```
heatgate sample --out heat.jsonl --interval 5 &   # stop with SIGTERM
heatgate report heat.jsonl --json-out heat.json
```

## License
MIT
