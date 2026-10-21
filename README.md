# heatgate

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
pip install -e .        # or: pip install git+https://github.com/fitzyracing1/heatgate
```

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
