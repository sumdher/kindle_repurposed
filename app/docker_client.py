"""Docker SDK wrapper — container list + per-container resource stats."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone

import pytz

logger = logging.getLogger(__name__)
ROME_TZ = pytz.timezone("Europe/Rome")


@dataclass
class ContainerInfo:
    name: str
    image: str
    status: str           # "running", "exited", "paused", etc.
    status_detail: str    # "Up 2h 15m" or "Stopped 3d 4h"
    cpu_pct: float        # 0.0 when stopped or unavailable
    mem_mb: int           # RSS memory in MB
    mem_limit_mb: int     # memory limit in MB (0 = no limit / unknown)
    mem_pct: float        # mem_mb / mem_limit_mb * 100 (0 when unknown)
    net_rx: str           # formatted receive bytes total  e.g. "1.2 MB"
    net_tx: str           # formatted transmit bytes total
    blk_read: str         # formatted block-device read total
    blk_write: str        # formatted block-device write total
    stats_ok: bool        # False if stats fetch failed or container not running


def get_containers() -> tuple[list[ContainerInfo], str | None]:
    """Return (containers, error_message). error_message is None on success."""
    try:
        import docker  # type: ignore

        client = docker.from_env(timeout=5)
        raw = client.containers.list(all=True)

        running = [c for c in raw if c.status == "running"]

        # Fetch stats for all running containers in parallel (stream=False = single snapshot).
        stats_map: dict[str, dict] = {}
        if running:
            with ThreadPoolExecutor(max_workers=min(len(running), 8)) as pool:
                futures = {pool.submit(_fetch_stats, c): c.id for c in running}
                for fut in as_completed(futures, timeout=8):
                    cid = futures[fut]
                    try:
                        stats_map[cid] = fut.result()
                    except Exception:
                        stats_map[cid] = {}

        result: list[ContainerInfo] = []
        for c in raw:
            status = c.status
            attrs  = c.attrs or {}
            state  = attrs.get("State", {})

            if status == "running":
                detail = _since_label(state.get("StartedAt", ""), prefix="Up")
            else:
                detail = _since_label(state.get("FinishedAt", ""), prefix="Stopped")

            tags      = c.image.tags
            image_str = tags[0] if tags else (c.image.short_id or "unknown")
            # Trim long registry prefix for readability
            if "/" in image_str and len(image_str) > 40:
                image_str = image_str.rsplit("/", 1)[-1]

            if status == "running" and c.id in stats_map and stats_map[c.id]:
                info = _parse_stats(stats_map[c.id])
            else:
                info = _empty_stats()

            result.append(ContainerInfo(
                name=c.name,
                image=image_str,
                status=status,
                status_detail=detail,
                **info,
            ))

        result.sort(key=lambda x: (x.status != "running", x.name))
        return result, None

    except ImportError:
        return [], "Docker SDK not installed (pip install docker)"
    except Exception as exc:
        logger.warning("Docker unavailable: %s", exc)
        return [], str(exc)


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------

def _fetch_stats(container) -> dict:
    """Single-snapshot stats call (stream=False). Returns raw Docker stats dict."""
    return container.stats(stream=False)


def _parse_stats(s: dict) -> dict:
    cpu_pct      = _cpu_percent(s)
    mem_mb, mem_limit_mb, mem_pct = _memory(s)
    net_rx, net_tx  = _network(s)
    blk_read, blk_write = _blkio(s)
    return dict(
        cpu_pct=cpu_pct,
        mem_mb=mem_mb,
        mem_limit_mb=mem_limit_mb,
        mem_pct=mem_pct,
        net_rx=net_rx,
        net_tx=net_tx,
        blk_read=blk_read,
        blk_write=blk_write,
        stats_ok=True,
    )


def _empty_stats() -> dict:
    return dict(
        cpu_pct=0.0,
        mem_mb=0,
        mem_limit_mb=0,
        mem_pct=0.0,
        net_rx="—",
        net_tx="—",
        blk_read="—",
        blk_write="—",
        stats_ok=False,
    )


def _cpu_percent(s: dict) -> float:
    try:
        cpu_now  = s["cpu_stats"]["cpu_usage"]["total_usage"]
        cpu_prev = s["precpu_stats"]["cpu_usage"]["total_usage"]
        sys_now  = s["cpu_stats"]["system_cpu_usage"]
        sys_prev = s["precpu_stats"]["system_cpu_usage"]
        cpu_delta = cpu_now - cpu_prev
        sys_delta = sys_now - sys_prev
        if sys_delta <= 0:
            return 0.0
        num_cpus = (
            s["cpu_stats"].get("online_cpus")
            or len(s["cpu_stats"]["cpu_usage"].get("percpu_usage") or [1])
        )
        return round((cpu_delta / sys_delta) * num_cpus * 100.0, 1)
    except (KeyError, TypeError, ZeroDivisionError):
        return 0.0


def _memory(s: dict) -> tuple[int, int, float]:
    try:
        mem_stats = s["memory_stats"]
        usage = mem_stats["usage"]
        limit = mem_stats["limit"]
        # Subtract page cache so we show RSS
        cache = mem_stats.get("stats", {}).get("cache", 0)
        rss = max(0, usage - cache)
        mem_mb       = rss // (1024 * 1024)
        mem_limit_mb = limit // (1024 * 1024)
        mem_pct = round(rss / limit * 100, 1) if limit > 0 else 0.0
        return mem_mb, mem_limit_mb, mem_pct
    except (KeyError, TypeError, ZeroDivisionError):
        return 0, 0, 0.0


def _network(s: dict) -> tuple[str, str]:
    try:
        nets = s.get("networks") or {}
        rx = sum(v["rx_bytes"] for v in nets.values())
        tx = sum(v["tx_bytes"] for v in nets.values())
        return _fmt_bytes(rx), _fmt_bytes(tx)
    except (KeyError, TypeError):
        return "—", "—"


def _blkio(s: dict) -> tuple[str, str]:
    try:
        entries = (s.get("blkio_stats") or {}).get("io_service_bytes_recursive") or []
        r = sum(e["value"] for e in entries if e.get("op") == "Read")
        w = sum(e["value"] for e in entries if e.get("op") == "Write")
        return _fmt_bytes(r), _fmt_bytes(w)
    except (KeyError, TypeError):
        return "—", "—"


def _fmt_bytes(b: int) -> str:
    if b >= 1024 ** 3:
        return f"{b / 1024 ** 3:.1f} GB"
    if b >= 1024 ** 2:
        return f"{b / 1024 ** 2:.0f} MB"
    if b >= 1024:
        return f"{b / 1024:.0f} KB"
    return f"{b} B"


def _since_label(iso_str: str, prefix: str) -> str:
    if not iso_str or iso_str.startswith("0001"):
        return "—"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt
        total_s = int(delta.total_seconds())
        if total_s < 0:
            return "—"
        days  = total_s // 86400
        hours = (total_s % 86400) // 3600
        mins  = (total_s % 3600) // 60
        if days:
            return f"{prefix} {days}d {hours}h"
        if hours:
            return f"{prefix} {hours}h {mins}m"
        return f"{prefix} {mins}m"
    except Exception:
        return "—"
