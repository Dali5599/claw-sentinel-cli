"""Configuration management for claw-sentinel.

Configuration lives in a single TOML file found by walking:

1. ``$CLAW_SENTINEL_CONFIG`` environment variable, if set
2. the ``--config`` CLI option
3. ``~/.config/claw-sentinel/config.toml`` (XDG-style)
4. ``claw-sentinel.toml`` in the current working directory (fallback only)

Missing keys use built-in defaults; secrets can additionally be injected via
``CLAW_SENTINEL_TELEGRAM_TOKEN`` and ``CLAW_SENTINEL_TELEGRAM_CHAT_ID``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union

try:
    import tomllib
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

DEFAULT_CONFIG: Dict[str, Any] = {
    "general": {
        "name": "claw-sentinel",
        "log_level": "INFO",
        "version": "0.1.0",
    },
    "metrics": {
        "poll_interval": 0.3,
        "include_per_core": True,
        "max_cpu_cores": 16,
    },
    "alert": {
        "enabled": True,
        "default_level": "warning",
        "include_snapshot": True,
        "thresholds": {},
    },
    "dispatch": {
        "channels": {
            "stdout": {"enabled": True, "format": "json"},
            "json_file": {"enabled": False, "path": "claw-sentinel-alerts.jsonl", "append": True},
            "telegram": {"enabled": False, "token": "", "chat_id": "", "parse_mode": "MarkdownV2"},
        },
        "stop_on_failure": False,
    },
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge two dictionaries, with override winning."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _find_config_files(explicit: Optional[Path]) -> List[Path]:
    """Return an ordered list of candidate config file locations."""
    candidates: List[Path] = []
    if explicit:
        candidates.append(explicit.expanduser().resolve())
    env = os.environ.get("CLAW_SENTINEL_CONFIG")
    if env:
        candidates.append(Path(env).expanduser().resolve())
    candidates.append(Path.home() / ".config" / "claw-sentinel" / "config.toml")
    candidates.append(Path.cwd() / "claw-sentinel.toml")
    # Deduplicate while preserving order
    seen: Set[Path] = set()
    uniq: List[Path] = []
    for p in candidates:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


@dataclass
class Config:
    """Loaded configuration."""

    general: Dict[str, Any] = field(default_factory=dict)
    metrics: Dict[str, Any] = field(default_factory=dict)
    alert: Dict[str, Any] = field(default_factory=dict)
    dispatch: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, paths: Optional[List[Path]] = None) -> "Config":
        """Load configuration from disk, applying defaults and env overrides."""
        cfg = dict(DEFAULT_CONFIG)
        for path in _find_config_files(paths[0] if paths else None):
            if path.is_file():
                try:
                    with open(path, "rb") as fh:
                        file_cfg = tomllib.load(fh)
                    cfg = _deep_merge(cfg, file_cfg)
                except (OSError, tomllib.TOMLDecodeError) as exc:
                    print(f"warning: cannot read {path}: {exc}", file=sys.stderr)
        # Env var overrides for secrets (token/chat_id)
        token = os.environ.get("CLAW_SENTINEL_TELEGRAM_TOKEN")
        if token is not None:
            cfg.setdefault("dispatch", {}).setdefault("channels", {}).setdefault("telegram", {})["token"] = token
        chat_id = os.environ.get("CLAW_SENTINEL_TELEGRAM_CHAT_ID")
        if chat_id is not None:
            cfg.setdefault("dispatch", {}).setdefault("channels", {}).setdefault("telegram", {})["chat_id"] = chat_id
        # Normalize boolean flags
        cfg["alert"]["enabled"] = bool(cfg["alert"].get("enabled", True))
        cfg["dispatch"]["channels"]["stdout"]["enabled"] = bool(
            cfg["dispatch"]["channels"]["stdout"].get("enabled", True)
        )
        cfg["dispatch"]["channels"]["json_file"]["enabled"] = bool(
            cfg["dispatch"]["channels"]["json_file"].get("enabled", False)
        )
        cfg["dispatch"]["channels"]["telegram"]["enabled"] = bool(
            cfg["dispatch"]["channels"]["telegram"].get("enabled", False)
        )
        return cls(
            general=cfg.get("general", {}),
            metrics=cfg.get("metrics", {}),
            alert=cfg.get("alert", {}),
            dispatch=cfg.get("dispatch", {}),
        )

    def get_threshold_rules(self) -> List[Any]:
        """Return compiled threshold rules from the alert section."""
        from ._alerts import compile_config_thresholds
        return compile_config_thresholds(self.alert.get("thresholds", {}))

    def as_dict(self) -> Dict[str, Any]:
        """Export as a plain dictionary (for debugging)."""
        return {
            "general": self.general,
            "metrics": self.metrics,
            "alert": self.alert,
            "dispatch": self.dispatch,
        }