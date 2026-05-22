import logging
from dataclasses import dataclass
from datetime import datetime

import httpx
import pytz

logger = logging.getLogger(__name__)

ROME_TZ = pytz.timezone("Europe/Rome")
LAT = 45.4642
LON = 9.1900
BASE_URL = "https://api.open-meteo.com/v1/forecast"

WMO_LABELS: dict[int, str] = {
    0: "Clear sky",
    1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Icy fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow",
    77: "Snow grains",
    80: "Showers", 81: "Rain showers", 82: "Heavy showers",
    85: "Snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm+hail", 99: "Heavy storm+hail",
}


def wmo_label(code: int) -> str:
    return WMO_LABELS.get(code, f"Code {code}")


def wind_direction_text(degrees: float) -> str:
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return dirs[round(degrees / 45) % 8]


@dataclass
class CurrentWeather:
    temperature: float
    feels_like: float
    humidity: int
    wind_speed: float
    wind_direction: float
    pressure: float
    uv_index: float
    weather_code: int
    updated_at: datetime


@dataclass
class DailyForecast:
    temp_max: float
    temp_min: float
    sunrise: str        # "HH:MM"
    sunset: str         # "HH:MM"
    precip_sum: float   # mm
    uv_max: float


@dataclass
class HourlyPoint:
    hour: int           # 0–23
    temperature: float
    weather_code: int
    precip_prob: int    # 0–100 %


async def fetch_current_and_hourly() -> tuple[
    CurrentWeather | None,
    DailyForecast | None,
    list[HourlyPoint],
]:
    params = {
        "latitude": LAT,
        "longitude": LON,
        "current": (
            "temperature_2m,apparent_temperature,relative_humidity_2m,"
            "weather_code,wind_speed_10m,wind_direction_10m,"
            "surface_pressure,uv_index"
        ),
        "hourly": "temperature_2m,weather_code,precipitation_probability",
        "daily": (
            "temperature_2m_max,temperature_2m_min,"
            "sunrise,sunset,precipitation_sum,uv_index_max"
        ),
        "forecast_days": 1,
        "timezone": "Europe/Rome",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(BASE_URL, params=params)
            resp.raise_for_status()
            data = resp.json()

        # --- current ---
        c = data["current"]
        current = CurrentWeather(
            temperature=c["temperature_2m"],
            feels_like=c["apparent_temperature"],
            humidity=int(c["relative_humidity_2m"]),
            wind_speed=c["wind_speed_10m"],
            wind_direction=c["wind_direction_10m"],
            pressure=c["surface_pressure"],
            uv_index=c.get("uv_index") or 0,
            weather_code=c["weather_code"],
            updated_at=datetime.now(ROME_TZ),
        )

        # --- daily (today only) ---
        d = data["daily"]
        def _hm(iso: str) -> str:
            """'2025-05-20T06:12' → '06:12'"""
            try:
                return datetime.fromisoformat(iso).strftime("%H:%M")
            except Exception:
                return "—"

        daily = DailyForecast(
            temp_max=d["temperature_2m_max"][0],
            temp_min=d["temperature_2m_min"][0],
            sunrise=_hm(d["sunrise"][0]),
            sunset=_hm(d["sunset"][0]),
            precip_sum=d["precipitation_sum"][0] or 0.0,
            uv_max=d["uv_index_max"][0] or 0.0,
        )

        # --- hourly ---
        times = data["hourly"]["time"]
        temps = data["hourly"]["temperature_2m"]
        codes = data["hourly"]["weather_code"]
        probs = data["hourly"]["precipitation_probability"]
        hourly: list[HourlyPoint] = []
        for i, t in enumerate(times):
            dt = datetime.fromisoformat(t)
            hourly.append(HourlyPoint(
                hour=dt.hour,
                temperature=temps[i] if temps[i] is not None else 0.0,
                weather_code=codes[i] if codes[i] is not None else 0,
                precip_prob=int(probs[i]) if probs[i] is not None else 0,
            ))

        return current, daily, hourly

    except Exception as exc:
        logger.error("fetch_current_and_hourly failed: %s", exc)
        return None, None, []


async def fetch_historical_samples() -> list[tuple[datetime, float, float]]:
    """Return 48h of hourly (timestamp, pressure_hpa, wind_speed_kmh) for backfill."""
    # forecast_days=0 is no longer accepted by Open-Meteo; omit it (defaults to 1).
    # We filter out future timestamps in the caller so the extra forecast hour doesn't matter.
    params = {
        "latitude": LAT,
        "longitude": LON,
        "hourly": "surface_pressure,wind_speed_10m",
        "past_days": 2,
        "forecast_days": 1,
        "timezone": "Europe/Rome",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(BASE_URL, params=params)
            resp.raise_for_status()
            data = resp.json()

        times = data["hourly"]["time"]
        pressures = data["hourly"]["surface_pressure"]
        winds = data["hourly"]["wind_speed_10m"]

        result: list[tuple[datetime, float, float]] = []
        for i, t in enumerate(times):
            dt = datetime.fromisoformat(t)
            dt = ROME_TZ.localize(dt)
            p = pressures[i] if pressures[i] is not None else 1013.0
            w = winds[i] if winds[i] is not None else 0.0
            result.append((dt, p, w))
        return result

    except Exception as exc:
        logger.error("fetch_historical_samples failed: %s", exc)
        return []
