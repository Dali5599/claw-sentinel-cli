"""Hardware metrics collection.

The public entry point, :func:`collect_snapshot`, returns a fully populated
:class:`~sentinel.Snapshot` model covering CPU, memory, disk and services.
All collection runs on top of :pypi:`psutil` so the public API stays stable
across Python and OS versions while the back-end continues to use native data
sources.
"""
from __future__ import annotations

import datetime
import os
import platform
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

import psutil

from ._discovery import HostInfo, Service, list_services, IS_LINUX, IS_WINDOWS, IS_DARWIN

# Virtual / pseudo file systems that are not worth reporting.
_IGNORED_FS: Set[str] = {
    "tmpfs", "devtmpfs", "overlay", "proc", "sysfs", "devpts", "run",
    "cgroup", "cgroup2", "securityfs", "pstore", "bpf", "debugfs",
    "tracefs", "fusectl", "configfs", "hugetlbfs", "mqueue", "nsfs",
}
_IGNORED_MOUNT_PREFIXES = (
    "/proc", "/sys", "/dev", "/run", "/sys/fs", "/sys/kernel", "/sys/fs/cgroup",
    "/boot/efi",
)


@dataclass
class CPUInfo:
    """CPU usage and topology summary."""
    percent: float                       # usage over the poll interval
    per_core_percent: List[float]        # per-core usage, capped
    logical_cores: int
    physical_cores: int
    frequency_mhz: Optional[float]
    load_avg: Optional[Tuple[float, float, float]]  # 1 / 5 / 15 min
    user_percent: Optional[float]
    system_percent: Optional[float]
    idle_percent: Optional[float]
    iowait_percent: Optional[float]
    interrupts_per_sec: Optional[float]


@dataclass
class MemoryInfo:
    """RAM and swap usage."""
    total_gb: float
    used_gb: float
    free_gb: float
    available_gb: float
    percent: float
    swap_total_gb: float
    swap_used_gb: float
    swap_percent: float


@dataclass
class DiskInfo:
    """One mount point / volume."""
    mountpoint: str
    device: str
    fstype: Optional[str]
    total_gb: float
    used_gb: float
    free_gb: float
    percent: float
    inodes_total: Optional[int]
    inodes_used: Optional[int]
    inodes_free: Optional[int]
    inodes_percent: Optional[float]
    label: Optional[str]


@dataclass
class PlatformInfo:
    hostname: str
    kernel: str
    release: str
    machine: str
    python_version: str


@dataclass
class UptimeInfo:
    seconds: float
    minutes: float
    hours: float
    days: float
    formatted: str  # "42d 7h 13m 9s"


@dataclass
class ServicesInfo:
    total: int
    running: int
    stopped: int
    failed: int
    units: List[Service]


@dataclass
class Snapshot:
    """A point-in-time snapshot of all monitored hardware."""
    snapshot_id: str
    timestamp: datetime.datetime
    host: HostInfo
    platform: PlatformInfo
    uptime: UptimeInfo
    cpu: CPUInfo
    memory: MemoryInfo
    disks: List[DiskInfo]
    services: ServicesInfo
    tags: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Export as a JSON-serializable nested dictionary."""
        d = dataclasses.asdict(self)
        d["host"] = self.host.to_dict()
        return d


# ---------------------------------------------------------------------------
# Metric collection
# ---------------------------------------------------------------------------

def collect_cpu(poll_interval: Optional[float] = 0.3) -> CPUInfo:
    """Measure CPU usage.

    Parameters
    ----------
    poll_interval :
        Seconds to sample over. Use ``0`` to skip sampling and reuse the last
        measurement, or ``None`` to fall back to psutil's default 0.1 s.
    """
    # Overall CPU (all cores combined).
    percent = psutil.cpu_percent(interval=poll_interval)

    per_cpu = psutil.cpu_percent(percpu=True, interval=poll_interval)
    per_cpu = per_cpu[:16]  # cap at 16 cores for readability

    logical = psutil.cpu_count(logical=True) or 0
    physical = psutil.cpu_count(logical=False) or 0

    freq = psutil.cpu_freq()
    freq_mhz: Optional[float] = None
    if freq is not None:
        if isinstance(freq, list):
            freq_mhz = max(f.current for f in freq if f.current)
        else:
            freq_mhz = freq.current

    load_avg: Optional[Tuple[float, float, float]] = None
    if hasattr(os, "getloadavg"):
        try:
            load_avg = os.getloadavg()
        except (OSError, OverflowError):
            pass

    # CPU times percentages
    try:
        times = psutil.cpu_times_percent(interval=(poll_interval or 0.1))
        user = times.user
        system = times.system
        idle = times.idle
        iowait = getattr(times, "iowait", None)
    except (AttributeError, OSError):
        user = system = idle = iowait = None

    # Interrupt rate (Linux only)
    interrupts = None
    if IS_LINUX:
        def _intr():
            try:
                with open("/proc/stat") as fh:
                    for line in fh:
                        if line.startswith("intr "):
                            return sum(int(x) for x in line.split()[1:])
            except OSError:
                pass
            return 0
        try:
            t0 = time.time()
            a = _intr()
            time.sleep(0.2)
            b = _intr()
            interrupts = (b - a) / (time.time() - t0)
        except OSError:
            pass

    return CPUInfo(
        percent=percent,
        per_core_percent=per_cpu,
        logical_cores=logical,
        physical_cores=physical,
        frequency_mhz=freq_mhz,
        load_avg=load_avg,
        user_percent=user,
        system_percent=system,
        idle_percent=idle,
        iowait_percent=iowait,
        interrupts_per_sec=interrupts,
    )


def collect_memory() -> MemoryInfo:
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    return MemoryInfo(
        total_gb=vm.total / (1024.0 ** 3),
        used_gb=vm.used / (1024.0 ** 3),
        free_gb=vm.free / (1024.0 ** 3),
        available_gb=vm.available / (1024.0 ** 3),
        percent=vm.percent,
        swap_total_gb=sw.total / (1024.0 ** 3),
        swap_used_gb=sw.used / (1024.0 ** 3),
        swap_percent=sw.percent,
    )


def collect_disks() -> List[DiskInfo]:
    partitions = psutil.disk_partitions(include_fs=True)
    result = []
    for part in partitions:
        # Skip virtual/pseudo mounts early.
        if part.fstype and part.fstype.lower() in _IGNORED_FS:
            continue
        if any(part.mountpoint.startswith(p) for p in _IGNORED_MOUNT_PREFIXES):
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            result.append(DiskInfo(
                mountpoint=part.mountpoint,
                device=part.device,
                fstype=part.fstype,
                total_gb=0,
                used_gb=0,
                free_gb=0,
                percent=0,
                inodes_total=None,
                inodes_used=None,
                inodes_free=None,
                inodes_percent=None,
                label=None,
            ))
            continue

        # Inodes from statvfs (more portable than psutil)
        inodes_total = inodes_used = inodes_free = inodes_percent = None
        try:
            st = os.statvfs(part.mountpoint)
            inodes_total = st.f_files
            inodes_free = st.f_ffree
            inodes_used = inodes_total - inodes_free
            if inodes_total > 0:
                inodes_percent = (inodes_used / inodes_total) * 100.0
        except (OSError, ValueError):
            pass

        # Volume label (best effort)
        label = None
        try:
            if IS_LINUX and os.path.exists("/sbin/blkid"):
                out = subprocess.check_output(["/sbin/blkid", "-s", "LABEL", "-o", "value", part.device],
                                              stderr=subprocess.DEVNULL, timeout=2).decode().strip()
                if out:
                    label = out
            elif IS_WINDOWS:
                # Try to get volume label via wmi? Skip for simplicity.
                pass
            elif IS_DARWIN:
                # Try to get label via GetFileInfo? Skip.
                pass
        except (Exception, OSError):
            pass
        if not label:
            label = os.path.basename(part.device) if part.device != part.mountpoint else None

        result.append(DiskInfo(
            mountpoint=part.mountpoint,
            device=part.device,
            fstype=part.fstype,
            total_gb=usage.total / (1024.0 ** 3),
            used_gb=usage.used / (1024.0 ** 3),
            free_gb=usage.free / (1024.0 ** 3),
            percent=usage.percent,
            inodes_total=inodes_total,
            inodes_used=inodes_used,
            inodes_free=inodes_free,
            inodes_percent=inodes_percent,
            label=label,
        ))
    # Sort by usage descending for readability
    result.sort(key=lambda d: d.percent, reverse=True)
    return result


def collect_platform() -> PlatformInfo:
    return PlatformInfo(
        hostname=platform.node(),
        kernel=platform.release(),
        release=platform.version(),
        machine=platform.machine(),
        python_version=platform.python_version(),
    )


def collect_uptime() -> UptimeInfo:
    boot = psutil.boot_time()
    now = time.time()
    delta = now - boot
    days = int(delta // 86400)
    hours = int((delta % 86400) // 3600)
    minutes = int((delta % 3600) // 60)
    seconds = int(delta % 60)
    formatted = f"{days}d {hours}h {minutes}m {seconds}s" if days else f"{hours}h {minutes}m {seconds}s"
    return UptimeInfo(seconds=delta, minutes=delta/60, hours=delta/3600, days=delta/86400, formatted=formatted)


def collect_host() -> HostInfo:
    return get_host_info()


def collect_snapshot(tags: Optional[Set[str]] = None, poll_interval: float = 0.3) -> Snapshot:
    """Gather a complete hardware snapshot.

    Parameters
    ----------
    tags :
        If given, only populate matching sections. Valid tags: ``cpu``,
        ``memory``, ``disk``/``disks``, ``services``, ``platform``.
    poll_interval :
        Seconds to sample CPU over (see :func:`collect_cpu`).
    """
    if not tags:
        want = {"cpu", "memory", "disk", "services", "platform"}
    else:
        want = {"cpu", "memory", "disk", "services", "platform"} & set(tags)

    host = collect_host()
    platform_info = collect_platform()
    uptime = collect_uptime()

    cpu = collect_cpu(poll_interval) if "cpu" in want else CPUInfo(
        percent=0.0, per_core_percent=[], logical_cores=0, physical_cores=0,
        frequency_mhz=None, load_avg=None, user_percent=None,
        system_percent=None, idle_percent=None, iowait_percent=None,
        interrupts_per_sec=None,
    )
    memory = collect_memory() if "memory" in want else MemoryInfo(
        total_gb=0.0, used_gb=0.0, free_gb=0.0, available_gb=0.0,
        percent=0.0, swap_total_gb=0.0, swap_used_gb=0.0, swap_percent=0.0,
    )
    disks = collect_disks() if "disk" in want else []
    services_units = list_services() if "services" in want else []
    services_info = ServicesInfo(
        total=len(services_units),
        running=sum(1 for s in services_units if s.status == "running"),
        stopped=sum(1 for s in services_units if s.status == "stopped"),
        failed=sum(1 for s in services_units if s.status == "failed"),
        units=services_units,
    )

    # Build a stable, short ID for the snapshot
    snapshot_id = f"{host.hostname}_{int(time.time())}"

    return Snapshot(
        snapshot_id=snapshot_id,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
        host=host,
        platform=platform_info,
        uptime=uptime,
        cpu=cpu,
        memory=memory,
        disks=disks,
        services=services_info,
        tags=list(want),
    )