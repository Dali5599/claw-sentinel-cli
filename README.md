# claw-sentinel

Cross-platform system monitoring CLI with customizable JSON alerts and a Telegram webhook dispatcher.

Collect **CPU**, **memory**, **disk**, and **service** metrics on Linux, macOS, and Windows, evaluate threshold rules, and dispatch formatted alerts to stdout, a JSONL file, or a Telegram chat — all in one tool.

## Features

- **Single snapshot or continuous watch** — `check`, `watch`, and `alert` subcommands.
- **Cross-platform hardware metrics** — CPU (usage, per-core, frequency, load average), memory (RAM + swap), disk (usage, free space, inodes per mount), service enumerators per OS, uptime.
- **Customizable alert rules** — numeric thresholds with warning/critical levels, `gt/gte/lt/lte/eq/neq/between/in/contains` operators, defined in TOML or JSON.
- **Configurable output** — JSON (default), human-readable text, JSONL streaming for `watch`.
- **Telegram dispatch** — send alerts as Markdown to a group, channel, or forum topic; token/chat id can come from config, CLI, or environment variables.
- **Zero external HTTP deps** — Telegram uses the Python standard library only.

## Installation

Requires Python **3.11** or newer (uses `tomllib` for config parsing).

```bash
pip install claw-sentinel
```

Development install:

```bash
git clone https://github.com/claw-network/claw-sentinel.git
cd claw-sentinel
pip install -e .
```

Or run directly from source without installing:

```bash
python src/sentinel.py check --tags cpu,memory --format text
```

## Quick start

```bash
# One-time snapshot (JSON)
claw-sentinel check --tags cpu,memory,disk

# Human-readable summary
claw-sentinel check --tags cpu,memory --format text

# Continuous monitoring
claw-sentinel watch --interval 60 --output monitor.jsonl

# Read alerts from a file and dispatch
claw-sentinel alert --file alerts.jsonl
```

Exit code is **1** if any alert triggered, **0** otherwise, and **2** for usage errors.

## Subcommands

### `check`

Collect one snapshot, optionally evaluate alert rules, and emit the result.

```
claw-sentinel check [OPTIONS]
```

| Option | Description |
|---|---|
| `--tags, -t` | Metrics to include: `cpu`, `memory`, `disk`, `services`, `platform` (comma-separated). Default: all. |
| `--format, -f` | Output format: `json` or `text`. Default: `json`. |
| `--output, -o` | File to write, or `-` / `stdout` for stdout. Default: stdout. |
| `--snapshot-file` | Write the raw snapshot JSON to this file (in addition to stdout). |
| `--thresholds, -T` | JSON file of threshold rules that overrides the config rules. |
| `--no-alerts` | Skip alert evaluation and dispatch. |
| `--compact, -c` | Truncated one-line text output. |

### `watch`

Monitor continuously at a fixed interval, emitting snapshots and dispatching alerts.

```
claw-sentinel watch [OPTIONS]
```

| Option | Description |
|---|---|
| `--interval, -i` | Seconds between samples. Default: `60`. |
| `--tags, -t` | Metrics to include. |
| `--format, -f` | `json` (JSONL) or `text`. Default: `text`. |
| `--output, -o` | JSONL file to append; a timestamp is added to the name if none is given. |
| `--max-samples, -n` | Stop after this many samples. |
| `--no-alerts` | Skip alert evaluation and dispatch. |
| `--no-repeats` | Suppress repeated alerts for the same metric across samples. |
| `--thresholds, -T` | JSON file of threshold rules. |
| `--compact, -c` | Truncated text output. |

Ctrl+C or SIGTERM shuts the loop down cleanly.

### `alert`

Read an alert payload (JSON array or newline-delimited JSONL) and dispatch it through the configured channels. Snapshot objects are auto-promoted to INFO alerts.

```
claw-sentinel alert [OPTIONS]
```

| Option | Description |
|---|---|
| `--stdin, -s` | Read alert JSON from standard input. |
| `--file, -f` | Read alert JSON/JSONL from a file. |
| `--from-check, -x` | Run a check first, then dispatch its alerts. |
| `--dry-run, -d` | Print rendered messages only (no dispatch). |
| `--level, -L` | Override the alert level (`ok` | `info` | `warning` | `critical`). |
| `--tags, -t` | Override the alert tags. |
| `--thresholds, -T` | JSON file of threshold rules. |

## Configuration

Configuration is a TOML file found in this order (first match wins):

1. `$CLAW_SENTINEL_CONFIG` environment variable
2. `--config` / `-C` CLI option
3. `~/.config/claw-sentinel/config.toml`
4. `claw-sentinel.toml` in the current working directory

Example:

```toml
# ~/.config/claw-sentinel/config.toml
[general]
name = "my-server"
log_level = "INFO"

[metrics]
poll_interval = 0.3

[alert]
enabled = true
default_level = "warning"
include_snapshot = true

# Threshold rules — suffix form (default operator: gt)
"cpu.percent.warning" = 70
"cpu.percent.critical" = 90
"memory.percent.warning" = 80
"memory.percent.critical" = 95
"disk_free_gb.critical" = 10
"services.failed.warning" = 1

# Object form
"uptime.seconds" = { warning = 60, critical = 10 }

# Explicit operator form
"disk_used_percent" = { operator = "gt", value = 90, level = "critical" }

[dispatch.channels]
# Console output (stdout)
stdout = { enabled = true, format = "json" }
# JSONL alert file
json_file = { enabled = true, path = "alerts.jsonl", append = true }

# Telegram webhook
telegram = { enabled = true, token = "123456:ABC-DEF1234...", chat_id = "1987654321", parse_mode = "MarkdownV2" }

# Stop dispatching after the first back-end fails
stop_on_failure = false
```

Secrets may also be injected via environment variables, which take precedence over the config file:

| Variable | Purpose |
|---|---|
| `CLAW_SENTINEL_CONFIG` | Alternative config path |
| `CLAW_SENTINEL_TELEGRAM_TOKEN` | Bot token |
| `CLAW_SENTINEL_TELEGRAM_CHAT_ID` | Destination chat id |

## Alert rules

Rule values can be numeric thresholds or objects:

```json
{
  "cpu.percent.critical": 90,
  "memory.percent.warning": 80,
  "memory.percent.critical": 95,
  "disk_free_gb": { "operator": "lt", "value": 10, "level": "critical" },
  "services.failed": { "warning": 1, "critical": 3 }
}
```

Supported operators: `gt`, `gte`, `lt`, `lte`, `eq`, `neq`, `between` (list of two numbers), `in` (list/set), `contains` (substring for strings).

Metric paths in rules: `cpu.percent`, `cpu.cores_logical`, `cpu.cores_physical`, `cpu.load_avg_1m`, `cpu.load_avg_5m`, `cpu.load_avg_15m`, `cpu.util_percent`, `memory.percent`, `memory.swap_percent`, `disk_used_percent`, `disk_free_gb`, `disk_total_gb`, `services.total`, `services.running`, `services.stopped`, `services.failed`, `uptime.seconds`, `uptime.minutes`, `uptime.hours`.

## Alert output

### JSON (default)

```json
{
  "snapshot_id": "server01_1757812345",
  "timestamp": "2026-10-11T09:50:00Z",
  "host": {
    "hostname": "server01",
    "os_name": "linux",
    "platform": "linux",
    "platform_release": "6.8.0-79-generic",
    "machine": "x86_64",
    "arch": "64bit"
  },
  "platform": {
    "hostname": "server01",
    "kernel": "6.8.0-79-generic",
    "release": "Ubuntu 24.04 LTS",
    "machine": "x86_64",
    "python_version": "3.12.3"
  },
  "uptime": {
    "seconds": 3685312,
    "minutes": 61421.9,
    "hours": 1023.7,
    "days": 42.6,
    "formatted": "42d 15h 20m 12s"
  },
  "cpu": {
    "percent": 12.3,
    "per_core_percent": [8.1, 15.4, ...],
    "logical_cores": 8,
    "physical_cores": 4,
    "frequency_mhz": 2400.0,
    "load_avg": [0.52, 0.48, 0.42],
    "user_percent": 8.2,
    "system_percent": 4.1,
    "idle_percent": 87.7,
    "iowait_percent": 0.5,
    "interrupts_per_sec": 1234.5
  },
  "memory": {
    "total_gb": 16.0,
    "used_gb": 4.2,
    "free_gb": 11.5,
    "available_gb": 13.1,
    "percent": 26.2,
    "swap_total_gb": 4.0,
    "swap_used_gb": 0.0,
    "swap_percent": 0.0
  },
  "disks": [
    {
      "mountpoint": "/",
      "device": "/dev/nvme0n1p2",
      "fstype": "ext4",
      "total_gb": 100.0,
      "used_gb": 55.1,
      "free_gb": 44.9,
      "percent": 55.1,
      "inodes_total": 6246400,
      "inodes_used": 123456,
      "inodes_free": 6122944,
      "inodes_percent": 1.98,
      "label": "root"
    }
  ],
  "services": {
    "total": 234,
    "running": 228,
    "stopped": 6,
    "failed": 0,
    "units": []
  }
}
```

### Alerts

```json
{
  "event": "alert",
  "alert_id": "a1b2c3d4-...",
  "timestamp": "2026-10-11T09:50:00Z",
  "level": "critical",
  "metric": "disk_used_percent",
  "title": "Disk usage high",
  "message": "current=95.2 gt 90",
  "current_value": 95.2,
  "expected_value": 90,
  "platform": {
    "hostname": "server01",
    "os": "linux",
    "release": "Ubuntu 24.04 LTS",
    "machine": "x86_64"
  },
  "tags": ["cpu", "memory", "disk"]
}
```

### Telegram

Markdown-rendered alerts are sent with a small inline keyboard (`View JSON` / `Close`).

## Telegram setup

1. Message **@BotFather** on Telegram and create a bot.
2. Copy the bot token.
3. Get your chat id: message **@userinfobot** (or use `t.me/mybot?start` style) — channels need the numeric id (prefix with `-` for channels: `-100...`).
4. Add the token and chat id to `~/.config/claw-sentinel/config.toml` under `[dispatch.channels.telegram]`, or set `CLAW_SENTINEL_TELEGRAM_TOKEN` and `CLAW_SENTINEL_TELEGRAM_CHAT_ID` as environment variables.

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run the test suite
pytest tests/

# Type check
pyright src/   # or mypy src/
```

## License

MIT
