#!/usr/bin/env python3
"""claw-sentinel: cross-platform system monitoring with JSON alerts and Telegram dispatch.

This is the primary entry point for the ``claw-sentinel`` CLI. It provides three
subcommands:

* ``check``  - collect one snapshot of the hardware, optionally evaluate alert
  rules, and emit JSON or human-readable text.
* ``watch``  - monitor continuously at a fixed interval, emitting snapshots and
  dispatching alerts as they fire.
* ``alert``  - read an alert payload from JSON (standard input or a file) and
  dispatch it through the configured channels (Telegram, JSON files, stdout).

Configuration is loaded from a TOML file (see :func:`claw_sentinel._config.Config.load`)
and can be customized for thresholds, output formats, and notification back-ends.

Usage examples
--------------
    claw-sentinel check --tags cpu,memory --format text
    claw-sentinel check --thresholds rules.json > snapshot.json
    claw-sentinel watch --interval 60 --output monitor.jsonl
    claw-sentinel alert --file alerts.jsonl

The same commands are available through ``python -m sentinel`` or the installed
``claw-sentinel`` console script.

See Also
--------
src._metrics : hardware metrics collection.
src._alerts : alert rules and the alert data model.
src._telegram : Telegram Bot API client.
src._dispatcher : output back-ends (console, file, Telegram).
src._config : configuration loading.
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, TextIO

from ._alerts import Alert, AlertLevel, check_snapshot, compile_config_thresholds, alert_to_text, normalize_alert_input
from ._config import Config
from ._dispatcher import CompositeDispatcher, make_dispatcher_from_config
from ._metrics import Snapshot, collect_snapshot

# ---------------------------------------------------------------------------
# Version and help text
# ---------------------------------------------------------------------------

VERSION = "0.1.0"

DESCRIPTION = (
    "claw-sentinel: cross-platform system monitoring with JSON alerts and Telegram dispatch.\n"
    "\n"
    "Collect hardware metrics (CPU, memory, disk, services), evaluate threshold\n"
    "rules, and dispatch formatted alerts to configured back-ends (console,\n"
    "JSON file, Telegram).\n"
)

EPILOG = (
    "Examples\n"
    "--------\n"
    "  claw-sentinel check --tags cpu,memory --format text\n"
    "  claw-sentinel check --thresholds my-rules.json > snapshot.json\n"
    "  claw-sentinel watch --interval 60 --output logs.jsonl\n"
    "  claw-sentinel alert --file alerts.jsonl --dry-run\n"
)


class _HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Custom formatter that preserves the description and epilog layouts."""


# ---------------------------------------------------------------------------
# Argument validators
# ---------------------------------------------------------------------------

def _positive_float(value: str) -> float:
    f = float(value)
    if f <= 0:
        raise argparse.ArgumentTypeError(f"{value!r} must be > 0")
    return f


def _positive_int(value: str) -> int:
    i = int(value)
    if i <= 0:
        raise argparse.ArgumentTypeError(f"{value!r} must be > 0")
    return i


def _tags(value: str) -> Set[str]:
    return {t.strip() for t in value.split(",") if t.strip()}


def _json_file(value: str) -> Dict[str, Any]:
    try:
        with open(value, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise argparse.ArgumentTypeError(f"cannot read {value!r}: {exc}") from exc


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge two dictionaries; override values take precedence."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _emit_json(obj: Any, fh: TextIO) -> None:
    """Write one JSON object followed by a newline (JSONL style)."""
    json.dump(obj, fh, indent=2)
    fh.write("\n")
    fh.flush()


def _snapshot_to_text(snap: Snapshot, compact: bool = False) -> str:
    """Render a snapshot as plain text for human reading."""
    host = snap.host
    lines = [
        f"claw-sentinel  host={host.hostname}  platform={snap.platform.machine}  kernel={snap.platform.kernel}",
        f"  time     = {snap.timestamp.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"  uptime   = {snap.uptime.formatted}  snapshot_id={snap.snapshot_id[:8]}",
    ]

    cpu = snap.cpu
    if cpu.load_avg is not None:
        cpu_line = (
            f"  cpu      = {cpu.percent:6.1f}%  "
            f"({cpu.logical_cores}/{cpu.physical_cores} cores, {cpu.frequency_mhz or '?':.1f} GHz)  "
            f"load={cpu.load_avg[0]:.2f}"
        )
    else:
        cpu_line = f"  cpu      = {cpu.percent:6.1f}%  ({cpu.logical_cores}/{cpu.physical_cores} cores)"
    lines.append(cpu_line)
    lines.append(
        f"  memory   = {snap.memory.used_gb:7.1f}/{snap.memory.total_gb:7.1f} GB ({snap.memory.percent:5.1f}%)  "
        f"swap={snap.memory.swap_used_gb:6.1f}/{snap.memory.swap_total_gb:6.1f} GB ({snap.memory.swap_percent:4.1f}%)"
    )
    disk_parts = [f"{d.mountpoint} {d.percent:5.1f}% ({d.free_gb:6.1f} GB free)" for d in snap.disks]
    lines.append(f"  disk     = {', '.join(disk_parts) if disk_parts else 'n/a'}")
    lines.append(
        f"  services = {snap.services.running}/{snap.services.total} running, "
        f"{snap.services.stopped} stopped, {snap.services.failed} failed"
    )
    if compact:
        lines = lines[:5]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Subcommand: check
# ---------------------------------------------------------------------------

def cmd_check(args: argparse.Namespace) -> int:
    """Collect one snapshot, evaluate alert rules, and emit the result.

    Exit codes
    ----------
    0 - success, no alerts triggered
    1 - alerts triggered
    2 - usage or I/O error
    """
    config = Config.load(paths=args.config)
    dispatcher = make_dispatcher_from_config(config)

    # Thresholds from the CLI JSON file merge over the config file thresholds.
    cli_thresholds = _json_file(args.thresholds) if args.thresholds else {}
    merged = _deep_merge(config.alert.get("thresholds", {}), cli_thresholds)
    rules = compile_config_thresholds(merged)

    snap = collect_snapshot(tags=_tags(args.tags) if args.tags else None, poll_interval=0.3)

    # Optional side channel: write the raw snapshot JSON to a file.
    if args.snapshot_file:
        with open(args.snapshot_file, "w", encoding="utf-8") as fh:
            json.dump(snap.to_dict(), fh, indent=2)

    out: Optional[TextIO] = None
    try:
        if args.output and args.output not in ("-", "stdout"):
            out = open(args.output, "w", encoding="utf-8")
        else:
            out = sys.stdout
        try:
            if args.format == "text":
                out.write(_snapshot_to_text(snap, compact=args.compact) + "\n")
            else:
                json.dump(snap.to_dict(), out, indent=2)
                out.write("\n")
            out.flush()

            if not args.no_alerts and rules:
                alerts = check_snapshot(snap, rules)
                if alerts:
                    for alert in alerts:
                        dispatcher.dispatch(alert)
                    return 1
        finally:
            if out is not sys.stdout:
                out.close()
    except OSError as exc:
        print(f"error: output write failed: {exc}", file=sys.stderr)
        return 2
    return 0


# ---------------------------------------------------------------------------
# Subcommand: watch
# ---------------------------------------------------------------------------

_SHUTDOWN = threading.Event()


def _signal_handler(sig, frame) -> None:
    _SHUTDOWN.set()


def _resolve_watch_output(output: Optional[str]) -> Optional[str]:
    """Normalize the watch output path, adding a timestamp if needed."""
    if not output:
        return None
    if "." in output and not output.endswith(".jsonl"):
        return output
    base = datetime.now().strftime("%Y%m%dT%H%M%S")
    return f"{output}-{base}.jsonl"


def cmd_watch(args: argparse.Namespace) -> int:
    """Continuously monitor hardware at a fixed interval.

    Each iteration emits a snapshot (JSONL or text) and dispatches any alerts
    that fire. The loop exits cleanly on SIGINT/SIGTERM.
    """
    config = Config.load(paths=args.config)
    dispatcher = make_dispatcher_from_config(config)

    cli_thresholds = _json_file(args.thresholds) if args.thresholds else {}
    merged = _deep_merge(config.alert.get("thresholds", {}), cli_thresholds)
    rules = compile_config_thresholds(merged)

    interval = args.interval
    max_samples = args.max_samples
    samples = 0
    seen = set()

    output_path = _resolve_watch_output(args.output)
    output_file: Optional[TextIO] = None

    # Install signal handlers for graceful shutdown.
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    try:
        if output_path:
            output_file = open(output_path, "a", encoding="utf-8")
        while not _SHUTDOWN.is_set() and (max_samples is None or samples < max_samples):
            snap = collect_snapshot(tags=_tags(args.tags) if args.tags else None, poll_interval=interval)
            samples += 1

            if args.format == "text":
                print(_snapshot_to_text(snap, compact=args.compact))
            else:
                if output_file:
                    _emit_json(snap.to_dict(), output_file)
                else:
                    _emit_json(snap.to_dict(), sys.stdout)

            if not args.no_alerts and rules:
                alerts = check_snapshot(snap, rules)
                if args.no_repeats:
                    # Suppress alerts whose metric was already reported in this run.
                    current_metrics = seen.copy()
                    alerts = [a for a in alerts if a.metric not in current_metrics]
                    seen.update(a.metric for a in alerts)
                for alert in alerts:
                    dispatcher.dispatch(alert)

            time.sleep(interval)
        return 0
    finally:
        if output_file:
            output_file.close()


# ---------------------------------------------------------------------------
# Subcommand: alert
# ---------------------------------------------------------------------------

def _load_alert_input(args: argparse.Namespace) -> Any:
    """Read an alert payload from stdin or a file.

    Accepts a JSON array of alerts or a newline-delimited (JSONL) stream.
    Snapshot payloads are detected automatically and converted to INFO alerts.
    """
    if args.stdin:
        raw = sys.stdin.read()
        if raw.strip():
            return json.loads(raw)
        return []
    if args.file:
        with open(args.file, "r", encoding="utf-8") as fh:
            raw = fh.read()
        if raw.strip().startswith("["):
            return json.loads(raw)
        # JSONL: one object per line.
        out: List[Any] = []
        for line in raw.splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out
    raise argparse.ArgumentTypeError("alert requires either --stdin, --file or --from-check")


def cmd_alert(args: argparse.Namespace) -> int:
    """Read an alert payload and dispatch it through the configured channels.

    The payload may be a single alert object, an array of alert objects, or a
    snapshot object (which is promoted to an INFO alert). Snapshot payloads can
    also be generated on the fly with ``--from-check``.
    """
    config = Config.load(paths=args.config)
    dispatcher = make_dispatcher_from_config(config)

    cli_thresholds = _json_file(args.thresholds) if args.thresholds else {}
    merged = _deep_merge(config.alert.get("thresholds", {}), cli_thresholds)
    rules = compile_config_thresholds(merged)

    alerts: List[Alert] = []

    if args.from_check:
        snap = collect_snapshot(tags=_tags(args.tags) if args.tags else None, poll_interval=0.3)
        if args.no_alerts:
            # Promote the snapshot to an INFO event.
            alerts = normalize_alert_input(snap.to_dict())
        else:
            alerts = check_snapshot(snap, rules) if rules else []
    else:
        payload = _load_alert_input(args)
        alerts = normalize_alert_input(payload)

    # Apply global overrides on top of the payload values.
    if args.level:
        for alert in alerts:
            alert.level = AlertLevel.from_string(args.level)
    if args.tags:
        for alert in alerts:
            alert.tags = list(args.tags)

    if args.dry_run:
        for alert in alerts:
            print(alert_to_text(alert))
        return 0 if alerts else 0

    for alert in alerts:
        dispatcher.dispatch(alert)
    return 1 if alerts else 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Build the top-level parser with the check/watch/alert subcommands."""
    parser = argparse.ArgumentParser(
        prog="claw-sentinel",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=_HelpFormatter,
        add_help=False,
    )
    parser.add_argument("--config", "-C", type=str, help="path to the TOML configuration file")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--log-level", "-l", choices=["DEBUG", "INFO", "WARN", "ERROR"], default="INFO")
    parser.add_argument("--verbose", "-v", action="count", default=0)
    parser.add_argument("--quiet", "-q", action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)

    # ---- check ----
    p = sub.add_parser("check", help="collect one snapshot, check rules, emit JSON/text")
    p.add_argument("--tags", "-t", type=_tags, help="metrics to include (cpu,memory,disk,services,platform)")
    p.add_argument("--format", "-f", choices=["json", "text"], default="json")
    p.add_argument("--output", "-o", default="-", help="file to write, or '-' for stdout")
    p.add_argument("--snapshot-file", help="write the raw snapshot JSON to this file")
    p.add_argument("--thresholds", "-T", type=_json_file, help="JSON file of threshold rules (overrides config)")
    p.add_argument("--no-alerts", action="store_true", help="skip alert evaluation and dispatch")
    p.add_argument("--compact", "-c", action="store_true", help="truncated one-line text output")
    p.set_defaults(func=cmd_check)

    # ---- watch ----
    p = sub.add_parser("watch", help="continuous monitoring at a fixed interval")
    p.add_argument("--interval", "-i", type=_positive_float, default=60.0, help="seconds between samples")
    p.add_argument("--tags", "-t", type=_tags, help="metrics to include (cpu,memory,disk,services,platform)")
    p.add_argument("--format", "-f", choices=["json", "text"], default="text")
    p.add_argument("--output", "-o", default=None, help="JSONL file to append (timestamp added if none)")
    p.add_argument("--max-samples", "-n", type=_positive_int, help="stop after this many samples")
    p.add_argument("--no-alerts", action="store_true", help="skip alert evaluation and dispatch")
    p.add_argument("--no-repeats", action="store_true", help="suppress repeated alerts for the same metric")
    p.add_argument("--thresholds", "-T", type=_json_file, help="JSON file of threshold rules (overrides config)")
    p.add_argument("--compact", "-c", action="store_true", help="truncated one-line text output")
    p.set_defaults(func=cmd_watch)

    # ---- alert ----
    p = sub.add_parser("alert", help="read an alert payload and dispatch it")
    p.add_argument("--stdin", "-s", action="store_true", help="read alert JSON from standard input")
    p.add_argument("--file", "-f", help="read alert JSON or JSONL from this file")
    p.add_argument("--from-check", "-x", action="store_true", help="run a check first, then dispatch its alerts")
    p.add_argument("--dry-run", "-d", action="store_true", help="print rendered messages only")
    p.add_argument("--level", "-L", choices=["ok", "info", "warning", "critical"], help="override the alert level")
    p.add_argument("--tags", "-t", type=_tags, help="override the alert tags")
    p.add_argument("--thresholds", "-T", type=_json_file, help="JSON file of threshold rules (overrides config)")
    p.set_defaults(func=cmd_alert)

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    """Parse the command line and run the selected subcommand.

    Returns an exit code: 0 for success, 1 for alerts, 2 for usage errors,
    130 for a clean interrupt.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except SystemExit:
        raise
    except Exception as exc:  # pragma: no cover - unexpected internal errors
        print(f"error: {exc}", file=sys.stderr)
        return 2
