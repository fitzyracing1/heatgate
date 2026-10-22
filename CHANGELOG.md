# Changelog

## Unreleased

### Changed (breaking default)
- **Readings-only is now the default.** heatgate reports and gates on measured readings only: real
  temperature sensors (CPU/GPU/Wi-Fi/router), raw CPU utilization %, outside air temp, and grid demand MW.
  It gates on `--temp-high`/`--temp-low` (default 85/75 C). With no sensor it reports "no readings" and does
  not throttle (this includes most cloud VMs and GitHub-hosted runners).
- Estimates are opt-in with `--estimates`, `HEATGATE_ESTIMATES=1`, `Gate(estimates=True)`, the MCP
  `estimates: true` argument, or the Action input `estimates: true`. Estimates cover the load-as-heat proxy
  `heat`, energy Wh, and the `temp_stress`/`demand_stress`/`grid_stress`/`router_temp_stress` scores.
  To keep the old behaviour, set `HEATGATE_ESTIMATES=1`.
- MCP `heat_status` returns readings plus `mode`. `should_pause` and `recommended_workers` use temperatures
  in readings mode, and `energy_estimate` is disabled unless estimates are enabled.
- Action: new `estimates` input and `mode`, `temp-avg-c`, `temp-peak-c` outputs. `heat-*` and
  `est-energy-wh` are only set with `estimates: true`.

### Added
- Router / Wi-Fi chip temperature (`heatgate router`, `run --router`, `Gate(router=)`, MCP `router_status`).
- `heatgate watch` checks the temperatures of the devices you list (JSONL log, hot exit code, static status page).
- `run --max-wait` / `Gate(max_wait=)`: stop waiting forever under sustained stress.
