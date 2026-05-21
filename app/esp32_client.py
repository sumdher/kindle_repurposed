"""
ESP32 sensor data from InfluxDB v2 via the HTTP Flux query API.
Uses only httpx (already a dependency) — no influxdb-client package needed.

InfluxDB measurements written by the MQTT subscriber:
  telemetry: temperature, humidity, pressure, gas, accel_x/y/z, rssi, uptime_s
  status:    online (1/0)
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from datetime import datetime

import httpx
import pytz

logger = logging.getLogger(__name__)
ROME_TZ = pytz.timezone("Europe/Rome")


@dataclass
class ESP32Reading:
    """Latest sensor snapshot — None fields mean the sensor didn't report."""
    device: str = "esp32s3-01"
    temperature: float | None = None   # °C
    humidity: float | None = None      # %
    pressure: float | None = None      # hPa
    gas: float | None = None           # Ω (raw gas resistance)
    rssi: int | None = None            # dBm
    online: bool = False
    updated_at: datetime | None = None


@dataclass
class ESP32Sample:
    """One 2h-aggregated point for the 48h history line graphs."""
    timestamp: datetime
    temperature: float = 0.0
    humidity: float = 0.0
    pressure: float = 0.0


# ---------------------------------------------------------------------------
# Public async API
# ---------------------------------------------------------------------------

async def fetch_latest(
    url: str, token: str, org: str, bucket: str
) -> ESP32Reading | None:
    """Return the most recent telemetry reading (last 10 minutes window)."""
    flux = f"""
from(bucket: "{bucket}")
  |> range(start: -10m)
  |> filter(fn: (r) => r._measurement == "telemetry")
  |> filter(fn: (r) => r._field == "temperature" or r._field == "humidity" or
            r._field == "pressure" or r._field == "gas" or r._field == "rssi")
  |> last()
"""
    rows = await _query(url, token, org, flux)
    if rows is None:
        return None

    reading = ESP32Reading()
    for row in rows:
        fname  = row.get("_field", "")
        val_s  = row.get("_value", "")
        ts_s   = row.get("_time", "")
        device = row.get("device", "esp32s3-01")
        reading.device = device
        try:
            fval = float(val_s)
            if fname == "temperature":
                reading.temperature = round(fval, 1)
            elif fname == "humidity":
                reading.humidity = round(fval, 1)
            elif fname == "pressure":
                reading.pressure = round(fval, 1)
            elif fname == "gas":
                reading.gas = round(fval, 0)
            elif fname == "rssi":
                reading.rssi = int(fval)
        except (ValueError, TypeError):
            pass
        if ts_s and reading.updated_at is None:
            try:
                reading.updated_at = (
                    datetime.fromisoformat(ts_s.replace("Z", "+00:00"))
                    .astimezone(ROME_TZ)
                )
                reading.online = True
            except Exception:
                pass

    return reading if reading.temperature is not None else None


async def fetch_history(
    url: str, token: str, org: str, bucket: str
) -> list[ESP32Sample]:
    """Return the last 48h of sensor data aggregated to 2h mean windows."""
    flux = f"""
from(bucket: "{bucket}")
  |> range(start: -48h)
  |> filter(fn: (r) => r._measurement == "telemetry")
  |> filter(fn: (r) => r._field == "temperature" or r._field == "humidity" or r._field == "pressure")
  |> aggregateWindow(every: 2h, fn: mean, createEmpty: false)
"""
    rows = await _query(url, token, org, flux)
    if not rows:
        return []

    by_ts: dict[str, ESP32Sample] = {}
    for row in rows:
        ts_s   = row.get("_time", "")
        fname  = row.get("_field", "")
        val_s  = row.get("_value", "")
        if not ts_s or not val_s:
            continue
        try:
            ts  = datetime.fromisoformat(ts_s.replace("Z", "+00:00")).astimezone(ROME_TZ)
            val = float(val_s)
        except (ValueError, TypeError):
            continue

        if ts_s not in by_ts:
            by_ts[ts_s] = ESP32Sample(timestamp=ts)
        sample = by_ts[ts_s]
        if fname == "temperature":
            sample.temperature = round(val, 1)
        elif fname == "humidity":
            sample.humidity = round(val, 1)
        elif fname == "pressure":
            sample.pressure = round(val, 1)

    return sorted(by_ts.values(), key=lambda s: s.timestamp)


# ---------------------------------------------------------------------------
# Internal HTTP helper
# ---------------------------------------------------------------------------

async def _query(url: str, token: str, org: str, flux: str) -> list[dict] | None:
    """POST a Flux query, parse the CSV response, return list of row dicts."""
    endpoint = f"{url.rstrip('/')}/api/v2/query"
    headers = {
        "Authorization": f"Token {token}",
        "Content-Type":  "application/vnd.flux",
        "Accept":        "application/csv",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                endpoint,
                content=flux.encode(),
                headers=headers,
                params={"org": org},
            )
        if resp.status_code != 200:
            logger.warning(
                "InfluxDB returned %d: %s", resp.status_code, resp.text[:300]
            )
            return None

        # InfluxDB CSV has multiple table sections separated by blank lines.
        # csv.DictReader handles them all if we strip annotation rows (starting with #).
        lines = [l for l in resp.text.splitlines() if not l.startswith("#")]
        reader = csv.DictReader(io.StringIO("\n".join(lines)))
        return [row for row in reader if row.get("_value", "").strip()]

    except httpx.ConnectError:
        logger.warning("InfluxDB unreachable at %s", url)
        return None
    except Exception as exc:
        logger.warning("InfluxDB query error: %s", exc)
        return None
