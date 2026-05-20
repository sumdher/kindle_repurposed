"""Docker SDK wrapper with graceful fallback when Docker is unavailable."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import pytz

logger = logging.getLogger(__name__)
ROME_TZ = pytz.timezone("Europe/Rome")


@dataclass
class ContainerInfo:
    name: str
    image: str
    status: str       # "running", "exited", "paused", etc.
    status_detail: str  # human-readable uptime or stopped-time string


def get_containers() -> tuple[list[ContainerInfo], str | None]:
    """
    Returns (containers, error_message).
    error_message is None on success, a string on failure.
    """
    try:
        import docker  # type: ignore

        client = docker.from_env(timeout=5)
        raw = client.containers.list(all=True)
        result: list[ContainerInfo] = []
        for c in raw:
            status = c.status  # "running", "exited", etc.
            attrs = c.attrs or {}
            state = attrs.get("State", {})

            if status == "running":
                started_raw = state.get("StartedAt", "")
                detail = _since_label(started_raw, prefix="Up")
            else:
                finished_raw = state.get("FinishedAt", "")
                detail = _since_label(finished_raw, prefix="Stopped")

            image_tags = c.image.tags
            image_str = image_tags[0] if image_tags else (c.image.short_id or "unknown")

            result.append(ContainerInfo(
                name=c.name,
                image=image_str,
                status=status,
                status_detail=detail,
            ))

        result.sort(key=lambda x: (x.status != "running", x.name))
        return result, None

    except ImportError:
        return [], "Docker SDK not installed"
    except Exception as exc:
        logger.warning("Docker unavailable: %s", exc)
        return [], str(exc)


def _since_label(iso_str: str, prefix: str) -> str:
    """Convert an ISO timestamp string to a human 'Up 2h 15m' style label."""
    if not iso_str or iso_str.startswith("0001"):
        return "—"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = now - dt
        total_s = int(delta.total_seconds())
        if total_s < 0:
            return "—"
        days = total_s // 86400
        hours = (total_s % 86400) // 3600
        minutes = (total_s % 3600) // 60
        if days:
            return f"{prefix} {days}d {hours}h"
        if hours:
            return f"{prefix} {hours}h {minutes}m"
        return f"{prefix} {minutes}m"
    except Exception:
        return "—"
