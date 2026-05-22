"""
UPS monitor via NUT protocol — pure Python TCP client, no subprocess or
external NUT tools required inside the container.

NUT wire protocol (TCP 3493):
  → LIST VAR <ups_name>\n
  ← VAR <ups_name> <key> "<value>"
  ← ...
  ← END LIST VAR <ups_name>
"""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass
from datetime import datetime

import pytz

logger = logging.getLogger(__name__)
ROME_TZ = pytz.timezone("Europe/Rome")

UPS_MAX_WATTS = 360.0   # Green Cell UPSLM360 nominal capacity

_STATUS_LABELS: dict[str, str] = {
    "OL":       "On Line",
    "OB":       "On Battery",
    "LB":       "Low Battery",
    "CHRG":     "Charging",
    "DISCHRG":  "Discharging",
    "OL CHRG":  "Charging",
    "OL LB":    "Low Battery",
}


@dataclass
class UpsSample:
    """One time-series point stored for the 48h history graphs."""
    timestamp: datetime
    load_pct: int
    watts: float
    input_voltage: float
    battery_charge: int


@dataclass
class UpsReading:
    """Full live reading returned on every page request."""
    status: str               # raw NUT status, e.g. "OL"
    status_label: str         # human label, e.g. "On Line"
    on_battery: bool
    low_battery: bool
    load_pct: int
    watts: float
    input_voltage: float
    output_voltage: float
    frequency: float
    battery_charge: int       # %
    battery_voltage: float
    battery_voltage_nominal: float
    firmware: str
    updated_at: datetime


# Last successful reading (survives transient NUT failures)
_ups_cache: dict = {"reading": None, "stale": False}


# ---------------------------------------------------------------------------
# NUT TCP client
# ---------------------------------------------------------------------------

def poll_nut(host: str, port: int, ups_name: str) -> dict[str, str] | None:
    """
    Open a TCP connection to the NUT daemon, send LIST VAR, collect response.
    Returns key→value dict or None on any failure.
    """
    try:
        with socket.create_connection((host, port), timeout=5) as sock:
            sock.sendall(f"LIST VAR {ups_name}\n".encode())
            buf = bytearray()
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                if b"END LIST VAR" in buf:
                    break

        result: dict[str, str] = {}
        for line in buf.decode(errors="replace").splitlines():
            if line.startswith("VAR "):
                # VAR greencell battery.charge "100"
                parts = line.split(" ", 3)
                if len(parts) == 4:
                    result[parts[2]] = parts[3].strip('"')

        return result if result else None

    except Exception as exc:
        logger.warning("NUT poll failed (%s:%s %s): %s", host, port, ups_name, exc)
        return None


def _safe_float(d: dict, key: str, default: float = 0.0) -> float:
    try:
        return float(d.get(key) or default)
    except (ValueError, TypeError):
        return default


def _safe_int(d: dict, key: str, default: int = 0) -> int:
    try:
        return int(float(d.get(key) or default))
    except (ValueError, TypeError):
        return default


def parse_nut(d: dict[str, str]) -> UpsReading:
    status = (d.get("ups.status") or "UNKNOWN").strip()
    label  = _STATUS_LABELS.get(status, status)
    load   = _safe_int(d, "ups.load")
    watts  = round(load / 100 * UPS_MAX_WATTS, 1)
    return UpsReading(
        status=status,
        status_label=label,
        on_battery=("OB" in status),
        low_battery=("LB" in status),
        load_pct=load,
        watts=watts,
        input_voltage=_safe_float(d, "input.voltage"),
        output_voltage=_safe_float(d, "output.voltage"),
        frequency=_safe_float(d, "output.frequency"),
        battery_charge=_safe_int(d, "battery.charge"),
        battery_voltage=_safe_float(d, "battery.voltage"),
        battery_voltage_nominal=_safe_float(d, "battery.voltage.nominal", 12.0),
        firmware=d.get("ups.firmware.aux") or "?",
        updated_at=datetime.now(ROME_TZ),
    )


def get_ups_reading(host: str, port: int = 3493, ups_name: str = "greencell") -> UpsReading | None:
    """Poll NUT and return a parsed reading, or None if unreachable."""
    raw = poll_nut(host, port, ups_name)
    if raw is None:
        return None
    return parse_nut(raw)
