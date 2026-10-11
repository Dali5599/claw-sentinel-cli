"""Alert rules and the alert model.

This module defines the alert data model, the rule engine that evaluates
threshold expressions against a :class:`~sentinel._metrics.Snapshot`, and the
JSON/text rendering used by the dispatchers.
"""
from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Union

from ._metrics import Snapshot

ALERT_LEVELS = ("ok", "info", "warning", "critical")

# Operators supported by the rule engine.
_OPERATORS = {"gt", "gte", "lt", "lte", "eq", "neq", "between", "in", "contains"}
_LEVELS = ("ok", "info", "warning", "critical")


class AlertLevel(str, Enum):
    """Severity level of an alert."""

    OK = "ok"
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"

    @classmethod
    def from_string(cls, value: str) -> "AlertLevel":
        try:
            return cls(value.lower())
        except ValueError:
            return cls.WARNING


@dataclass
class ThresholdRule:
    """A single threshold rule.

    Attributes
    ----------
    metric :
        Path into the snapshot (e.g. ``cpu.percent``).
    operator :
        One of ``gt``, ``gte``, ``lt``, ``lte``, ``eq``, ``neq``, ``between``,
        ``in``, ``contains``.
    value :
        The threshold value (``int`` or ``float``), or a ``list`` for
        ``between``/``in``.
    level :
        Trigger level (``warning`` or ``critical``).
    title :
        Human-readable title, if supplied.
    """
    metric: str
    operator: str
    value: Union[int, float, List[Union[int, float]]]
    level: AlertLevel = AlertLevel.WARNING
    title: Optional[str] = None

    def __post_init__(self):
        self.operator = self.operator.lower()
        self.level = AlertLevel.from_string(self.level)


@dataclass
class Alert:
    """An alert produced by the engine."""
    alert_id: str
    timestamp: datetime
    level: AlertLevel
    metric: str
    title: str
    message: str
    current_value: Any
    expected_value: Any
    platform: Dict[str, Any]
    tags: List[str]

    def to_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        d["timestamp"] = self.timestamp.isoformat(timespec="seconds")
        if isinstance(self.level, AlertLevel):
            d["level"] = self.level.value
        return d

    def __str__(self) -> str:
        return f"[{self.level.value.upper()}] {self.title}: {self.message}"


# ---------------------------------------------------------------------------
# Metric path resolution
# ---------------------------------------------------------------------------

def _resolve_path(snapshot: Snapshot, path: str) -> Any:
    """Resolve a dotted metric path into the snapshot.

    Supported paths::

        cpu.percent              -> overall CPU usage
        cpu.cores_logical        -> logical core count
        cpu.load_avg_1m          -> 1 min load average
        memory.percent           -> RAM usage
        memory.swap_percent      -> swap usage
        disk_used_percent        -> most filled mount (computed alias)
        disk_free_gb             -> least free mount (computed alias)
        services.failed          -> number of failed units
        uptime.seconds           -> uptime in seconds
        platform.hostname        -> host name
        disk                     -> list of mount points (for CLI rendering)
    """
    parts = path.split(".")
    node: Any = _snapshot_tree(snapshot)
    for part in parts:
        if isinstance(node, dict):
            if part in node:
                node = node[part]
            else:
                raise KeyError(path)
        elif isinstance(node, list):
            node = node[int(part)]
        else:
            raise KeyError(path)
    return node


def _snapshot_tree(snapshot: Snapshot) -> Dict[str, Any]:
    """Build the metric tree from a snapshot."""
    d: Dict[str, Any] = {
        "cpu": dataclasses.asdict(snapshot.cpu),
        "memory": dataclasses.asdict(snapshot.memory),
        "services": {
            "total": snapshot.services.total,
            "running": snapshot.services.running,
            "stopped": snapshot.services.stopped,
            "failed": snapshot.services.failed,
        },
        "uptime": dataclasses.asdict(snapshot.uptime),
        "platform": dataclasses.asdict(snapshot.platform),
        "host": snapshot.host.to_dict(),
        "tags": snapshot.tags,
    }
    if snapshot.disks:
        d["disk_used_percent"] = max(di.percent for di in snapshot.disks)
        d["disk_free_gb"] = min(di.free_gb for di in snapshot.disks)
        d["disk_total_gb"] = sum(di.total_gb for di in snapshot.disks)
    else:
        d["disk_used_percent"] = 0.0
        d["disk_free_gb"] = float("inf")
        d["disk_total_gb"] = 0.0
    return d


# ---------------------------------------------------------------------------
# Rule compilation from configuration
# ---------------------------------------------------------------------------

def compile_config_thresholds(raw: Dict[str, Any]) -> List[ThresholdRule]:
    """Turn a config dict into :class:`ThresholdRule` instances.

    The config may use any of these forms::

        # Suffix form: operator defaults to ``gt``
        cpu.percent.warning = 70
        cpu.percent.critical = 90

        # Object form
        "memory.percent" = { warning = 80, critical = 95 }

        # Explicit operator form
        "disk_free_gb" = { operator = "lt", value = 10, level = "critical" }

    Unknown keys and unparseable values are ignored so a bad config never
    prevents the collector from running.
    """
    rules: List[ThresholdRule] = []
    for key, value in raw.items():
        if isinstance(value, dict):
            _add_object_rule(rules, key, value)
        else:
            _add_suffix_rule(rules, key, value)
    return rules


def _add_suffix_rule(rules: List[ThresholdRule], key: str, value: Any) -> None:
    """Handle a dotted key whose last segment is a level, e.g. cpu.percent.critical = 90."""
    if not isinstance(value, (int, float)):
        return
    tokens = key.split(".")
    if tokens[-1] not in _LEVELS:
        # Plain key -> default gt / warning
        rules.append(ThresholdRule(metric=key, operator="gt", value=value, level="warning"))
        return
    level = AlertLevel.from_string(tokens[-1])
    rest = tokens[:-1]
    op = "gt"
    if rest and rest[-1] in _OPERATORS:
        op = rest.pop()
    rules.append(ThresholdRule(metric=".".join(rest), operator=op, value=value, level=level))


def _add_object_rule(rules: List[ThresholdRule], key: str, value: Dict[str, Any]) -> None:
    """Handle an object value, e.g. { warning = 80, critical = 95 }."""
    value = dict(value)
    op = "gt"
    target = None
    if any(o in value for o in _OPERATORS):
        op = next(o for o in value if o in _OPERATORS)
        target = value.pop(op)
    elif "value" in value:
        target = value.pop("value")
    level = AlertLevel.from_string(value.pop("level", "warning"))
    if target is not None:
        rules.append(ThresholdRule(metric=key, operator=op, value=target, level=level))
    # Plain object form: {"warning": 80, "critical": 95}
    for lvl in ("warning", "critical", "info", "ok"):
        if lvl in value:
            rules.append(ThresholdRule(metric=key, operator="gt", value=value[lvl], level=lvl))


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def _apply_op(value: Any, operator: str, target: Any) -> bool:
    if operator == "gt":
        return value > target
    if operator == "gte":
        return value >= target
    if operator == "lt":
        return value < target
    if operator == "lte":
        return value <= target
    if operator == "eq":
        return value == target
    if operator == "neq":
        return value != target
    if operator == "between":
        lo, hi = sorted(target)
        return lo <= value <= hi
    if operator == "in":
        return value in target
    if operator == "contains":
        return str(target) in str(value)
    raise ValueError(f"unknown operator: {operator}")


def check_snapshot(snapshot: Snapshot, rules: List[ThresholdRule]) -> List[Alert]:
    """Check every rule against a snapshot and return triggered alerts.

    A rule triggers when ``current`` evaluates true against ``rule.value``.
    Duplicate alerts (same metric + value + level) are collapsed into one
    so the watch loop does not spam identical notifications.
    """
    alerts: List[Alert] = []
    seen: set = set()

    for rule in rules:
        try:
            current = _resolve_path(snapshot, rule.metric)
        except KeyError:
            continue  # rule references an unknown metric; skip it

        if rule.operator in ("in", "contains") and isinstance(current, list):
            ok = any(_apply_op(v, rule.operator, rule.value) for v in current)
        else:
            if isinstance(current, list):
                current = max(current)
            ok = _apply_op(current, rule.operator, rule.value)
        if not ok:
            continue

        title = rule.title or _default_title(rule.metric)
        message = _rule_message(rule, current)
        key = (rule.metric, rule.operator, str(rule.value), rule.level.value)
        if key in seen:
            continue
        seen.add(key)

        alerts.append(Alert(
            alert_id=str(uuid.uuid4()),
            timestamp=datetime.now(timezone.utc),
            level=rule.level,
            metric=rule.metric,
            title=title,
            message=message,
            current_value=current,
            expected_value=rule.value,
            platform={
                "hostname": snapshot.host.hostname,
                "os": snapshot.host.os_name,
                "release": snapshot.platform.release,
                "machine": snapshot.platform.machine,
            },
            tags=list(snapshot.tags),
        ))
    return alerts


def _default_title(metric: str) -> str:
    """Generate a human-readable title for a metric."""
    return {
        "cpu.percent": "CPU usage high",
        "memory.percent": "Memory usage high",
        "memory.swap_percent": "Swap usage high",
        "disk_used_percent": "Disk usage high",
        "disk_free_gb": "Disk free space low",
        "services.failed": "Failed services detected",
        "services.running": "Services down",
        "uptime.seconds": "System uptime",
    }.get(metric, f"{metric} threshold reached")


def _rule_message(rule: ThresholdRule, current: Any) -> str:
    return f"current={current} {rule.operator} {rule.value}"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def alert_to_json(alert: Alert) -> Dict[str, Any]:
    """Render an alert as a JSON object suitable for files/APIs."""
    d = alert.to_dict()
    d["event"] = "alert"
    return d


def alert_to_text(alert: Alert, template: Optional[str] = None) -> str:
    """Render an alert as plain text.

    Available template fields:
    ``{level}`` ``{metric}`` ``{title}`` ``{message}`` ``{current_value}``
    ``{expected}`` ``{hostname}`` ``{timestamp}`` ``{tags}``.
    """
    emoji = {
        AlertLevel.OK: ":white_check_mark:",
        AlertLevel.INFO: ":information:",
        AlertLevel.WARNING: ":warning:",
        AlertLevel.CRITICAL: ":rotating_light:",
    }.get(alert.level, "")
    level = str(alert.level.value).upper()
    if isinstance(alert.current_value, float) and not alert.current_value.is_integer():
        current = f"{alert.current_value:.2f}"
    else:
        current = str(alert.current_value)
    expected = ", ".join(str(x) for x in alert.expected_value) if isinstance(alert.expected_value, list) else str(alert.expected_value)
    tags = ", ".join(alert.tags) if alert.tags else "none"

    if template is None:
        template = (
            "{emoji} [{level}] {metric}\n"
            "  Host: {hostname}\n"
            "  Value: {current}    Expected: {expected}\n"
            "  {message}\n"
            "  Time: {timestamp}\n"
            "  Snapshot: claw-sentinel check --tags all\n"
            "  Tags: {tags}"
        )
    return template.format(
        emoji=emoji, level=level, metric=alert.metric, title=alert.title,
        message=alert.message, current=current, expected=expected,
        hostname=alert.platform.get("hostname", "unknown"),
        timestamp=alert.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC"),
        tags=tags,
    )


# ---------------------------------------------------------------------------
# Alert normalization for the ``alert`` subcommand
# ---------------------------------------------------------------------------

def normalize_alert_input(data: Any) -> List[Alert]:
    """Convert user input (dict/list) into a list of :class:`Alert` objects."""
    items = data if isinstance(data, list) else [data]
    out: List[Alert] = []
    for item in items:
        if isinstance(item, dict):
            d = dict(item)
            # Snapshot payloads are detected by the snapshot_id + cpu presence.
            if "snapshot_id" in d and "cpu" in d:
                out.append(_snapshot_as_info_alert(d))
                continue
            out.append(_dict_to_alert(d))
        else:
            out.append(_snapshot_as_info_alert({"snapshot_id": "unknown", "cpu": {}, **item}))
    return out


def _dict_to_alert(d: Dict[str, Any]) -> Alert:
    platform = d.pop("platform", {})
    if isinstance(d.get("platform"), dict):
        platform = d["platform"]
    level = AlertLevel.from_string(d.pop("level", "warning"))
    return Alert(
        alert_id=d.pop("alert_id", str(uuid.uuid4())),
        timestamp=datetime.fromisoformat(d.pop("timestamp", datetime.now(timezone.utc).isoformat())),
        level=level,
        metric=d.pop("metric", "unknown"),
        title=d.pop("title", "claw-sentinel alert"),
        message=d.pop("message", ""),
        current_value=d.pop("current_value", None),
        expected_value=d.pop("expected_value", None),
        platform=platform,
        tags=d.pop("tags", []),
    )


def _snapshot_as_info_alert(d: Dict[str, Any]) -> Alert:
    host = d.get("host", {})
    if isinstance(host, dict):
        hostname = host.get("hostname", "unknown")
    else:
        hostname = str(host)
    cpu = d.get("cpu", {})
    if isinstance(cpu, dict):
        cpu_val = cpu.get("percent", 0)
    else:
        cpu_val = cpu
    mem = d.get("memory", {})
    if isinstance(mem, dict):
        mem_val = mem.get("percent", 0)
    else:
        mem_val = mem
    return Alert(
        alert_id=d.get("snapshot_id", str(uuid.uuid4())),
        timestamp=datetime.now(timezone.utc),
        level=AlertLevel.INFO,
        metric="snapshot",
        title="claw-sentinel snapshot collected",
        message=f"CPU {cpu_val}% | memory {mem_val}% | "
                f"disk {d.get('disk_used_percent', 'n/a')} | "
                f"services {d.get('services', {}).get('total', 'n/a')} units, "
                f"uptime {d.get('uptime', {}).get('formatted', 'n/a')}",
        current_value=d,
        expected_value=None,
        platform=platform if isinstance(host, dict) else {"hostname": hostname},
        tags=d.get("tags", []),
    )