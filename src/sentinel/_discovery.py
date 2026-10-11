"""Cross-platform service and host discovery.

This module abstracts away platform differences when enumerating the services
and daemons that run on a machine. Each OS uses its native tooling:

+----------+------------------------------------------------------------+
| Platform | Mechanism                                                  |
+==========+============================================================+
| Linux    | :command:`systemctl list-units`                            |
|          | with ``service --status-all`` fallback (SysV systems)      |
+----------+------------------------------------------------------------+
| Windows  | ``Get-Service`` via PowerShell with WMI fallback           |
+----------+------------------------------------------------------------+
| macOS    | :command:`launchctl list`                                  |
|          | with Homebrew Services fallback                            |
+----------+------------------------------------------------------------+

Discovery never crashes the collector: a missing service layer is reported as
an empty list with a :term:`debug` log entry.

See Also
--------
src.sentinel._metrics : consumes this layer to fill the Snapshot services
    field.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# OS detection
# ---------------------------------------------------------------------------
OPERATING_SYSTEM = platform.system().lower()
IS_LINUX = OPERATING_SYSTEM == "linux"
IS_WINDOWS = OPERATING_SYSTEM == "windows"
IS_DARWIN = OPERATING_SYSTEM == "darwin"

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Service:
    """A single discovered service / daemon unit.

    Attributes
    ----------
    name :
        Internal unit name (e.g. ``sshd`` or ``WinRM``).
    status :
        One of ``running``, ``stopped``, ``failed``, ``unknown``.
    display_name :
        Human-readable description, when the platform provides one.
    """
    name: str
    status: str
    display_name: Optional[str] = None
    description: Optional[str] = None

    def __str__(self) -> str:
        return f"{self.name}: {self.status}"


@dataclass(frozen=True)
class HostInfo:
    """Static host identification data collected at start-up."""
    hostname: str
    os_name: str
    platform: str                      # e.g. ``linux`` / ``windows`` / ``darwin``
    platform_release: str
    platform_version: str
    kernel_version: str
    machine: str                       # e.g. ``x86_64``
    arch: str                          # e.g. ``64bit``
    hardware_serial: Optional[str] = None
    os_version: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_TIMEOUT = 30  # seconds
_MAX_LINES = 4000  # service units are numerous; limit for safety


def _run(
    cmd: List[str],
    *,
    shell: bool = False,
    timeout: Optional[float] = None,
    env: Optional[Dict[str, str]] = None,
    input_text: Optional[str] = None,
) -> Tuple[int, str, str]:
    """Run a command and return (returncode, stdout, stderr).

    Subprocess errors and timeouts are caught and reported on stderr instead of
    propagating, because service discovery is best-effort.
    """
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout or _DEFAULT_TIMEOUT,
            shell=shell,
            env=env,
            input=input_text,
        )
        return result.returncode, result.stdout or "", result.stderr or ""
    except subprocess.TimeoutExpired as exc:
        return -1, "", f"command timed out: {exc.args[0]!r}"
    except (OSError, ValueError) as exc:
        return -1, "", f"command execution failed: {exc!r}"


def _parse_systemctl_units(output: str) -> List[Service]:
    """Parse ``systemctl list-units`` text into :class:`Service` instances.

    Expected columns (when ``--no-legend`` is used):
    ``UNIT  LOAD   ACTIVE   SUB     DESCRIPTION``.
    """
    services = []
    seen = set()
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        name = parts[0]
        load_state = parts[1]
        active_state = parts[2]
        sub_state = parts[3]
        description = " ".join(parts[4:]) if len(parts) > 4 else ""

        if name in seen:
            continue
        seen.add(name)

        status = "unknown"
        if load_state == "not-found":
            continue  # masked/ghost unit, not a real service
        if load_state == "error":
            status = "failed"
        elif active_state == "failed":
            status = "failed"
        elif active_state == "running":
            status = "running"
        elif active_state in {"inactive", "dead", "stopped", "activating"}:
            status = "stopped"

        # Only report units that look like real services.
        if not name.endswith(".service"):
            continue

        services.append(Service(
            name=name[:-9],  # strip ".service" suffix
            status=status,
            description=description,
        ))
    return services[:_MAX_LINES]


def _list_services_linux() -> Tuple[List[Service], str]:
    """Return (services, error_message)."""
    # Prefer systemd; fall back to SysV ``service --status-all``.
    if _which("systemctl") and os.path.exists("/run/systemd/system"):
        code, out, err = _run([
            "systemctl", "list-units", "--type=service",
            "--no-pager", "--no-legend",
            "--state=running", "--state=failed", "--state=activating",
        ])
        if code >= 0 and out.strip():
            return _parse_systemctl_units(out), ""
        # Still ask for a complete snapshot when the state filter yields
        # nothing (rare when running as a non-systemd service).
        code, out, err = _run(["systemctl", "list-units", "--type=service",
                               "--no-pager", "--no-legend"])
        if code >= 0:
            return _parse_systemctl_units(out), ""
        return [], err or "systemctl unavailable"

    if _which("service"):
        code, out, err = _run(["service", "--status-all"])
        if code >= 0:
            # ``+  /usr/bin/service foo   ...`` style output.
            services = []
            for line in out.splitlines():
                line = line.strip()
                if not line.startswith((" + ", " - ")):
                    continue
                status = "running" if line.startswith(" + ") else "stopped"
                parts = line.split()
                name = parts[-1] if parts else ""
                if name:
                    services.append(Service(name=name, status=status))
            return services, ""
        return [], err or "service unavailable"

    return [], "no service discovery tool found (expected systemctl or service)"


def _which(program: str) -> Optional[str]:
    return shutil.which(program)


def _list_services_windows() -> Tuple[List[Service], str]:
    """Enumerate services via PowerShell ``Get-Service``.

    Falls back to WMI ``Win32_Service`` when the CIM cmdlet is unavailable.
    """
    # PowerShell path.
    code, out, err = _run([
        "powershell.exe", "-NoProfile", "-NonInteractive",
        "-Command",
        "$s = Get-Service | "
        "Select-Object -Property Name,Status,DisplayName,"
        "\"@{N='StartMode';E={$_.StartType}}\" | "
        "ConvertTo-Json -Depth 4",
    ], timeout=60)
    if code >= 0 and out.strip():
        services = _parse_windows_json(out)
        if services:
            return services, ""
        return [], err or "Get-Service returned no services"

    # WMI fallback.
    code, out, err = _run([
        "wmic", "service", "get", "Name,Status,DisplayName,StartMode",
        "/format:list",
    ], timeout=60)
    if code >= 0 and out.strip():
        services = _parse_windows_wmic(out)
        if services:
            return services, ""
        return [], err or "wmic returned no services"

    return [], err or "PowerShell and WMI are unavailable"


def _parse_windows_json(json_text: str) -> List[Service]:
    import json
    services = []
    try:
        rows = json.loads(json_text)
        for row in (rows if isinstance(rows, list) else [rows]):
            name = str(row.get("Name", "")).strip()
            status = str(row.get("Status", "")).lower()
            display = str(row.get("DisplayName", "")).strip()
            if not name:
                continue
            services.append(Service(
                name=name,
                status=status,
                display_name=display if display else None,
            ))
    except (ValueError, AttributeError):
        return []
    return services


def _parse_windows_wmic(text: str) -> List[Service]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    services = []
    current: Dict[str, Any] = {}
    for line in lines:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key, value = key.strip().lower(), value.strip()
        if key in ("name", "status", "displayname", "startmode"):
            current[key] = value
        elif current:
            # Record ended (next non-field line).
            if current.get("name"):
                services.append(Service(
                    name=current["name"],
                    status=current.get("status", "unknown"),
                    display_name=current.get("displayname"),
                ))
            current = {}
    if current.get("name"):
        services.append(Service(
            name=current["name"],
            status=current.get("status", "unknown"),
            display_name=current.get("displayname"),
        ))
    return services


def _list_services_macos() -> Tuple[List[Service], str]:
    """Enumerate services via ``launchctl`` (with Homebrew fallback)."""
    code, out, err = _run(["launchctl", "list"])
    if code >= 0:
        services = _parse_launchctl(out)
        if services:
            return services, ""
    # Homebrew-managed services are a first-class macOS concept.
    if _which("brew"):
        code, out, err = _run(["brew", "services", "list"])
        if code >= 0 and out.strip():
            return _parse_brew_services(out), ""
    return [], err or "launchctl unavailable"


def _parse_launchctl(text: str) -> List[Service]:
    """Parse ``launchctl list`` output (PID, status, label)."""
    services = []
    for line in text.splitlines()[1:]:  # skip header
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        pid, status = parts[0], parts[1]
        label = parts[2] if len(parts) > 2 else ""
        if pid == "-":  # not currently loaded
            continue
        services.append(Service(name=label, status="running"))
    return services[:_MAX_LINES]


def _parse_brew_services(text: str) -> List[Service]:
    """Parse ``brew services list`` output."""
    services = []
    for line in text.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        # "name    status    user    plist"
        parts = line.split()
        if len(parts) < 2:
            continue
        name, status = parts[0], parts[1]
        services.append(Service(
            name=name,
            status={"installed": "running", "uninstalled": "stopped"}.get(
                status, "unknown"),
        ))
    return services[:_MAX_LINES]


def get_host_info() -> HostInfo:
    """Gather host identification data."""
    try:
        hostname = os.uname().nodename
    except AttributeError:
        hostname = os.getenv("COMPUTERNAME") or os.getenv("HOSTNAME") or "unknown"
    try:
        uname = os.uname()
    except AttributeError:
        uname = platform.uname()
    return HostInfo(
        hostname=hostname,
        os_name=OPERATING_SYSTEM,
        platform=platform.system().lower(),
        platform_release=uname.release,
        platform_version=uname.version,
        kernel_version=platform.release(),
        machine=uname.machine,
        arch=platform.machine(),
    )


def list_services() -> List[Service]:
    """Return a list of discovered services, OS-specific backend chosen.

    This function is best-effort: missing tooling degrades to an empty list
    rather than raising.
    """
    if IS_LINUX:
        services, _ = _list_services_linux()
    elif IS_WINDOWS:
        services, _ = _list_services_windows()
    elif IS_DARWIN:
        services, _ = _list_services_macos()
    else:
        services = []
    return services