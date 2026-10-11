"""Output dispatchers.

Dispatchers serialize an :class:`~sentinel._alerts.Alert` into a transport
format. :class:`CompositeDispatcher` fans out to several back-ends and
collects results so callers know exactly which channels delivered and which
failed.
"""
from __future__ import annotations

import abc
import json
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, TextIO

from ._alerts import Alert, AlertLevel, alert_to_json, alert_to_text
from ._telegram import TelegramBot, TelegramError

# Default text template for alert notifications.
DEFAULT_ALERT_TEXT = (
    "{emoji} [{level}] {metric}\n"
    "  Host: {hostname}\n"
    "  Value: {current_value}    Expected: {expected_value}\n"
    "  {message}\n"
    "  Time: {timestamp}  Snapshot: claw-sentinel check --tags all\n"
    "  Tags: {tags}"
)

# Default Markdown template for Telegram payloads.
DEFAULT_TELEGRAM_TEXT = (
    "*{emoji}* **[{level}]** *{metric}*\n"
    "  *Host:* {hostname}\n"
    "  *Value:* `{current_value}`   *Expected:* `{expected_value}`\n"
    "  *{message}*\n"
    "  *Time:* {timestamp}  *Snapshot:* `claw-sentinel check --tags all`\n"
    "  *Tags:* {tags}"
)


# ---------------------------------------------------------------------------
# Base dispatcher
# ---------------------------------------------------------------------------


class Dispatcher(abc.ABC):
    """Base class for all dispatch back-ends."""

    @abc.abstractmethod
    def dispatch(self, alert: Alert) -> bool:
        """Send one alert to the back-end.

        Returns ``True`` on success. Exceptions are never raised by the base
        implementation; back-ends report failures through the return value.
        """

    def write_snapshot(self, snapshot: Any) -> bool:
        """Override to support writing a snapshot payload. Default: no-op."""
        return True

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Display name for this back-end."""


# ---------------------------------------------------------------------------
# Individual back-ends
# ---------------------------------------------------------------------------


class NullDispatcher(Dispatcher):
    """A back-end that silently discards everything.

    Useful when the user wants alerts parsed/rendered but not sent anywhere.
    """

    @property
    def name(self) -> str:
        return "null"

    def dispatch(self, alert: Alert) -> bool:
        return True


class ConsoleText(Dispatcher):
    """Write human-readable alerts to standard output.

    Uses :func:`alert_to_text` with a default template.
    """

    def __init__(self, template: Optional[str] = None, indent: int = 2):
        self.template = template
        self.indent = indent

    @property
    def name(self) -> str:
        return "console(text)"

    def dispatch(self, alert: Alert) -> bool:
        try:
            print(alert_to_text(alert, self.template))
            return True
        except Exception:
            return False


class ConsoleJSON(Dispatcher):
    """Write one JSON object per alert to standard output (JSONL)."""

    @property
    def name(self) -> str:
        return "console(json)"

    def dispatch(self, alert: Alert) -> bool:
        try:
            print(json.dumps(alert_to_json(alert), indent=2))
            return True
        except Exception:
            return False


class FileJSON(Dispatcher):
    """Append alerts as newline-delimited JSON to a file."""

    def __init__(self, path: str, append: bool = True):
        self.path = path
        self.append = append

    @property
    def name(self) -> str:
        return f"file({self.path})"

    def dispatch(self, alert: Alert) -> bool:
        try:
            mode = "a" if self.append else "w"
            with open(self.path, mode, encoding="utf-8") as fh:
                fh.write(json.dumps(alert_to_json(alert), indent=2) + "\n")
            return True
        except OSError:
            return False


class TelegramWebhook(Dispatcher):
    """Send alerts to a Telegram chat via a bot webhook.

    The message is rendered with the Markdown template so formatting, bold
    headings and code spans work as expected in the Telegram client.
    """

    def __init__(self, bot: TelegramBot, template: Optional[str] = None):
        self.bot = bot
        self.template = template or DEFAULT_TELEGRAM_TEXT

    @property
    def name(self) -> str:
        return f"telegram({self.bot.chat_id})"

    def dispatch(self, alert: Alert) -> bool:
        try:
            self.bot.send_message(alert_to_text(alert, self.template))
            return True
        except TelegramError:
            return False


# ---------------------------------------------------------------------------
# Composite dispatcher
# ---------------------------------------------------------------------------


@dataclass
class CompositeDispatcher(Dispatcher):
    """Fan out to multiple back-ends and aggregate results.

    Parameters
    ----------
    backends :
        Ordered list of ``(name, Dispatcher)`` pairs.
    stop_on_failure :
        If ``True`` stop dispatching as soon as one back-end fails.
    """

    backends: List[tuple]
    stop_on_failure: bool = False

    @property
    def name(self) -> str:
        return "composite"

    def dispatch(self, alert: Alert) -> Dict[str, bool]:
        results: Dict[str, bool] = {}
        for name, backend in self.backends:
            try:
                results[name] = backend.dispatch(alert)
            except Exception:  # never let a back-end crash the whole dispatch
                results[name] = False
                if self.stop_on_failure:
                    break
        return results


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------

@dataclass
class AlertContext:
    """Fields available in alert text/markdown templates."""
    level: str
    metric: str
    title: str
    message: str
    current_value: str
    expected_value: str
    hostname: str
    timestamp: str
    tags: str
    emoji: str

    @classmethod
    def from_alert(cls, alert: Alert, text: bool = True) -> "AlertContext":
        emoji = {
            AlertLevel.OK: ":white_check_mark:",
            AlertLevel.INFO: ":information:",
            AlertLevel.WARNING: ":warning:",
            AlertLevel.CRITICAL: ":rotating_light:",
        }.get(alert.level, "")
        if isinstance(alert.current_value, float) and not alert.current_value.is_integer():
            current = f"{alert.current_value:.2f}"
        else:
            current = str(alert.current_value)
        if isinstance(alert.expected_value, list):
            expected = ", ".join(str(x) for x in alert.expected_value)
        else:
            expected = str(alert.expected_value)
        return cls(
            level=alert.level.value,
            metric=alert.metric,
            title=alert.title,
            message=alert.message,
            current_value=current,
            expected_value=expected,
            hostname=alert.platform.get("hostname", "unknown"),
            timestamp=alert.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC"),
            tags=", ".join(alert.tags) if alert.tags else "none",
            emoji=emoji,
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def make_dispatcher_from_config(config: Any) -> CompositeDispatcher:
    """Build a dispatcher tree from the parsed configuration."""
    if hasattr(config, "as_dict"):
        config = config.as_dict()
    elif not isinstance(config, dict) and hasattr(config, "dispatch"):
        config = {"dispatch": config.dispatch}
    channels = config.get("dispatch", {}).get("channels", {})
    telegram_cfg = channels.get("telegram", {})

    backends: List[tuple] = []
    if channels.get("stdout", {}).get("enabled", True):
        fmt = channels.get("stdout", {}).get("format", "json")
        backends.append(("console", ConsoleJSON() if fmt == "json" else ConsoleText()))
    if channels.get("json_file", {}).get("enabled", False):
        path = channels["json_file"].get("path", "claw-sentinel-alerts.jsonl")
        backends.append(("json_file", FileJSON(path, append=channels["json_file"].get("append", True))))
    if channels.get("telegram", {}).get("enabled", False):
        token = telegram_cfg.get("token") or ""
        chat_id = telegram_cfg.get("chat_id") or ""
        if token and chat_id:
            parse_mode = telegram_cfg.get("parse_mode", "MarkdownV2")
            bot = TelegramBot(token, chat_id, parse_mode=parse_mode)
            backends.append(("telegram", TelegramWebhook(bot)))
        else:
            pass  # telegram disabled when credentials are missing
    if not backends:
        backends.append(("null", NullDispatcher()))
    return CompositeDispatcher(backends)