"""
System analytics via psutil — btop-style stats for the Kindle dashboard.

Run the container with `pid: host` in docker-compose so psutil sees host
processes and CPU stats. Without it, you get container-level metrics only.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime

import psutil
import pytz

ROME_TZ = pytz.timezone("Europe/Rome")

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CpuStats:
    overall_pct: float
    per_core: list[float]          # per logical core
    freq_mhz: float
    freq_max_mhz: float
    load_avg_1: float
    load_avg_5: float
    load_avg_15: float
    core_count_logical: int
    core_count_physical: int
    temp_c: float | None           # None if sensor unavailable


@dataclass
class MemStats:
    total_gb: float
    used_gb: float
    available_gb: float
    used_pct: float
    swap_total_gb: float
    swap_used_gb: float
    swap_pct: float


@dataclass
class DiskStat:
    mount: str
    total_gb: float
    used_gb: float
    free_gb: float
    used_pct: float
    fstype: str


@dataclass
class NetStat:
    iface: str
    bytes_sent_mb: float
    bytes_recv_mb: float
    send_kbps: float               # rate since last call
    recv_kbps: float


@dataclass
class GpuStat:
    index: int
    name: str
    util_pct: float
    mem_used_mb: float
    mem_total_mb: float
    mem_pct: float
    temp_c: float | None
    power_w: float | None


@dataclass
class ProcessStat:
    pid: int
    name: str
    cpu_pct: float
    mem_pct: float
    mem_rss_mb: float
    status: str
    username: str


@dataclass
class SysStats:
    cpu: CpuStats
    mem: MemStats
    disks: list[DiskStat]
    nets: list[NetStat]
    procs: list[ProcessStat]       # top 12 by CPU
    gpus: list[GpuStat]
    uptime_str: str
    updated_at: datetime


# ---------------------------------------------------------------------------
# Module-level state for computing network rates between calls
# ---------------------------------------------------------------------------

_prev_net: dict = {}
_prev_time: float = 0.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_uptime(sec: float) -> str:
    d = int(sec // 86400)
    h = int((sec % 86400) // 3600)
    m = int((sec % 3600) // 60)
    if d:
        return f"{d}d {h}h {m}m"
    return f"{h}h {m}m"


def _get_gpus() -> list[GpuStat]:
    try:
        import pynvml  # type: ignore
        pynvml.nvmlInit()
        count = pynvml.nvmlDeviceGetCount()
        gpus: list[GpuStat] = []
        for i in range(count):
            h = pynvml.nvmlDeviceGetHandleByIndex(i)
            name = pynvml.nvmlDeviceGetName(h)
            if isinstance(name, bytes):
                name = name.decode()
            util = pynvml.nvmlDeviceGetUtilizationRates(h)
            mem  = pynvml.nvmlDeviceGetMemoryInfo(h)
            try:
                temp: float | None = float(pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU))
            except Exception:
                temp = None
            try:
                power_w: float | None = pynvml.nvmlDeviceGetPowerUsage(h) / 1000.0
            except Exception:
                power_w = None
            mem_used_mb  = mem.used  / 1e6
            mem_total_mb = mem.total / 1e6
            mem_pct      = (mem.used / mem.total * 100) if mem.total else 0.0
            gpus.append(GpuStat(
                index=i, name=name,
                util_pct=float(util.gpu),
                mem_used_mb=mem_used_mb, mem_total_mb=mem_total_mb, mem_pct=mem_pct,
                temp_c=temp, power_w=power_w,
            ))
        return gpus
    except Exception:
        return []


def _cpu_temp() -> float | None:
    try:
        temps = psutil.sensors_temperatures()
        if not temps:
            return None
        # Prefer well-known sensor names
        for key in ("coretemp", "k10temp", "cpu_thermal", "acpitz", "cpu-thermal"):
            if key in temps and temps[key]:
                return temps[key][0].current
        # Fall back to first available sensor
        for entries in temps.values():
            if entries:
                return entries[0].current
    except (AttributeError, Exception):
        pass
    return None


# ---------------------------------------------------------------------------
# Main stats collector
# ---------------------------------------------------------------------------

def get_sys_stats() -> SysStats:
    global _prev_net, _prev_time

    now = datetime.now(ROME_TZ)

    # ── CPU ──────────────────────────────────────────────────────────────────
    overall_pct = psutil.cpu_percent(interval=0.3)
    per_core    = psutil.cpu_percent(interval=None, percpu=True)

    freq = psutil.cpu_freq()
    freq_mhz     = freq.current if freq else 0.0
    freq_max_mhz = freq.max     if freq else 0.0

    load = psutil.getloadavg()

    cpu = CpuStats(
        overall_pct=overall_pct,
        per_core=list(per_core),
        freq_mhz=freq_mhz,
        freq_max_mhz=freq_max_mhz,
        load_avg_1=load[0],
        load_avg_5=load[1],
        load_avg_15=load[2],
        core_count_logical=psutil.cpu_count(logical=True) or 1,
        core_count_physical=psutil.cpu_count(logical=False) or 1,
        temp_c=_cpu_temp(),
    )

    # ── Memory ───────────────────────────────────────────────────────────────
    vm = psutil.virtual_memory()
    sm = psutil.swap_memory()
    mem = MemStats(
        total_gb=vm.total / 1e9,
        used_gb=vm.used / 1e9,
        available_gb=vm.available / 1e9,
        used_pct=vm.percent,
        swap_total_gb=sm.total / 1e9,
        swap_used_gb=sm.used / 1e9,
        swap_pct=sm.percent,
    )

    # ── Disks ─────────────────────────────────────────────────────────────────
    disks: list[DiskStat] = []
    _seen_devices: set[str] = set()
    for part in psutil.disk_partitions(all=False):
        if part.fstype in ("", "squashfs", "tmpfs", "devtmpfs", "overlay", "proc", "sysfs"):
            continue
        # Deduplicate: Docker bind-mounts can expose the same block device
        # at multiple mount points (e.g. /, /etc/hostname, /data all on sda1).
        if part.device in _seen_devices:
            continue
        _seen_devices.add(part.device)
        try:
            usage = psutil.disk_usage(part.mountpoint)
            disks.append(DiskStat(
                mount=part.mountpoint,
                total_gb=usage.total / 1e9,
                used_gb=usage.used / 1e9,
                free_gb=usage.free / 1e9,
                used_pct=usage.percent,
                fstype=part.fstype,
            ))
        except (PermissionError, OSError):
            pass
    disks.sort(key=lambda d: d.mount)
    disks = disks[:6]

    # ── Network ───────────────────────────────────────────────────────────────
    cur_net  = psutil.net_io_counters(pernic=True)
    cur_time = time.monotonic()
    elapsed  = (cur_time - _prev_time) if _prev_time else 1.0
    elapsed  = max(elapsed, 0.001)

    nets: list[NetStat] = []
    for iface, counters in cur_net.items():
        if iface == "lo":
            continue
        prev = _prev_net.get(iface)
        if prev and elapsed > 0:
            send_kbps = (counters.bytes_sent - prev.bytes_sent) / elapsed / 1024
            recv_kbps = (counters.bytes_recv - prev.bytes_recv) / elapsed / 1024
        else:
            send_kbps = recv_kbps = 0.0
        nets.append(NetStat(
            iface=iface,
            bytes_sent_mb=counters.bytes_sent / 1e6,
            bytes_recv_mb=counters.bytes_recv / 1e6,
            send_kbps=max(0.0, send_kbps),
            recv_kbps=max(0.0, recv_kbps),
        ))

    _prev_net  = cur_net
    _prev_time = cur_time

    # Drop interfaces with zero total traffic (e.g. unused VPNs at startup)
    nets = [n for n in nets if n.bytes_sent_mb + n.bytes_recv_mb > 0][:5]

    # ── Processes ─────────────────────────────────────────────────────────────
    procs_raw: list[ProcessStat] = []
    attrs = ["pid", "name", "cpu_percent", "memory_percent", "memory_info", "status", "username"]
    for proc in psutil.process_iter(attrs):
        try:
            info = proc.info
            procs_raw.append(ProcessStat(
                pid=info["pid"],
                name=(info.get("name") or "?")[:22],
                cpu_pct=info.get("cpu_percent") or 0.0,
                mem_pct=info.get("memory_percent") or 0.0,
                mem_rss_mb=(info["memory_info"].rss if info.get("memory_info") else 0) / 1e6,
                status=info.get("status") or "?",
                username=(info.get("username") or "?")[:10],
            ))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass

    procs_raw.sort(key=lambda p: p.cpu_pct, reverse=True)
    procs = procs_raw[:14]

    # ── GPU ───────────────────────────────────────────────────────────────────
    gpus = _get_gpus()

    # ── Uptime ────────────────────────────────────────────────────────────────
    uptime_str = _fmt_uptime(time.time() - psutil.boot_time())

    return SysStats(
        cpu=cpu, mem=mem, disks=disks, nets=nets, procs=procs,
        gpus=gpus, uptime_str=uptime_str, updated_at=now,
    )
